"""Pins that the warm engine and hook-door sources never reach WSL (wsl.exe, \\\\wsl$ shares, wslpath)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[2]
_DOOR_PATH_DIRS = ("warm", "hooks", "bash_guards")
_WSL_TOKEN = re.compile(r"wsl\.exe|wslpath|wsl\$|\\\\wsl|//wsl|\bwsl\s+-")


def _door_path_sources() -> list[Path]:
    found: list[Path] = []
    for name in _DOOR_PATH_DIRS:
        for path in sorted((_CORE / name).rglob("*.py")):
            if "tests" in path.relative_to(_CORE).parts:
                continue
            found.append(path)
    return found


def test_scan_covers_the_door_path_sources() -> None:
    names = {p.name for p in _door_path_sources()}
    assert {"front_door.py", "server.py", "settings.py"} <= names


@pytest.mark.parametrize("path", _door_path_sources(), ids=lambda p: p.name)
def test_door_path_source_names_no_wsl_dependency(path: Path) -> None:
    hits = [
        (n, line.strip())
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
        if _WSL_TOKEN.search(line)
    ]
    assert not hits, f"{path.name} reaches WSL: {hits}"


def test_the_pattern_refuses_a_wsl_reach() -> None:
    assert _WSL_TOKEN.search('subprocess.run(["wsl.exe", "ls"])')
    assert _WSL_TOKEN.search(r'Path(r"\\wsl$\Ubuntu\home")')
    assert not _WSL_TOKEN.search("def answers(self): return 1")
