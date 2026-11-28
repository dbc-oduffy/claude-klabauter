"""`dispatch.stage_rag_gate`: did the workers of one run's stage use example-retrieval-repo.

Reads every non-relay agent transcript of the Workflow run whose agent heads name `run_id`
(no subprocess, one pass per transcript) and returns `rag_use.stage_rag_verdict`. A verdict is a
stage outcome the caller turns into a halt-with-next-action, never a deny.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.dispatch_emit import rag_use

OP_NAME = "dispatch.stage_rag_gate"
_HEAD_BYTES = 65536
_RELAY_AGENT_TYPE = "coordinator:op-relay"
_CLOSE_LABEL = "research-close"
HALT_UE_PHASE_RETRIEVAL_MISSING = rag_use.HALT_UE_PHASE_RETRIEVAL_MISSING
_STAGE_PHASE = {"sizing": "sizing", "plan": "planning", "review": "review"}
_STRUCTURAL_TOOLS = frozenset(
    f"mcp__project-rag__{n}" for n in ("project_code_outline", "project_symbol", "project_referencers", "project_trace"))
_HEALTH_UNJUDGED = ("health_unreadable", "health_unknown_repo")


def _head(path: Path, nbytes: int) -> str:
    try:
        with open(path, "rb") as handle:
            return handle.read(nbytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _run_dirs(session_dir: Path, run_id: str) -> list[Path]:
    """Workflow `wf_` dirs of the session with an agent transcript head naming `run_id`."""
    from coordinator_core.usage import agent_usage, run_locate

    wf_root = session_dir / "subagents" / "workflows"
    try:
        wf_dirs = [Path(e.path) for e in os.scandir(wf_root) if e.name.startswith("wf_") and e.is_dir()]
    except OSError:
        return []
    floor = run_locate._run_floor(run_id)
    hits: list[Path] = []
    for wf in wf_dirs:
        if floor is not None and run_locate._mtime(wf / "journal.jsonl") < floor:
            continue
        agents = agent_usage.started_agents(str(wf / "journal.jsonl"))
        if any(run_id in _head(wf / f"agent-{aid}.jsonl", _HEAD_BYTES) for aid, _ in agents):
            hits.append(wf)
    return hits


def _worker_rows(wf: Path, labels: Optional[set]) -> list[dict]:
    """Usage rows of the run's worker agents: op-relay and research-close agents are excluded."""
    from coordinator_core.usage import agent_usage
    from coordinator_core.workflow_watch.stall import _transcript_path

    rows: list[dict] = []
    for agent_id, label in agent_usage.started_agents(str(wf / "journal.jsonl")):
        if label == _CLOSE_LABEL or (labels and label not in labels):
            continue
        transcript = _transcript_path(str(wf), agent_id)
        if agent_usage.agent_meta(str(wf), agent_id, transcript).get("agentType") == _RELAY_AGENT_TYPE:
            continue
        if transcript is None or os.path.getsize(transcript) == 0:
            continue
        rows.append(agent_usage._agent_row(str(wf), agent_id, label))
    return rows


def _unreadable(reason: str) -> dict:
    return {"verdict": rag_use.TRANSCRIPT_UNREADABLE, "rag_calls": 0, "per_worker": [], "reason": reason}


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Per-agent example-retrieval-repo verdict for the workers of run `run_id`, armed by `rag_use.rag_tier`.

    Params: ``repo_root``, ``run_id``, optional ``session_id`` (else the caller's session),
    optional ``labels`` (only those agent labels count), ``plugin_root`` (gated-roles file home;
    default the installed plugin roots), ``stage`` (sizing|research|plan|review|build, echoed).
    Tier none is ``not_applicable``; tier rag or ue arms the gate even when the repo is stale or
    unserved. Returns ``{verdict: not_applicable|ok|degraded|rag_not_used|transcript_unreadable,
    tier, rag_calls, per_worker, roles_source, halted_by?, reason?}``; ``verdict`` is rag_not_used
    when any gated agent made zero calls, and ``halted_by`` (from `rag_use.agent_rag_halt`) is set
    when a halt applies. Unknown (no session, run or transcript) is ``transcript_unreadable``,
    never zero, and never halts.
    """
    root = str(params.get("repo_root") or repo_root or Path.cwd())
    tier = rag_use.rag_tier(root)
    stage = params.get("stage")
    stamp: dict[str, Any] = {"tier": tier}
    if isinstance(stage, str) and stage:
        stamp["stage"] = stage
    if tier == rag_use.TIER_NONE:
        return {**rag_use.stage_rag_verdict(served=False, rows=None), **stamp}
    run_id = str(params.get("run_id") or "")
    if not run_id:
        return {**_unreadable("run_id is required"), **stamp}
    raw_labels = params.get("labels")
    labels = {str(x) for x in raw_labels} if isinstance(raw_labels, list) and raw_labels else None
    try:
        from coordinator_core.session import core
        from coordinator_core.usage import run_locate

        session_id = str(params.get("session_id") or core.resolve_session_id(root) or "")
        session_dir = run_locate.session_dir_for(session_id) if session_id else None
        if session_dir is None:
            return {**_unreadable("session dir not located"), **stamp}
        rows: list[dict[str, Any]] = []
        wf_dirs = _run_dirs(session_dir, run_id)
        for wf in wf_dirs:
            rows.extend(_worker_rows(wf, labels))
    except Exception as exc:  # noqa: BLE001 -- unknown is never zero and never passes silently
        return {**_unreadable(f"transcript unreadable: {type(exc).__name__}: {exc}"), **stamp}
    if not rows:
        return {**rag_use.stage_rag_verdict(served=True, rows=None), **stamp,
                "reason": f"no worker transcript found in a run naming {run_id!r}"}
    out = _judge(root, tier, rows, params.get("plugin_root"), stamp)
    phase = _STAGE_PHASE.get(stage) if isinstance(stage, str) else None
    if phase and rag_use.is_ue_repo(root):
        try:
            _apply_ue_phase(out, root, run_id, phase, rows, wf_dirs)
        except Exception as exc:  # noqa: BLE001 -- an unreadable phase check is reported, never a pass
            out["ue_phase"] = {"phase": phase, "agents": [], "error": f"{type(exc).__name__}: {exc}"}
    return out


def _semantic_lane_down(root: str) -> bool:
    """True when project_health judges the repo's semantic lane down or partial; an unjudgeable health read is not down."""
    from coordinator_core.ops.dispatch_emit import rag_health

    entry = rag_health.repo_verdict(root)
    return bool(entry) and entry.get("verdict") not in _HEALTH_UNJUDGED


def _structural_calls(wf_dirs: list[Path], agent_id: str) -> int:
    """Count of structural example-retrieval-repo tool calls in the agent's transcript."""
    if not agent_id:
        return 0
    for wf in wf_dirs:
        path = wf / f"agent-{agent_id}.jsonl"
        if not path.is_file():
            continue
        count = 0
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "tool_use" not in line:
                    continue
                try:
                    content = (json.loads(line).get("message") or {}).get("content")
                except (ValueError, AttributeError):
                    continue
                for block in content if isinstance(content, list) else []:
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in _STRUCTURAL_TOOLS:
                        count += 1
        return count
    return 0


def _apply_ue_phase(out: dict, root: str, run_id: str, phase: str, rows: list[dict], wf_dirs: list[Path]) -> None:
    """Join the stage's agents with example-game-repo metrics rows, set `out["ue_phase"]`, and halt on a missing phase retrieval."""
    from coordinator_core.ops.dispatch_emit import run_rag_use_op, ue_phase_map

    metrics = ue_phase_map.read_gate_metrics(root, run_id)
    by_agent: dict[str, list[dict]] = {}
    for m in metrics:
        by_agent.setdefault(str(m.get("agent_id") or ""), []).append(m)
    friction = run_rag_use_op._friction_rows(root, rows, "")
    lane_down = _semantic_lane_down(root)
    agents: list[dict] = []
    in_phase: list[dict] = []
    for r in rows:
        agent_id = str(r.get("agent_id") or "")
        own = by_agent.get(agent_id, []) if agent_id else []
        types = [r.get("agent_type")] + [m.get("agent_type") for m in own]
        if phase not in {ue_phase_map.phase_of(t) for t in types}:
            continue
        in_phase.append(r)
        structural = _structural_calls(wf_dirs, agent_id)
        retrievals = int(r.get("ue_calls") or 0) + sum(1 for m in own if m.get("decision") == "retrieval")
        if lane_down:
            retrievals += structural
        agents.append({
            "agent_id": agent_id, "agent_type": str(r.get("agent_type") or ""), "retrievals": retrievals,
            "structural_calls": structural,
            "denies": sum(1 for f in friction if agent_id and str(f.get("agent_id") or f.get("agentId") or "") == agent_id),
            "metrics_rows": len(own),
        })
    outage = run_rag_use_op._outage_reports(wf_dirs, in_phase)
    out["ue_phase"] = {"phase": phase, "agents": agents, "semantic_lane_down": lane_down}
    if outage:
        cause = rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_OUTAGE_REPORTED
        halt: dict[str, Any] = {"halted": cause, "cause": cause, "phase": phase, "agents": agents, "reports": outage}
    elif any(a["denies"] > 0 for a in agents):
        cause = rag_use.HALT_UE_RETRIEVAL_CHECK_FAILED
        halt = {"halted": cause, "cause": cause, "phase": phase, "agents": agents}
    elif not any(a["retrievals"] > 0 for a in agents):
        cause = HALT_UE_PHASE_RETRIEVAL_MISSING
        halt = {"halted": cause, "cause": cause, "phase": phase, "agents": agents}
    else:
        return
    if out.get("halted_by"):
        out["rag_halted_by"] = out["halted_by"]
    out["halted_by"] = halt


def _judge(root: str, tier: str, rows: list[dict], plugin_root: Any, stamp: dict) -> dict:
    roles = rag_use.load_gated_roles(plugin_root or None)
    serving: Any = None
    if tier == rag_use.TIER_UE:
        from coordinator_core.bash_guards._rag_serving import serves_repo

        serving = bool(serves_repo(root))
    halted_by = rag_use.agent_rag_halt(rows, tier=tier, roles=roles, serving=serving)
    failed_ids = {a.get("agent_id") for a in (halted_by or {}).get("agents", [])}
    per_worker: list[dict] = []
    calls = bad = 0
    silent_gated = False
    gated_all_errored = True
    gated_timeouts = 0
    for r in rows:
        n, errors = int(r.get("rag_calls") or 0), int(r.get("rag_errors") or 0)
        gated = rag_use.is_gated(r.get("agent_type"), roles)
        silent_gated = silent_gated or (gated and n <= 0)
        if gated and n > 0:
            gated_all_errored = gated_all_errored and errors >= n
            gated_timeouts += int(r.get("rag_timeouts") or 0)
        calls += n
        bad += int(r.get("rag_failed") or 0)
        verdict = "exempt" if not gated else ("fail" if n <= 0 or r.get("agent_id") in failed_ids else "pass")
        per_worker.append({
            "label": str(r.get("label") or ""), "agent_id": str(r.get("agent_id") or ""),
            "agent_type": str(r.get("agent_type") or ""), "gated": gated, "rag_calls": n,
            "rag_failed": int(r.get("rag_failed") or 0), "rag_errors": errors,
            "grep_calls": int(r.get("grep_calls") or 0), "verdict": verdict,
        })
    if silent_gated:
        verdict = rag_use.RAG_NOT_USED
    elif calls > 0 and bad >= calls:
        verdict = rag_use.DEGRADED
    else:
        verdict = "ok"
    out: dict[str, Any] = {"verdict": verdict, "rag_calls": calls, "per_worker": per_worker,
                           "roles_source": roles.get("source"), **stamp}
    if roles.get("source") == "fallback":
        out["roles_reason"] = roles.get("reason")
    if verdict == rag_use.DEGRADED and gated_all_errored and gated_timeouts > 0:
        out[rag_use.SEARCH_DEGRADED_KEY] = True
    if halted_by:
        out["halted_by"] = halted_by
    return out
