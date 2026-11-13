"""
coordinator_core.symbol_knowledge.agent_batches -- the detached Haiku/Sonnet batch job.

Purpose: ``launch_batches`` writes a job payload and spawns this module as one detached process
(``spawn_detached``); the job runs one Haiku call for the ``adjust`` symbols and one Sonnet call for
the ``net-new`` symbols, writes the sidecar at the terminal sha, commits only the sidecar path, and
amends the ``symbol-knowledge`` leg in the close-job record through ``record_leg_outcome``.

Negative-spec:
  - ``launch_batches`` runs no model call and writes only the job payload; an empty flagged set
    spawns nothing.
  - At most one call per model per job; a kind with no flagged symbol makes no call.
  - The commit names ``SIDECAR_REL`` alone, so a peer's dirty file is never staged.
  - A runner that raises or answers unparseably loses only its own kind's symbols; the job never
    raises into the spawner and always records a final outcome.
  - Batches only the symbols it is handed (tier 0 already fetched and found empty upstream); it never
    fetches or substitutes tier 0. Symbols over the context budget are deferred, not truncated.
  - Emits no ``symbol_description`` line: that needs a SCIP key resolved through example-retrieval-repo, and no
    in-process resolver exists; no local SCIP and no qualname key stands in.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Union

from coordinator_core.symbol_knowledge import batch_context
from coordinator_core.symbol_knowledge.contract import KIND_ADJUST, KIND_NET_NEW, BuildCost, FlaggedSymbol
from coordinator_core.symbol_knowledge.sidecar import (
    SIDECAR_REL,
    merge_entries,
    read_sidecar,
    write_sidecar,
)

LEG = "symbol-knowledge"
HAIKU_MODEL = "haiku"
SONNET_MODEL = "sonnet"
MODEL_FOR_KIND = {KIND_ADJUST: HAIKU_MODEL, KIND_NET_NEW: SONNET_MODEL}
JOB_DIR_REL = ".coordinator-local/symbol-knowledge"
RUNNER_TIMEOUT_S = 600

COST_OP = "code_intel.batch_job"


@dataclass(frozen=True)
class RunnerReply:
    """A runner's text plus the token usage its model reported."""

    text: str
    tokens_in: int
    tokens_out: int


#: ``runner(model, prompt) -> text | RunnerReply``: the injectable agent call. A bare string carries
#: no usage, so its tokens are estimated.
AgentRunner = Callable[[str, str], Union[str, RunnerReply]]

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def job_path(root: "str | Path", run_id: str) -> Path:
    return Path(root) / JOB_DIR_REL / f"{_SAFE.sub('_', run_id)}.job.json"


def _head_sha(root: "str | Path") -> str:
    from coordinator_core.git.git_state import head_sha

    return head_sha(root) or ""


def launch_batches(
    root: "str | Path",
    run_id: str,
    flagged: Sequence[FlaggedSymbol],
    traps: Mapping[str, "list[str]"],
    *,
    sha: Optional[str] = None,
    spawn: Optional[Callable[..., bool]] = None,
) -> str:
    """Detach one batch job for *flagged* and return the leg's detail line."""
    if not flagged:
        return "no flagged symbols"
    from coordinator_core.atomic_replace import atomic_write_bytes

    payload = {
        "run_id": run_id,
        "sha": sha or _head_sha(root),
        "flagged": [
            {
                "qualified_name": f.qualified_name,
                "path": f.path,
                "kind": f.kind,
                "public": f.public,
                "docstring_empty": f.docstring_empty,
            }
            for f in flagged
        ],
        "traps": {k: list(v) for k, v in traps.items()},
    }
    path = job_path(root, run_id)
    atomic_write_bytes(path, json.dumps(payload, sort_keys=True).encode("utf-8"))
    if spawn is None:
        from coordinator_core.ops.ceremony.detached_spawn import spawn_detached as spawn
    ok = spawn(str(root), str(Path(__file__).resolve()), ["--job", str(path)])
    kinds = {k: sum(1 for f in flagged if f.kind == k) for k in MODEL_FOR_KIND}
    if not ok:
        return f"batch job not spawned for {len(flagged)} symbols"
    return f"batch job spawned: {kinds[KIND_ADJUST]} adjust, {kinds[KIND_NET_NEW]} net-new"


def build_prompt(
    kind: str,
    symbols: Sequence[Mapping[str, Any]],
    existing: Mapping[str, Mapping[str, Any]],
    contexts: Optional[Mapping[str, batch_context.FileContext]] = None,
) -> str:
    """The batch prompt: one JSON object back, ``{qualified_name: one-paragraph definition}``, with
    one shared-context block per file from *contexts* (resolved once per job)."""
    verb = "Revise the existing definition of" if kind == KIND_ADJUST else "Write a definition for"
    lines = [
        f"{verb} each symbol below: what it is for, its contract, and its traps, in one short "
        "paragraph per symbol. The code is in the shared context blocks after the list; do not read files.",
        "Reply with a single JSON object mapping each qualified name exactly as given to its "
        "definition string, and nothing else.",
        "",
    ]
    for s in symbols:
        name = s["qualified_name"]
        prior = existing.get(name, {}).get("definition", "")
        lines.append(f"- {name} (file: {s['path']})" + (f"\n  existing definition: {prior}" if prior else ""))
    if contexts:
        by_path: "dict[str, list[str]]" = {}
        for s in symbols:
            by_path.setdefault(str(s["path"]), []).append(str(s["qualified_name"]))
        for path, names in by_path.items():
            if path in contexts:
                lines += ["", batch_context.render_block(contexts[path], names)]
    return "\n".join(lines)


def parse_definitions(text: str, wanted: Sequence[str]) -> "dict[str, str]":
    """Definitions for the *wanted* names out of a runner's reply; unparseable replies yield ``{}``."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    allowed = set(wanted)
    return {k: v.strip() for k, v in data.items() if k in allowed and isinstance(v, str) and v.strip()}


def _usage_tokens(usage: Any) -> "tuple[int, int]":
    """``(tokens_in, tokens_out)`` from a ``claude -p`` usage object; cache reads and writes count as input."""
    if not isinstance(usage, dict):
        return 0, 0

    def num(key: str) -> int:
        v = usage.get(key)
        return v if isinstance(v, int) else 0

    tin = num("input_tokens") + num("cache_creation_input_tokens") + num("cache_read_input_tokens")
    return tin, num("output_tokens")


def claude_runner(model: str, prompt: str, *, cwd: "str | Path | None" = None) -> "str | RunnerReply":
    """Default runner: headless ``claude -p --output-format json`` with the prompt on stdin; returns
    the reply with its reported usage, or plain stdout when the output is not the JSON envelope."""
    exe = shutil.which("claude")
    if exe is None:
        raise FileNotFoundError("claude executable not on PATH")
    done = subprocess.run(
        [exe, "-p", "--model", model, "--output-format", "json"],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd) if cwd else None,
        timeout=RUNNER_TIMEOUT_S,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if done.returncode != 0:
        raise RuntimeError(f"claude exited {done.returncode}")
    try:
        envelope = json.loads(done.stdout)
        text = envelope["result"]
    except (ValueError, KeyError, TypeError):
        return done.stdout
    if not isinstance(text, str):
        return done.stdout
    tin, tout = _usage_tokens(envelope.get("usage"))
    return RunnerReply(text, tin, tout)


def _commit_sidecar(root: "str | Path", sha: str, count: int) -> None:
    from coordinator_core.git.commit import NothingToCommit, commit_paths

    try:
        commit_paths(root, [SIDECAR_REL], f"symbol-knowledge: {count} definitions current at {sha[:10]}")
    except NothingToCommit:
        pass


def run_job(
    root: "str | Path",
    payload: Mapping[str, Any],
    runner: Optional[AgentRunner] = None,
    commit: Optional[Callable[["str | Path", str, int], None]] = None,
) -> "tuple[str, str]":
    """Run the batches for *payload*, write and commit the sidecar, return ``(status, detail)``.

    The detail ends with the job's ``tokens_in``/``tokens_out``/``tokens_per_symbol``; the same
    numbers land in ``cost_path`` beside the job payload."""
    from coordinator_core.symbol_knowledge.build_cost import SpawnCounter, cpu_seconds

    stats: "dict[str, Any]" = {"tin": 0, "tout": 0, "sent": 0, "estimated": False, "deferred": []}
    with SpawnCounter() as spawns:
        before = cpu_seconds()
        status, detail = _execute(root, payload, runner, commit, stats)
        elapsed = cpu_seconds() - before
    sent = stats["sent"]
    per_symbol = round((stats["tin"] + stats["tout"]) / sent) if sent else 0
    if sent:
        source = "estimated" if stats["estimated"] else "reported"
        detail += f"; tokens_in={stats['tin']} tokens_out={stats['tout']} tokens_per_symbol={per_symbol} ({source})"
    _write_cost(
        root,
        str(payload.get("run_id", "")),
        BuildCost(COST_OP, round(elapsed * 1000, 3), spawns.count, stats["tin"], stats["tout"], 0),
        per_symbol,
        stats,
    )
    return status, detail


def cost_path(root: "str | Path", run_id: str) -> Path:
    return Path(root) / JOB_DIR_REL / f"{_SAFE.sub('_', run_id)}.cost.json"


def _write_cost(root: "str | Path", run_id: str, cost: BuildCost, per_symbol: int, stats: Mapping[str, Any]) -> None:
    if not run_id:
        return
    from dataclasses import asdict

    from coordinator_core.atomic_replace import atomic_write_bytes

    record = {
        **asdict(cost),
        "tokens_per_symbol": per_symbol,
        "tokens_source": "estimated" if stats["estimated"] else "reported",
        "symbols_sent": stats["sent"],
        "deferred": [str(s["qualified_name"]) for s in stats["deferred"]],
    }
    path = cost_path(root, run_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, json.dumps(record, sort_keys=True).encode("utf-8"))
    except OSError:
        pass


def _execute(
    root: "str | Path",
    payload: Mapping[str, Any],
    runner: Optional[AgentRunner],
    commit: Optional[Callable[["str | Path", str, int], None]],
    stats: "dict[str, Any]",
) -> "tuple[str, str]":
    sha = str(payload.get("sha", ""))
    flagged = list(payload.get("flagged", []))
    traps = {k: list(v) for k, v in dict(payload.get("traps", {})).items()}
    existing = read_sidecar(root)
    call = runner or (lambda model, prompt: claude_runner(model, prompt, cwd=root))
    contexts = batch_context.resolve(root, flagged)
    definitions: "dict[str, tuple[str, str]]" = {}
    errors = []
    for kind, model in MODEL_FOR_KIND.items():
        group = [s for s in flagged if s.get("kind") == kind]
        kept, over = batch_context.fit(group, contexts)
        stats["deferred"].extend(over)
        if not kept:
            continue
        prompt = build_prompt(kind, kept, existing, contexts)
        try:
            reply = call(model, prompt)
        except Exception as exc:  # noqa: BLE001 -- a failed batch loses only its own kind
            errors.append(f"{model} {type(exc).__name__}")
            continue
        if isinstance(reply, RunnerReply):
            text, tin, tout = reply.text, reply.tokens_in, reply.tokens_out
        else:
            text, tin, tout = reply, batch_context.estimate_tokens(prompt), batch_context.estimate_tokens(reply)
            stats["estimated"] = True
        stats["tin"] += tin
        stats["tout"] += tout
        stats["sent"] += len(kept)
        found = parse_definitions(text, [s["qualified_name"] for s in kept])
        if not found:
            errors.append(f"{model} unparseable")
        definitions.update({name: (d, sha) for name, d in found.items()})
    if stats["deferred"]:
        errors.append(f"{len(stats['deferred'])} deferred over budget")
    if not definitions and not traps:
        return "failed", "no definitions produced: " + ", ".join(errors or ["empty reply"])
    write_sidecar(root, merge_entries(existing, definitions, traps))
    try:
        (commit or _commit_sidecar)(root, sha, len(definitions))
    except Exception as exc:  # noqa: BLE001 -- the file is written; report the commit miss
        return "failed", f"sidecar written, commit failed: {type(exc).__name__}"
    detail = f"{len(definitions)} current at {sha[:10]}"
    if errors:
        detail += "; " + ", ".join(errors)
    return "ran", detail


def _emit_descriptions(
    root: "str | Path",
    payload: Mapping[str, Any],
    flagged: Sequence[Mapping[str, Any]],
    definitions: Mapping[str, "tuple[str, str]"],
    models: Mapping[str, str],
    contexts: Mapping[str, batch_context.FileContext],
    resolve_key: Optional[KeyResolver],
) -> "tuple[int, int]":
    """Write one tier-1 ``symbol_description`` line per described symbol whose docstring is empty,
    has a source span and resolves to a SCIP key; returns ``(emitted, skipped)``."""
    resolve_key = resolve_key or _default_resolve_key
    repo = str(payload.get("repo") or Path(root).name)
    sha = str(payload.get("sha", ""))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    records, skipped = [], 0
    for s in flagged:
        name = str(s["qualified_name"])
        if name not in definitions or not s.get("docstring_empty", True):
            continue
        path = str(s["path"]).replace("\\", "/")
        ctx = contexts.get(str(s["path"]))
        span = ctx.spans.get(name) if ctx else None
        key = resolve_key(path, name) if span else None
        if span is None or not key:
            skipped += 1
            continue
        records.append(
            {
                "kind": "symbol_description",
                "v": 1,
                "repo": repo,
                "symbol_key": key,
                "file_path": path,
                "body_hash": span.body_hash,
                "commit_sha": sha,
                "description": _two_sentences(definitions[name][0]),
                "tier": 1,
                "model": models.get(name, ""),
                "generated_at": now,
                "producer": PRODUCER,
            }
        )
    if records:
        _write_descriptions(root, records)
    return len(records), skipped


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Detached entry: ``--job <payload.json>``; never raises."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "--job":
        return 2
    job = Path(args[1])
    try:
        payload = json.loads(job.read_text(encoding="utf-8"))
        root = job.parents[2]
        run_id = str(payload["run_id"])
        status, detail = run_job(root, payload)
    except Exception as exc:  # noqa: BLE001
        status, detail, root, run_id = "failed", f"batch job {type(exc).__name__}", None, None
        try:
            root = job.parents[2]
            run_id = str(json.loads(job.read_text(encoding="utf-8"))["run_id"])
        except Exception:  # noqa: BLE001
            return 1
    recorded = True
    try:
        from coordinator_core.ops.dispatch_emit.run_close import record_leg_outcome, scrub_detail

        record_leg_outcome(Path(root), run_id, LEG, status, scrub_detail(detail, root))
    except Exception:  # noqa: BLE001
        recorded = False
    try:
        job.unlink()
    except OSError:
        pass
    return 0 if recorded and status == "ran" else 1


if __name__ == "__main__":
    raise SystemExit(main())
