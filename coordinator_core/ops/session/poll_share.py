"""coordinator_core.ops.session.poll_share -- the EM poll-share metric over a session transcript.

DoE wiki event-driven-waits-and-stall-watchdog.md section 5: the share of EM assistant turns,
inside dispatch-to-completion-notice windows, that were polls. One streaming pass over the JSONL;
the file is never loaded whole. The output carries no path.

Classifier (CLASSIFIER_VERSION pins it):
  * Turn: all main-chain assistant records sharing one requestId (a turn is split per content block).
  * Poll turn: has tool calls and every one is a POLL_TOOLS call, a Monitor call with no `action`,
    or a Bash whose command is only `sleep <n>`.
  * Keep-alive tick: a turn with no tool call that answers a Monitor event. Counted separately and
    never in poll_turns; it is in em_turns.
  * Window: opens at an Agent tool_use, closes at the task-notification carrying its tool-use id.
    The dispatching turn and the turn answering the notice fall outside it. A window with no
    notice stays open (`closed: false`).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.hooks.flag_em_poll_in_flight import POLL_TOOLS
from coordinator_core.ipc import register_op

CLASSIFIER_VERSION = 1

_SLEEP_ONLY = re.compile(r"^\s*sleep\s+\d+(?:\.\d+)?[smh]?\s*;?\s*$")
_NOTICE_ID = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")
_MONITOR_EVENT = re.compile(r"<monitor[-_ ]?event|Monitor event", re.IGNORECASE)


def _is_poll_call(block: Dict[str, Any]) -> bool:
    name = block.get("name")
    if name in POLL_TOOLS:
        return True
    inp = block.get("input")
    inp = inp if isinstance(inp, dict) else {}
    if name == "Monitor":
        return not inp.get("action")
    if name == "Bash":
        return bool(_SLEEP_ONLY.match(str(inp.get("command", ""))))
    return False


def _classify(calls: List[Dict[str, Any]], answers_monitor: bool) -> str:
    if not calls:
        return "keepalive" if answers_monitor else "other"
    return "poll" if all(_is_poll_call(c) for c in calls) else "other"


def compute(transcript_path: str, session_id: str) -> Dict[str, Any]:
    windows: List[Dict[str, Any]] = []
    open_by_id: Dict[str, Dict[str, Any]] = {}
    totals = {"em_turns": 0, "poll_turns": 0, "keepalive_ticks": 0}

    turn_key: Optional[str] = None
    turn_calls: List[Dict[str, Any]] = []
    turn_monitor = False
    turn_windows: List[Dict[str, Any]] = []
    pending_monitor = False

    def flush() -> None:
        nonlocal turn_key, turn_calls, turn_monitor, turn_windows
        if turn_key is None:
            return
        if turn_windows:
            kind = _classify(turn_calls, turn_monitor)
            totals["em_turns"] += 1
            for w in turn_windows:
                w["em_turns"] += 1
            if kind == "poll":
                totals["poll_turns"] += 1
                for w in turn_windows:
                    w["poll_turns"] += 1
            elif kind == "keepalive":
                totals["keepalive_ticks"] += 1
                for w in turn_windows:
                    w["keepalive_ticks"] += 1
        turn_key, turn_calls, turn_monitor, turn_windows = None, [], False, []

    with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"type":"assistant"' in line:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "assistant" or rec.get("isSidechain"):
                    continue
                sid = rec.get("sessionId")
                if sid and session_id and sid != session_id:
                    continue
                msg = rec.get("message") or {}
                key = rec.get("requestId") or msg.get("id") or rec.get("uuid")
                if key != turn_key:
                    flush()
                    turn_key = key
                    turn_monitor = pending_monitor
                    turn_windows = list(open_by_id.values())
                    pending_monitor = False
                content = msg.get("content")
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    turn_calls.append(block)
                    if block.get("name") == "Agent" and block.get("id"):
                        w = {"em_turns": 0, "poll_turns": 0, "keepalive_ticks": 0, "closed": False}
                        windows.append(w)
                        open_by_id[block["id"]] = w
            elif '"type":"user"' in line:
                flush()
                if "<task-notification>" in line:
                    m = _NOTICE_ID.search(line)
                    w = open_by_id.pop(m.group(1), None) if m else None
                    if w is not None:
                        w["closed"] = True
                pending_monitor = bool(_MONITOR_EVENT.search(line))
    flush()

    em = totals["em_turns"]
    out_windows = [
        {"index": i, **w, "poll_share": (w["poll_turns"] / w["em_turns"]) if w["em_turns"] else 0.0}
        for i, w in enumerate(windows)
    ]
    return {
        "session_id": session_id,
        "classifier_version": CLASSIFIER_VERSION,
        "em_turns": em,
        "poll_turns": totals["poll_turns"],
        "keepalive_ticks": totals["keepalive_ticks"],
        "poll_share": (totals["poll_turns"] / em) if em else 0.0,
        "windows": out_windows,
    }


@register_op("session.poll_share")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """session.poll_share -- {transcript_path, session_id} -> the poll-share metric as JSON.

    Streams the transcript once; see the module docstring for the classifier. A missing or
    unreadable transcript raises ValueError.
    """
    path = params.get("transcript_path")
    if not isinstance(path, str) or not path:
        raise ValueError("session.poll_share: transcript_path is required")
    try:
        return await asyncio.to_thread(compute, path, str(params.get("session_id") or ""))
    except OSError as exc:
        raise ValueError(f"session.poll_share: cannot read transcript ({type(exc).__name__})") from exc
