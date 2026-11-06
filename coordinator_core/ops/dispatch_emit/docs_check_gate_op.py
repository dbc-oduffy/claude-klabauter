"""`dispatch.docs_check_rag_gate`: did the docs-checker leg use example-retrieval-repo when it had to.

Reads the docs-checker child's own transcript (no subprocess): a UE repo with
Example-retrieval-repo installed whose docs-checker made no successful example-retrieval-repo call is `rag_corpus_down`.
An unlocatable or unreadable transcript on such a repo is also `rag_corpus_down`: unknown never
passes a UE fact stage. Otherwise every `[rag:]`/`[live:]` citation in the checker's final text
must be backed by a tool call in any agent transcript of the run (`citation_unbacked` if not).
Precedence: rag_corpus_down > citation_unbacked > ok.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_CITATION_UNBACKED
from coordinator_core.ops.dispatch_emit import docs_check_citations, rag_use

OP_NAME = "dispatch.docs_check_rag_gate"
_HEAD_BYTES = 65536


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


def _down(reason: str, **extra: Any) -> dict:
    return {"verdict": rag_use.HALT_RAG_CORPUS_DOWN, "message": rag_use.RESTORE_MESSAGE,
            "reason": reason, **extra}


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Verdict for the docs-checker leg `label` of run `run_id`.

    Params: ``repo_root``, ``run_id``, ``label``, optional ``session_id`` (else the caller's
    session). Returns ``{verdict: not_applicable|ok|rag_corpus_down|citation_unbacked, message?,
    reason?, rag_calls?, rag_failed?, unbacked?}``; ``message`` is the restore text on
    ``rag_corpus_down``; ``unbacked`` lists the citations no run call backs.
    """
    root = str(params.get("repo_root") or repo_root or Path.cwd())
    if not (rag_use.is_ue_repo(root) and rag_use.rag_installed(root)):
        return {"verdict": "not_applicable"}
    run_id, label = str(params.get("run_id") or ""), str(params.get("label") or "")
    if not run_id or not label:
        return _down("run_id and label are required")
    try:
        from coordinator_core.session import core
        from coordinator_core.usage import agent_usage, run_locate

        session_id = str(params.get("session_id") or core.resolve_session_id(root) or "")
        session_dir = run_locate.session_dir_for(session_id) if session_id else None
        if session_dir is None:
            return _down("session dir not located")
        located = _locate_agent(session_dir, run_id, label)
        if located is None:
            return _down(f"no unique agent labelled {label!r} in a run naming {run_id!r}")
        run_dir, agent_id = located
        row = agent_usage._agent_row(run_dir, agent_id, label)
    except Exception as exc:  # noqa: BLE001 -- unknown never passes a UE fact stage
        return _down(f"transcript unreadable: {type(exc).__name__}: {exc}")
    calls, failed = int(row.get("rag_calls") or 0), int(row.get("rag_failed") or 0)
    verdict = rag_use.docs_check_rag_verdict(
        ue=True, rag_installed=True, rag_calls=calls, rag_failed=failed
    )
    out: dict = {"verdict": verdict, "rag_calls": calls, "rag_failed": failed}
    if verdict == rag_use.HALT_RAG_CORPUS_DOWN:
        out["message"] = rag_use.RESTORE_MESSAGE
        return out
    missing = _unbacked_citations(Path(run_dir), agent_id)
    if missing:
        out["verdict"] = HALT_CITATION_UNBACKED
        out["unbacked"] = [f"[{c.form}: {c.tool} {c.subject}]" for c in missing]
    else:
        out["unbacked"] = []
    return out
