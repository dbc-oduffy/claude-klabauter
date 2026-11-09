"""land_diff writes the engine stamp after every other payload file and deletion.

The warm pair stays the stamped one for the whole copy only if nothing else lands after the stamp.
"""

from __future__ import annotations

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


def test_stamp_swap_is_the_last_destination_write(tmp_path, monkeypatch):
    root = _dest(tmp_path)
    landed: list = []
    real_swap = diff_commit._swap
    real_converge = diff_commit._converge_unchanged
    real_unlink = Path.unlink

    def _swap(tmp, path):
        landed.append(Path(path).relative_to(root).as_posix())
        real_swap(tmp, path)

    def _converge(*args, **kwargs):
        landed.append("<converge-unchanged>")
        return real_converge(*args, **kwargs)

    def _unlink(self, *args, **kwargs):
        if self.parent == root and self.name == "gone.txt":
            landed.append("gone.txt")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(diff_commit, "_swap", _swap)
    monkeypatch.setattr(diff_commit, "_converge_unchanged", _converge)
    monkeypatch.setattr(Path, "unlink", _unlink)

    writes = _writes(**{_STAMP: "sha:new\n", "a.txt": "v1\n", "b.txt": "v1\n", "same.txt": "v0\n"})
    out = land_diff(root, writes, ["gone.txt"], "m", commit=False)

    assert _STAMP in out.written
    assert landed[-1] == _STAMP, landed
    assert landed.count(_STAMP) == 1
    assert {"a.txt", "b.txt", "gone.txt"} <= set(landed[:-1]), landed

