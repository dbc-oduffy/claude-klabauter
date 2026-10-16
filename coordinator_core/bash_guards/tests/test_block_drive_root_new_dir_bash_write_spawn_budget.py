"""Spawn budget for ``block_drive_root_new_dir_bash_write``: a verdict costs zero processes.

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
from coordinator_core.bash_guards import block_drive_root_new_dir_bash_write as guard
from coordinator_core.bash_guards import dispatch

_GUARD_FILE = os.path.normcase(guard.__file__)
_SPAWNERS = ("spawnv", "spawnve", "spawnl", "spawnle", "spawnlp", "spawnvp", "system")


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "root"
    home = tmp_path / "home"
    (root / "existing").mkdir(parents=True)
    home.mkdir()
    monkeypatch.setenv("COORDINATOR_DRIVE_ROOTS", str(root))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "strict")
    monkeypatch.delenv("COORDINATOR_ALLOW_DRIVE_ROOT_NEW_DIR", raising=False)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
    machine_profile.reset_cache()
    yield root
    machine_profile.reset_cache()


def _raw(cmd, tool, cwd):
    return json.dumps(
        {"tool_name": tool, "tool_input": {"command": cmd}, "cwd": str(cwd), "session_id": "drive-root-spawn-budget"}
    )


@pytest.fixture
def spawns_from_guard(monkeypatch) -> List[str]:
    """Spawn sites whose call stack passes through the guard module."""
    seen: List[str] = []

    def wrap(original):
        def recorder(*args, **kwargs):
            if _GUARD_FILE in [os.path.normcase(f.filename) for f in traceback.extract_stack()]:
                seen.append(repr(args[:1]))
            return original(*args, **kwargs)

        return recorder

    monkeypatch.setattr(subprocess, "Popen", wrap(subprocess.Popen))
    for name in _SPAWNERS:
        if hasattr(os, name):
            monkeypatch.setattr(os, name, wrap(getattr(os, name)))
    return seen


def test_guard_check_spawns_nothing_when_spawning_is_forbidden(world, tmp_path, monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("spawn attempted")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    for name in _SPAWNERS:
        if hasattr(os, name):
            monkeypatch.setattr(os, name, forbidden)
    r = world.as_posix()
    fire = [guard.check({"tool_name": t, "tool_input": {"command": c}, "cwd": str(tmp_path)})
            for c, t in ((f"mkdir {r}/new", "Bash"), (f"mkdir {r}/new", "PowerShell"))]
    quiet = [guard.check({"tool_name": "Bash", "tool_input": {"command": c}, "cwd": str(tmp_path)})
             for c in (f"mkdir {r}/existing/a", "echo x > out.txt", "ls /e/some/path")]
    assert all(x is not None for x in fire)
    assert all(x is None for x in quiet)


@pytest.mark.parametrize("cmd,denied", [("mkdir {r}/new", True), ("mkdir {r}/existing/a", False)])
def test_dispatch_verdict_spawns_nothing_from_this_guard(world, tmp_path, spawns_from_guard, cmd, denied):
    out = dispatch.evaluate_payload_json(_raw(cmd.format(r=world.as_posix()), "Bash", tmp_path))
    assert ("new top-level folder" in json.dumps(out)) is denied, out
    assert spawns_from_guard == []
