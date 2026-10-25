"""dispatch.land_run import-closure over a run-owned merge: an import of a module the merge itself
carries is landed, not uncommitted; outside a merge, and under a foreign merge, nothing widens."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.git import merge_owner
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

B = "src/b_mod.py"
C = "src/c_conf.py"
NEW = "src/a_new.py"
IMPORTER = "from src.b_mod import path_tokens\n"


def _merging(root: Path) -> tuple:
    """theirs adds path_tokens to B and a module importing it (both auto-merged); it conflicts with
    ours on C. A declared path outside the merge refuses merge_foreign, so NEW rides the merge."""
    prod._write(root, "src/__init__.py", "")
    prod._write(root, B, "x = 1\n")
    prod._write(root, C, "x = 0\n")
    prod._git(["add", "."], root)
    prod._git(["commit", "-q", "-m", "base"], root)
    prod._git(["checkout", "-q", "-b", "theirs"], root)
    prod._write(root, B, "x = 1\n\n\ndef path_tokens():\n    return []\n")
    prod._write(root, C, "x = 2\n")
    prod._write(root, NEW, IMPORTER)
    prod._git(["add", "."], root)
    prod._git(["commit", "-q", "-m", "theirs"], root)
    prod._git(["checkout", "-q", "-"], root)
    prod._write(root, C, "x = 1\n")
    prod._git(["commit", "-q", "-am", "ours"], root)
    ours = prod._git(["rev-parse", "HEAD"], root).strip()
    theirs = prod._git(["rev-parse", "theirs"], root).strip()
    merge = prod.subprocess.run(
        ["git", "merge", "theirs"], cwd=str(root), capture_output=True, text=True,
        **prod.no_console_creationflags(),
    )
    assert merge.returncode != 0
    return ours, theirs


def _resolve_and_author(root: Path) -> None:
    prod._author(root, C, "x = 3\n")
    prod._git(["add", C], root)
    prod._author(root, NEW, IMPORTER)
    prod._declare(root, NEW, C)
    prod._record_dir(root)


def test_a_module_the_merge_carries_is_landed_not_uncommitted(repo):
    ours, theirs = _merging(repo)
    _resolve_and_author(repo)
    merge_owner.record(repo, prod.RUN, theirs, ours)

    out = prod._land(repo)

    assert out["committed"] is True, out
    merge_sha = prod._git(["rev-list", "--parents", "-n1", "HEAD~1"], repo).split()
    assert merge_sha[1:] == [ours, theirs]
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert "path_tokens" in prod._git(["show", f"{merge_sha[0]}:{B}"], repo)
    assert prod._git(["show", f"{merge_sha[0]}:{NEW}"], repo) == IMPORTER


def test_an_uncommitted_import_outside_a_merge_still_refuses(repo):
    prod._write(repo, "src/__init__.py", "")
    prod._write(repo, B, "x = 1\n")
    prod._git(["add", "."], repo)
    prod._git(["commit", "-q", "-m", "base"], repo)
    head = prod._git(["rev-parse", "HEAD"], repo).strip()
    prod._write(repo, B, "x = 1\n\n\ndef path_tokens():\n    return []\n")
    prod._author(repo, NEW, IMPORTER)
    prod._declare(repo, NEW)
    prod._record_dir(repo)

    out = prod._land(repo)

    assert out.get("refused") == "import-closure-untracked", out
    assert prod._git(["rev-parse", "HEAD"], repo).strip() == head


def test_a_foreign_merge_widens_nothing(repo):
    ours, theirs = _merging(repo)
    _resolve_and_author(repo)
    merge_owner.record(repo, "ask-20200101T000009-other0", theirs, ours)

    out = prod._land(repo)

    assert out.get("committed") is not True, out
    assert out.get("refused") == "import-closure-untracked", out
    assert (repo / ".git" / "MERGE_HEAD").exists()
    assert prod._git(["rev-parse", "HEAD"], repo).strip() == ours
