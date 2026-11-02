"""agent_pipe.agy -- Antigravity adapter: headless `agy -p ... --output-format stream-json` in, `--conversation <id> -p` out.

Messages are `step_update` events of `step_type == "agent_response"` with non-blank `text_delta`;
every other event (init, user_input, whitespace-only DONE delta, result) is not a message. The
conversation id rides on each step_update.
"""
from __future__ import annotations

import json
import shutil
from typing import Optional

_OUTPUT_FLAGS = ["--output-format", "stream-json"]


def _agy() -> str:
    return shutil.which("agy") or "agy"


class AgyAdapter:
    """Adapter for the Antigravity CLI; argv never carries a permission-bypass flag or a settings path."""

    def spawn_argv(self, prompt: str, conversation: Optional[str] = None) -> list[str]:
        """argv for a fresh headless run; a given `conversation` resumes it instead."""
        if conversation:
            return self.resume_argv(conversation, prompt)
        return [_agy(), "-p", prompt, *_OUTPUT_FLAGS]

    def resume_argv(self, conversation: str, text: str) -> list[str]:
        """argv delivering `text` into an existing agy conversation."""
        return [_agy(), "--conversation", conversation, "-p", text, *_OUTPUT_FLAGS]

    def parse_line(self, line: str) -> Optional[tuple[str, str]]:
        """(conversation, content) for an agent_response step_update carrying text, else None."""
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(event, dict) or event.get("event") != "step_update":
            return None
        step = event.get("step_update")
        if not isinstance(step, dict) or step.get("step_type") != "agent_response":
            return None
        text = step.get("text_delta")
        conversation = step.get("conversation_id")
        if not isinstance(text, str) or not text.strip() or not isinstance(conversation, str) or not conversation:
            return None
        return conversation, text.strip()
