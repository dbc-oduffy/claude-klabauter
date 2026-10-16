"""Spawn budget for ``block_home_root_bash_write``: a verdict costs zero processes.

Asserts nothing about wall clock; the unit is process count (DR-344).
"""

from __future__ import annotations

import json
import os
import subprocess
import traceback
from typing import List

import pytest

from coordinator_core import machine_profile
from coordinator_core.bash_guards import block_home_root_bash_write as guard
from coordinator_core.bash_guards import dispatch

_GUARD_FILE = os.path.normcase(guard.__file__)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "X" / "repo" / "scratch").mkdir(parents=True)  # an existing repo's scratch, not a new home-dir folder
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "strict")
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: h))
    machine_profile.reset_cache()
    yield h
    machine_profile.reset_cache()


def _raw(cmd, tool, cwd):
    return json.dumps(
        {
            "tool_name": tool,
            "tool_input": {"command": cmd},
            "cwd": str(cwd),
            "session_id": "home-root-spawn-budget",
        }
    )


@pytest.fixture
def spawns_from_guard(monkeypatch) -> List[str]:
    """Spawn sites whose call stack passes through the guard module; other guards' spawns are not counted."""
    seen: List[str] = []

    def wrap(original):
        def recorder(*args, **kwargs):
            frames = [os.path.normcase(f.filename) for f in traceback.extract_stack()]
            if _GUARD_FILE in frames:
                seen.append(repr(args[:1]))
            return original(*args, **kwargs)

        return recorder

    monkeypatch.setattr(subprocess, "Popen", wrap(subprocess.Popen))
    for name in ("spawnv", "spawnve", "spawnl", "spawnle", "spawnlp", "spawnvp", "system"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, wrap(getattr(os, name)))
    return seen


def test_guard_check_spawns_nothing_when_spawning_is_forbidden(home, tmp_path, monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("spawn attempted")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    for name in ("spawnv", "spawnve", "spawnl", "spawnle", "spawnlp", "spawnvp", "system"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, forbidden)
    cases = [
        ("echo x > ~/fo.txt", "Bash"),
        (r"Set-Content -Path $env:USERPROFILE\fo.txt -Value x", "PowerShell"),
        ("echo x > ~/.bashrc", "Bash"),
        ("echo x > out.txt", "Bash"),
    ]
    results = [
        guard.check({"tool_name": t, "tool_input": {"command": c}, "cwd": str(tmp_path)})
        for c, t in cases
    ]
    assert results[0] is not None and results[1] is not None
    assert results[2] is None and results[3] is None


@pytest.mark.parametrize(
    "cmd,tool,denied",
    [
        ("echo x > ~/fo.txt", "Bash", True),
        ("echo x > ~/X/repo/scratch/x", "Bash", False),
    ],
)
def test_dispatch_verdict_spawns_nothing_from_this_guard(
    home, tmp_path, spawns_from_guard, cmd, tool, denied
):
    out = dispatch.evaluate_payload_json(_raw(cmd, tool, tmp_path))
    text = json.dumps(out)
    assert ("home dir" in text) is denied, text
    assert spawns_from_guard == []
