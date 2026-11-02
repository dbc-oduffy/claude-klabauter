"""Fake headless agent CLI plus its adapter, for relay and op tests (pure Python, any OS).

Script: `fake_agent.py spawn <prompt>` or `fake_agent.py resume <conversation> <text>` prints
non-message noise then NDJSON message lines and exits. `FakeAdapter` is the Adapter for it.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

CONVERSATION = "fake-conv-1"
SCRIPT = str(Path(__file__).resolve())


class FakeAdapter:
    def spawn_argv(self, prompt: str, conversation: Optional[str] = None) -> list[str]:
        return [sys.executable, SCRIPT, "spawn", prompt]

    def resume_argv(self, conversation: str, text: str) -> list[str]:
        return [sys.executable, SCRIPT, "resume", conversation, text]

    def parse_line(self, line: str) -> Optional[tuple[str, str]]:
        try:
            event = json.loads(line)
        except ValueError:
            return None
        if not isinstance(event, dict) or event.get("type") != "message":
            return None
        return event["conversation"], event["content"]


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    mode = argv[0]
    if mode == "resume":
        _emit({"type": "init"})
        _emit({"type": "message", "conversation": argv[1], "content": f"echo:{argv[2]}"})
        return 0
    _emit({"type": "init"})
    _emit({"type": "message", "conversation": CONVERSATION, "content": "first"})
    time.sleep(0.2)
    _emit({"type": "message", "conversation": CONVERSATION, "content": "second"})
    _emit({"type": "result"})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
