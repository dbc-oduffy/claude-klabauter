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
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from coordinator_core.symbol_knowledge.contract import KIND_ADJUST, KIND_NET_NEW, FlaggedSymbol
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

#: ``runner(model, prompt) -> text``: the injectable agent call.
AgentRunner = Callable[[str, str], str]

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
            {"qualified_name": f.qualified_name, "path": f.path, "kind": f.kind, "public": f.public}
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


def build_prompt(kind: str, symbols: Sequence[Mapping[str, Any]], existing: Mapping[str, Mapping[str, Any]]) -> str:
    """The batch prompt: one JSON object back, ``{qualified_name: one-paragraph definition}``."""
    verb = "Revise the existing definition of" if kind == KIND_ADJUST else "Write a definition for"
    lines = [
        f"{verb} each symbol below: what it is for, its contract, and its traps, in one short "
        "paragraph per symbol. Read the named file in the working directory for the current code.",
        "Reply with a single JSON object mapping each qualified name exactly as given to its "
        "definition string, and nothing else.",
        "",
    ]
    for s in symbols:
        name = s["qualified_name"]
        prior = existing.get(name, {}).get("definition", "")
        lines.append(f"- {name} (file: {s['path']})" + (f"\n  existing definition: {prior}" if prior else ""))
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


def claude_runner(model: str, prompt: str, *, cwd: "str | Path | None" = None) -> str:
    """Default runner: headless ``claude -p`` with the prompt on stdin."""
    exe = shutil.which("claude")
    if exe is None:
        raise FileNotFoundError("claude executable not on PATH")
    done = subprocess.run(
        [exe, "-p", "--model", model],
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
    return done.stdout


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
    """Run the batches for *payload*, write and commit the sidecar, return ``(status, detail)``."""
    sha = str(payload.get("sha", ""))
    flagged = list(payload.get("flagged", []))
    traps = {k: list(v) for k, v in dict(payload.get("traps", {})).items()}
    existing = read_sidecar(root)
    call = runner or (lambda model, prompt: claude_runner(model, prompt, cwd=root))
    definitions: "dict[str, tuple[str, str]]" = {}
    errors = []
    for kind, model in MODEL_FOR_KIND.items():
        group = [s for s in flagged if s.get("kind") == kind]
        if not group:
            continue
        try:
            reply = call(model, build_prompt(kind, group, existing))
        except Exception as exc:  # noqa: BLE001 -- a failed batch loses only its own kind
            errors.append(f"{model} {type(exc).__name__}")
            continue
        found = parse_definitions(reply, [s["qualified_name"] for s in group])
        if not found:
            errors.append(f"{model} unparseable")
        definitions.update({name: (text, sha) for name, text in found.items()})
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
