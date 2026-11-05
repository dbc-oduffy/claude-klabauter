"""import_closure.detect: a landed file reading `m.name` where only a withheld (uncommitted) `m` defines `name`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.authoring_leaks import HeadView, import_closure
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


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
    _write(tmp_path, "pk/__init__.py", "")
    _write(tmp_path, "pk/m.py", "def old():\n    pass\n")
    _write(tmp_path, "pk/u.py", "def run():\n    return 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _write(tmp_path, "pk/m.py", "def old():\n    pass\n\n\ndef note_ids():\n    return []\n")
    return tmp_path


def _detect(root, gate):
    return import_closure.detect(root, gate, HeadView(root, import_closure.candidates(gate)))


LOCAL = "def run():\n    from pk import m\n\n    return m.note_ids()\n"
TOP = "from pk import m\n\n\ndef run():\n    return m.note_ids()\n"


@pytest.mark.parametrize("text", [LOCAL, TOP], ids=["function-local-import", "top-level-import"])
def test_landed_read_of_a_name_only_the_withheld_file_defines_is_found(repo, text):
    _write(repo, "pk/u.py", text)
    found = _detect(repo, ["pk/u.py"])
    assert len(found) == 1, found
    assert found[0].path == "pk/u.py"
    assert "note_ids" in found[0].detail and "pk/m.py" in found[0].detail


@pytest.mark.parametrize("text", [LOCAL, TOP], ids=["function-local-import", "top-level-import"])
def test_landing_both_files_is_no_finding(repo, text):
    _write(repo, "pk/u.py", text)
    assert _detect(repo, ["pk/u.py", "pk/m.py"]) == []


def test_read_of_a_name_the_withheld_file_does_not_define_is_no_finding(repo):
    _write(repo, "pk/u.py", LOCAL.replace("note_ids", "nowhere"))
    assert _detect(repo, ["pk/u.py"]) == []
