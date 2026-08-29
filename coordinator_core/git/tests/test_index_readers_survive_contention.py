"""Both index readers, and the single ladder underneath them.

DEMONSTRATED-RED: measured bare against the retried sibling under identical
load, in one window with identical attempt counts --
`git_state.read_index` 11 failures in 12,727 attempts, `git_index.scoped_
status` 0 in 12,727. After `86e9dda3c`: 0 and 0 across ~12.5k, and 0 and 0
across 18,529 on an independent heavier re-run whose 7,680 writer `os.replace`
collisions establish the contention was genuinely present. Recorded in
`state/audits/2026-08-27-commit-paths-job-object-spawn-and-process-time.md`;
probe at `state/audits/2026-08-28-index-reader-contention-control.py`.

WHY THIS FILE EXISTS SEPARATELY FROM `test_replace_contention_is_survived.py`.
That file covers the WRITE sites and imports only `git_objects`, so it asserts
nothing about either reader -- which is exactly how the first version of the
fix shipped covering `git_index`'s reader and leaving `git_state`'s bare while
its own suite stayed green. Until this file, the reader legs rested on a
measurement quoted in a commit message, and a measurement is not a regression
guard: revert the ladder and nothing goes red.

THE STRUCTURAL LEG IS THE POINT. Individual retry tests would both pass again
on a per-module copy of the ladder, which is the shape that produced the gap in
the first place. `TestExactlyOneLadder` is what makes the copy fail.

A read needs no correctness argument to retry -- it takes no lock, moves no ref
and writes nothing, so it cannot duplicate work or lose a race. That is what
separates these legs from the write side, where each site's retry had to earn
itself, and it is why one shared ladder is right here and wrong there.
"""
from __future__ import annotations

import inspect
import pathlib
import struct

import pytest

from coordinator_core.git import git_index, git_objects, git_state
from coordinator_core.git.git_state import IndexParseError

#: A valid, empty v2 index: `DIRC`, version 2, zero entries, 20-byte trailer.
#: Zero spawns -- these legs assert OUR retry behaviour, never real git's, so a
#: `git init` would buy nothing and cost a process on a box where process
#: creation IS the cost. Empty is sufficient: every assertion here is about
#: reaching the parse, not about what the parse finds.
_EMPTY_INDEX = b"DIRC" + struct.pack(">II", 2, 0) + b"\x00" * 20

#: Injected-failure count for the "transient, then recovers" legs below,
#: derived from the live ladder rather than hardcoded -- Review: code-reviewer
#: finding 3 (a0f120ca85333568d) -- a hardcoded failure count that happens to
#: sit under the ladder's length today goes stale silently if the ladder is
#: ever shortened; deriving it keeps the relationship explicit and the count
#: fewer than the ladder's max, so headroom is never exhausted by accident.
_INJECTED_FAILURES = len(git_objects._TRANSIENT_READ_RETRY_DELAYS_S) - 1
#: The call that must succeed: one real attempt, then a retry per delay
#: consumed by the injected failures above.
_ATTEMPTS_TO_SUCCEED = _INJECTED_FAILURES + 1


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    gitdir = tmp_path / ".git"
    gitdir.mkdir(parents=True)
    (gitdir / "index").write_bytes(_EMPTY_INDEX)
    return tmp_path


def _flaky(monkeypatch, attr: str, failures: int, exc: OSError):
    """Make `Path.<attr>` raise `exc` for the first `failures` calls AGAINST
    THE INDEX FILE ONLY, then behave normally.

    Scoped to the index by name because `resolve_git_dir` reads and stats other
    paths on the way in; a blanket patch would fail those instead and the test
    would pass for the wrong reason.
    """
    real = getattr(pathlib.Path, attr)
    calls = {"n": 0}

    def flaky(self, *a, **kw):
        if self.name == "index":
            calls["n"] += 1
            if calls["n"] <= failures:
                raise exc
        return real(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, attr, flaky)
    return calls


class TestGitStateReadIndex:
    """The reader the fix originally missed. It is not a bystander: it builds
    the commit path's own context, and it is the FIRST read `diverging_paths`
    issues on its `context is None` arm -- so its `IndexParseError` was what
    collapsed the staged-content guard to `[]`, i.e. to "nothing diverged"."""

    def test_a_transient_read_is_retried_rather_than_becoming_a_parse_error(
        self, repo, monkeypatch
    ):
        calls = _flaky(
            monkeypatch,
            "read_bytes",
            _INJECTED_FAILURES,
            PermissionError(5, "Access is denied"),
        )

        assert git_state.read_index(repo, fresh=True) == {}
        assert calls["n"] == _ATTEMPTS_TO_SUCCEED

    def test_the_stat_beside_the_read_is_retried_too(self, repo, monkeypatch):
        # Two reads of the same file in one call, and only the first was ever
        # the reported symptom -- an unretried `stat` here would surface as a
        # raw `OSError`, not even as the `IndexParseError` the contract names.
        calls = _flaky(
            monkeypatch,
            "stat",
            _INJECTED_FAILURES,
            PermissionError(5, "Access is denied"),
        )

        assert git_state.read_index(repo, fresh=True) == {}
        # Review: code-reviewer finding 2 (a0f120ca85333568d) -- `read_index`
        # calls `index_path.stat` exactly once, so the count is deterministic;
        # tightened from `>=` to match the sibling `read_bytes` assertion.
        assert calls["n"] == _ATTEMPTS_TO_SUCCEED

    def test_a_persistent_failure_still_raises_indexparseerror(
        self, repo, monkeypatch
    ):
        # The ladder must pause a transient, never convert a real failure into
        # a silent empty snapshot: `{}` here would read as "nothing staged".
        _flaky(monkeypatch, "read_bytes", 999, PermissionError(5, "Access is denied"))

        with pytest.raises(IndexParseError):
            git_state.read_index(repo, fresh=True)

    def test_an_absent_index_is_not_retried_and_is_not_an_error(
        self, repo, monkeypatch
    ):
        # An unborn repo is a real, settled state. Retrying it would wait for
        # something nobody is about to write -- occupancy cost on a box where
        # the load is us.
        (repo / ".git" / "index").unlink()
        calls = _flaky(monkeypatch, "read_bytes", 0, PermissionError())

        snapshot = git_state.read_index(repo, fresh=True)

        assert snapshot == {}
        assert snapshot.stat_identity is None
        assert calls["n"] <= 1


class TestReadIndexStatIdentity:
    """The compare-and-swap re-observation's own read. Its whole job is to
    prove THIS call touched the filesystem just now, so a transient here does
    not degrade an answer -- it destroys the only property the caller wants."""

    def test_a_transient_stat_is_retried(self, repo, monkeypatch):
        calls = _flaky(
            monkeypatch,
            "stat",
            _INJECTED_FAILURES,
            PermissionError(5, "Access is denied"),
        )

        assert git_state.read_index_stat_identity(repo) is not None
        assert calls["n"] == _ATTEMPTS_TO_SUCCEED

    def test_an_absent_index_returns_none_without_retrying(self, repo, monkeypatch):
        (repo / ".git" / "index").unlink()
        calls = _flaky(monkeypatch, "stat", 0, PermissionError())

        assert git_state.read_index_stat_identity(repo) is None
        assert calls["n"] <= 1


class TestGitIndexScopedStatus:
    """The reader that was already covered. Kept as the CONTROL: it is what
    made the original finding a coverage gap rather than a measurement of a
    busy box, and now that both readers share one ladder it is also the leg
    that says the shared ladder did not regress the side that was fine."""

    def test_a_transient_read_is_retried(self, repo, monkeypatch):
        calls = _flaky(
            monkeypatch,
            "read_bytes",
            _INJECTED_FAILURES,
            PermissionError(5, "Access is denied"),
        )

        assert git_index.scoped_status(repo, ["a.txt"]) == {"a.txt": "untracked"}
        assert calls["n"] == _ATTEMPTS_TO_SUCCEED


class TestExactlyOneLadder:
    """`git_index` imports `git_state`, so neither can host a helper the other
    uses. The resolution was to put one ladder in `git_objects`, below both --
    NOT to give each module a copy. A copy passes every retry test above and
    reintroduces the exact defect, because the two ladders drift and only one
    of them gets the next fix."""

    def test_both_readers_route_through_the_git_objects_ladder(self, repo, monkeypatch):
        seen = []
        real = git_objects._retry_transient_read

        def counting(op):
            seen.append(op)
            return real(op)

        # Every namespace the name is bound in. Both readers do `from
        # git_objects import _retry_transient_read`, so patching the defining
        # module alone leaves their own bindings pointing at the original --
        # which reads as "bypassed the ladder" and is a bug in the test, not
        # in the reader.
        for module in (git_objects, git_state, git_index):
            monkeypatch.setattr(module, "_retry_transient_read", counting)

        git_state.read_index(repo, fresh=True)
        assert seen, "git_state.read_index bypassed the shared ladder"

        seen.clear()
        git_index.scoped_status(repo, ["a.txt"])
        assert seen, "git_index.scoped_status bypassed the shared ladder"

    def test_neither_reader_module_calls_time_sleep_directly(self):
        # Review: code-reviewer finding 1 (a0f120ca85333568d) -- a name-grep
        # for "RETRY_DELAY" is evaded by a resurrected ladder under any other
        # name (`_BACKOFF_S`, `_READ_PAUSE_S`, a local var `vars(module)`
        # never sees), so it was never "the tell" its own comment claimed. A
        # per-site ladder must itself pause, and only `time.sleep` can do
        # that -- scanning the module's source for the literal call is not
        # evadable by renaming a delay tuple or hiding it as a local.
        for module in (git_state, git_index):
            source = inspect.getsource(module)
            assert "time.sleep(" not in source, (
                f"{module.__name__} calls time.sleep directly; a resurrected "
                "per-site retry ladder lives here instead of in git_objects "
                "-- see this class's docstring"
            )
