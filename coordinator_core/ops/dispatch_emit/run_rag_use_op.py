"""`dispatch.run_rag_use`: the per-run rag-vs-grep receipt (`rag_use` of the wake digest) from agent transcripts.

Resolves the run's workflow dirs and worker rows exactly as `stage_rag_gate_op` does and builds the
receipt with `rag_prefetch.rag_use_receipt`, so the receipt verdict and the build-gate halt read one
rule (`rag_use.is_gated` / `rag_use.agent_rag_halt`). Read-only: no file written, no process spawned.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.dispatch_emit import rag_prefetch, rag_use, stage_rag_gate_op, ue_phase_map

_LOG = logging.getLogger(__name__)

OP_NAME = "dispatch.run_rag_use"


_TAIL_BYTES = 262144
_OUTAGE_RE = re.compile(
    r"example-retrieval-repo(?:\s+(?:mcp|server|daemon|index|tools?))?\s+(?:(?:is|was|were|has been|had been|stayed|remained)\s+)?"
    r"(?:down|unreachable|timed out|not serving|an? outage)\b|\boutage\b[^.;,]{0,30}\bexample_retrieval_repo"
    r"|example-retrieval-repo(?:\s+(?:mcp|server|daemon|index|tools?))?\s+outage\b",
    re.IGNORECASE,
)
_OUTAGE_NEGATED_RE = re.compile(r"\b(?:not|no|never|no longer|without)\b|n't", re.IGNORECASE)
_OUTAGE_RECOVERED_RE = re.compile(r"\b(?:worked|recovered|retr(?:y|ied))\b", re.IGNORECASE)
_FRICTION_KIND = "retrieval_check_failed"


def _tail_lines(path: Path) -> list[str]:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - _TAIL_BYTES))
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    return lines[1:] if size > _TAIL_BYTES and lines else lines


def _friction_rows(root: str, rows: list[dict], session_id: str) -> list[dict]:
    """`retrieval_check_failed` rows of hook-friction.jsonl naming a worker `agent_id` or the run's `session_id`."""
    ids = {str(r.get("agent_id")) for r in rows if r.get("agent_id")}
    if session_id:
        ids.add(session_id)
    kept: list[dict] = []
    for line in _tail_lines(Path(root) / ".coordinator-local" / "hook-friction.jsonl"):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or _FRICTION_KIND not in {str(v) for v in row.values()}:
            continue
        if any(str(row.get(k)) in ids for k in ("agent_id", "agentId", "session_id", "sessionId")):
            kept.append(row)
    return kept


def _final_report(transcript: Path) -> str:
    for line in reversed(_tail_lines(transcript)):
        try:
            msg = json.loads(line).get("message") or {}
        except (ValueError, AttributeError):
            continue
        if msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
            continue
        text = "\n".join(
            str(b.get("text") or "") for b in msg["content"] if isinstance(b, dict) and b.get("type") == "text"
        )
        if text.strip():
            return text
    return ""


def _outage_asserted(line: str) -> bool:
    """True when `line` states a example-retrieval-repo outage: no negation before the outage word and no recovery word on the line."""
    m = _OUTAGE_RE.search(line)
    if not m or _OUTAGE_NEGATED_RE.search(line[: m.end()]):
        return False
    return not _OUTAGE_RECOVERED_RE.search(re.split(r"[.;]", line[m.end():], maxsplit=1)[0])


def _outage_reports(wf_dirs: list[Path], rows: list[dict]) -> list[str]:
    """Lines of each worker's final report naming a example-retrieval-repo outage, as `<label>: <line>`."""
    found: list[str] = []
    for wf in wf_dirs:
        for row in rows:
            agent_id = row.get("agent_id")
            path = wf / f"agent-{agent_id}.jsonl"
            if not agent_id or not path.is_file():
                continue
            for text_line in _final_report(path).splitlines():
                if _outage_asserted(text_line):
                    found.append(f"{row.get('label') or agent_id}: {text_line.strip()[:240]}")
    return found


def _with_metrics(rows: list[dict], metrics: list[dict]) -> list[dict]:
    """Copies of `rows` with `agent_type` filled and `ue_calls` raised from the gate-metrics rows of the same `agent_id`."""
    out: list[dict] = []
    for r in rows:
        aid = str(r.get("agent_id") or "")
        mine = [m for m in metrics if aid and str(m.get("agent_id") or "") == aid]
        row = dict(r)
        if not row.get("agent_type"):
            row["agent_type"] = next((m["agent_type"] for m in mine if m.get("agent_type")), row.get("agent_type"))
        if row.get("ue_calls") is not None:
            row["ue_calls"] = max(int(row["ue_calls"] or 0), sum(1 for m in mine if m.get("decision") == "retrieval"))
        out.append(row)
    return out


def _by_phase(rows: list[dict], friction: list[dict], metrics: list[dict]) -> dict:
    """Per-phase, per-agent retrievals, gate denies and metrics rows; a null or unmapped `agent_type` lands under `unmapped`."""
    phases: dict[str, dict[str, Any]] = {}
    for r in rows:
        aid = str(r.get("agent_id") or "")
        phase = ue_phase_map.phase_of(r.get("agent_type")) or "unmapped"
        slot = phases.setdefault(phase, {"retrievals": 0, "denies": 0, "metrics_rows": 0, "agents": []})
        entry = {
            "agent_id": aid,
            "agent_type": r.get("agent_type") or None,
            "retrievals": int(r.get("ue_calls") or 0),
            "denies": sum(1 for f in friction if aid and str(f.get("agent_id") or f.get("agentId") or "") == aid),
            "metrics_rows": sum(1 for m in metrics if aid and str(m.get("agent_id") or "") == aid),
        }
        slot["agents"].append(entry)
        for k in ("retrievals", "denies", "metrics_rows"):
            slot[k] += entry[k]
    return phases


def _unreadable(reason: str) -> dict:
    return {"error": "transcript_unreadable", "reason": reason}


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """The `rag_use` receipt for the workers of run `run_id`, plus the run's `halted_by` when one applies.

    Params: ``repo_root``, ``run_id``, optional ``session_id`` (else the caller's session), optional
    ``labels``, ``plugin_root`` (gated-roles file home). Returns ``{rag_use, halted_by?}``; an
    unreadable run (no run id, session dir or workflow dir) is ``{error: "transcript_unreadable",
    reason}``, never zero counts. Tier none returns the receipt with no agents and never halts.
    """
    root = str(params.get("repo_root") or repo_root or Path.cwd())
    run_id = str(params.get("run_id") or "")
    if not run_id:
        return _unreadable("run_id is required")
    raw_labels = params.get("labels")
    labels = {str(x) for x in raw_labels} if isinstance(raw_labels, list) and raw_labels else None
    try:
        from coordinator_core.session import core
        from coordinator_core.usage import run_locate

        session_id = str(params.get("session_id") or core.resolve_session_id(root) or "")
        session_dir = run_locate.session_dir_for(session_id) if session_id else None
        if session_dir is None:
            return _unreadable("session dir not located")
        wf_dirs = stage_rag_gate_op._run_dirs(session_dir, run_id)
        if not wf_dirs:
            return _unreadable(f"no workflow run names {run_id!r}")
        rows: list[dict[str, Any]] = []
        for wf in wf_dirs:
            rows.extend(stage_rag_gate_op._worker_rows(wf, labels))
    except Exception as exc:  # noqa: BLE001 -- unknown is never zero
        return _unreadable(f"transcript unreadable: {type(exc).__name__}: {exc}")
    if not rows and rag_use.is_ue_repo(root):
        return _unreadable(f"no worker rows for {run_id!r}")
    tier = rag_use.rag_tier(root)
    roles = rag_use.load_gated_roles(params.get("plugin_root") or None)
    out: dict[str, Any] = {
        "rag_use": rag_prefetch.rag_use_receipt({"agents": rows}, repo_root=root, roles=roles, tier=tier)
    }
    ue_halt = None
    if rag_use.is_ue_repo(root):
        metrics = ue_phase_map.read_gate_metrics(root, run_id)
        ue_rows = _with_metrics(rows, metrics)
        friction = _friction_rows(root, rows, session_id)
        ue_halt = rag_use.ue_retrieval_verdict(
            ue=True,
            rows=ue_rows,
            friction_rows=friction,
            outage_reports=_outage_reports(wf_dirs, rows),
            metrics_rows=metrics,
        )
        out["ue_retrieval"] = {"by_phase": _by_phase(ue_rows, friction, metrics)}
        if ue_halt:
            ue_halt["by_phase"] = out["ue_retrieval"]["by_phase"]
    if ue_halt:
        out["halted_by"] = ue_halt
    elif tier != rag_use.TIER_NONE:
        serving: Any = None
        if tier == rag_use.TIER_UE:
            try:
                from coordinator_core.bash_guards._rag_serving import serves_repo

                serving = bool(serves_repo(root))
            except Exception as exc:  # noqa: BLE001 -- unknown serving stays None, never False
                serving = None
                _LOG.warning("serves_repo failed for %s: %r", root, exc)
        halted_by = rag_use.agent_rag_halt(rows, tier=tier, roles=roles, serving=serving)
        if halted_by:
            out["halted_by"] = halted_by
    return out
