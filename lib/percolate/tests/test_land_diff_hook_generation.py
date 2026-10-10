"""A reader following the engine-landing protocol never observes a mixed engine across land_diff.

The reader loops: gate (`wait_until_clear`, when `coordinator_core._engine_landing` exists),
read module A's GEN, pause (standing in for lazy-import latency), read module B's GEN. A pair
whose generations differ is a mixed engine.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from percolate import diff_commit  # noqa: E402
from percolate.diff_commit import DestWrite, land_diff  # noqa: E402

pytestmark = [pytest.mark.spawns_process]

_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_STAMP = "coordinator_core/_engine_stamp"
_MODS = ("mod_a.py", "mod_b.py")
_REPLACE_PAUSE_S = 0.03
_LAZY_IMPORT_S = 0.005
_GEN = re.compile(rb"GEN = (\d+)")


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True, **_NO_CONSOLE
    ).stdout


def _engine_root(tmp_path):
    root = tmp_path / "engine"
    (root / "coordinator_core").mkdir(parents=True)
    (root / _STAMP).write_bytes(b"stamp-1\n")
    for name in _MODS:
        (root / name).write_bytes(b"GEN = 1\n")
    _git(root, "init", "-q", "-b", "work/z")
    _git(root, "config", "user.email", "t@local")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _round_writes():
    return [DestWrite(name, b"GEN = 2\n", 0o100644) for name in _MODS] + [
        DestWrite(_STAMP, b"stamp-2\n", 0o100644)
    ]


def _gate():
    try:
        from coordinator_core import _engine_landing
    except ImportError:
        return None
    return _engine_landing.wait_until_clear


def _slow_replaces(monkeypatch, root):
    """Stretch the replace loop so the mid-round window is real; the writer is unmocked."""
    real = os.replace
    mods = {str(root / name) for name in _MODS}

    def paced(src, dst, *a, **kw):
        out = real(src, dst, *a, **kw)
        if str(dst) in mods:
            time.sleep(_REPLACE_PAUSE_S)
        return out

    monkeypatch.setattr(diff_commit.os, "replace", paced)


class _Reader(threading.Thread):
    def __init__(self, root, armed=None):
        super().__init__(daemon=True)
        self.root, self.armed = root, armed
        self.stop = threading.Event()
        self.pairs = []

    def _gen(self, name):
        return int(_GEN.search((self.root / name).read_bytes()).group(1))

    def run(self):
        gate = _gate()
        while not self.stop.is_set():
            counted = self.armed is None or self.armed.is_set()
            if gate is not None:
                gate(self.root)
            a = self._gen(_MODS[0])
            time.sleep(_LAZY_IMPORT_S)
            b = self._gen(_MODS[1])
            if counted:
                self.pairs.append((a, b))


def _mixed(pairs):
    return [p for p in pairs if p[0] != p[1]]


def _run_round(root, reader, body):
    reader.start()
    time.sleep(0.05)
    try:
        body()
    finally:
        time.sleep(0.05)
        reader.stop.set()
        reader.join(5)


def test_landing_round_is_never_read_mixed(tmp_path, monkeypatch):
    root = _engine_root(tmp_path)
    _slow_replaces(monkeypatch, root)
    reader = _Reader(root)
    _run_round(root, reader, lambda: land_diff(root, _round_writes(), [], "round", commit=True))
    assert {(2, 2)} <= set(reader.pairs) and len(reader.pairs) > 10
    assert _mixed(reader.pairs) == []
    assert not (root / ".git" / "coordinator-engine-landing").exists()


def test_restore_after_a_lost_commit_is_never_read_mixed(tmp_path, monkeypatch):
    root = _engine_root(tmp_path)
    _slow_replaces(monkeypatch, root)
    armed = threading.Event()

    def lost_cas(*a, **kw):
        armed.set()
        raise RuntimeError("CAS lost")

    monkeypatch.setattr(diff_commit._gcommit, "commit_paths", lost_cas)
    reader = _Reader(root, armed)

    def body():
        with pytest.raises(RuntimeError, match="CAS lost"):
            land_diff(root, _round_writes(), [], "round", commit=True)

    _run_round(root, reader, body)
    assert reader.pairs
    assert _mixed(reader.pairs) == []
    assert not (root / ".git" / "coordinator-engine-landing").exists()
    assert [(root / n).read_bytes() for n in _MODS] == [b"GEN = 1\n"] * 2
