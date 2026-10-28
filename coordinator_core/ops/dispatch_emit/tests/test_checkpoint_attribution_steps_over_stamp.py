"""`_checkpoint_attribution` steps over a checkpoint's own `Checkpoint-Stamp:` commit and keeps
walking, while any other non-checkpoint commit still ends the walk."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.ops.dispatch_emit import checkpoint_stamp, terminal_commit
from coordinator_core.ops.dispatch_emit import terminal_commit_checkpoints
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PLAN = "docs/plans/p.md"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit(repo: Path, rel: str, text: str, *messages: str) -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _git(repo, "add", "--", rel)
    args = ["commit", "-q"]
    for m in messages:
        args += ["-m", m]
    _git(repo, *args)
    return _git(repo, "rev-parse", "HEAD")


def _checkpoint(repo: Path, rel: str, row: str) -> str:
    return _commit(
        repo, rel, f"{row}\n",
        f"checkpoint(wave 0): 1 rows — {row}", f"Checkpoint-Plan: {_PLAN}\nCheckpoint-Base: abc1234",
    )


def _stamp(repo: Path, n: int, of: str) -> str:
    return _commit(
        repo, _PLAN, f"plan {n}\n",
        f"mark 1 rows coded ({of[:7]})", f"Checkpoint-Stamp: {of}",
    )


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.example")
    _git(r, "config", "user.name", "t")
    _commit(r, _PLAN, "plan 0\n", "add plan")
    return r


def _chunks():
    return [
        SimpleNamespace(id="C1", paths=["docs/x/c1.txt"]),
        SimpleNamespace(id="C2", paths=["docs/x/c2.txt"]),
    ]


def test_walk_attributes_a_checkpoint_behind_a_stamp_commit(repo):
    c1 = _checkpoint(repo, "docs/x/c1.txt", "C1")
    _stamp(repo, 1, c1)
    c2 = _checkpoint(repo, "docs/x/c2.txt", "C2")
    terminal = _commit(repo, "docs/x/t.txt", "t\n", "terminal")

    out = terminal_commit_checkpoints._checkpoint_attribution(repo, terminal, _PLAN, _chunks())

    assert out == {"C1": c1, "C2": c2}


def test_walk_attributes_through_consecutive_stamps(repo):
    c1 = _checkpoint(repo, "docs/x/c1.txt", "C1")
    s1 = _stamp(repo, 1, c1)
    c2 = _checkpoint(repo, "docs/x/c2.txt", "C2")
    _stamp(repo, 2, c2)
    terminal = _commit(repo, "docs/x/t.txt", "t\n", "terminal")

    out = terminal_commit_checkpoints._checkpoint_attribution(repo, terminal, _PLAN, _chunks())

    assert out == {"C1": c1, "C2": c2}
    assert s1 not in out.values()


def test_a_plain_commit_between_checkpoints_still_ends_the_walk(repo):
    _checkpoint(repo, "docs/x/c1.txt", "C1")
    _commit(repo, "docs/x/peer.txt", "p\n", "peer work")
    c2 = _checkpoint(repo, "docs/x/c2.txt", "C2")
    terminal = _commit(repo, "docs/x/t.txt", "t\n", "terminal")

    out = terminal_commit_checkpoints._checkpoint_attribution(repo, terminal, _PLAN, _chunks())

    assert out == {"C2": c2}


def test_terminal_reuses_the_shared_regexes():
    assert terminal_commit.CHECKPOINT_SUBJECT is checkpoint_stamp.CHECKPOINT_SUBJECT
    assert terminal_commit.CHECKPOINT_PLAN_LINE is checkpoint_stamp.CHECKPOINT_PLAN_LINE
    assert not hasattr(terminal_commit, "_CHECKPOINT_SUBJECT")
