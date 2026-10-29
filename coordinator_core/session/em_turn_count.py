"""EM turn counter in the calibration ledger's unit: one non-synthetic main-thread assistant message.

State lives at `<session hub>/<sid>/em-turns.json`. `advance` reads only transcript bytes appended
since the stored offset, never the whole file, and spawns nothing.

Negative-spec:
  - No full-transcript walk: a pending delta over READ_CAP jumps the offset to the tail and marks
    the record `partial` (counts are then a floor).
  - Only lines carrying the assistant-record marker reach `json.loads`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from coordinator_core import warp_fire
from coordinator_core.bounded_io import run_bounded

# experiments calibration-ladder/step_ledger_model.py EM_PRE_FIRE_TURNS_MAX at ec3b3e4945e0 (ledger rule O2).
PRE_FIRE_TURNS_MAX = 10
# experiments calibration-ladder/step_ledger_model.py EM_POST_LANDING_TURNS_MAX at ec3b3e4945e0 (ledger rule O3).
POST_LANDING_TURNS_MAX = 5

READ_CAP = 1 << 20

_STATE_FILENAME = "em-turns.json"
# Byte prefilter: matches only compact JSON (no space after the colon), as the harness writes
# transcripts; a spaced serialization would count zero turns and so stay silent, never over-signal.
_ASSISTANT_MARK = b'"role":"assistant"'


def state_path(repo_root, sid: str) -> Path:
    """`em-turns.json` beside the session's `xs-lane.json`; raises when the hub is unresolvable."""
    from coordinator_core.session import xs_lane_budget

    return xs_lane_budget.lane_path(repo_root, sid).with_name(_STATE_FILENAME)


def fresh_state(offset: int = 0) -> dict:
    return {
        "offset": offset,
        "last_msg_id": None,
        "turns": 0,
        "fire_turn": None,
        "landings": {},
        "pre_fire_signalled": False,
        "last_engine_run_id": None,
    }


def _tail(transcript_path) -> int:
    try:
        return os.stat(transcript_path).st_size
    except OSError:
        return 0


def load_state(path, transcript_path) -> dict:
    """The stored record; absent starts at offset 0, malformed resets at the transcript tail so a
    lost record never re-signals from zero turns."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return fresh_state()
    except OSError:
        return fresh_state(_tail(transcript_path))
    try:
        rec = json.loads(raw)
    except ValueError:
        return fresh_state(_tail(transcript_path))
    if (
        not isinstance(rec, dict)
        or not isinstance(rec.get("offset"), int)
        or isinstance(rec.get("offset"), bool)
        or rec["offset"] < 0
        or not isinstance(rec.get("turns"), int)
        or not isinstance(rec.get("landings"), dict)
        or (rec.get("fire_turn") is not None and not isinstance(rec.get("fire_turn"), int))
    ):
        return fresh_state(_tail(transcript_path))
    base = fresh_state()
    base.update(rec)
    return base


def save_state(path, state: dict) -> None:
    p = Path(path)
    payload = json.dumps(state)

    def _write() -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, p)

    # Best-effort counter: a stalled disk drops the save rather than hold the op.
    # Trap: the overrun worker completes late, so a stale state can overwrite a
    # newer one saved after the stalled key recovers.
    run_bounded(str(p), _write, default=None)


def _is_engine_ask_fire(content) -> bool:
    if not isinstance(content, list):
        return False
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") == "Workflow"
            and isinstance(block.get("input"), dict)
            and warp_fire.is_submit_name(block["input"].get("name"))
        ):
            return True
    return False


def advance(transcript_path, state: dict) -> dict:
    """Count turns in transcript bytes appended since `state['offset']`; returns a new state.

    One turn per distinct non-synthetic main-thread `message.id`; the first engine-ask `Workflow`
    tool_use sets `fire_turn` to its turn index (1-based). A trailing partial line is left for the
    next call. An unreadable transcript returns the state unchanged.
    """
    st = dict(state)
    st["landings"] = dict(state.get("landings") or {})
    try:
        with open(transcript_path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            offset = int(st.get("offset") or 0)
            if offset > size:
                offset = size
            if size - offset > READ_CAP:
                st["offset"] = size
                st["partial"] = True
                return st
            fh.seek(offset)
            data = fh.read(size - offset)
    except OSError:
        return st
    end = data.rfind(b"\n")
    if end < 0:
        return st
    st["offset"] = offset + end + 1
    last_id = st.get("last_msg_id")
    turns = int(st.get("turns") or 0)
    for line in data[:end].split(b"\n"):
        line = line.rstrip(b"\r")
        if _ASSISTANT_MARK not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("isSidechain") is True:
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        if msg.get("model") == "<synthetic>":
            continue
        mid = msg.get("id")
        if not isinstance(mid, str):
            continue
        if mid != last_id:
            turns += 1
            last_id = mid
        if st.get("fire_turn") is None and _is_engine_ask_fire(msg.get("content")):
            st["fire_turn"] = turns
    st["turns"] = turns
    st["last_msg_id"] = last_id
    return st


def read_state(repo_root, sid: str, transcript_path) -> Optional[dict]:
    """Convenience: the loaded state for a session, None when the hub is unresolvable."""
    try:
        return load_state(state_path(repo_root, sid), transcript_path)
    except Exception:
        return None
