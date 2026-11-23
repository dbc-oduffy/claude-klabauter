"""The publish leg of a round runs at idle priority, so its disk I/O cannot starve the warm engine.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_publish_priority.py -q
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "percolate-round.py"


def test_the_real_publish_leg_is_spawned_with_the_idle_priority_flags() -> None:
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    legs = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_run"
            and any(k.arg == "timeout" and getattr(k.value, "id", "") == "_PUBLISH_LEG_TIMEOUT_SECS"
                    for k in n.keywords)]
    assert legs, "no publish leg found"
    for call in legs:
        flags = [k for k in call.keywords if k.arg == "creationflags"]
        assert flags and getattr(flags[0].value, "id", "") == "_PUBLISH_CHILD_CREATIONFLAGS"


@pytest.mark.skipif(sys.platform != "win32", reason="priority classes are Windows creation flags")
def test_the_flags_carry_idle_priority_and_no_console() -> None:
    src = _SCRIPT.read_text(encoding="utf-8")
    ns: dict = {"subprocess": subprocess}
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", "") == "_PUBLISH_CHILD_CREATIONFLAGS")
    exec(ast.get_source_segment(src, node), ns)
    assert ns["_PUBLISH_CHILD_CREATIONFLAGS"] & subprocess.IDLE_PRIORITY_CLASS
    assert ns["_PUBLISH_CHILD_CREATIONFLAGS"] & subprocess.CREATE_NO_WINDOW
