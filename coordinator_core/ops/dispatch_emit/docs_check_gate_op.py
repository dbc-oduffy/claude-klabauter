"""`dispatch.docs_check_rag_gate`: did the docs-checker leg use example-retrieval-repo when it had to.

Reads the docs-checker child's own transcript (no subprocess): a UE repo with
Example-retrieval-repo installed whose docs-checker had every call refused (or engine_queryable False) is
`rag_corpus_down`; failures that were only timeouts yield `degraded`; a checker that made no
Example-retrieval-repo call yields `rag_not_used`, and an unlocatable or unreadable transcript yields
`transcript_unreadable`: neither is an outage, and each reply carries `branch` naming which fired.
Otherwise every `[rag:]`/`[live:]` citation in the checker's final text
must be backed by a tool call in any agent transcript of the run (`citation_unbacked` if not).
Precedence: rag_corpus_down > transcript_unreadable > citation_unbacked > rag_not_used > degraded > ok.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops import rag_freshness
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_CITATION_UNBACKED
from coordinator_core.ops.dispatch_emit import docs_check_citations, rag_use
from coordinator_core.win_portability import no_console_creationflags

OP_NAME = "dispatch.docs_check_rag_gate"
_HEAD_BYTES = 65536
# One CLI name for probe and halt text: `rag_use` owns it, so no second literal can drift or be scrubbed.
_CORPUS = rag_use._CLI
_REPROBE_BACKOFF_S = (0.1,)
_PROBE_TIMEOUT_S = 0.4
_RAW_LIMIT = 300


def _cli_runner(argv: list[str]) -> Optional[str]:
    """Stdout of `argv`, or None when the CLI is absent, fails or times out."""
    if shutil.which(argv[0]) is None:
        return None
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


# Module-level seams: tests replace them; production probes the real CLI.
_probe_runner: Callable[[list[str]], Optional[str]] = _cli_runner
_sleep: Callable[[float], None] = time.sleep


def _reprobe() -> tuple[bool, str]:
    """(recovered, raw probe output): the daemon is re-probed with a short backoff.

    Recovered only when a probe answers state ok or degraded; an absent or unreadable
    probe is never recovery, so an unreachable CLI keeps the down verdict.
    """
    raw = ""
    for delay in _REPROBE_BACKOFF_S:
        _sleep(delay)
        out = _probe_runner([_CORPUS, "probe-reachability", "--json"])
        raw = (out or "").strip()[:_RAW_LIMIT] or raw
        state = rag_use.probe(runner=lambda _argv, _out=out: _out)
        if state is not None and state.state in ("ok", "degraded"):
            return True, raw
    return False, raw


def _down_message(branch: str, raw: str) -> str:
    """Halt text naming the real corpus, the probe that failed, its raw output and the restorer."""
    probe = (
        "engine_domain_status engine_queryable=false" if branch == "engine_queryable_false"
        else "docs-checker project calls all refused (connection refused)"
    )
    shown = raw or "no output (probe CLI absent or silent)"
    return (
        f"{_CORPUS} engine corpus is down: probe {probe}; re-probe {_CORPUS} probe-reachability "
        f"returned: {shown}. UE work will not succeed without it. "
        f"Ask the {_CORPUS} owner session to restore it, then refire this run."
    )


def _head(path: Path, nbytes: int) -> str:
    try:
        with open(path, "rb") as handle:
            return handle.read(nbytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _locate_agent(session_dir: Path, run_id: str, label: str) -> Optional[tuple[str, str]]:
    """(run dir, agent id) of the one agent labelled `label` in the Workflow run that names `run_id`.

    Several runs of a session can carry the same label; the run whose agent transcript head
    names `run_id` wins. None when no agent, or no unique one, matches.
    """
    from coordinator_core.usage import agent_usage

    wf_root = session_dir / "subagents" / "workflows"
    try:
        wf_dirs = [Path(e.path) for e in os.scandir(wf_root) if e.name.startswith("wf_") and e.is_dir()]
    except OSError:
        return None
    hits: list[tuple[Path, str, list[str]]] = []
    for wf in wf_dirs:
        agents = agent_usage.started_agents(str(wf / "journal.jsonl"))
        for agent_id, agent_label in agents:
            if agent_label == label:
                hits.append((wf, agent_id, [aid for aid, _ in agents]))
    if len(hits) > 1:
        hits = [
            h for h in hits
            if any(run_id in _head(h[0] / f"agent-{aid}.jsonl", _HEAD_BYTES) for aid in h[2])
        ]
    return (str(hits[0][0]), hits[0][1]) if len(hits) == 1 else None


def _final_text(transcript: Path) -> str:
    """Text blocks of the last assistant message that has any; unreadable lines are skipped."""
    last = ""
    try:
        fh = open(transcript, encoding="utf-8", errors="replace")
    except OSError:
        return last
    with fh:
        for line in fh:
            if '"text"' not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            msg = obj.get("message") if isinstance(obj, dict) else None
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                last = content or last
            elif isinstance(content, list):
                text = "\n".join(
                    str(b.get("text", "")) for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
                last = text or last
    return last


def _unbacked_citations(run_dir: Path, agent_id: str) -> list:
    """Citations in the checker's final text that no tool call anywhere in the run backs."""
    cites = docs_check_citations.parse(_final_text(run_dir / f"agent-{agent_id}.jsonl"))
    if not cites:
        return []
    calls: list[tuple[str, str]] = []
    try:
        names = sorted(e.name for e in os.scandir(run_dir) if e.name.startswith("agent-") and e.name.endswith(".jsonl"))
    except OSError:
        names = []
    for name in names:
        calls.extend(docs_check_citations.tool_calls(run_dir / name))
    return docs_check_citations.unbacked(cites, calls)


def _unreadable(branch: str, reason: str, queryable: Any) -> dict:
    """Verdict for a checker transcript that cannot be read: `branch` names which lookup missed.

    Unknown is never an outage; only an explicit engine_queryable False is a definite down.
    """
    if queryable is False:
        recovered, raw = _reprobe()
        if recovered:
            return {"verdict": rag_use.DEGRADED, "reason": reason, "branch": "reprobe_recovered",
                    "probe": raw}
        return {"verdict": rag_use.HALT_RAG_CORPUS_DOWN,
                "message": _down_message("engine_queryable_false", raw),
                "reason": reason, "branch": "engine_queryable_false"}
    return {"verdict": rag_use.TRANSCRIPT_UNREADABLE, "reason": reason, "branch": branch}


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """`_verdict` plus ``index_freshness {state, refresh_queued}``, raised before the RAG-use judgment.

    Only a UE repo with example-retrieval-repo installed carries the field; the precondition is advisory
    and never changes the verdict.
    """
    root = str(params.get("repo_root") or repo_root or Path.cwd())
    fresh = None
    if rag_use.is_ue_repo(root) and rag_use.rag_installed(root):
        pre = rag_freshness.freshness_precondition(root)
        fresh = {"state": pre["state"], "refresh_queued": pre["refresh_queued"]}
    out = _verdict(params, repo_root)
    if fresh is not None:
        out["index_freshness"] = fresh
    return out


def _verdict(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Verdict for the docs-checker leg `label` of run `run_id`.

    Params: ``repo_root``, ``run_id``, ``label``, optional ``session_id`` (else the caller's
    session), optional bool ``engine_queryable`` (False is a definite down). Returns ``{verdict: not_applicable|ok|degraded|rag_not_used|transcript_unreadable|rag_corpus_down|citation_unbacked,
    message?, reason?, branch?, rag_calls?, rag_failed?, rag_timeouts?, rag_refused?, unbacked?}``;
    ``message`` is the restore text on ``rag_corpus_down``; ``branch`` names which lookup or
    count produced a down, unreadable or not-used verdict; ``degraded`` (timeout-only failures)
    halts nothing and still runs the citation check; ``unbacked`` lists the citations no run
    call backs.
    """
    root = str(params.get("repo_root") or repo_root or Path.cwd())
    if not (rag_use.is_ue_repo(root) and rag_use.rag_installed(root)):
        return {"verdict": "not_applicable"}
    run_id, label = str(params.get("run_id") or ""), str(params.get("label") or "")
    queryable = params.get("engine_queryable")
    if not run_id or not label:
        return _unreadable("params_missing", "run_id and label are required", queryable)
    try:
        from coordinator_core.session import core
        from coordinator_core.usage import agent_usage, run_locate

        session_id = str(params.get("session_id") or core.resolve_session_id(root) or "")
        session_dir = run_locate.session_dir_for(session_id) if session_id else None
        if session_dir is None:
            return _unreadable("session_dir_not_located", "session dir not located", queryable)
        located = _locate_agent(session_dir, run_id, label)
        if located is None:
            return _unreadable(
                "agent_not_located",
                f"no unique agent labelled {label!r} in a run naming {run_id!r}", queryable)
        run_dir, agent_id = located
        from coordinator_core.workflow_watch.stall import _transcript_path

        transcript = _transcript_path(run_dir, agent_id)
        if transcript is None or os.path.getsize(transcript) == 0:
            return _unreadable(
                "transcript_missing", f"no transcript content for agent {agent_id!r}", queryable)
        row = agent_usage._agent_row(run_dir, agent_id, label)
    except Exception as exc:  # noqa: BLE001 -- unknown is never an outage and never passes silently
        return _unreadable(
            "transcript_unreadable",
            f"transcript unreadable: {type(exc).__name__}: {exc}", queryable)
    calls, failed = int(row.get("rag_calls") or 0), int(row.get("rag_failed") or 0)
    timeouts, refused = int(row.get("rag_timeouts") or 0), int(row.get("rag_refused") or 0)
    verdict = rag_use.docs_check_rag_verdict(
        ue=True, rag_installed=True, rag_calls=calls, rag_failed=failed,
        rag_timeouts=timeouts, rag_refused=refused,
        engine_queryable=queryable if isinstance(queryable, bool) else None,
    )
    out: dict = {
        "verdict": verdict, "rag_calls": calls, "rag_failed": failed,
        "rag_timeouts": timeouts, "rag_refused": refused,
    }
    if verdict == rag_use.HALT_RAG_CORPUS_DOWN:
        branch = "engine_queryable_false" if queryable is False else "all_calls_refused"
        recovered, raw = _reprobe()
        if recovered:
            out["verdict"] = rag_use.DEGRADED
            out["branch"] = "reprobe_recovered"
            out["probe"] = raw
        else:
            out["message"] = _down_message(branch, raw)
            out["branch"] = branch
            return out
    if verdict == rag_use.RAG_NOT_USED:
        out["branch"] = "zero_rag_calls"
    missing = _unbacked_citations(Path(run_dir), agent_id)
    if missing:
        out["verdict"] = HALT_CITATION_UNBACKED
        out["unbacked"] = [f"[{c.form}: {c.tool} {c.subject}]" for c in missing]
    else:
        out["unbacked"] = []
    return out
