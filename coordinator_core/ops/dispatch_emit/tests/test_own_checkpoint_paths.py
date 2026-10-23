"""Pins ``checkpoint_landed_paths``: which paths a run's own same-plan checkpoints landed."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.own_checkpoint_paths import checkpoint_landed_paths

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PLAN = "docs/plans/p.md"
OTHER = "docs/plans/other.md"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True,
        creationflags=_NO_WINDOW,
    ).stdout.strip()


def _commit(root: Path, subject: str, files: dict, trailers: str = "") -> str:
    for name, text in files.items():
        f = root / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    msg = subject + ("\n\n" + trailers if trailers else "")
    _git(root, "commit", "-qm", msg)
    return _git(root, "rev-parse", "HEAD")


def _cp(root: Path, plan: str, files: dict, extra: str = "") -> str:
    return _commit(root, "checkpoint(wave 0): 1 row — C1", files, f"Checkpoint-Plan: {plan}\n{extra}".strip())


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    _commit(tmp_path, "base", {"README.md": "x"})
    return tmp_path


def test_own_checkpoint_with_peer_commit_on_top(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _cp(repo, PLAN, {"a.py": "1"})
    _commit(repo, "peer", {"b.py": "2"})
    assert checkpoint_landed_paths(repo, PLAN, base, ["a.py", "b.py"]) == {"a.py"}


def test_other_plan_checkpoint_lands_nothing(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _cp(repo, OTHER, {"a.py": "1"})
    assert checkpoint_landed_paths(repo, PLAN, base, ["a.py"]) == frozenset()


def test_non_checkpoint_change_lands_nothing(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "plain", {"a.py": "1"})
    assert checkpoint_landed_paths(repo, PLAN, base, ["a.py"]) == frozenset()


def test_walk_stops_at_run_base(repo: Path) -> None:
    _cp(repo, PLAN, {"old.py": "1"})
    base = _git(repo, "rev-parse", "HEAD")
    _cp(repo, PLAN, {"new.py": "1"})
    assert checkpoint_landed_paths(repo, PLAN, base[:12], ["old.py", "new.py"]) == {"new.py"}


def test_no_run_base_peer_on_top_stops(repo: Path) -> None:
    _cp(repo, PLAN, {"a.py": "1"})
    _commit(repo, "peer", {"b.py": "2"})
    assert checkpoint_landed_paths(repo, PLAN, None, ["a.py"]) == frozenset()


def test_no_run_base_steps_over_stamp_and_counts_checkpoint(repo: Path) -> None:
    _cp(repo, PLAN, {"a.py": "1"})
    sha = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "stamp", {"stamp.txt": "s"}, f"Checkpoint-Stamp: {sha}")
    assert checkpoint_landed_paths(repo, PLAN, None, ["a.py"]) == {"a.py"}


def test_not_landed_checkpoint_lands_nothing(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _cp(repo, PLAN, {"a.py": "1"}, "Checkpoint-Not-Landed: run-open")
    assert checkpoint_landed_paths(repo, PLAN, base, ["a.py"]) == frozenset()


def test_empty_inputs(repo: Path) -> None:
    _cp(repo, PLAN, {"a.py": "1"})
    assert checkpoint_landed_paths(repo, None, None, ["a.py"]) == frozenset()
    assert checkpoint_landed_paths(repo, PLAN, None, []) == frozenset()


def test_zero_spawns(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _cp(repo, PLAN, {"a.py": "1"})

    def boom(*a, **k):
        raise AssertionError("spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert checkpoint_landed_paths(repo, PLAN, base, ["a.py"]) == {"a.py"}
