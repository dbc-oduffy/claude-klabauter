"""Idle-compaction eligibility: one pure-ish verdict per session per tick.

``decide`` returns inject or skip with a reason. The transcript path and idle epoch come from
``group_em.read_pass`` (never a second resolver); fired-engine-ask from ``em_turn_count``;
context percent from ``context_usage_sidecar``. ``armed_state`` is the caller's
``{session_id: {"size": int, "blocked_size": int?}}`` ledger: ``arm`` records an injection, and
``decide`` mutates only the ``blocked_size`` key when it first sees a missing compaction sentinel.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, MutableMapping, Optional

from coordinator_core.group_em.read_pass import _transcript_path_for, transcript_activity_epoch
from coordinator_core.idle_compact import contract
from coordinator_core.session import context_usage_sidecar, em_turn_count

INJECT = "inject"
SKIP = "skip"


@dataclass(frozen=True)
class Decision:
    """``action`` is ``inject`` or ``skip``; ``reason`` is the log reason."""

    action: str
    reason: str


def _skip(reason: str) -> Decision:
    return Decision(SKIP, reason)


def arm(armed_state: MutableMapping[str, dict], session_id: str, size: int) -> None:
    """Record an injection keyed by the transcript size at injection time."""
    armed_state[session_id] = {"size": size}


def transcript_size(transcript_path: Optional[str]) -> Optional[int]:
    if not transcript_path:
        return None
    try:
        return os.path.getsize(transcript_path)
    except OSError:
        return None


def _sentinel_dir() -> str:
    from coordinator_core.temp_layout import managed_tempdir

    return str(managed_tempdir())


def _compaction_occurred(session_id: str, injected_size: int) -> bool:
    """The PreCompact sentinel exists and records a pre-compaction size at or past the injection's."""
    path = os.path.join(_sentinel_dir(), f"compaction-occurred-{session_id}")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
    except OSError:
        return False
    try:
        return int(text) >= injected_size
    except ValueError:
        return True


def _allowlisted(record: Any) -> bool:
    import json

    try:
        with open(contract.allowlist_path(), encoding="utf-8") as fh:
            entries = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(entries, list):
        return False
    cwd = getattr(record, "cwd", None)
    candidates = {getattr(record, "name", None)}
    if cwd:
        candidates.add(os.path.basename(str(cwd).rstrip("/\\")))
        candidates.add(str(cwd).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1])
    candidates.discard(None)
    candidates.discard("")
    return any(isinstance(e, str) and e in candidates for e in entries)


def _marker_present(path) -> bool:
    return path is not None and os.path.exists(path)


def decide(
    sid: str,
    record: Any,
    transcript_path: Optional[str],
    now: float,
    armed_state: MutableMapping[str, dict],
) -> Decision:
    """Skips, in order: not-allowlisted, opted-out, compact-hold, no-engine-ask, not-idle,
    under-threshold, then the re-arm checks (already-injected-this-stretch, precompact-blocked)."""
    cwd = getattr(record, "cwd", None)
    if not _allowlisted(record):
        return _skip("not-allowlisted")
    if _marker_present(contract.optout_marker_path(sid, cwd)):
        return _skip("opted-out")
    if _marker_present(contract.compact_hold_marker_path(sid, cwd)):
        return _skip("compact-hold")

    if not cwd:
        return _skip("no-cwd")
    if not transcript_path:
        transcript_path = _transcript_path_for(sid, cwd)

    state = em_turn_count.read_state(cwd, sid, transcript_path)
    if not state or state.get("fire_turn") is None:
        return _skip("no-engine-ask")

    epoch, _timestamped = transcript_activity_epoch(sid, cwd)
    if epoch is None or now - epoch < contract.IDLE_SECONDS:
        return _skip("not-idle")

    reading = context_usage_sidecar.read_usage(sid, now=now, transcript_path=transcript_path)
    pct = None
    if reading is not None:
        pct = reading.context_window.get("used_percentage")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)) or pct <= contract.CONTEXT_PCT:
        return _skip("under-threshold")

    entry = armed_state.get(sid)
    if entry is None:
        return Decision(INJECT, "eligible")

    size = transcript_size(transcript_path)
    injected_size = entry.get("size", 0)
    if size is None or size <= injected_size:
        return _skip("already-injected-this-stretch")
    if _compaction_occurred(sid, injected_size):
        return Decision(INJECT, "eligible")
    blocked_size = entry.get("blocked_size")
    if blocked_size is None:
        entry["blocked_size"] = size
        return _skip("precompact-blocked")
    if size <= blocked_size:
        return _skip("precompact-blocked")
    return Decision(INJECT, "eligible")
