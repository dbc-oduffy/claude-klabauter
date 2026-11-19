"""landing_integrity.committed_image_text: the committed image (HEAD + landing blobs) is judged, not the worktree."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.authoring_leaks.tests._git_fixture import git as _git, write as _write
from coordinator_core.ops.dispatch_emit import landing_integrity as li

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

CALLER = "from pkg import b\n\n\ndef f():\n    return b.new_fn()\n"
CALLER_FROM = "from pkg.b import new_fn\n"
B_HEAD = "def old_fn():\n    pass\n"
B_DIRT = B_HEAD + "\ndef new_fn():\n    pass\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/b.py", B_HEAD)
    _write(tmp_path, "pkg/a.py", "x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _reasons(found):
    return [f.reason for f in found]


def test_name_defined_only_in_peer_dirt_is_refused_for_the_committed_image(repo) -> None:
    _write(repo, "pkg/b.py", B_DIRT)
    _write(repo, "pkg/a.py", CALLER_FROM)
    text = li.committed_image_text(repo, {"pkg/a.py": CALLER_FROM})
    found = li.import_closure_findings(repo, ["pkg/a.py"], [], landed_text=text)
    assert _reasons(found) == [li.IMPORT_CLOSURE_UNTRACKED]
    assert found[0].path == "pkg/a.py"


def test_importer_is_judged_on_its_landing_blob_not_the_worktree(repo) -> None:
    _write(repo, "pkg/b.py", B_DIRT)
    _write(repo, "pkg/a.py", CALLER_FROM)
    text = li.committed_image_text(repo, {"pkg/a.py": "x = 2\n"})
    assert li.import_closure_findings(repo, ["pkg/a.py"], [], landed_text=text) == []


def test_definer_landing_with_the_caller_passes(repo) -> None:
    _write(repo, "pkg/b.py", B_DIRT)
    _write(repo, "pkg/a.py", CALLER_FROM)
    text = li.committed_image_text(repo, {"pkg/a.py": CALLER_FROM, "pkg/b.py": B_DIRT.encode()})
    assert li.import_closure_findings(repo, ["pkg/a.py", "pkg/b.py"], [], landed_text=text) == []


def test_landing_blob_wins_over_worktree_and_head(repo) -> None:
    _write(repo, "pkg/a.py", "dirt = 1\n")
    text = li.committed_image_text(repo, {"pkg/a.py": b"landed = 1\n", "pkg/gone.py": None})
    assert text("pkg/a.py") == "landed = 1\n"
    assert text("pkg/gone.py") is None


def test_other_paths_read_head_never_the_worktree(repo) -> None:
    _write(repo, "pkg/b.py", B_DIRT)
    _write(repo, "pkg/new_untracked.py", "x = 1\n")
    text = li.committed_image_text(repo, {})
    assert text("pkg/b.py") == B_HEAD
    assert text("pkg/new_untracked.py") is None


def test_non_utf8_blob_reads_as_absent(repo) -> None:
    assert li.committed_image_text(repo, {"pkg/a.py": b"\xff\xfe"})("pkg/a.py") is None


def test_provider_spawns_nothing(repo, monkeypatch) -> None:
    def boom(*a, **k):
        raise AssertionError("spawn")

    _write(repo, "pkg/b.py", B_DIRT)
    _write(repo, "pkg/a.py", CALLER_FROM)
    monkeypatch.setattr(subprocess, "Popen", boom)
    text = li.committed_image_text(repo, {"pkg/a.py": CALLER_FROM})
    assert li.import_closure_findings(repo, ["pkg/a.py"], [], landed_text=text)
