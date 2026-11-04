"""agent_pipe.codex -- Adapter for headless `codex exec --json` sessions.

Starts with `exec --json`, resumes with `exec resume <id> --json`, and reads `item.completed`
events whose item is an `agent_message`. Neither argv carries a sandbox-bypass flag.
"""
from __future__ import annotations

import json
from typing import Optional

from coordinator_core.agent_pipe.contract import classify_exit
from coordinator_core.codex_runner.resolve import resolve_codex

# The run dir is not a git repo; without this codex refuses to start.
_BASE_FLAGS = ("--json", "--skip-git-repo-check")


class CodexAdapter:
    """Adapter for codex. `parse_line` remembers the `thread.started` id and the first failure `cause`; it is stateful per run."""

    def __init__(self) -> None:
        self.thread_id: Optional[str] = None
        self.cause: Optional[str] = None

    def spawn_argv(self, prompt: str, conversation: Optional[str] = None) -> list[str]:
        """`<node> <codex.js> exec --json <prompt>`; a given `conversation` resumes it instead."""
        if conversation:
            return self.resume_argv(conversation, prompt)
        return [*resolve_codex(), "exec", *_BASE_FLAGS, prompt]

    def resume_argv(self, conversation: str, text: str) -> list[str]:
        """`<node> <codex.js> exec resume <id> --json <text>`."""
        return [*resolve_codex(), "exec", "resume", *_BASE_FLAGS, conversation, text]

    def proxy_argv(self) -> list[str]:
        """`<node> <codex.js> app-server proxy`: stdio bytes to the app-server control socket."""
        return [*resolve_codex(), "app-server", "proxy"]

    def parse_line(self, line: str) -> Optional[tuple[str, str]]:
        """(thread_id, text) for an agent_message item; None for every other line, malformed ones included."""
        line = line.strip()
        if not line:
            return None
        try:
            ev = json.loads(line)
        except ValueError:
            return None
        if not isinstance(ev, dict):
            return None
        if ev.get("type") in ("error", "turn.failed"):
            self.cause = self.cause or classify_exit(line)
            return None
        if ev.get("type") == "thread.started":
            tid = ev.get("thread_id")
            if isinstance(tid, str) and tid:
                self.thread_id = tid
            return None
        if ev.get("type") != "item.completed":
            return None
        item = ev.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            return None
        text = item.get("text")
        if not isinstance(text, str) or not text:
            return None
        return (self.thread_id or "", text)
