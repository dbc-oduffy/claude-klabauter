"""coordinator_core.hooks.agent_stop_reaper -- SubagentStop orphan-tree reaper.

At the moment a subagent is final, reaps the orphan process tree of each timed-out (harness
auto-backgrounded) launch the agent left, every kill verified by (pid, ctime) through the same
attribution as the TaskStop reaper. Never reaps run_in_background records (TaskStop's) and never
runs at a main-session Stop. Any doubt or failure means no kill plus a log row. Windows only.
"""

from __future__ import annotations

import sys

from coordinator_core.bash_guards import _host_probe as _hp
from coordinator_core.bash_guards import _process_kill
from coordinator_core.bash_guards import _taskstop_launch_store as store
from coordinator_core.bash_guards._taskstop_contract import ReapOutcome
from coordinator_core.bash_guards._taskstop_kill import reap_launch
from coordinator_core.hooks._envelope import no_advisory, payload_of
from coordinator_core.ipc import register_op

# Seams: tests substitute a KillPrimitives and a snapshot callable.
_kill = _process_kill
_snapshot = _hp.snapshot

# Every outcome but AMBIGUOUS is final for a stopped agent (killed, kill refused, or no attributable
# tree); AMBIGUOUS keeps the record, where a later look may resolve.
_DROP = (ReapOutcome.KILLED, ReapOutcome.INCOMPLETE, ReapOutcome.NO_CANDIDATE)


def _reap_one(rec) -> None:
    row = reap_launch(rec, store.records_near(rec.mark), _snapshot, _kill)
    store.append_row(row)
    if row.outcome in _DROP:
        store.delete_record(rec.task_id)


def _reap(session_id: str, agent_id: str) -> None:
    for rec in store.records_for_agent(session_id, agent_id):
        try:
            _reap_one(rec)
        except Exception:
            continue


@register_op("hooks.agent_stop_reaper")
def _handler(params: dict, repo_root=None) -> dict:
    """Reap the orphan trees of the timed-out launches the stopping subagent left;
    always returns no_advisory()."""
    try:
        if sys.platform == "win32":
            payload = payload_of(params)
            session_id, agent_id = payload.get("session_id"), payload.get("agent_id")
            if isinstance(session_id, str) and isinstance(agent_id, str) and session_id and agent_id:
                _reap(session_id, agent_id)
    except Exception:
        pass
    return no_advisory()
