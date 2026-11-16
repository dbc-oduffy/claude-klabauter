"""land_diff stages the engine stamp after every other payload file and swaps its unit last.

The warm pair stays the stamped one for the whole copy only if nothing else lands after the stamp.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from percolate import diff_commit  # noqa: E402
from percolate.diff_commit import DestWrite, land_diff  # noqa: E402

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_STAMP = diff_commit._STAMP


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True, **_NO_CONSOLE
    ).stdout


def _dest(tmp_path):
    root = tmp_path / "dest"
    for rel, text in {_STAMP: "sha:old\n", "a.txt": "v0\n", "b.txt": "v0\n", "gone.txt": "v0\n", "same.txt": "v0\n"}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(text.encode())
    _git(root, "init", "-q", "-b", "work/z")
    _git(root, "config", "user.email", "t@local")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _writes(**changed):
    return [DestWrite(rel, text.encode(), 0o100644) for rel, text in changed.items()]


def test_stamp_is_the_last_staged_write_and_its_unit_the_last_swapped(tmp_path, monkeypatch):
    root = _dest(tmp_path)
    staged_order: list = []
    swapped: list = []
    staged_mod = diff_commit._staged
    real_write, real_replace, real_rename = staged_mod.write_entry, os.replace, os.rename

    def write(path, data, mode):
        staged_order.append(Path(path).name)
        real_write(path, data, mode)

    def _note(dst):
        if Path(dst).parent == root:
            swapped.append(Path(dst).name)

    def replace(src, dst):
        _note(dst)
        return real_replace(src, dst)

    def rename(src, dst, *a, **k):
        _note(dst)
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(staged_mod, "write_entry", write)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "rename", rename)

    writes = _writes(**{_STAMP: "sha:new\n", "a.txt": "v1\n", "b.txt": "v1\n", "same.txt": "v0\n"})
    out = land_diff(root, writes, ["gone.txt"], "m", commit=False)

    assert _STAMP in out.written
    assert staged_order[-1] == "_engine_stamp" and staged_order.count("_engine_stamp") == 1
    assert {"a.txt", "b.txt"} <= set(staged_order[:-1])
    assert swapped[-1] == "coordinator_core", swapped
    assert {"a.txt", "b.txt"} <= set(swapped[:-1]) and not (root / "gone.txt").exists()
