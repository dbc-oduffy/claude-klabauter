"""`ensure_session` — the session directory's ONE constructor.

This module replaces `test_ensure_meta.py`, deleted 2026-08-26 along with the
`ensure_meta` it covered. That function pinned the WRITE path's
self-sufficiency (a writer about to set a `meta.json` field gets a record
first) and its own negative-spec said: "Does NOT assert that record-less
directories stop being created. That is the constructor fix, sized as its own
plan." The PM overturned that deferral, `ensure_session` is the constructor,
and `ensure_meta` briefly survived as a two-line alias before being deleted
for reading as a supported second way in. What follows pins the property the
alias could not:

  - directory and record are produced by the same call, or neither;
  - repeated calls are idempotent and never clobber a concurrent writer's
    fields;
  - the pre-resolved `sessions_base`/`root` seam lands the record in the tree
    the CALLER resolved, never one re-derived from the process cwd.

The static half of the rule — that nothing else in the corpus mkdirs a session
directory — is `coordinator_core/tests/test_session_dir_has_one_constructor.py`.
Both halves are needed: this module proves the constructor works, that one
proves it is the only one.

Hermetic by construction: a bare `.git/` directory with a `HEAD` file is
enough for `git_common_dir`'s walk and for `git_state`'s in-process reads, so
nothing here spawns git (unlike `test_ensure_meta.py`, which is
`spawns_process`/`cadence`-marked for its real `git init`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.session import core


@pytest.fixture()
def repo(tmp_path):
    """A directory `git_common_dir`'s walk accepts, with no git spawn."""
    r = tmp_path / "repo"
    (r / ".git").mkdir(parents=True)
    (r / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (r / ".git" / "objects").mkdir()
    (r / ".git" / "refs").mkdir()
    core.reset_sessions_dir_cache()
    yield r
    core.reset_sessions_dir_cache()


def _hub(repo: Path) -> Path:
    return repo / ".git" / "coordinator-sessions"


class TestDirectoryAndRecordTogether:
    def test_a_fresh_session_gets_both(self, repo):
        """The whole point. Before this, a writer mkdir-ed the directory and
        the record arrived only if some lazy initializer happened to win a
        race; a session that lost it read `holder_goal_state: undeclared` to
        every peer and could never be reaped."""
        sid = "sess-fresh"
        assert not (_hub(repo) / sid).exists()

        resolved = core.ensure_session(sid, str(repo))

        sdir = Path(resolved)
        assert sdir == _hub(repo) / sid
        assert sdir.is_dir()
        assert (sdir / "meta.json").is_file()
        record = json.loads((sdir / "meta.json").read_text(encoding="utf-8"))
        assert record["session_id"] == sid

    def test_neither_when_the_record_write_fails(self, repo, monkeypatch):
        """"Or neither" is observable, not decorative: a create that cannot
        complete leaves no directory behind for `liveness.live_session_ids` to
        enumerate as a phantom session."""
        sid = "sess-boom"

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated init failure")

        monkeypatch.setattr(core, "init", _boom)

        resolved = core.ensure_session(sid, str(repo))

        # The resolved path is still returned (the caller's no-op-and-warn
        # branch stays reachable — see the return contract in the docstring),
        # but nothing was created under it.
        assert resolved == core.session_dir(sid, str(repo))
        assert not Path(resolved).exists()

    def test_an_existing_record_less_directory_is_repaired(self, repo):
        """The residue case: directories minted by the pre-constructor writers
        are still on disk across the fleet. Reaching one through
        `ensure_session` heals it rather than stepping over it."""
        sid = "sess-legacy-residue"
        sdir = _hub(repo) / sid
        sdir.mkdir(parents=True)
        (sdir / "touched.txt").touch()
        assert core.update_meta_field(str(sdir), "goal", "before") is False

        core.ensure_session(sid, str(repo))

        assert (sdir / "meta.json").is_file()
        assert core.update_meta_field(str(sdir), "goal", "after") is True


class TestIdempotenceAndConcurrency:
    def test_a_concurrent_writers_goal_survives_a_repeat_call(self, repo):
        """`init` is an idempotent CREATE here, never a read-modify-write of
        `goal` — the bounded exception `hooks/session_heartbeat._bootstrap_
        meta` already relies on. Peers race on this hub constantly; a
        constructor that clobbered `goal` would break the exact field the
        constructor exists to make readable."""
        sid = "sess-idempotent"
        core.ensure_session(sid, str(repo))
        sdir = _hub(repo) / sid
        core.update_meta_field(str(sdir), "goal", "peer-goal")

        for _ in range(3):
            core.ensure_session(sid, str(repo), goal="a-different-goal")

        record = json.loads((sdir / "meta.json").read_text(encoding="utf-8"))
        assert record["goal"] == "peer-goal"

    def test_the_first_create_is_atomic(self, repo):
        """A truncate-then-write leaves a window in which a peer reads a torn
        `meta.json`, and a torn record is WORSE than an absent one: the
        `is_file()` arm sees a record and returns, while every
        `update_meta_field` write against it no-ops on the JSON parse. The
        create goes through mkstemp + `os.replace`, so no partial file is ever
        visible under the record's own name."""
        sid = "sess-atomic"
        core.ensure_session(sid, str(repo))
        sdir = _hub(repo) / sid

        # No temp file is left behind, and what landed parses.
        leftovers = [p.name for p in sdir.iterdir() if p.name.startswith("meta.json.")]
        assert leftovers == []
        assert isinstance(json.loads((sdir / "meta.json").read_text(encoding="utf-8")), dict)


class TestRestampArmIsBoundedPerProcess:
    """The re-stamp arm repairs a record created record-but-unstamped -- it is
    the ONLY path that can. But `scope.touch` calls `ensure_session` on every
    sanctioned mutating op, so on a host where the stamp can never succeed the
    repair re-ran Guard-1's psutil parent inspection on EVERY touch.

    Measured 2026-08-26 (RUSAGE process time, n=400): 0.29ms per call against
    the stamped fast arm's 0.021ms -- 13.5x -- on a live corpus whose sessions
    make a median of 19 and a maximum of 65 touches. The bound took it to
    0.005ms (1.1x). The affected population is exactly K-006's: POSIX hosts
    where the parent-name check misses AND `CLAUDE_PID` does not resolve.
    """

    @staticmethod
    def _unstampable(monkeypatch):
        """Force Guard-1 to be unable to stamp, the way a POSIX host whose
        parent process is not `claude` and which exports no usable
        `CLAUDE_PID` cannot."""
        monkeypatch.setattr(core, "_is_harness_process", lambda name: False)
        monkeypatch.setattr(core, "_resolve_claude_pid_from_env", lambda: (None, "test-forced-miss"))

    def test_a_session_that_cannot_stamp_is_attempted_once_per_process(self, repo, monkeypatch):
        sid = "sess-unstampable"
        core.reset_stamp_attempt_memo()
        self._unstampable(monkeypatch)

        # Create the record FIRST and uncounted. The create is a legitimate
        # `init()` call and is not what this test is about -- isolating it is
        # what makes the count below read purely as re-stamp attempts.
        core.ensure_session(sid, str(repo))
        core.reset_stamp_attempt_memo()

        calls = []
        real_init = core.init

        def _counting_init(*args, **kwargs):
            calls.append(1)
            return real_init(*args, **kwargs)

        monkeypatch.setattr(core, "init", _counting_init)

        for _ in range(20):
            core.ensure_session(sid, str(repo))

        assert not core.read_meta_field(str(_hub(repo) / sid), "stable_pid"), (
            "fixture precondition: this session must be unstampable"
        )
        assert len(calls) == 1, (
            f"the re-stamp arm ran init() {len(calls)} times across 20 calls for one "
            "session in one process; it must attempt once and then stop re-deriving "
            "an answer that cannot change"
        )

    def test_a_new_process_re_attempts_so_a_later_stamp_still_lands(self, repo, monkeypatch):
        """The bound must NOT become a permanent give-up. A persisted "gave up"
        marker would silently reopen K-006's Layer-1 gap; a process-local memo
        cannot, because the next process starts empty. This is the test that
        distinguishes the two designs."""
        sid = "sess-later-stampable"
        core.reset_stamp_attempt_memo()

        with monkeypatch.context() as m:
            self._unstampable(m)
            core.ensure_session(sid, str(repo))
            core.ensure_session(sid, str(repo))
            assert not core.read_meta_field(str(_hub(repo) / sid), "stable_pid")

        # A NEW process: same session dir, memo empty, Guard-1 now able to stamp.
        core.reset_stamp_attempt_memo()
        core.ensure_session(sid, str(repo))

        assert core.read_meta_field(str(_hub(repo) / sid), "stable_pid"), (
            "a session that became stampable was never re-attempted -- the bound "
            "turned into a permanent give-up and K-006's Layer-1 gap is reopened"
        )

    def test_the_memo_is_bounded(self, repo, monkeypatch):
        """The warm server is long-lived and serves many sessions, so an
        unbounded set is a slow leak."""
        core.reset_stamp_attempt_memo()
        self._unstampable(monkeypatch)

        for i in range(core._STAMP_ATTEMPTED_MAX + 5):
            core.ensure_session(f"sess-bound-{i}", str(repo))

        assert len(core._STAMP_ATTEMPTED) <= core._STAMP_ATTEMPTED_MAX, (
            f"memo grew to {len(core._STAMP_ATTEMPTED)}, past its "
            f"{core._STAMP_ATTEMPTED_MAX} bound"
        )

    def test_a_stamped_session_never_enters_the_memo(self, repo):
        """The fast arm returns before the memo is consulted -- the common case
        pays nothing for this mechanism."""
        core.reset_stamp_attempt_memo()
        sid = "sess-stamped-not-memoed"
        core.ensure_session(sid, str(repo))
        if not core.read_meta_field(str(_hub(repo) / sid), "stable_pid"):
            pytest.skip("Guard-1 could not stamp on this host; nothing to assert")
        before = set(core._STAMP_ATTEMPTED)
        core.ensure_session(sid, str(repo))
        assert core._STAMP_ATTEMPTED == before


class TestPreResolvedSeam:
    def test_the_record_lands_in_the_hub_the_caller_resolved(self, repo, tmp_path):
        """Write confinement. `hooks/track_touched_files` hands over the hub
        and the worktree root PRE-RESOLVED from the common dir it already
        resolved, and is pinned at ZERO `core.git_root` calls by its own
        guard. `ensure_session` must therefore re-derive neither — a seam that
        quietly fell back to the process cwd could land a record in a tree the
        caller never resolved."""
        sid = "sess-seam"
        hub = _hub(repo)

        called = []
        original = core.git_root

        def _tracking(cwd=None):
            called.append(cwd)
            return original(cwd)

        try:
            core.git_root = _tracking  # type: ignore[assignment]
            resolved = core.ensure_session(
                sid, sessions_base=str(hub), root=str(repo)
            )
        finally:
            core.git_root = original  # type: ignore[assignment]

        assert Path(resolved) == hub / sid
        assert (hub / sid / "meta.json").is_file()
        assert called == [], "the pre-resolved seam re-derived the worktree root"

    def test_empty_when_the_hub_is_unresolvable(self, tmp_path):
        core.reset_sessions_dir_cache()
        assert core.ensure_session("sess-x", str(tmp_path / "not-a-repo")) == ""

    def test_an_empty_session_id_is_rejected(self, repo):
        """Mirrors `session_dir`'s own contract. Silently resolving to the hub
        itself would let a caller with no session id mkdir over it."""
        with pytest.raises(ValueError):
            core.ensure_session("", str(repo))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
