"""Falsifier: worktree-only names named by a landing module are refused over the real committed-tree reader.

Two plants over a real git repo: the 2eefb20bcc shape (lazy import plus an attribute call on a function
only the worktree has) and the example-game-repo 5e9d02f115 shape (a from-import constant, non-coordinator_core
package).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.commit_source import post_commit_reader
from coordinator_core.ops.ceremony.landed_names import uncommitted_name_refusals
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "pkg").mkdir(parents=True)
    _git(["init", "-q"], r)
    _git(["config", "user.email", "t@t.example"], r)
    _git(["config", "user.name", "t"], r)
    (r / "pkg/__init__.py").write_text("", encoding="utf-8")
    (r / "pkg/idx.py").write_text("def add_run(): ...\n", encoding="utf-8")
    (r / "pkg/consts.py").write_text("OLD = 1\n", encoding="utf-8")
    _git(["add", "-A"], r)
    _git(["commit", "-q", "-m", "seed"], r)
    # Worktree-only additions the landing modules depend on.
    (r / "pkg/idx.py").write_text("def add_run(): ...\ndef record_claim(): ...\n", encoding="utf-8")
    (r / "pkg/consts.py").write_text("OLD = 1\nARM_BYPASS = 2\n", encoding="utf-8")
    (r / "pkg/caller.py").write_text(
        "def f():\n    from pkg import idx\n    idx.record_claim()\n", encoding="utf-8"
    )
    (r / "pkg/user.py").write_text("from pkg.consts import ARM_BYPASS\n", encoding="utf-8")
    return r


def _refusals(repo: Path, paths):
    reader = post_commit_reader(repo, repo / ".git", paths=paths, deleted_paths=())
    return uncommitted_name_refusals(repo, paths, reader)


def test_attribute_call_plant_refused(repo):
    out = _refusals(repo, ["pkg/caller.py"])
    assert out == ["pkg/caller.py:3: names pkg.idx.record_claim, which exists only uncommitted"]


def test_from_import_constant_plant_refused(repo):
    out = _refusals(repo, ["pkg/user.py"])
    assert out == ["pkg/user.py:1: names pkg.consts.ARM_BYPASS, which exists only uncommitted"]


def test_landing_the_dependencies_with_them_is_clean(repo):
    paths = ["pkg/caller.py", "pkg/idx.py", "pkg/user.py", "pkg/consts.py"]
    assert _refusals(repo, paths) == []
