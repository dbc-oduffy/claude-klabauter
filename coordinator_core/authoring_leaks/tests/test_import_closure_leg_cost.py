"""import_closure reads each declared path once per detection, not once per imported name.

Base (HEAD before the change), the `authoring_leaks.import_closure._read_text` leg: every call is a
land_deadline leg start, and an importer of N uncommitted names from one module re-read that module
N times (6 reads of pkg/b.py for 5 names here), a gate target was read again after the gate pass
(2 reads). After: 1 read each.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.authoring_leaks import HeadView, import_closure
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

NAMES = ["n1", "n2", "n3", "n4", "n5"]


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, **no_console_creationflags(),
    )


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/b.py", "def old_fn():\n    pass\n")
    _write(tmp_path, "pkg/a.py", "x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _write(tmp_path, "pkg/b.py", "def old_fn():\n    pass\n" + "".join(f"\ndef {n}():\n    pass\n" for n in NAMES))
    _write(tmp_path, "pkg/a.py", f"from pkg.b import {', '.join(NAMES)}\n")
    return tmp_path


def _detect(root: Path, gate: list, monkeypatch):
    reads: list = []
    real = import_closure._read_text

    def spy(path):
        reads.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(import_closure, "_read_text", spy)
    found = import_closure.detect(root, gate, HeadView(root, gate))
    return found, reads


def test_uncommitted_target_is_read_once_for_all_its_names(repo, monkeypatch):
    found, reads = _detect(repo, ["pkg/a.py"], monkeypatch)

    assert len(found) == len(NAMES)
    assert all("is uncommitted in the worktree" in f.detail for f in found)
    assert reads.count("b.py") == 1 and reads.count("a.py") == 1


def test_gate_target_is_not_read_again_after_the_gate_pass(repo, monkeypatch):
    found, reads = _detect(repo, ["pkg/a.py", "pkg/b.py"], monkeypatch)

    assert found == []
    assert reads.count("b.py") == 1 and reads.count("a.py") == 1
