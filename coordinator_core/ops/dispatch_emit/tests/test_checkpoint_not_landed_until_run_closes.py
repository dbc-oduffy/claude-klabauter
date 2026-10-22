"""A Checkpoint-Not-Landed checkpoint lands its rows for outside readers only after a run close."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import checkpoint_stamp as cs
from coordinator_core.ops.dispatch_emit.tests.test_checkpoint_rows_landed import PLAN, _commit, _git

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BASE = "a" * 40
REVIEW = "b" * 40
OTHER_BASE = "c" * 40


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "seed")
    return tmp_path


def _marked(root, ids=("A1",), base=BASE, review=None):
    trailers = f"Checkpoint-Plan: {PLAN}\nCheckpoint-Base: {base}"
    if review:
        trailers += f"\nCheckpoint-Review-Base: {review}"
    trailers += "\nCheckpoint-Not-Landed: run-open"
    return _commit(root, f"checkpoint(wave 0): {len(ids)} rows — {', '.join(ids)}", trailers)


def _close(root, *bases):
    return _commit(root, "terminal", "\n".join(f"Checkpoint-Run-Closed: {b}" for b in bases))


def _at_head(root, ids=("A1",)):
    return cs.rows_landed_at_head(root, PLAN, frozenset(ids))


def test_marked_checkpoint_is_not_landed_at_head(repo):
    _marked(repo)
    _commit(repo, "later")
    assert _at_head(repo) == frozenset()


def test_later_close_of_its_base_lands_it(repo):
    _marked(repo)
    _close(repo, BASE)
    assert _at_head(repo) == {"A1"}


def test_close_naming_another_base_does_not_count(repo):
    _marked(repo)
    _close(repo, OTHER_BASE)
    assert _at_head(repo) == frozenset()


def test_close_older_than_the_checkpoint_does_not_count(repo):
    _close(repo, BASE)
    _marked(repo)
    assert _at_head(repo) == frozenset()


def test_review_base_close_counts(repo):
    _marked(repo, review=REVIEW)
    _close(repo, REVIEW)
    assert _at_head(repo) == {"A1"}


def test_one_close_line_per_base_each_counts(repo):
    _marked(repo, ids=("A1",), base=BASE)
    _marked(repo, ids=("A2",), base=OTHER_BASE)
    _close(repo, BASE, OTHER_BASE)
    assert _at_head(repo, ("A1", "A2")) == {"A1", "A2"}


def test_legacy_checkpoint_stays_landed(repo):
    _commit(repo, "checkpoint(wave 0): 1 rows — A1", f"Checkpoint-Plan: {PLAN}\nCheckpoint-Base: {BASE}")
    assert _at_head(repo) == {"A1"}


def test_run_internal_walk_ignores_the_marker(repo):
    head = _marked(repo)
    walk = cs.checkpoint_rows_landed(repo, PLAN, head, frozenset({"A1"}))
    assert walk.landed == {head: frozenset({"A1"})}


def test_parse_both_shapes_and_run_closed_bases():
    marked = (
        f"checkpoint(wave 1): 1 rows — C1\n\nCheckpoint-Plan: {PLAN}\n"
        f"Checkpoint-Base: {BASE}\nCheckpoint-Review-Base: {REVIEW}\nCheckpoint-Not-Landed: run-open"
    )
    cp = cs.parse_checkpoint(marked)
    assert (cp.base, cp.review_base, cp.not_landed) == (BASE, REVIEW, True)
    legacy = cs.parse_checkpoint(f"checkpoint(wave 1): 1 rows — C1\n\nCheckpoint-Plan: {PLAN}")
    assert (legacy.base, legacy.review_base, legacy.not_landed) == (None, None, False)
    msg = f"done\n\nCheckpoint-Run-Closed: {BASE}\nCheckpoint-Run-Closed: {REVIEW}\nCheckpoint-Run-Closed: short"
    assert cs.run_closed_bases(msg) == {BASE, REVIEW}
    assert cs.run_closed_bases("nothing") == frozenset()
