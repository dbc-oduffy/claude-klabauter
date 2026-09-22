"""The executor fence admits the row's own `surface:`, not just its `writes:`.

The fence and the commit pathspec answer different questions. A row whose
target is gitignored drops it from `writes:` -- correct, because a gitignored
path is never a committable write -- and that drop used to fence the executor
out of the one file the row's body told it to write. Three rows in one session
stopped there and reported BLOCKED, each correctly: a K-016 append to a
gitignored kill ledger, and two memo rows whose drafts land under a gitignored
outbox.
"""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import _fenced_paths


def test_the_surface_joins_the_fence():
    fenced = _fenced_paths(
        ["engine/thing.py", "reports/R.md"], [], ".coordinator-local/kill-ledger.md"
    )
    assert ".coordinator-local/kill-ledger.md" in fenced, (
        "a row whose only target is gitignored is fenced out of it, so its "
        "executor cannot discharge the row at all"
    )


def test_a_surface_already_declared_is_not_repeated():
    fenced = _fenced_paths(["engine/thing.py"], [], "engine/thing.py")
    assert fenced.count("engine/thing.py") == 1, (
        "the rendered fence lists the same path twice"
    )


def test_prefixes_and_writes_both_survive():
    fenced = _fenced_paths(["a.py"], ["state/audits/"], "engine/")
    assert fenced[:2] == ["a.py", "state/audits/"], (
        "the surface must be appended, never reorder or displace what the "
        "row declared"
    )


def test_an_empty_surface_adds_nothing():
    assert _fenced_paths(["a.py"], [], "") == ["a.py"]
