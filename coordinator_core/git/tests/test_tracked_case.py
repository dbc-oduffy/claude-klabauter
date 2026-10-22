"""tracked_spelling: git's spelling of a declared path, or None."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.tracked_case import tracked_spelling
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd: Path, **kw):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **kw, **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "coordinator" / "bin").mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "coordinator" / "bin" / "blocked.py").write_text("x\n", encoding="utf-8")
    (root / "coordinator" / "bin" / "gone.py").write_text("x\n", encoding="utf-8")
    _git(["add", "coordinator"], root)
    _git(["commit", "-q", "-m", "init"], root)
    (root / "coordinator" / "bin" / "gone.py").unlink()
    (root / "coordinator" / "bin" / "fresh.py").write_text("n\n", encoding="utf-8")
    return root


def _one(root, path):
    return tracked_spelling(root, [path])[path]


def test_exact_tracked(repo):
    assert _one(repo, "coordinator/bin/blocked.py") == "coordinator/bin/blocked.py"


def test_case_variant_leaf(repo):
    assert _one(repo, "coordinator/bin/BLOCKED.py") == "coordinator/bin/blocked.py"


def test_case_variant_directory_component(repo):
    assert _one(repo, "Coordinator/bin/blocked.py") == "coordinator/bin/blocked.py"


def test_tracked_but_deleted_on_disk(repo):
    assert _one(repo, "coordinator/bin/gone.py") == "coordinator/bin/gone.py"
    assert _one(repo, "coordinator/bin/GONE.py") == "coordinator/bin/gone.py"


def test_exact_untracked_present(repo):
    assert _one(repo, "coordinator/bin/fresh.py") == "coordinator/bin/fresh.py"


def test_untracked_present_case_variant_gets_disk_spelling(repo):
    assert _one(repo, "coordinator/bin/FRESH.py") == "coordinator/bin/fresh.py"


def test_absent_everywhere(repo):
    assert _one(repo, "coordinator/bin/tests/test_BLOCKED.py") is None
    assert _one(repo, "nope.py") is None


def test_ambiguous_casefold_is_none(repo):
    blob = _git(["rev-parse", "HEAD:coordinator/bin/blocked.py"], repo).stdout.strip()
    for name in ("amb/Foo.py", "amb/FOO.py"):
        _git(["update-index", "--add", "--cacheinfo", f"100644,{blob},{name}"], repo)
    _git(["commit", "-q", "-m", "amb"], repo)
    assert _one(repo, "amb/foo.py") is None


def test_non_repo_is_disk_only(tmp_path):
    root = tmp_path / "plain"
    (root / "d").mkdir(parents=True)
    (root / "d" / "a.py").write_text("x", encoding="utf-8")
    got = tracked_spelling(root, ["d/a.py", "d/A.py", "d/b.py"])
    assert got == {"d/a.py": "d/a.py", "d/A.py": "d/a.py", "d/b.py": None}


def test_degenerate_paths_do_not_raise(repo):
    assert tracked_spelling(repo, ["", "a//b"]) == {"": None, "a//b": None}
