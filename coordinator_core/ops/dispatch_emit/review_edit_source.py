"""Which files did this session's review-stage agents write since a run began.

Provenance is the engine's own per-agent touch record, never a reviewer's prose. The
agent set comes from the session's ``dispatched-agents.txt`` (agent id, host, subagent
type, epoch), so only the candidate agents' sinks are opened; enumerating every agent
dir (``claim_index._enumerate_claim_sinks``) costs 47-156ms against ~870 dirs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from coordinator_core.session import core, touch_record

#: Subagent types that are NOT review-stage writers. A denylist, because the review
#: roster is DoE-supplied and open-ended: an unrecognised type must fail toward
#: landing or halting its edit, never toward silently stranding it. The executor stays
#: listed: an executor writing outside its row is the wrong-spine signal that
#: ``terminal_commit._undeclared_dirty`` reports, not review residue. Evidence: the
#: S0 probe of the 2026-10-16 incident runs (reviewer writes seen only from
#: ``coordinator:code-reviewer``; executor, plan-author and staff-eng wrote in other stages).
NON_REVIEW_SUBAGENT_TYPES = frozenset(
    {
        "executor",
        "enricher",
        "plan-author",
        "plan-author-residue",
        "residue-author",
        "sizing-scout",
        "git-commit-agent",
        "apm",
        "queue-grind-op-runner",
        "staff-eng",
        "exit-criterion-falsifier",
    }
)

_DISPATCHED_AGENTS = "dispatched-agents.txt"
_AGENTS_SUBDIR = ".agents"


def _bare_type(subagent_type: str) -> str:
    return subagent_type.strip().rsplit(":", 1)[-1]


def _agent_ids(session_dir: str) -> Optional[list[str]]:
    """Agent ids dispatched by the session whose type is not in the denylist; None when
    the dispatch ledger is unreadable; an absent ledger means no agent was dispatched."""
    try:
        text = Path(session_dir, _DISPATCHED_AGENTS).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        return None
    ids: list[str] = []
    for row in text.splitlines():
        fields = row.split("\t")
        if len(fields) < 3 or not fields[0]:
            continue
        if _bare_type(fields[2]) in NON_REVIEW_SUBAGENT_TYPES:
            continue
        if fields[0] not in ids:
            ids.append(fields[0])
    return ids


def session_review_writes(
    worktree_root: Path, session_id: Optional[str], since: Optional[float]
) -> Optional[dict[str, str]]:
    """path -> agent id for every write-kind touch event with ``ts >= since`` by a
    non-denylisted agent owned by ``session_id``; the latest event names the agent.

    None means the source cannot answer (no session, no ``since``, no store, or an unreadable or malformed record) and is never "no writes".
    """
    got = _collect(worktree_root, session_id, since)
    return None if got is None else got[0]


def session_agent_writes(
    worktree_root: Path, session_id: Optional[str], since: Optional[float]
) -> Optional[dict[str, set[str]]]:
    """agent id -> every path it wrote since ``since`` (same agent set and None contract as
    ``session_review_writes``). Lets a caller ask which paths one agent touched, which the
    path -> latest-agent map cannot answer."""
    got = _collect(worktree_root, session_id, since)
    return None if got is None else got[1]


def sole_run_attested_writes(
    worktree_root: Path, session_id: Optional[str], run_id: Optional[str], since: Optional[float]
) -> frozenset[str]:
    """Paths the engine's touch record attributes to the session's review agents, when no other
    run holds this session: with a single run in the session every such write is that run's.
    Empty when another run of the session is held, the run is unnamed, or the record cannot answer."""
    if not session_id or not run_id:
        return frozenset()
    from coordinator_core.ops.dispatch_emit import run_token

    try:
        records = run_token.held_records(worktree_root)
    except Exception:  # noqa: BLE001 -- unreadable run tokens cannot prove the run is alone
        return frozenset()
    if any(r.get("session_id") == session_id and r.get("run_id") != run_id for r in records):
        return frozenset()
    by_agent = session_agent_writes(worktree_root, session_id, since)
    if not by_agent:
        return frozenset()
    return frozenset(p for paths in by_agent.values() for p in paths)


def _collect(
    worktree_root: Path, session_id: Optional[str], since: Optional[float]
) -> Optional[tuple[dict[str, str], dict[str, set[str]]]]:
    if not session_id or since is None:
        return None
    hub = core.sessions_dir(str(worktree_root))
    if not hub:
        return None
    ids = _agent_ids(os.path.join(hub, session_id))
    if ids is None:
        return None
    writes: dict[str, str] = {}
    by_agent: dict[str, set[str]] = {}
    stamped: dict[str, float] = {}
    for agent_id in ids:
        agent_dir = os.path.join(hub, _AGENTS_SUBDIR, agent_id)
        owner, conclusive = touch_record.agent_owner_sid(agent_dir)
        if not conclusive:
            return None
        if owner != session_id:
            continue
        for member in touch_record.discover_family(touch_record.sink_path(agent_dir)):
            try:
                raw = member.read_bytes()
            except FileNotFoundError:
                continue
            except OSError:
                return None
            for line in touch_record.iter_complete_lines(raw):
                try:
                    event = touch_record.decode_line(line)
                except touch_record.MalformedRecordLine:
                    return None
                if event.verb != touch_record.VERB_TOUCH or event.kind == touch_record.KIND_READ:
                    continue
                if event.timestamp < since:
                    continue
                by_agent.setdefault(agent_id, set()).add(event.path)
                if event.timestamp >= stamped.get(event.path, float("-inf")):
                    stamped[event.path] = event.timestamp
                    writes[event.path] = agent_id
    return writes, by_agent
