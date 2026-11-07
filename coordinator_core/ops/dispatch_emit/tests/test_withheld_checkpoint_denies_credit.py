"""A commit whose `Checkpoint-Withheld:` trailer names a chunk's declared path earns that chunk no credit."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.dispatch_emit import terminal_commit_checkpoints
from coordinator_core.ops.dispatch_emit.plan_row_coding import outside_landings
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PLAN = "docs/plans/p.md"
_C1 = "src/c1.py"
_C2 = "src/c2.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _commit(repo: Path, files: dict, message: str) -> str:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _checkpoint_message(withheld: tuple = ()) -> str:
    trailers = "".join(f"Checkpoint-Withheld: {p}\n" for p in withheld)
    return f"checkpoint(wave 0): 2 rows — C1, C2\n\nCheckpoint-Plan: {_PLAN}\nCheckpoint-Base: abc1234\n{trailers}"


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _commit(r, {_C1: "0\n", _C2: "0\n"}, "seed")
    _commit(r, {_PLAN: "plan\n"}, "plan")
    return r


def _chunks():
    return [SimpleNamespace(id="C1", paths=[_C1]), SimpleNamespace(id="C2", paths=[_C2])]


def _landed_checkpoint(repo: Path, withheld: tuple) -> tuple:
    c = _commit(repo, {_C1: "1\n", _C2: "1\n"}, _checkpoint_message(withheld))
    terminal = _commit(repo, {"t.txt": "t\n"}, "terminal")
    return c, terminal


def test_attribution_credits_every_chunk_when_nothing_was_withheld(repo):
    c, terminal = _landed_checkpoint(repo, ())

    out = terminal_commit_checkpoints._checkpoint_attribution(repo, terminal, _PLAN, _chunks())

    assert out == {"C1": c, "C2": c}


def test_attribution_denies_the_chunk_whose_declared_path_was_withheld(repo):
    c, terminal = _landed_checkpoint(repo, (_C1,))

    out = terminal_commit_checkpoints._checkpoint_attribution(repo, terminal, _PLAN, _chunks())

    assert out == {"C2": c}


def test_close_background_attribution_denies_the_withheld_chunk(repo):
    c, terminal = _landed_checkpoint(repo, (_C1,))
    ctx = cb.JobContext(repo, repo / ".git", "run", (_PLAN,), None, terminal)

    out = cb._checkpoint_of(ctx, _PLAN, terminal, _chunks())

    assert out == {"C2": c}


def test_close_background_attribution_credits_when_nothing_was_withheld(repo):
    c, terminal = _landed_checkpoint(repo, ())
    ctx = cb.JobContext(repo, repo / ".git", "run", (_PLAN,), None, terminal)

    assert cb._checkpoint_of(ctx, _PLAN, terminal, _chunks()) == {"C1": c, "C2": c}


def test_outside_landing_is_no_hit_for_a_chunk_whose_path_the_commit_withheld(repo):
    withheld = _commit(repo, {_C1: "1\n", _C2: "1\n"}, "outside work\n\nCheckpoint-Withheld: " + _C1 + "\n")
    terminal = _commit(repo, {"t.txt": "t\n"}, "terminal")

    out = outside_landings(repo, terminal, _PLAN, _chunks())

    assert out == {"C2": [withheld]}


def test_outside_landing_credits_both_chunks_when_nothing_was_withheld(repo):
    landed = _commit(repo, {_C1: "1\n", _C2: "1\n"}, "outside work")
    terminal = _commit(repo, {"t.txt": "t\n"}, "terminal")

    assert outside_landings(repo, terminal, _PLAN, _chunks()) == {"C1": [landed], "C2": [landed]}


def test_review_only_hit_skips_a_commit_that_withheld_the_row_path(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, {_C1: "1\n"}, "work\n\nCheckpoint-Withheld: " + _C1 + "\n")
    tip = _commit(repo, {"t.txt": "t\n"}, "terminal")
    ctx = cb.JobContext(repo, repo / ".git", "run", (_PLAN,), None, tip)
    review_only = {"run_base": base, "open_rows": ["C1"], "rows": [{"id": "C1", "paths": [_C1]}]}

    hits, no_delta = cb._review_only_hits(ctx, tip, review_only, {"C1"})

    assert hits == {} and no_delta == ["C1"]
