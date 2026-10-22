"""
coordinator_core.arrival_record -- the per-session SubagentStop arrival log.

One line per subagent stop at
``<git-common-dir>/coordinator-sessions/<session_id>/subagent-arrivals.jsonl``.
It records arrival time, unlike ``agent-audit.jsonl``, which records
dispatch-out time. It lives beside the session, not under
``<session-id>/subagents/``, so it survives that directory's deletion.
Revives the cancelled spec
archive/handoffs/2026-07/2026-07-31-durable-subagent-arrival-record.md.

Never import anything under ``coordinator_core.hooks`` here: the stop-watcher
reads this module on a hot path and must not pay the hooks package import.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Union

from coordinator_core.atomic_append import append_line

_ARRIVALS_NAME = "subagent-arrivals.jsonl"
_SESSIONS_DIR = "coordinator-sessions"

PathLike = Union[str, Path]


def _segment(value: str, what: str) -> str:
    """Return ``value`` if it is exactly one safe path segment, else raise ValueError."""
    if (
        not isinstance(value, str)
        or not value
        or value in (".", "..")
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or ":" in value
    ):
        raise ValueError(f"{what} is not a single path segment: {value!r}")
    return value


def arrival_log_path(common_dir: PathLike, session_id: str) -> Path:
    """Path of the session's arrival log under the git common dir."""
    return (
        Path(common_dir)
        / _SESSIONS_DIR
        / _segment(session_id, "session_id")
        / _ARRIVALS_NAME
    )


def append_arrival(
    common_dir: PathLike,
    session_id: str,
    agent_id: str,
    agent_type: str,
    stopped_at: str,
) -> None:
    """Append one compact arrival line. Unconditional: no dedupe, no read-before-write."""
    _segment(agent_id, "agent_id")
    path = arrival_log_path(common_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"agent_id": agent_id, "agent_type": agent_type, "stopped_at": stopped_at},
        separators=(",", ":"),
    )
    append_line(path, (line + "\n").encode("utf-8"))


def has_arrived(common_dir: PathLike, session_id: str, agent_id: str) -> bool:
    """True when the log holds a line for ``agent_id``.

    A missing or unreadable log is False: the watcher fails toward waking,
    never toward silence.
    """
    _segment(agent_id, "agent_id")
    try:
        text = arrival_log_path(common_dir, session_id).read_text(encoding="utf-8")
    except OSError:
        return False
    return f'"agent_id":"{agent_id}"' in text
