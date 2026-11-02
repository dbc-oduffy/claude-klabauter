"""Fake peer CLI plus its adapter, for peer relay tests (pure Python, any OS).

Script modes:
  `fake_peer.py stdin <name> [opening]`          reads one JSON turn per stdin line until EOF.
  `fake_peer.py spawn <name> [opening]`          prints the opening message and exits.
  `fake_peer.py resume <name> <conv> <text>`     prints the reply to one delivered turn and exits.
A delivered turn reads `[from <sender>] <body>`. A body not starting `echo:` is answered
`@<sender> echo:<body>`; an `echo:` body is answered `got:<body>` (unaddressed, ends the exchange).
`FAKE_PEER_ALWAYS=1` answers every body with `@<sender> echo:<body>`, so two peers ping-pong.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

SCRIPT = str(Path(__file__).resolve())
_FROM = re.compile(r"^\[from (\S+)\] (.*)$", re.DOTALL)


def conversation_of(name: str) -> str:
    return f"conv-{name}"


class FakePeerAdapter:
    """`kind` is "stdin" (long-lived, JSON turns on stdin) or "resume" (one child per delivery)."""

    def __init__(self, name: str, kind: str) -> None:
        self.name = name
        self.kind = kind

    def spawn_argv(self, prompt: str, conversation: Optional[str] = None) -> list[str]:
        mode = "stdin" if self.kind == "stdin" else "spawn"
        return [sys.executable, SCRIPT, mode, self.name, prompt]

    def resume_argv(self, conversation: str, text: str) -> list[str]:
        return [sys.executable, SCRIPT, "resume", self.name, conversation, text]

    def parse_line(self, line: str) -> Optional[tuple[str, str]]:
        try:
            event = json.loads(line)
        except ValueError:
            return None
        if not isinstance(event, dict) or event.get("type") != "message":
            return None
        return event["conversation"], event["content"]


def _emit(name: str, content: str) -> None:
    sys.stdout.write(json.dumps({"type": "message", "conversation": conversation_of(name), "content": content}) + "\n")
    sys.stdout.flush()


def _reply(name: str, text: str) -> None:
    match = _FROM.match(text)
    if not match:
        _emit(name, f"got:{text}")
        return
    sender, body = match.groups()
    if body.startswith("echo:") and not os.environ.get("FAKE_PEER_ALWAYS"):
        _emit(name, f"got:{body}")
    else:
        _emit(name, f"@{sender} echo:{body}")


def main(argv: list[str]) -> int:
    mode, name = argv[0], argv[1]
    sys.stdout.write(json.dumps({"type": "init"}) + "\n")
    if mode == "resume":
        _reply(name, argv[3])
        return 0
    opening = argv[2] if len(argv) > 2 else ""
    if mode == "spawn":
        if opening:
            _emit(name, opening)
        return 0
    if opening and opening.startswith("@"):
        _emit(name, opening)
    for line in sys.stdin:
        turn = json.loads(line)
        _reply(name, turn["message"]["content"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
