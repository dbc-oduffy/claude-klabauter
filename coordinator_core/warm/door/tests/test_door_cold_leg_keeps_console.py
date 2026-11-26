"""An interactive launcher's cold fall-through keeps the parent's console.

agy-coordinator.exe / grok-coordinator.exe exec a TUI through the door's cold leg; a
CREATE_NO_WINDOW spawn gave the python child a private hidden conhost the TUI drew
into. The cold leg must pass 0 when the door owns a console window and keep
CREATE_NO_WINDOW only for a windowless parent (hooks), where it prevents a flash.
"""

from __future__ import annotations

import re
from pathlib import Path

DOOR_DIR = Path(__file__).resolve().parents[1]
DOOR_C = DOOR_DIR / "door.c"


def _cold_fall_through_body() -> str:
    src = DOOR_C.read_text(encoding="utf-8")
    end = src.index("could not launch the fallback")
    start = src.rindex("build_fallback_cmdline(", 0, end)
    return src[start:end]


def test_cold_fall_through_gates_create_no_window_on_a_console_window() -> None:
    body = _cold_fall_through_body()
    assert re.search(r"GetConsoleWindow\(\)\s*!=\s*NULL\s*\?\s*0\s*:\s*CREATE_NO_WINDOW", body)
    spawn = re.search(r"CreateProcessW\(\s*NULL, cmdline_w, NULL, NULL, TRUE, (\w+),", body)
    assert spawn and spawn.group(1) == "cold_flags"
