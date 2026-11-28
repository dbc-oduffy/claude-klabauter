"""agent_pipe.inbox_receipts -- per-sender receipt store: a receiver stamps a msg id once it has taken the message.

Layout `<settings_home>/agent-pipe/receipts/<sender_conversation>.jsonl`, one `{"msg_id","receiver","ts"}` line per receipt.
Pure file I/O: no subprocess is spawned.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from coordinator_core._settings_home import settings_home
from coordinator_core.agent_pipe.contract import PIPE_DIRNAME, _checked_id
from coordinator_core.agent_pipe.mailbox import _append_lock

SETTINGS_KEY = "COORDINATOR_SETTINGS_HOME"


class ReceiptRootMissing(RuntimeError):
    """The settings root the receipt store lives under does not exist; names the key that resolves it."""


def _root() -> Path:
    home = settings_home()
    if not home.is_dir():
        raise ReceiptRootMissing(f"receipt store needs {SETTINGS_KEY} to name an existing directory; {str(home)!r} is missing")
    return home / PIPE_DIRNAME / "receipts"


def receipts_path(sender_conversation: str) -> Path:
    """Receipt file for one sender conversation; rejects ids that could escape the receipts dir, raises ReceiptRootMissing without a settings root."""
    return _root() / (_checked_id("sender_conversation", sender_conversation) + ".jsonl")


def write(sender_conversation: str, msg_id: str, receiver: str, ts: str) -> Path:
    """Append one receipt line for `msg_id` under the sender's file and return that path."""
    path = receipts_path(sender_conversation)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps({"msg_id": _checked_id("msg_id", msg_id), "receiver": receiver, "ts": ts}, ensure_ascii=False) + "\n").encode("utf-8")
    with _append_lock(path):
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    return path


def read(sender_conversation: str) -> dict[str, dict]:
    """Map msg_id to its first receipt `{"msg_id","receiver","ts"}`; {} when the sender has none. Torn or non-object lines are skipped."""
    try:
        text = receipts_path(sender_conversation).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    out: dict[str, dict] = {}
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("msg_id"), str):
            out.setdefault(rec["msg_id"], rec)
    return out
