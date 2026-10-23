"""close-commit stamps ``Checkpoint-Run-Closed`` only when the run's terminal commit was a landing
that does not already name the base."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.ceremony.tests.test_close_background_land_run_legs import (
    PLAN, RUN, _commit, _git, _write,
)
from coordinator_core.ops.dispatch_emit.checkpoint_stamp import run_closed_bases
from coordinator_core.ops.tests.test_deliverable_cascade_kinds import _init_repo

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BASE = "a" * 40


def _close(root: Path, terminal_message: str) -> str:
    """Commit ``terminal_message`` as the run's terminal commit, run close-commit, return its message."""
    _init_repo(root)
    _write(root, "src/a.py", "landed\n")
    terminal = _commit(root, terminal_message, "src/a.py")
    _write(root, PLAN, "# closed\n")
    ctx = cb.JobContext(
        root, root / ".git", RUN, (), None, terminal_sha=terminal,
        record={"review_run_base": BASE}, written=[PLAN],
    )
    assert cb._close_commit(ctx).status == "ran"
    return _git(root, "log", "-1", "--format=%B")


def test_close_after_a_landing_without_the_trailer_stamps_it(tmp_path):
    message = _close(tmp_path / "r", "land\n\nLands-Chunks: C1")
    assert message.startswith("close: ")
    assert run_closed_bases(message) == {BASE}


def test_close_after_an_inline_review_landing_stamps_it(tmp_path):
    assert run_closed_bases(_close(tmp_path / "r", "land\n\nInline-Review: ok")) == {BASE}


def test_close_after_a_halt_does_not_stamp(tmp_path):
    message = _close(tmp_path / "r", "warp: record halted ask-1 run artifacts\n\nLands-Chunks: C1")
    assert run_closed_bases(message) == frozenset()


def test_close_after_an_untyped_commit_does_not_stamp(tmp_path):
    assert run_closed_bases(_close(tmp_path / "r", "wip")) == frozenset()


def test_landing_that_already_closes_the_base_is_not_repeated(tmp_path):
    message = _close(tmp_path / "r", f"land\n\nLands-Chunks: C1\nCheckpoint-Run-Closed: {BASE}")
    assert message.count("Checkpoint-Run-Closed") == 0
    assert run_closed_bases(message) == frozenset()
