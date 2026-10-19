"""A ledger resume whose declared product landed in the run's own post-halt checkpoints, worktree clean, is not unchanged since the halt."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import UNCHANGED_SINCE_HALT_REASON
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# Pins the helper only; it passes on the pre-fix code, so it is not the R1 reproduction.
# The terminal-commit entrypoint seam is unexercised here.

MET = {"items": [{"id": "R1", "status": "met", "wired": True}]}


def _git(root, *a):
    out = subprocess.run(
        ["git", *a], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "prod.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("y = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "halt")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"id": "C1", "writes": ["prod.py"]}]}), encoding="utf-8")
    return tmp_path, manifest, _git(tmp_path, "rev-parse", "HEAD")


def _resume(halt, **extra):
    return {"run_id": "old", "halt_sha": halt, "blocking": ["R1"], "unlanded": ["C1"], **extra}


def _checkpoint(root, name, text):
    (root / name).write_text(text, encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-q", "-m", f"checkpoint {name}")


def test_declared_path_committed_by_post_halt_checkpoint_with_clean_tree_passes(repo):
    root, manifest, halt = repo
    _checkpoint(root, "prod.py", "x = 2\n")
    assert _git(root, "status", "--porcelain", "--untracked-files=no") == ""
    assert tc._unchanged_since_halt_failures(root, _resume(halt), MET, manifest) == []


def test_post_halt_checkpoint_of_an_undeclared_path_still_refuses(repo):
    root, manifest, halt = repo
    _checkpoint(root, "other.py", "y = 2\n")
    got = tc._unchanged_since_halt_failures(root, _resume(halt), MET, manifest)
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


@pytest.fixture
def halted_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "prod.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("y = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path, "commit", "-q", "-m", "warp: record halted old run artifacts",
        "-m", "Halt-Plan: docs/plans/p.md\nHalted-By: ledger-check",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"id": "C1", "writes": ["prod.py"]}]}), encoding="utf-8")
    return tmp_path, manifest


def _null_resume(**extra):
    return _resume(None, plan="docs/plans/p.md", **extra)


def test_null_halt_sha_resolves_halt_commit_and_post_halt_checkpoint_passes(halted_repo):
    root, manifest = halted_repo
    _checkpoint(root, "prod.py", "x = 2\n")
    assert tc._unchanged_since_halt_failures(root, _null_resume(), MET, manifest) == []


def test_null_halt_sha_without_change_since_halt_still_refuses(halted_repo):
    root, manifest = halted_repo
    got = tc._unchanged_since_halt_failures(root, _null_resume(), MET, manifest)
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def test_null_halt_sha_with_unresolvable_run_refuses(halted_repo):
    root, manifest = halted_repo
    _checkpoint(root, "prod.py", "x = 2\n")
    resume = {**_null_resume(), "run_id": "never-halted"}
    got = tc._unchanged_since_halt_failures(root, resume, MET, manifest)
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def test_no_change_since_halt_still_refuses(repo):
    root, manifest, halt = repo
    got = tc._unchanged_since_halt_failures(root, _resume(halt), MET, manifest)
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]
