"""The editor-kill guard reads a script file only when the command launches it. A `.py` passed as data
(a pytest target, a `git diff` path) is not run; reading it denied running and diffing this guard's own
tests (run 0cbad1 test leg, 2026-11-22), whose fixtures carry the kill text."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.bash_guards import block_editor_kill_by_name as guard

KILL = "taskkill /IM UnrealEditor.exe /F\n"


def _check(cmd: str, cwd: Path):
    return guard.check({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd)})


@pytest.fixture
def killer(tmp_path: Path) -> Path:
    (tmp_path / "killer.py").write_text(KILL, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("cmd", [
    "python -m pytest -q killer.py",
    "git diff HEAD -- killer.py",
    "cat killer.py",
    "python -c \"print(1)\" killer.py",
])
def test_a_script_passed_as_data_is_not_read(killer: Path, cmd: str) -> None:
    assert _check(cmd, killer) is None


@pytest.mark.parametrize("cmd", ["python killer.py", "python -X utf8 killer.py", "./killer.py"])
def test_a_launched_script_holding_a_name_kill_is_still_denied(killer: Path, cmd: str) -> None:
    assert _check(cmd, killer) is not None
