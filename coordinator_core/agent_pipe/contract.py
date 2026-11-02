"""agent_pipe.contract -- message shape, adapter protocol and on-disk locations every agent_pipe module shares.

Layout under `<settings_home>/agent-pipe`: `runs/<run_id>` per pipe run, `mailboxes/<session_id>.jsonl`
per receiving Claude session (one JSON object per line).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Protocol

from coordinator_core._settings_home import settings_home

AGENTS = ("antigravity", "codex")

PIPE_DIRNAME = "agent-pipe"


@dataclass(frozen=True)
class PipeMessage:
    """One unprompted reply from a headless agent session to a live Claude session."""

    id: str
    sender_agent: str
    sender_conversation: str
    to_session: str
    content: str
    ts: str

    def to_json_line(self) -> str:
        """Single-line JSON, no trailing newline; embedded newlines in content stay escaped."""
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


class Adapter(Protocol):
    """Per-agent CLI knowledge: how to start, resume and read one headless session."""

    def spawn_argv(self, prompt: str, conversation: Optional[str] = None) -> list[str]: ...

    def resume_argv(self, conversation: str, text: str) -> list[str]: ...

    def parse_line(self, line: str) -> Optional[tuple[str, str]]:
        """Return (conversation, content) for a line carrying a message, else None."""
        ...


def _checked_id(kind: str, value: str) -> str:
    if not value or value in (".", "..") or any(c in value for c in "/\\\0"):
        raise ValueError(f"invalid {kind}: {value!r}")
    return value


def _pipe_root() -> Path:
    return settings_home() / PIPE_DIRNAME


def run_dir(run_id: str) -> Path:
    """Run directory `<settings_home>/agent-pipe/runs/<run_id>`; rejects ids that could escape it."""
    return _pipe_root() / "runs" / _checked_id("run_id", run_id)


def mailbox_path(session_id: str) -> Path:
    """Mailbox file `<settings_home>/agent-pipe/mailboxes/<session_id>.jsonl`; rejects ids that could escape it."""
    return _pipe_root() / "mailboxes" / (_checked_id("session_id", session_id) + ".jsonl")
