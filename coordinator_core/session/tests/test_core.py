"""
coordinator_core.session.tests.test_core — parity tests for
coordinator_core.session.core, the Python engine port of the
retained-in-hub helpers.

Port of: coordinator-session.sh (DoE e34f2484, 2026-07-22).

Q20 (golden-diff): stable_pid_alive MUST golden-diff against REAL on-disk
meta.json, not synthetic fixtures alone — see the corpus-backed tests below.

The corpus these tests golden-diff against used to be a live
``sorted(Path(core.git_root() or ".", ".git", "coordinator-sessions").glob(
"*/meta.json"))[:25]`` walk. That corpus lives inside ``.git/`` — untracked,
machine-local, never cloned — so on a fresh clone, in CI, or on any machine
that has never run a coordinator session, the glob returns ``[]``,
``parametrize`` silently collects ZERO tests, and this golden-diff reports
nothing wrong while exercising nothing at all. Frozen 2026-07-22 into
``fixtures/frozen_coordinator_sessions_corpus_2026-07-22.json`` (a snapshot
of the real records this machine had at the time, preserved verbatim).
Never re-point this test at the LIVE ``.git/`` corpus again — that is
exactly the bug being fixed.

Recipe: scratch/subagent-sandbox/bash-to-python-engine-migration/
recipe-t4a-coordinator-session-hub.md § core.py, § GIVES PAUSE item 4
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.session import core

# Every test in this file builds its repo via `_make_repo(tmp_path)`, spawning
# real git (init/config/add/commit) because the production code under test --
# `core.git_root()`, session-hub resolution via `git rev-parse
# --git-common-dir`, and HEAD/branch resolution -- reads real git state that
# no mock stands in for. `tmp_path` is function-scoped and tests reuse the
# same session ids across the file, so the repo fixture stays per-test rather
# than hoisted to module scope. The spawn ratchet's `_BASELINE` is
# shrink-only pre-existing residue and is explicitly not the route for this
# file -- coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

try:
    import psutil
except ImportError:  # guarded per-test via pytest.mark.skipif(find_spec is None)
    psutil = None  # type: ignore[assignment]

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_FROZEN_CORPUS_FIXTURE = (
    _FIXTURES_DIR / "frozen_coordinator_sessions_corpus_2026-07-22.json"
)
# Corpus-size pin: a re-freeze over a machine with fewer stored sessions than
# this one would silently narrow the golden-diff to a smaller corpus without
# ever failing. Bumping this constant is a deliberate acknowledgment that the
# frozen corpus changed; a re-freeze that narrows it must fail loud.
_EXPECTED_CORPUS_SIZE = 53

_FROZEN_CORPUS_ENTRIES = json.loads(
    _FROZEN_CORPUS_FIXTURE.read_text(encoding="utf-8")
)["entries"]
# Collection-time (not test-time) non-empty + size guard: a bare
# `@pytest.mark.parametrize` over an empty/narrowed list just collects fewer
# tests and reports nothing wrong -- the exact silent-vanish shape this fix
# exists to close.
assert len(_FROZEN_CORPUS_ENTRIES) == _EXPECTED_CORPUS_SIZE, (
    f"frozen corpus fixture {_FROZEN_CORPUS_FIXTURE} has "
    f"{len(_FROZEN_CORPUS_ENTRIES)} entries, expected "
    f"{_EXPECTED_CORPUS_SIZE} -- update _EXPECTED_CORPUS_SIZE only as a "
    "deliberate acknowledgment of a corpus re-freeze, never to silence this."
)

# NOTE (2026-08-08): a module-level `_REAL_ASSERTION_COUNT` counter used to
# live here, incremented only on the branch of
# TestStablePidAliveGoldenDiff.test_real_on_disk_meta_json_corpus that
# reaches a real (non-skip) assertion, and checked by
# test_frozen_meta_corpus_did_not_all_skip below. It was retired: with
# pytest-randomly active (see coordinator_core/pytest.ini and
# pyproject.toml), test execution order is NOT file order, so
# test_frozen_meta_corpus_did_not_all_skip could run BEFORE the golden-diff
# class populated the counter -- an order-dependent false failure, not a
# real one. test_frozen_meta_corpus_did_not_all_skip now recomputes the same
# "does at least one corpus entry carry a stable_pid" fact directly from
# _FROZEN_CORPUS_ENTRIES at check time, independent of any other test having
# run first, in any order.


# ---------------------------------------------------------------------------
# git_root / sessions_dir / session_dir
# ---------------------------------------------------------------------------


def test_git_root_resolves_this_repo(tmp_path):
    root = core.git_root()
    assert root
    assert Path(root).is_dir()
    assert (Path(root) / ".git").exists()


def test_git_root_empty_outside_a_repo(tmp_path):
    assert core.git_root(cwd=str(tmp_path)) == ""


def test_sessions_dir_and_session_dir_composition():
    root = core.git_root()
    sdir = core.sessions_dir()
    assert sdir == str(Path(root) / ".git" / "coordinator-sessions")
    one = core.session_dir("abc-123")
    assert one == str(Path(sdir) / "abc-123")


def test_session_dir_requires_sid():
    with pytest.raises(ValueError):
        core.session_dir("")


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)


def test_sessions_dir_normal_repo_matches_legacy_dot_git_shape(tmp_path):
    """Regression guard: a non-worktree repo must resolve to the exact same
    ``<root>/.git/coordinator-sessions`` shape as before the --git-common-dir
    fix, so no existing on-disk session hub relocates."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)
    sdir = core.sessions_dir(cwd=str(repo))
    assert sdir == str(repo / ".git" / "coordinator-sessions")


def test_sessions_dir_in_real_worktree_is_creatable(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)
    worktree = tmp_path / "wt1"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(worktree), "-b", "wt1-branch"],
        cwd=repo,
        check=True,
    )

    assert (worktree / ".git").is_file()

    sdir = core.sessions_dir(cwd=str(worktree))
    assert sdir
    Path(sdir).mkdir(parents=True, exist_ok=True)
    assert Path(sdir).is_dir()
    assert Path(sdir).parent == (repo / ".git")


def test_sessions_dir_two_worktrees_share_one_hub(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)
    wt1 = tmp_path / "wt1"
    wt2 = tmp_path / "wt2"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(wt1), "-b", "wt1-branch"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-q", str(wt2), "-b", "wt2-branch"],
        cwd=repo,
        check=True,
    )

    assert core.sessions_dir(cwd=str(wt1)) == core.sessions_dir(cwd=str(wt2))
    assert core.sessions_dir(cwd=str(wt1)) == core.sessions_dir(cwd=str(repo))


def test_sessions_dir_outside_repo_still_empty(tmp_path):
    assert core.sessions_dir(cwd=str(tmp_path)) == ""


# ---------------------------------------------------------------------------
# sessions_dir() caching (explicit-cwd only — see docstring)
# ---------------------------------------------------------------------------


def test_sessions_dir_explicit_cwd_spawns_once_across_repeat_calls(tmp_path, monkeypatch):
    """N calls with the same explicit cwd must produce exactly ONE
    subprocess spawn and the same return value every time."""
    core.reset_sessions_dir_cache()
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)

    real_run = subprocess.run
    calls = []

    def counting_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(core.subprocess, "run", counting_run)

    first = core.sessions_dir(cwd=str(repo))
    for _ in range(5):
        assert core.sessions_dir(cwd=str(repo)) == first
    assert first == str(repo / ".git" / "coordinator-sessions")
    assert len(calls) == 1, f"expected exactly one spawn, got {len(calls)}: {calls}"
    core.reset_sessions_dir_cache()


def test_sessions_dir_cwd_none_not_cached_still_spawns_per_call(monkeypatch):
    """cwd=None resolves against the live process cwd, which can legitimately
    change mid-process (mirrors git_root()'s documented contract) — it must
    NOT be cached, and must spawn on every call."""
    core.reset_sessions_dir_cache()
    real_run = subprocess.run
    calls = []

    def counting_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(core.subprocess, "run", counting_run)

    core.sessions_dir()
    core.sessions_dir()
    core.sessions_dir()
    assert len(calls) == 3, f"expected one spawn per call, got {len(calls)}: {calls}"


def test_sessions_dir_failed_resolution_not_cached(tmp_path, monkeypatch):
    """A failed resolution (not a git repo) for a given explicit cwd must
    NOT be memoized as authoritative — re-running after the cwd becomes a
    real repo must succeed, not keep returning the stale "" answer."""
    core.reset_sessions_dir_cache()
    target = tmp_path / "not-yet-a-repo"
    target.mkdir()

    assert core.sessions_dir(cwd=str(target)) == ""
    # Same cwd, but now a real repo -- if the failure had been cached this
    # would still incorrectly return "".
    _make_repo(target)
    assert core.sessions_dir(cwd=str(target)) == str(target / ".git" / "coordinator-sessions")
    core.reset_sessions_dir_cache()


def test_sessions_dir_cache_clear_forces_respawn(tmp_path, monkeypatch):
    core.reset_sessions_dir_cache()
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)

    real_run = subprocess.run
    calls = []

    def counting_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(core.subprocess, "run", counting_run)

    core.sessions_dir(cwd=str(repo))
    core.sessions_dir(cwd=str(repo))
    assert len(calls) == 1
    core.reset_sessions_dir_cache()
    core.sessions_dir(cwd=str(repo))
    assert len(calls) == 2
    core.reset_sessions_dir_cache()


def test_init_populates_stable_pid_when_parent_comm_is_claude(tmp_path, monkeypatch):
    """Regression (defect A, 2026-07-24): core.init stamps meta.json's Layer-1
    stable_pid when the parent process comm resolves to 'claude' — the case on
    the in-process hook self-heal paths (empirically verified: the PostToolUse
    hook subprocess's parent is the live `claude` wrapper). Also asserts the
    poisoned state (session dir present, meta.json absent) is healed, not skipped,
    and that stable_pid pins to the resolved parent pid — not left empty (an empty
    stable_pid makes Layer-1 liveness inert, the whole defect).

    Mechanism (updated 2026-07-27, ps-to-psutil port): core.init's POSIX Guard-1
    no longer spawns `ps -p <ppid> -o comm=,lstart=`; it comm-verifies the parent
    via ``psutil.Process(ppid).name()`` and derives the persisted epoch from
    ``psutil.Process(ppid).create_time()``. This test patches ``core.psutil`` so
    the mock hits the same source `init()` actually reads, and additionally
    asserts the persisted `stable_pid_start_epoch` equals `int()` of the mocked
    `create_time()` — the pre-port version of this test never checked the epoch
    at all, and that field is now the load-bearing one (it drives the derived-
    epoch write path; see core.py's POSIX Guard-1 comment).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)
    monkeypatch.setattr(core, "_IS_WINDOWS", False)

    sid = "cafe0000-1111-2222-3333-444455556666"
    sdir = Path(core.sessions_dir(cwd=str(repo))) / sid
    sdir.mkdir(parents=True, exist_ok=True)  # another writer won the create race
    assert not (sdir / "meta.json").exists(), "precondition: poisoned (no meta.json)"

    fake_create_time = 1785000000.0  # arbitrary fixed epoch, distinct from 0/now

    class FakeParentProcess:
        def __init__(self, pid):
            self._pid = pid

        def name(self):
            return "claude"

        def create_time(self):
            return fake_create_time

    # core.psutil is a deferred-import cache (docs/plans/2026-08-08-seven-
    # measured-levers-load-norm.md § C1) — force resolution via core._psutil()
    # before patching, rather than reading the possibly-unresolved attribute.
    monkeypatch.setattr(core._psutil(), "Process", FakeParentProcess)

    assert core.init(sid, cwd=str(repo)) is True

    meta = json.loads((sdir / "meta.json").read_text())
    assert meta["session_id"] == sid
    assert meta.get("stable_pid"), "stable_pid must be populated when parent comm is claude"
    assert meta["stable_pid"] == str(os.getppid())
    assert meta["stable_pid_start_epoch"] == str(int(fake_create_time))


# ---------------------------------------------------------------------------
# Clock helpers
# ---------------------------------------------------------------------------


def test_now_iso_format():
    iso = core.now_iso()
    assert iso.endswith("Z")
    assert "T" in iso
    # round-trips through iso_to_epoch without raising
    assert core.iso_to_epoch(iso) > 0


def test_now_epoch_is_close_to_wall_clock():
    assert abs(core.now_epoch() - int(time.time())) <= 2


# ---------------------------------------------------------------------------
# pid_alive
# ---------------------------------------------------------------------------


def test_pid_alive_true_for_self():
    assert core.pid_alive(os.getpid()) is True


def test_pid_alive_false_for_bogus_pid():
    # PID 2**31-1 is not a valid/assignable process id on any real system.
    assert core.pid_alive(2**31 - 1) is False


def test_pid_alive_false_for_garbage_input():
    assert core.pid_alive("not-a-pid") is False
    assert core.pid_alive(None) is False
    assert core.pid_alive(-5) is False


def test_pid_alive_posix_unaffected_by_missing_psutil(monkeypatch):
    """POSIX pid_alive shells out to os.kill(pid, 0) and never touches
    psutil — absence of psutil must not change POSIX behavior at all,
    alive or dead."""
    monkeypatch.setattr(core, "_IS_WINDOWS", False)
    monkeypatch.setattr(core, "psutil", None)
    assert core.pid_alive(os.getpid()) is True
    assert core.pid_alive(2**31 - 1) is False


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("psutil") is None,
    reason="psutil (the Windows liveness mechanism) not installed",
)
def test_pid_alive_windows_uses_psutil_pid_exists(monkeypatch):
    # Flip the platform seam only (see TestWindowsCreateTimePath in
    # test_liveness.py for why _IS_WINDOWS and not os.name).
    monkeypatch.setattr(core, "_IS_WINDOWS", True)
    assert core.pid_alive(os.getpid()) is True
    assert core.pid_alive(2**31 - 1) is False


def test_pid_alive_windows_missing_psutil_raises_loud(monkeypatch):
    """The defect this test guards against: on native Windows with psutil
    absent, pid_alive used to silently return False for every PID (every
    session reads DEAD -> wrongful claim takeover). It must now raise a
    clear, actionable error instead."""
    monkeypatch.setattr(core, "_IS_WINDOWS", True)
    monkeypatch.setattr(core, "psutil", None)
    with pytest.raises(core.MissingPsutilError, match="psutil is a required dependency"):
        core.pid_alive(os.getpid())


def test_win_create_time_epoch_missing_psutil_raises_loud(monkeypatch):
    """_win_create_time_epoch backs stable_pid_alive's Windows Layer 1 —
    the ACTUAL session-liveness signal (pid_alive is explicitly NOT one,
    per this module's negative-spec). Before this fix, psutil-absent
    collapsed into None -> stable_pid_alive read every session DEAD. It
    must now raise instead of returning None."""
    monkeypatch.setattr(core, "psutil", None)
    with pytest.raises(core.MissingPsutilError, match="psutil is a required dependency"):
        core._win_create_time_epoch(os.getpid())


def test_stable_pid_alive_windows_missing_psutil_raises_loud(monkeypatch):
    """End-to-end: stable_pid_alive on simulated Windows with psutil absent
    must propagate MissingPsutilError rather than silently reporting the
    session dead."""
    monkeypatch.setattr(core, "_IS_WINDOWS", True)
    monkeypatch.setattr(core, "psutil", None)
    with pytest.raises(core.MissingPsutilError):
        core.stable_pid_alive(os.getpid(), "", "12345")


# ---------------------------------------------------------------------------
# stable_pid_alive — golden-diff against a REAL live process (self) +
# REAL on-disk meta.json corpus.
# ---------------------------------------------------------------------------


def _self_lstart_and_epoch():
    """Fetch this test process's own real ps -o lstart= and derive its
    epoch via the same algorithm core.lstart_to_epoch uses — an
    independent-but-identical computation used only to build a KNOWN-GOOD
    fixture, not to bypass the function under test."""
    result = subprocess.run(
        ["ps", "-p", str(os.getpid()), "-o", "lstart="],
        capture_output=True,
        text=True,
    )
    lstart = result.stdout.strip()
    assert lstart, "ps -p <self> -o lstart= must succeed on a live test process"
    return lstart


class TestStablePidAliveGoldenDiff:
    # Class-scoped skip reason for the four methods below (not the whole
    # class — test_legacy_string_compare_path_false_on_mismatch,
    # test_dead_pid_false_regardless_of_stored_fields, test_garbage_pid_false,
    # and test_real_on_disk_meta_json_corpus don't shell `ps` and must keep
    # running on Windows): these fixtures are built from POSIX `ps -o
    # lstart=` output on the CURRENT process, which is empty on Windows.
    _ps_oracle_skip = pytest.mark.skipif(
        os.name == "nt",
        reason="fixture built via POSIX `ps -o lstart=`; no Windows equivalent (see test_liveness.py's TestWindowsCreateTimePath for the Windows-side coverage)",
    )

    @_ps_oracle_skip
    def test_epoch_path_true_for_live_self(self):
        lstart = _self_lstart_and_epoch()
        epoch = core.lstart_to_epoch(lstart)
        assert epoch > 0
        assert core.stable_pid_alive(os.getpid(), lstart, str(epoch)) is True

    @_ps_oracle_skip
    def test_epoch_path_false_on_recycled_pid_mismatch(self):
        lstart = _self_lstart_and_epoch()
        real_epoch = core.lstart_to_epoch(lstart)
        # A deliberately wrong stored epoch simulates a recycled PID.
        assert core.stable_pid_alive(os.getpid(), lstart, str(real_epoch + 999999)) is False

    @_ps_oracle_skip
    def test_epoch_path_true_within_1s_skew(self):
        """The ±2s tolerance (_STABLE_PID_EPOCH_TOLERANCE_SECS): a stored
        epoch off by 1s from the live psutil-derived epoch must still read
        alive — this is the load-bearing case the tolerance exists for
        (ps-derived vs psutil-derived epochs for the SAME process can
        legitimately differ by ~1s)."""
        lstart = _self_lstart_and_epoch()
        epoch = core.lstart_to_epoch(lstart)
        assert epoch > 0
        assert core.stable_pid_alive(os.getpid(), lstart, str(epoch + 1)) is True
        assert core.stable_pid_alive(os.getpid(), lstart, str(epoch - 1)) is True

    @_ps_oracle_skip
    def test_epoch_path_true_at_2s_skew_boundary(self):
        """Exactly at the tolerance boundary (±2s) must still read alive —
        the tolerance is inclusive (`<=`), not exclusive."""
        lstart = _self_lstart_and_epoch()
        epoch = core.lstart_to_epoch(lstart)
        assert epoch > 0
        assert core.stable_pid_alive(os.getpid(), lstart, str(epoch + 2)) is True
        assert core.stable_pid_alive(os.getpid(), lstart, str(epoch - 2)) is True

    @_ps_oracle_skip
    def test_epoch_path_false_beyond_tolerance(self):
        """A skew well beyond the tolerance window is still a genuine
        recycled-PID mismatch, not a false positive from the new tolerance
        (distinct from test_epoch_path_false_on_recycled_pid_mismatch above,
        which pins the +999999 case; this pins the boundary is not loose)."""
        lstart = _self_lstart_and_epoch()
        epoch = core.lstart_to_epoch(lstart)
        assert epoch > 0
        assert core.stable_pid_alive(os.getpid(), lstart, str(epoch + 999999)) is False

    @_ps_oracle_skip
    def test_legacy_string_compare_path_true_for_live_self(self):
        """Legacy fallback (no stored_start_epoch): the 2026-07-27
        ps-to-psutil port redesigned this arm from a literal ps-text string
        compare into `lstart_to_epoch(stored_lstart)` fed through the SAME
        tolerant epoch compare used everywhere else (stable_pid_alive
        PLATFORM SPLIT docstring) — no `ps` spawn on the read side anymore.
        A live self process must still resolve alive under that
        redesign."""
        lstart = _self_lstart_and_epoch()
        # No stored_start_epoch -> legacy fallback branch.
        assert core.stable_pid_alive(os.getpid(), lstart, "") is True

    def test_legacy_string_compare_path_false_on_mismatch(self):
        assert core.stable_pid_alive(os.getpid(), "Sat Jan  1 00:00:00 2000", "") is False

    def test_dead_pid_false_regardless_of_stored_fields(self):
        assert core.stable_pid_alive(2**31 - 1, "Sat Jan  1 00:00:00 2000", "946684800") is False

    def test_garbage_pid_false(self):
        assert core.stable_pid_alive("not-a-pid", "", "") is False

    @_ps_oracle_skip
    def test_corrupt_stored_epoch_fails_closed_dead(self):
        # Review: code-reviewer P1 — a corrupted (non-numeric) stored_start_epoch
        # with a live self process (current lstart parses fine) must return dead
        # (fail-closed), never fall through to the legacy string compare. Rig
        # stored_lstart to STRING-MATCH the current lstart so that, if the code
        # regressed to the fall-through, the string compare would return True
        # (alive) -- proving this test actually exercises the fail-closed path
        # rather than passing for an unrelated reason.
        lstart = _self_lstart_and_epoch()
        assert (
            core.stable_pid_alive(os.getpid(), lstart, "not-a-number") is False
        )

    @pytest.mark.parametrize(
        "entry",
        _FROZEN_CORPUS_ENTRIES,
        ids=lambda e: e["sid"],
    )
    def test_real_on_disk_meta_json_corpus(self, entry):
        """Golden-diff against REAL stored meta.json records (Q20), frozen
        (see module docstring): for every stable_pid/stable_pid_lstart/
        stable_pid_start_epoch triple actually written by the bash cs_init,
        the port must (a) never raise and (b) agree with the bash semantics
        that a dead-process PID (virtually certain for historical session
        records) is NOT alive — the exact answer the bash
        ps -p ... -o lstart= empty-string ESRCH branch would also produce.
        """
        data = entry["meta"]
        stable_pid = data.get("stable_pid", "")
        if not stable_pid:
            pytest.skip("no stable_pid recorded in this meta.json (recency-only fallback)")
            return
        stored_lstart = data.get("stable_pid_lstart", "")
        stored_epoch = data.get("stable_pid_start_epoch", "")
        # Must not raise for any real stored value shape.
        result = core.stable_pid_alive(stable_pid, stored_lstart, stored_epoch)
        assert isinstance(result, bool)
        # A historical session's stable_pid is virtually never still the
        # current process (this test's own pid) — assert non-live unless
        # by coincidence it IS this process (impossible to rule out with
        # 100% certainty on a shared machine, so only assert the type/no-raise
        # contract when it doesn't match; when it does match, the epoch/lstart
        # comparison above already covers correctness).
        if str(stable_pid) != str(os.getpid()):
            assert result is False


def test_frozen_meta_corpus_did_not_all_skip():
    """Guards against the compounding failure mode alongside the corpus-size
    pin above: even with a non-empty frozen corpus, every entry could still
    hit the no-stable_pid pytest.skip() branch, reporting an all-green class
    with zero real assertions ever executed.

    2026-08-08: this check is deliberately ORDER-INDEPENDENT. It used to read
    a module-level ``_REAL_ASSERTION_COUNT`` counter incremented as a side
    effect of ``TestStablePidAliveGoldenDiff.test_real_on_disk_meta_json_
    corpus`` running first -- a real dependency that pytest-randomly (active
    here; see coordinator_core/pytest.ini and pyproject.toml) breaks whenever
    it schedules this test before that one, producing a false "every entry
    skipped" failure with no code change. Recomputing directly from
    ``_FROZEN_CORPUS_ENTRIES`` at check time reproduces the exact same
    per-entry skip predicate the golden-diff class itself uses
    (``data.get("stable_pid", "")`` falsy -> skip), without depending on any
    other test having executed.
    """
    entries_with_stable_pid = [
        e for e in _FROZEN_CORPUS_ENTRIES if e["meta"].get("stable_pid", "")
    ]
    assert entries_with_stable_pid, (
        "every entry in the frozen meta.json corpus skipped (no stable_pid) "
        "-- the golden-diff class would execute zero real assertions"
    )


# ---------------------------------------------------------------------------
# mtime_epoch / iso_to_epoch / lstart_to_epoch
# ---------------------------------------------------------------------------


def test_mtime_epoch_missing_file_returns_zero():
    assert core.mtime_epoch("/no/such/file/at/all") == 0


def test_mtime_epoch_matches_stat(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hi")
    st = os.stat(f)
    assert core.mtime_epoch(str(f)) == int(st.st_mtime)


def test_iso_to_epoch_empty_is_zero():
    assert core.iso_to_epoch("") == 0


def test_iso_to_epoch_known_value():
    # 2026-07-14T15:26:28Z is exactly epoch 1784042788 (UTC).
    assert core.iso_to_epoch("2026-07-14T15:26:28Z") == 1784042788


def test_iso_to_epoch_malformed_returns_zero():
    assert core.iso_to_epoch("not-a-timestamp") == 0


def test_lstart_to_epoch_empty_is_zero():
    assert core.lstart_to_epoch("") == 0


def test_lstart_to_epoch_malformed_returns_zero():
    assert core.lstart_to_epoch("garbage") == 0


def test_lstart_to_epoch_bsd_double_space_normalized():
    # BSD/macOS ps pads single-digit days with an extra space (%e).
    single = core.lstart_to_epoch("Sat Jun  7 22:41:38 2026")
    normalized = core.lstart_to_epoch("Sat Jun 7 22:41:38 2026")
    assert single == normalized
    assert single > 0


# Note: test_lstart_to_epoch_matches_bsd_date_j (cross-checking this port's
# lstart_to_epoch algorithm against the bash original's BSD `date -j -f`
# fallback) was removed 2026-07-27 in the ps-to-psutil port. It existed to
# pin parity against the retired bash `_cs_stable_pid_alive`/`_cs_lstart_to_
# epoch` oracle (retired 2026-07-22, per the cross-repo memo confirming that
# parity contract has no live counterparty) — lstart_to_epoch itself is
# still in production use (the POSIX legacy-fallback read arm in
# stable_pid_alive), just no longer against a bash parity target worth
# cross-checking. Its removal was itself a defect: that test was the ONLY
# TZ/DST oracle in the suite, and it `pytest.skip()`ed whenever the `date -j`
# binary was absent (i.e. everywhere but macOS/BSD) — a silent-skip that gave
# Windows and Linux zero protection against a TZ/DST misparse. A misparse
# here is an error of HOURS (`lstart_to_epoch` is still load-bearing on the
# POSIX legacy read path, `stable_pid_alive`'s pre-2026-07-27-meta fallback),
# and the port's `+/-2s` tolerance does nothing to catch it. The platform-
# independent replacement below (`test_lstart_to_epoch_tz_dst_oracle` /
# `test_lstart_to_epoch_windows_local_offset_oracle`) restores that coverage
# without the BSD-only vacuity.


@pytest.fixture
def _local_tz(monkeypatch):
    """POSIX-only helper: point the process's local zone at ``tz_name`` via
    ``TZ`` + ``time.tzset()`` for the duration of one test. ``monkeypatch``
    reverts the ``TZ`` env var automatically on teardown, but that alone
    does not un-cache libc's zone state — the finalizer below re-runs
    ``tzset()`` after revert so the process's local zone actually snaps
    back, instead of leaking the test's zone into whatever runs next.
    """

    def _apply(tz_name: str) -> None:
        monkeypatch.setenv("TZ", tz_name)
        time.tzset()

    yield _apply
    time.tzset()


@pytest.mark.skipif(
    not hasattr(time, "tzset"),
    reason="time.tzset() is POSIX-only; see test_lstart_to_epoch_windows_local_offset_oracle",
)
@pytest.mark.parametrize(
    "tz_name, lstart_str, note",
    [
        ("America/New_York", "Wed Jan 14 12:00:00 2026", "non-DST (EST, UTC-5)"),
        ("America/New_York", "Tue Jul 14 12:00:00 2026", "DST (EDT, UTC-4)"),
        # 2026-03-08 02:00 local -> 03:00 local (US spring-forward): 02:30
        # local never occurs. lstart_to_epoch resolves this NONEXISTENT wall
        # time as if the PRE-transition (still-standard-time, fold=0) offset
        # applied — pinned choice, not asserted-as-"correct": it is what
        # naive datetime.strptime(...).timestamp() actually does via libc
        # mktime under a TZ-set process, and this test exists to catch any
        # future divergence from that, not to declare it the "right" answer.
        ("America/New_York", "Sun Mar  8 02:30:00 2026", "spring-forward gap (nonexistent local time)"),
        # 2026-11-01 02:00 local -> 01:00 local (US fall-back): 01:30 local
        # occurs TWICE. lstart_to_epoch resolves this AMBIGUOUS wall time as
        # the fold=0 (still-DST, EDT/UTC-4) occurrence — again a pinned,
        # observed choice; the historical +/-3600s residual this ambiguity
        # produces is accepted per lstart_to_epoch's own docstring.
        ("America/New_York", "Sun Nov  1 01:30:00 2026", "fall-back ambiguous local time (fold=0 pinned)"),
        ("UTC", "Wed Jan 14 12:00:00 2026", "UTC control"),
        ("Asia/Kolkata", "Wed Jan 14 12:00:00 2026", "non-whole-hour offset (UTC+5:30, no DST)"),
        ("Europe/London", "Tue Jul 14 12:00:00 2026", "second DST-observing zone (BST, UTC+1)"),
    ],
)
def test_lstart_to_epoch_tz_dst_oracle(_local_tz, tz_name, lstart_str, note):
    """Platform-independent TZ/DST oracle for lstart_to_epoch (restores the
    coverage test_lstart_to_epoch_matches_bsd_date_j lost on removal — see
    the note above). Pins a known ``ps``-format lstart string, under an
    explicitly-controlled process TZ, against an epoch computed via an
    INDEPENDENT code path: ``zoneinfo.ZoneInfo`` interpreting the same naive
    wall-clock string directly against the IANA tz database, rather than
    going through the process's ``TZ`` env var + libc ``mktime`` (the path
    ``lstart_to_epoch`` itself exercises via ``datetime.strptime(...)
    .timestamp()``). A TZ/DST misparse in the implementation shows up as a
    disagreement between these two independently-derived epochs.
    """
    _local_tz(tz_name)
    naive = datetime.strptime(lstart_str, core._LSTART_FORMAT)
    from zoneinfo import ZoneInfo

    oracle = int(naive.replace(tzinfo=ZoneInfo(tz_name), fold=0).timestamp())
    assert core.lstart_to_epoch(lstart_str) == oracle, note


@pytest.mark.skipif(
    hasattr(time, "tzset"),
    reason="POSIX already covered by test_lstart_to_epoch_tz_dst_oracle with full TZ control",
)
def test_lstart_to_epoch_windows_local_offset_oracle():
    """Windows has no ``time.tzset()`` — the process's local zone cannot be
    swapped from within a test the way ``_local_tz`` does above, so this
    cannot exercise a CHOSEN zone's DST rules. It still asserts a real
    expectation against the box's ACTUAL current local UTC offset rather
    than silently skipping — the shape
    ``test_lstart_to_epoch_matches_bsd_date_j`` was retired for
    (``pytest.skip()`` whenever the oracle binary was absent, which was
    everywhere but macOS/BSD, leaving Windows/Linux with zero coverage).

    HONEST LIMITATION (2026-08-08 audit): this is NOT an independently
    derived oracle in the sense ``test_lstart_to_epoch_tz_dst_oracle`` above
    is. That POSIX test computes its expectation via ``zoneinfo.ZoneInfo``
    against an explicit IANA zone name — a genuinely different code path
    from ``lstart_to_epoch``'s ``datetime.strptime(...).timestamp()``. Here,
    ``naive.astimezone()`` resolves the offset through the SAME
    platform-local-zone / ``mktime``-family machinery
    ``datetime.strptime(...).timestamp()`` uses under the hood — there is no
    second, independent source of truth for the offset on this path. A
    genuinely independent Windows oracle would need to resolve the box's
    actual IANA zone name (e.g. via the third-party ``tzlocal`` package,
    not installed here / not a project dependency) and feed it through
    ``zoneinfo`` the way the POSIX oracle does; that is not available in
    this environment, so it is not attempted. What this test DOES still
    catch: any divergence between ``lstart_to_epoch``'s internal timestamp
    derivation and a fresh, independently-called ``datetime.astimezone()``
    for the SAME naive value on this box (e.g. an accidental UTC-coercion,
    an off-by-one-day, or a garbled offset sign) — a real regression class,
    just not a TZ/DST-rule-correctness oracle. What it does NOT catch: an
    implementation bug that happens to reproduce the same platform
    ``mktime`` resolution this test also uses (the two paths are close
    enough that such a bug could slip through both).

    Known residual gap: if the Windows host's local zone happens to be UTC
    at run time, this cannot distinguish a "force UTC" implementation
    sabotage from correct behavior — closing that requires an actual
    non-UTC Windows box exercising test_lstart_to_epoch_tz_dst_oracle's
    chosen-zone coverage, which needs ``time.tzset()`` and is POSIX-only.

    The oracle offset MUST be the UTC offset in effect AT the parsed
    instant (Jan 14), not at "now" (test-run time) — ``datetime.astimezone()``
    on a naive datetime resolves the offset for that specific wall-clock
    value via the platform's local-zone rules (same ``mktime``-family path
    ``lstart_to_epoch`` itself exercises), unlike
    ``datetime.now().astimezone().utcoffset()`` which reads whatever DST
    state happens to be active right now and silently misapplies it to a
    different point in the year — an exact 3600s bug on any DST-observing
    host when "now" and the parsed instant straddle a DST boundary.
    """
    lstart_str = "Wed Jan 14 12:00:00 2026"
    naive = datetime.strptime(lstart_str, core._LSTART_FORMAT)
    oracle = int(naive.astimezone().timestamp())
    assert core.lstart_to_epoch(lstart_str) == oracle


# ---------------------------------------------------------------------------
# read_meta_field / update_meta_field
# ---------------------------------------------------------------------------


def test_read_meta_field_missing_file_returns_empty(tmp_path):
    assert core.read_meta_field(str(tmp_path), "goal") == ""


def test_read_meta_field_missing_field_returns_empty(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({"session_id": "x"}))
    assert core.read_meta_field(str(tmp_path), "goal") == ""


def test_read_write_meta_field_roundtrip(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({"session_id": "x", "goal": ""}))
    assert core.update_meta_field(str(tmp_path), "goal", "ship the thing") is True
    assert core.read_meta_field(str(tmp_path), "goal") == "ship the thing"
    # Other fields untouched.
    assert core.read_meta_field(str(tmp_path), "session_id") == "x"


def test_update_meta_field_coerces_to_string(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({"session_id": "x"}))
    core.update_meta_field(str(tmp_path), "pid", 12345)
    data = json.loads((tmp_path / "meta.json").read_text())
    assert data["pid"] == "12345"
    assert isinstance(data["pid"], str)


def test_update_meta_field_missing_file_returns_false(tmp_path):
    assert core.update_meta_field(str(tmp_path), "goal", "x") is False


def test_update_meta_field_is_atomic_no_tmp_leftovers(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({"session_id": "x"}))
    core.update_meta_field(str(tmp_path), "goal", "y")
    leftovers = [p for p in tmp_path.iterdir() if p.name != "meta.json"]
    assert leftovers == []


# ---------------------------------------------------------------------------
# is_confined_findings_agent
# ---------------------------------------------------------------------------


def test_is_confined_findings_agent_true_for_code_reviewer():
    assert core.is_confined_findings_agent("coordinator:code-reviewer") is True


@pytest.mark.parametrize(
    "t",
    [
        "",
        None,
        "coordinator:staff-eng",
        "coordinator:executor",
        "security-audit-worker",
        "dep-cve-auditor",
    ],
)
def test_is_confined_findings_agent_false_for_everything_else(t):
    assert core.is_confined_findings_agent(t) is False


# ---------------------------------------------------------------------------
# resolve_session_id
# ---------------------------------------------------------------------------


class TestResolveSessionId:
    def test_tier1_coordinator_session_id_wins(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "tier1-sid")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "tier2-sid")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "tier3-sid")
        assert core.resolve_session_id() == "tier1-sid"

    def test_tier2_claude_session_id_when_tier1_absent(self, monkeypatch):
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "tier2-sid")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "tier3-sid")
        assert core.resolve_session_id() == "tier2-sid"

    def test_tier3_claude_code_session_id_when_tiers_1_2_absent(self, monkeypatch):
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "tier3-sid")
        assert core.resolve_session_id() == "tier3-sid"

    def test_all_tiers_empty_outside_a_git_repo(self, monkeypatch, tmp_path):
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        assert core.resolve_session_id(cwd=str(tmp_path)) == ""

    def test_sentinel_ignored_when_env_vars_absent(self, monkeypatch, tmp_path):
        """KS-4 (2026-08-07): the removed tier-4 sentinel read must no
        longer influence resolution at all — a present, well-formed
        sentinel file with no env vars set still resolves to "".
        """
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        subprocess.run(["git", "init", "-q"], cwd=tmp_path)
        sessions_dir = tmp_path / ".git" / "coordinator-sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / ".current-session-id").write_text("legacy-sid")
        assert core.resolve_session_id(cwd=str(tmp_path)) == ""

    def test_sentinel_ignored_even_when_it_matches_a_live_session(
        self, monkeypatch, tmp_path
    ):
        """A sentinel that WOULD have resolved unambiguously under the old
        tier-4 ambiguity guard (solo live session, sentinel matches) must
        still be ignored — tier-3 env-var absence resolves to "", not the
        sentinel's value.
        """
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        subprocess.run(["git", "init", "-q"], cwd=tmp_path)
        sessions_dir = tmp_path / ".git" / "coordinator-sessions"
        sdir = sessions_dir / "live-sid"
        sdir.mkdir(parents=True)
        (sdir / "meta.json").write_text(
            json.dumps({"pid": "1", "last_activity": core.now_iso()}),
            encoding="utf-8",
        )
        (sessions_dir / ".current-session-id").write_text("live-sid")
        assert core.resolve_session_id(cwd=str(tmp_path)) == ""

    def test_tier3_env_var_wins_over_a_present_sentinel(self, monkeypatch, tmp_path):
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "tier3-sid")
        subprocess.run(["git", "init", "-q"], cwd=tmp_path)
        sessions_dir = tmp_path / ".git" / "coordinator-sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / ".current-session-id").write_text("legacy-sid")
        assert core.resolve_session_id(cwd=str(tmp_path)) == "tier3-sid"


# ---------------------------------------------------------------------------
# SESSION_ENV_PRECEDENCE — single-source-of-truth property across consumers
#
# Regression coverage for the chain-review defect (slice D, F1) that motivated
# centralizing this chain: `block_consumed_handoff_edit`'s guard read ONLY
# `COORDINATOR_SESSION_ID` while `handoff_correct_body` (the op it routes to)
# walked the full three-tier chain, so a real session carrying only
# `CLAUDE_CODE_SESSION_ID` was wrongly told "Not your claim." All three
# consumers now import `core.SESSION_ENV_PRECEDENCE` rather than each
# maintaining its own copy — this asserts they agree, tier by tier, the
# property whose absence let that divergence happen.
# ---------------------------------------------------------------------------


class TestSessionEnvPrecedenceSingleSource:
    @pytest.fixture(autouse=True)
    def _clear_session_env(self, monkeypatch):
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    def _guard_is_holder_lookup(self):
        """Reproduce `block_consumed_handoff_edit.check`'s `is_holder`
        session-id lookup in isolation (no handoff file / payload needed) —
        it walks the same imported `SESSION_ENV_PRECEDENCE` the guard's
        `check()` consumes."""
        import os

        from coordinator_core.write_guards import block_consumed_handoff_edit as guard

        session_id = ""
        for var in guard.SESSION_ENV_PRECEDENCE:
            session_id = os.environ.get(var, "")
            if session_id:
                break
        return session_id

    @pytest.mark.parametrize(
        "env_vars, expected",
        [
            (
                {
                    "COORDINATOR_SESSION_ID": "tier1-sid",
                    "CLAUDE_SESSION_ID": "tier2-sid",
                    "CLAUDE_CODE_SESSION_ID": "tier3-sid",
                },
                "tier1-sid",
            ),
            (
                {
                    "CLAUDE_SESSION_ID": "tier2-sid",
                    "CLAUDE_CODE_SESSION_ID": "tier3-sid",
                },
                "tier2-sid",
            ),
            ({"CLAUDE_CODE_SESSION_ID": "tier3-sid"}, "tier3-sid"),
        ],
        ids=["tier1", "tier2", "tier3"],
    )
    def test_all_three_consumers_resolve_identically(self, monkeypatch, env_vars, expected):
        # Deliberately excludes the "no env var set" case: `core.resolve_
        # session_id` alone falls through to its tier-4 sentinel lookup
        # there (by design — see its docstring), a behavior the op/guard
        # consumers never implement. That divergence is intentional, out of
        # scope, and orthogonal to the property under test here (tiers 1-3,
        # the shared `SESSION_ENV_PRECEDENCE` chain, agree byte-for-byte).
        for var, val in env_vars.items():
            monkeypatch.setenv(var, val)

        # Consumer 1: coordinator_core.session.core.resolve_session_id — env
        # tiers 1-3 only (one of the three set here, so tier-4 never engages).
        assert core.resolve_session_id(cwd=None) == expected

        # Consumer 2: handoff_correct_body._resolve_session_id_with_source.
        from coordinator_core.ops import handoff_correct_body

        sid2, _source = handoff_correct_body._resolve_session_id_with_source()
        assert (sid2 or "") == expected

        # Consumer 3: block_consumed_handoff_edit's is_holder lookup.
        sid3 = self._guard_is_holder_lookup()
        assert sid3 == expected

        # Consumer 4 (KS-6, 2026-08-07): worktree_safety.resolve_self_session_id
        # — widened from a CLAUDE_CODE_SESSION_ID-only read to delegate to
        # core.resolve_session_id directly.
        from coordinator_core.session import worktree_safety

        assert worktree_safety.resolve_self_session_id(cwd=None) == expected

        # Consumer 5 (KS-6, 2026-08-07): ops.session_context.resolve_current_session_id
        # — widened from a 2-tier (CLAUDE_SESSION_ID, CLAUDE_CODE_SESSION_ID)
        # read to the full 3-tier ladder.
        from coordinator_core.ops import session_context

        assert session_context.resolve_current_session_id() == expected

        # Consumer 6 (KS-6, 2026-08-07): git.commit_trailers._resolve_session_id
        # — widened from a 2-tier read to delegate to core.resolve_session_id.
        from coordinator_core.git import commit_trailers

        assert commit_trailers._resolve_session_id(git_dir="") == expected

    def test_all_three_consumers_import_the_same_tuple_object(self):
        from coordinator_core.ops import handoff_correct_body
        from coordinator_core.write_guards import block_consumed_handoff_edit as guard

        assert handoff_correct_body._SESSION_ENV_PRECEDENCE is core.SESSION_ENV_PRECEDENCE
        assert guard.SESSION_ENV_PRECEDENCE is core.SESSION_ENV_PRECEDENCE


# ---------------------------------------------------------------------------
# init() (cs_init)
# ---------------------------------------------------------------------------


class TestInit:
    def _make_repo(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
        (tmp_path / "README.md").write_text("x")
        subprocess.run(["git", "add", "."], cwd=tmp_path)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)
        return tmp_path

    def test_init_creates_session_files(self, tmp_path):
        repo = self._make_repo(tmp_path)
        ok = core.init("test-sid-1", goal="ship it", cwd=str(repo))
        assert ok is True
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "test-sid-1"
        assert (sdir / "started_at").is_file()
        assert (sdir / "head_at_start").is_file()
        assert (sdir / "touched.txt").is_file()
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["session_id"] == "test-sid-1"
        assert meta["goal"] == "ship it"
        assert meta["branch"]
        assert meta["pid"] == str(os.getpid())

    def test_init_is_idempotent_preserves_started_at_and_goal(self, tmp_path):
        repo = self._make_repo(tmp_path)
        core.init("test-sid-2", goal="original goal", cwd=str(repo))
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "test-sid-2"
        started_at_first = (sdir / "started_at").read_text()

        # Re-init with no goal -> existing goal preserved, started_at untouched.
        core.init("test-sid-2", goal="", cwd=str(repo))
        assert (sdir / "started_at").read_text() == started_at_first
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["goal"] == "original goal"

    def test_init_refresh_does_not_overwrite_goal_with_new_value(self, tmp_path):
        """Bash ``cs_init`` writes ``goal`` only on first create (its own
        source comment: "meta.json — always refresh pid and last_activity;
        write goal only on first create"). On the refresh (existing
        meta.json) path, a NEW non-empty goal argument must NOT overwrite
        the stored goal — only pid/last_activity/branch/stable_pid* refresh.
        """
        repo = self._make_repo(tmp_path)
        core.init("test-sid-goal-refresh", goal="original goal", cwd=str(repo))
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "test-sid-goal-refresh"

        # Re-init with a DIFFERENT non-empty goal -> stored goal unchanged.
        core.init("test-sid-goal-refresh", goal="a completely different goal", cwd=str(repo))
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["goal"] == "original goal"

    def test_init_requires_session_id(self, tmp_path):
        repo = self._make_repo(tmp_path)
        with pytest.raises(ValueError):
            core.init("", cwd=str(repo))

    def test_init_returns_false_outside_git_repo(self, tmp_path):
        assert core.init("some-sid", cwd=str(tmp_path)) is False


# ---------------------------------------------------------------------------
# init() Windows Guard-1 — psutil-based comm-verify (`.exe`-suffix strip +
# exact "claude" match) and create_time() dual-write.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("psutil") is None,
    reason="psutil (the Windows liveness mechanism) not installed",
)
class TestInitWindowsGuard1:
    """Review: code-reviewer P2 — init()'s Windows Guard-1 branch
    (core.py:695-722) previously had zero test coverage, despite being
    mechanically testable without a real Windows box via the same
    `_IS_WINDOWS` monkeypatch technique already used in
    test_liveness.py's TestWindowsCreateTimePath. Only the literal
    fact "the real ppid process is named claude.exe" needs a real Windows +
    Claude Code process (deferred Gate-(d) dogfood) — faked here by wrapping
    `psutil.Process`."""

    def _make_repo(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
        (tmp_path / "README.md").write_text("x")
        subprocess.run(["git", "add", "."], cwd=tmp_path)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)
        return tmp_path

    def test_windows_guard1_writes_stable_pid_with_exe_suffix_stripped(
        self, tmp_path, monkeypatch
    ):
        import psutil

        repo = self._make_repo(tmp_path)
        monkeypatch.setattr(core, "_IS_WINDOWS", True)
        # Isolate from a real CLAUDE_PID in this test process's own
        # environment (set by the real Claude Code harness running this
        # suite) -- this class exercises the ancestor-walk fallback path,
        # not the CLAUDE_PID-preferred source (see
        # TestInitWindowsGuard1PreferenceOrder for that).
        monkeypatch.delenv("CLAUDE_PID", raising=False)

        real_process_cls = psutil.Process
        real_ppid = os.getppid()
        real_ct = int(real_process_cls(real_ppid).create_time())

        class _FakeParent:
            def name(self):
                return "claude.exe"

            def create_time(self):
                return real_ct

        def _fake_process(pid=None):
            return _FakeParent() if pid == real_ppid else real_process_cls(pid)

        monkeypatch.setattr(psutil, "Process", _fake_process)

        ok = core.init("test-sid-win-guard1", goal="win guard1", cwd=str(repo))
        assert ok is True
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "test-sid-win-guard1"
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["stable_pid"] == str(real_ppid)
        assert meta["stable_pid_start_epoch"] == str(real_ct)
        assert meta["stable_pid_lstart"] == str(real_ct)

    def test_windows_guard1_non_claude_parent_leaves_stable_pid_unset(
        self, tmp_path, monkeypatch
    ):
        import psutil

        repo = self._make_repo(tmp_path)
        monkeypatch.setattr(core, "_IS_WINDOWS", True)
        # See the sibling test above -- isolate from a real CLAUDE_PID.
        monkeypatch.delenv("CLAUDE_PID", raising=False)

        real_process_cls = psutil.Process
        real_ppid = os.getppid()

        class _FakeParent:
            def name(self):
                return "bash.exe"

            def create_time(self):
                return 123456789

        def _fake_process(pid=None):
            return _FakeParent() if pid == real_ppid else real_process_cls(pid)

        monkeypatch.setattr(psutil, "Process", _fake_process)

        ok = core.init("test-sid-win-guard1-nomatch", goal="x", cwd=str(repo))
        assert ok is True
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "test-sid-win-guard1-nomatch"
        meta = json.loads((sdir / "meta.json").read_text())
        assert "stable_pid" not in meta


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("psutil") is None,
    reason="psutil (the Windows liveness mechanism) not installed",
)
class TestFindWindowsClaudeAncestor:
    """Pins the bounded-ancestor-walk fix for Guard-1's "parent-only never
    matches on Windows" defect (real chain: python.exe -> bash.exe x3 ->
    claude.exe, 4 hops — a direct-parent check can never see the claude
    rung). Proves non-vacuity against the pre-fix parent-only logic: each
    of these fake chains has NO claude match at depth 0/1, so the old
    ``psutil.Process(ppid).name()`` check would leave stable_pid unset for
    all of them — the walk is what makes them pass."""

    class _FakeProc:
        def __init__(
            self,
            pid,
            name,
            ppid=None,
            create_time=1000.0,
            raise_on=None,
            name_exc=None,
        ):
            self._pid = pid
            self._name = name
            self._ppid = ppid
            self._create_time = create_time
            self._raise_on = raise_on or ()
            self._name_exc = name_exc

        def name(self):
            if "name" in self._raise_on:
                exc_cls = self._name_exc or psutil.AccessDenied
                raise exc_cls(self._pid)
            return self._name

        def create_time(self):
            if "create_time" in self._raise_on:
                raise psutil.NoSuchProcess(self._pid)
            return self._create_time

        def ppid(self):
            if "ppid" in self._raise_on:
                raise psutil.AccessDenied(self._pid)
            return self._ppid

    def _chain_process_factory(self, procs_by_pid, monkeypatch):
        def _fake_process(pid=None):
            try:
                return procs_by_pid[pid]
            except KeyError:
                raise psutil.NoSuchProcess(pid)

        monkeypatch.setattr(psutil, "Process", _fake_process)

    def test_finds_claude_ancestor_beyond_direct_parent(self, monkeypatch):
        # 1 (start) -> 2 (bash) -> 3 (bash) -> 4 (bash) -> 5 (claude.exe)
        procs = {
            1: self._FakeProc(1, "python.exe", ppid=2),
            2: self._FakeProc(2, "bash.exe", ppid=3),
            3: self._FakeProc(3, "bash.exe", ppid=4),
            4: self._FakeProc(4, "bash.exe", ppid=5),
            5: self._FakeProc(5, "claude.exe", ppid=6, create_time=555.0),
        }
        self._chain_process_factory(procs, monkeypatch)

        result, reason = core._find_windows_claude_ancestor(1)
        assert result == (5, 555.0)
        assert reason == "walk-hit:4"

    def test_no_claude_within_cap_returns_none_no_raise(self, monkeypatch):
        # A straight-line chain of non-claude processes, deeper than the cap.
        procs = {}
        depth = core._STABLE_PID_WINDOWS_ANCESTOR_DEPTH + 5
        for i in range(1, depth + 2):
            procs[i] = self._FakeProc(i, "bash.exe", ppid=i + 1)
        self._chain_process_factory(procs, monkeypatch)

        result, reason = core._find_windows_claude_ancestor(1)
        assert result is None
        assert reason == "walk-miss:depth-exhausted"

    def test_vanished_rung_mid_walk_returns_none_no_raise(self, monkeypatch):
        # pid 2's ppid() raises AccessDenied mid-walk (a short-lived bash
        # rung that has already exited).
        procs = {
            1: self._FakeProc(1, "python.exe", ppid=2),
            2: self._FakeProc(2, "bash.exe", ppid=3, raise_on=("ppid",)),
            3: self._FakeProc(3, "claude.exe", ppid=4, create_time=999.0),
        }
        self._chain_process_factory(procs, monkeypatch)

        result, reason = core._find_windows_claude_ancestor(1)
        assert result is None
        assert reason == "walk-miss:rung-unreadable:AccessDenied:1"

    def test_vanished_rung_process_lookup_returns_none_no_raise(self, monkeypatch):
        # pid 3 does not resolve at all (psutil.Process(3) raises).
        procs = {
            1: self._FakeProc(1, "python.exe", ppid=2),
            2: self._FakeProc(2, "bash.exe", ppid=3),
        }
        self._chain_process_factory(procs, monkeypatch)

        result, reason = core._find_windows_claude_ancestor(1)
        assert result is None
        assert reason == "walk-miss:rung-unreadable:NoSuchProcess:2"

    def test_depth_cap_is_respected(self, monkeypatch):
        # The walk examines hop indices 0..max_depth-1 (max_depth checks
        # total); claude sits at hop index max_depth (one hop past the last
        # examined rung) — must NOT be found.
        cap = 3
        procs = {
            1: self._FakeProc(1, "bash.exe", ppid=2),  # hop 0
            2: self._FakeProc(2, "bash.exe", ppid=3),  # hop 1
            3: self._FakeProc(3, "bash.exe", ppid=4),  # hop 2
            4: self._FakeProc(4, "claude.exe", ppid=5, create_time=42.0),  # hop 3 — out of range
        }
        self._chain_process_factory(procs, monkeypatch)

        result, reason = core._find_windows_claude_ancestor(1, max_depth=cap)
        assert result is None
        assert reason == "walk-miss:depth-exhausted"

    def test_walk_miss_no_parent_reason(self, monkeypatch):
        # ppid() returns falsy (chain terminates) — distinct reason code
        # from depth-exhausted / rung-unreadable.
        procs = {
            1: self._FakeProc(1, "bash.exe", ppid=0),
        }
        self._chain_process_factory(procs, monkeypatch)

        result, reason = core._find_windows_claude_ancestor(1)
        assert result is None
        assert reason == "walk-miss:no-parent"

    def test_depth_cap_finds_claude_exactly_at_boundary(self, monkeypatch):
        # claude sits at the LAST examined hop index (max_depth - 1) — must
        # be found.
        cap = 3
        procs = {
            1: self._FakeProc(1, "bash.exe", ppid=2),  # hop 0
            2: self._FakeProc(2, "bash.exe", ppid=3),  # hop 1
            3: self._FakeProc(3, "claude.exe", ppid=4, create_time=42.0),  # hop 2 — last examined
        }
        self._chain_process_factory(procs, monkeypatch)

        result, reason = core._find_windows_claude_ancestor(1, max_depth=cap)
        assert result == (3, 42.0)
        assert reason == "walk-hit:2"

    def test_access_denied_on_construction_terminates_with_depth(self, monkeypatch):
        # psutil.Process(pid) itself can raise AccessDenied (Windows does an
        # identity check on construction) -- a failed construction yields no
        # proc object at all, so there is no verified .ppid() link to climb;
        # this must terminate the walk (not skip) and still carry depth.
        def _fake_process(pid=None):
            if pid == 2:
                raise psutil.AccessDenied(pid)
            return procs[pid]

        procs = {
            1: self._FakeProc(1, "bash.exe", ppid=2),
            3: self._FakeProc(3, "claude.exe", ppid=4, create_time=42.0),
        }
        monkeypatch.setattr(psutil, "Process", _fake_process)

        result, reason = core._find_windows_claude_ancestor(1)
        assert result is None
        assert reason == "walk-miss:rung-unreadable:AccessDenied:1"

    def test_access_denied_rung_is_skipped_and_walk_continues(self, monkeypatch):
        # pid 2's name() raises AccessDenied but its ppid() link is intact
        # -> C2 steps over it and finds claude at pid 3.
        procs = {
            1: self._FakeProc(1, "bash.exe", ppid=2),
            2: self._FakeProc(
                2, "bash.exe", ppid=3, raise_on=("name",), name_exc=psutil.AccessDenied
            ),
            3: self._FakeProc(3, "claude.exe", ppid=4, create_time=42.0),
        }
        self._chain_process_factory(procs, monkeypatch)

        result, reason = core._find_windows_claude_ancestor(1)
        assert result == (3, 42.0)
        assert reason == "walk-hit:2+skipped:AccessDenied:1"

    def test_zombie_process_rung_is_skipped_and_walk_continues(self, monkeypatch):
        procs = {
            1: self._FakeProc(1, "bash.exe", ppid=2),
            2: self._FakeProc(
                2, "bash.exe", ppid=3, raise_on=("name",), name_exc=psutil.ZombieProcess
            ),
            3: self._FakeProc(3, "claude.exe", ppid=4, create_time=42.0),
        }
        self._chain_process_factory(procs, monkeypatch)

        result, reason = core._find_windows_claude_ancestor(1)
        assert result == (3, 42.0)
        assert reason == "walk-hit:2+skipped:ZombieProcess:1"

    def test_ppid_raises_inside_skip_branch_is_distinct_from_no_parent(self, monkeypatch):
        # Review: coordinator:code-reviewer P2 — pid 2's name() raises
        # AccessDenied (skip-eligible), and its ppid() ALSO raises
        # (AccessDenied). The skip branch's own except-clause must produce
        # the same "walk-miss:rung-unreadable:<exc>:<depth>" shape the main
        # (non-skip) path produces on an identical ppid() failure, not
        # collapse it into the falsy/self-referential "walk-miss:no-parent"
        # code used elsewhere in this same branch.
        procs = {
            1: self._FakeProc(1, "bash.exe", ppid=2),
            2: self._FakeProc(
                2,
                "bash.exe",
                ppid=3,
                raise_on=("name", "ppid"),
                name_exc=psutil.AccessDenied,
            ),
        }
        self._chain_process_factory(procs, monkeypatch)

        result, reason = core._find_windows_claude_ancestor(1)
        assert result is None
        assert reason == "walk-miss:rung-unreadable:AccessDenied:1"

    def test_multiple_skipped_rungs_all_annotated_before_match(self, monkeypatch):
        procs = {
            1: self._FakeProc(1, "bash.exe", ppid=2),
            2: self._FakeProc(
                2, "bash.exe", ppid=3, raise_on=("name",), name_exc=psutil.AccessDenied
            ),
            3: self._FakeProc(
                3, "bash.exe", ppid=4, raise_on=("name",), name_exc=psutil.ZombieProcess
            ),
            4: self._FakeProc(4, "claude.exe", ppid=5, create_time=42.0),
        }
        self._chain_process_factory(procs, monkeypatch)

        result, reason = core._find_windows_claude_ancestor(1)
        assert result == (4, 42.0)
        assert reason == (
            "walk-hit:3+skipped:AccessDenied:1+skipped:ZombieProcess:2"
        )

    def test_skipped_rung_consumes_depth_and_cap_still_honoured(self, monkeypatch):
        # Cap of 2: hop 0 is a skipped rung, hop 1 examines claude (out of
        # range at cap=2 -> claude sits at hop index max_depth, never
        # examined) so the walk must miss, not find claude by treating the
        # skip as free.
        cap = 2
        procs = {
            1: self._FakeProc(
                1, "bash.exe", ppid=2, raise_on=("name",), name_exc=psutil.AccessDenied
            ),
            2: self._FakeProc(2, "bash.exe", ppid=3),
            3: self._FakeProc(3, "claude.exe", ppid=4, create_time=42.0),
        }
        self._chain_process_factory(procs, monkeypatch)

        result, reason = core._find_windows_claude_ancestor(1, max_depth=cap)
        assert result is None
        assert reason == "walk-miss:depth-exhausted"

    def test_skipped_rungs_preserved_when_hit_rungs_create_time_raises(self, monkeypatch):
        # Review: coordinator:code-reviewer P3 — a rung was skipped on the
        # way to the eventual "claude" match, but that match's own
        # create_time() then raises. The accumulated "+skipped:..."
        # annotation must survive onto the resulting walk-miss reason
        # instead of being silently dropped.
        procs = {
            1: self._FakeProc(1, "bash.exe", ppid=2),
            2: self._FakeProc(
                2, "bash.exe", ppid=3, raise_on=("name",), name_exc=psutil.AccessDenied
            ),
            3: self._FakeProc(3, "claude.exe", ppid=4, raise_on=("create_time",)),
        }
        self._chain_process_factory(procs, monkeypatch)

        result, reason = core._find_windows_claude_ancestor(1)
        assert result is None
        assert reason == "walk-miss:rung-unreadable:NoSuchProcess:2+skipped:AccessDenied:1"

    def test_no_such_process_on_name_read_still_terminates_walk(self, monkeypatch):
        # NoSuchProcess is never skipped, even on the name read.
        procs = {
            1: self._FakeProc(1, "bash.exe", ppid=2),
            2: self._FakeProc(
                2, "bash.exe", ppid=3, raise_on=("name",), name_exc=psutil.NoSuchProcess
            ),
            3: self._FakeProc(3, "claude.exe", ppid=4, create_time=42.0),
        }
        self._chain_process_factory(procs, monkeypatch)

        result, reason = core._find_windows_claude_ancestor(1)
        assert result is None
        assert reason == "walk-miss:rung-unreadable:NoSuchProcess:1"

    def test_skip_that_runs_out_of_depth_lands_on_depth_exhausted(self, monkeypatch):
        cap = 3
        procs = {
            1: self._FakeProc(1, "bash.exe", ppid=2),
            2: self._FakeProc(
                2, "bash.exe", ppid=3, raise_on=("name",), name_exc=psutil.AccessDenied
            ),
            3: self._FakeProc(
                3, "bash.exe", ppid=4, raise_on=("name",), name_exc=psutil.ZombieProcess
            ),
            4: self._FakeProc(4, "claude.exe", ppid=5, create_time=42.0),
        }
        self._chain_process_factory(procs, monkeypatch)

        result, reason = core._find_windows_claude_ancestor(1, max_depth=cap)
        assert result is None
        assert reason == "walk-miss:depth-exhausted"


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("psutil") is None,
    reason="psutil (the Windows liveness mechanism) not installed",
)
class TestResolveClaudePidFromEnv:
    """Pins the CLAUDE_PID-preferred Guard-1 source (core.py:
    _resolve_claude_pid_from_env) — the harness-exported PID of the
    session's own claude.exe, tried ahead of the bounded ancestor walk. Same
    ``_FakeProc``/comm-verify idiom as TestFindWindowsClaudeAncestor."""

    class _FakeProc(TestFindWindowsClaudeAncestor._FakeProc):
        pass

    def _process_factory(self, procs_by_pid, monkeypatch):
        def _fake_process(pid=None):
            try:
                return procs_by_pid[pid]
            except KeyError:
                raise psutil.NoSuchProcess(pid)

        monkeypatch.setattr(psutil, "Process", _fake_process)

    def test_comm_verifying_claude_pid_resolves(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PID", "42")
        procs = {42: self._FakeProc(42, "claude.exe", create_time=777.0)}
        self._process_factory(procs, monkeypatch)

        result, reason = core._resolve_claude_pid_from_env()
        assert result == (42, 777.0)
        assert reason == "env-hit"

    def test_non_claude_pid_returns_none(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PID", "42")
        procs = {42: self._FakeProc(42, "bash.exe", create_time=777.0)}
        self._process_factory(procs, monkeypatch)

        result, reason = core._resolve_claude_pid_from_env()
        assert result is None
        assert reason == "env-miss:name-mismatch"

    def test_dead_pid_returns_none(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PID", "99999")
        self._process_factory({}, monkeypatch)

        result, reason = core._resolve_claude_pid_from_env()
        assert result is None
        assert reason == "env-miss:NoSuchProcess"

    @pytest.mark.parametrize(
        "raw, expected_reason",
        [
            ("", "env-miss:absent"),
            ("not-an-int", "env-miss:non-integer"),
            ("  ", "env-miss:non-integer"),
        ],
    )
    def test_unset_or_non_integer_returns_none(self, monkeypatch, raw, expected_reason):
        if raw == "":
            monkeypatch.delenv("CLAUDE_PID", raising=False)
        else:
            monkeypatch.setenv("CLAUDE_PID", raw)

        result, reason = core._resolve_claude_pid_from_env()
        assert result is None
        assert reason == expected_reason

    def test_non_positive_pid_returns_none(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PID", "-5")
        result, reason = core._resolve_claude_pid_from_env()
        assert result is None
        assert reason == "env-miss:non-positive"

    def test_psutil_absent_returns_none(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PID", "42")
        monkeypatch.setattr(core, "psutil", None)
        result, reason = core._resolve_claude_pid_from_env()
        assert result is None
        assert reason == "psutil-absent"


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("psutil") is None,
    reason="psutil (the Windows liveness mechanism) not installed",
)
class TestInitWindowsGuard1PreferenceOrder:
    """Pins init()'s Windows Guard-1 preference order: CLAUDE_PID first,
    the bounded ancestor walk only as fallback. Uses
    TestInitWindowsGuard1._make_repo's git-init helper via composition."""

    def _make_repo(self, tmp_path):
        return TestInitWindowsGuard1._make_repo(self, tmp_path)

    def test_claude_pid_set_and_verified_skips_the_walk(self, tmp_path, monkeypatch):
        repo = self._make_repo(tmp_path)
        monkeypatch.setattr(core, "_IS_WINDOWS", True)
        monkeypatch.setenv("CLAUDE_PID", "4242")

        procs = {4242: TestFindWindowsClaudeAncestor._FakeProc(4242, "claude.exe", create_time=321.0)}

        def _fake_process(pid=None):
            try:
                return procs[pid]
            except KeyError:
                raise psutil.NoSuchProcess(pid)

        monkeypatch.setattr(psutil, "Process", _fake_process)

        def _walk_should_not_run(*args, **kwargs):
            raise AssertionError("ancestor walk must not run when CLAUDE_PID resolves")

        monkeypatch.setattr(core, "_find_windows_claude_ancestor", _walk_should_not_run)

        ok = core.init("test-sid-claude-pid-preferred", goal="x", cwd=str(repo))
        assert ok is True
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "test-sid-claude-pid-preferred"
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["stable_pid"] == "4242"
        assert meta["stable_pid_start_epoch"] == "321"
        assert meta["stable_pid_lstart"] == "321"

    def test_claude_pid_naming_non_claude_falls_back_to_walk(self, tmp_path, monkeypatch):
        repo = self._make_repo(tmp_path)
        monkeypatch.setattr(core, "_IS_WINDOWS", True)
        monkeypatch.setenv("CLAUDE_PID", "4242")

        procs = {
            4242: TestFindWindowsClaudeAncestor._FakeProc(4242, "bash.exe", create_time=321.0),
        }

        def _fake_process(pid=None):
            try:
                return procs[pid]
            except KeyError:
                raise psutil.NoSuchProcess(pid)

        monkeypatch.setattr(psutil, "Process", _fake_process)
        monkeypatch.setattr(
            core, "_find_windows_claude_ancestor", lambda ppid: ((5555, 654.0), "walk-hit:0")
        )

        ok = core.init("test-sid-claude-pid-nonmatch-falls-back", goal="x", cwd=str(repo))
        assert ok is True
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "test-sid-claude-pid-nonmatch-falls-back"
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["stable_pid"] == "5555"
        assert meta["stable_pid_start_epoch"] == "654"

    def test_claude_pid_dead_falls_back_to_walk(self, tmp_path, monkeypatch):
        repo = self._make_repo(tmp_path)
        monkeypatch.setattr(core, "_IS_WINDOWS", True)
        monkeypatch.setenv("CLAUDE_PID", "99999")

        def _fake_process(pid=None):
            raise psutil.NoSuchProcess(pid)

        monkeypatch.setattr(psutil, "Process", _fake_process)
        monkeypatch.setattr(
            core, "_find_windows_claude_ancestor", lambda ppid: ((5556, 111.0), "walk-hit:0")
        )

        ok = core.init("test-sid-claude-pid-dead-falls-back", goal="x", cwd=str(repo))
        assert ok is True
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "test-sid-claude-pid-dead-falls-back"
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["stable_pid"] == "5556"
        assert meta["stable_pid_start_epoch"] == "111"

    @pytest.mark.parametrize("raw", [None, "", "not-an-int"])
    def test_claude_pid_unset_empty_or_garbage_falls_back_to_walk(
        self, tmp_path, monkeypatch, raw
    ):
        repo = self._make_repo(tmp_path)
        monkeypatch.setattr(core, "_IS_WINDOWS", True)
        if raw is None:
            monkeypatch.delenv("CLAUDE_PID", raising=False)
        else:
            monkeypatch.setenv("CLAUDE_PID", raw)

        monkeypatch.setattr(
            core, "_find_windows_claude_ancestor", lambda ppid: ((5557, 222.0), "walk-hit:0")
        )

        ok = core.init(
            f"test-sid-claude-pid-garbage-{raw!r}-falls-back", goal="x", cwd=str(repo)
        )
        assert ok is True
        sdir = (
            Path(repo)
            / ".git"
            / "coordinator-sessions"
            / f"test-sid-claude-pid-garbage-{raw!r}-falls-back"
        )
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["stable_pid"] == "5557"
        assert meta["stable_pid_start_epoch"] == "222"

    def test_neither_source_resolves_leaves_stable_pid_empty(self, tmp_path, monkeypatch):
        repo = self._make_repo(tmp_path)
        monkeypatch.setattr(core, "_IS_WINDOWS", True)
        monkeypatch.delenv("CLAUDE_PID", raising=False)

        monkeypatch.setattr(
            core, "_find_windows_claude_ancestor", lambda ppid: (None, "walk-miss:depth-exhausted")
        )

        ok = core.init("test-sid-claude-pid-neither-resolves", goal="x", cwd=str(repo))
        assert ok is True
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "test-sid-claude-pid-neither-resolves"
        meta = json.loads((sdir / "meta.json").read_text())
        assert "stable_pid" not in meta
        assert meta["stable_pid_capture"] == "env-miss:absent|walk-miss:depth-exhausted"


# ---------------------------------------------------------------------------
# stable_pid_capture breadcrumb (AC1-AC4, AC10, AC11 — stable-pid-capture-
# breadcrumb-and-liveness-basis plan § C1).
# ---------------------------------------------------------------------------


class TestStablePidCaptureBreadcrumb:
    def _make_repo(self, tmp_path):
        return TestInitWindowsGuard1._make_repo(self, tmp_path)

    # -- Windows -------------------------------------------------------

    @pytest.mark.skipif(
        __import__("importlib").util.find_spec("psutil") is None,
        reason="psutil (the Windows liveness mechanism) not installed",
    )
    def test_windows_env_hit_records_env_hit(self, tmp_path, monkeypatch):
        import psutil

        repo = self._make_repo(tmp_path)
        monkeypatch.setattr(core, "_IS_WINDOWS", True)
        monkeypatch.setenv("CLAUDE_PID", "4242")
        procs = {4242: TestFindWindowsClaudeAncestor._FakeProc(4242, "claude.exe", create_time=321.0)}

        def _fake_process(pid=None):
            try:
                return procs[pid]
            except KeyError:
                raise psutil.NoSuchProcess(pid)

        monkeypatch.setattr(psutil, "Process", _fake_process)

        ok = core.init("bc-win-env-hit", goal="x", cwd=str(repo))
        assert ok is True
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "bc-win-env-hit"
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["stable_pid_capture"] == "env-hit"

    @pytest.mark.skipif(
        __import__("importlib").util.find_spec("psutil") is None,
        reason="psutil (the Windows liveness mechanism) not installed",
    )
    def test_windows_walk_hit_falls_back_and_records_walk_reason(self, tmp_path, monkeypatch):
        """AC2 regression: when leg (a) misses and leg (b) hits, BOTH legs
        ran and both reasons must be recorded — not just the walk's — so
        "env-miss:absent rescued by the walk" stays distinguishable from
        "env-hit" outright (the exact distinction the plan's version-floor
        hypothesis needs)."""
        repo = self._make_repo(tmp_path)
        monkeypatch.setattr(core, "_IS_WINDOWS", True)
        monkeypatch.delenv("CLAUDE_PID", raising=False)
        monkeypatch.setattr(
            core, "_find_windows_claude_ancestor", lambda ppid: ((9001, 42.0), "walk-hit:3")
        )

        ok = core.init("bc-win-walk-hit", goal="x", cwd=str(repo))
        assert ok is True
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "bc-win-walk-hit"
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["stable_pid_capture"] == "env-miss:absent|walk-hit:3"
        assert meta["stable_pid"] == "9001"

    @pytest.mark.skipif(
        __import__("importlib").util.find_spec("psutil") is None,
        reason="psutil (the Windows liveness mechanism) not installed",
    )
    def test_windows_both_legs_fail_records_both_families(self, tmp_path, monkeypatch):
        repo = self._make_repo(tmp_path)
        monkeypatch.setattr(core, "_IS_WINDOWS", True)
        monkeypatch.delenv("CLAUDE_PID", raising=False)
        monkeypatch.setattr(
            core,
            "_find_windows_claude_ancestor",
            lambda ppid: (None, "walk-miss:rung-unreadable:AccessDenied:2"),
        )

        ok = core.init("bc-win-both-miss", goal="x", cwd=str(repo))
        assert ok is True
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "bc-win-both-miss"
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["stable_pid_capture"] == (
            "env-miss:absent|walk-miss:rung-unreadable:AccessDenied:2"
        )
        assert "stable_pid" not in meta

    def test_windows_psutil_absent_records_psutil_absent(self, tmp_path, monkeypatch):
        repo = self._make_repo(tmp_path)
        monkeypatch.setattr(core, "_IS_WINDOWS", True)
        monkeypatch.setattr(core, "psutil", None)

        ok = core.init("bc-win-psutil-absent", goal="x", cwd=str(repo))
        assert ok is True
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "bc-win-psutil-absent"
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["stable_pid_capture"] == "psutil-absent"
        assert "stable_pid" not in meta

    @pytest.mark.skipif(
        __import__("importlib").util.find_spec("psutil") is None,
        reason="psutil (the Windows liveness mechanism) not installed",
    )
    def test_windows_unexpected_exception_in_both_legs_does_not_propagate(
        self, tmp_path, monkeypatch
    ):
        """AC3: an unforeseen exception inside either capture leg must not
        propagate out of init() — the existing `except Exception` around
        each leg call must still catch it, and the reason is recorded with
        the raising exception's own class name rather than the vocabulary's
        curated psutil-exception names (a residual/'unknown' shape is
        acceptable; a raised exception out of init() is not)."""
        repo = self._make_repo(tmp_path)
        monkeypatch.setattr(core, "_IS_WINDOWS", True)
        monkeypatch.delenv("CLAUDE_PID", raising=False)

        def _raise_env(*a, **k):
            raise RuntimeError("boom-env")

        def _raise_walk(*a, **k):
            raise RuntimeError("boom-walk")

        monkeypatch.setattr(core, "_resolve_claude_pid_from_env", _raise_env)
        monkeypatch.setattr(core, "_find_windows_claude_ancestor", _raise_walk)

        ok = core.init("bc-win-unexpected-exc", goal="x", cwd=str(repo))
        assert ok is True  # init() must not raise
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "bc-win-unexpected-exc"
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["stable_pid_capture"] == "env-miss:RuntimeError|walk-miss:RuntimeError"
        assert "stable_pid" not in meta

    # -- POSIX -----------------------------------------------------------

    @pytest.mark.skipif(
        __import__("importlib").util.find_spec("psutil") is None,
        reason="psutil (the POSIX liveness mechanism) not installed",
    )
    def test_posix_parent_hit(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)
        monkeypatch.setattr(core, "_IS_WINDOWS", False)

        class HitParent:
            def __init__(self, pid):
                pass

            def name(self):
                return "claude"

            def create_time(self):
                return 222.0

        monkeypatch.setattr(core._psutil(), "Process", HitParent)

        ok = core.init("bc-posix-hit", cwd=str(repo))
        assert ok is True
        sdir = Path(core.sessions_dir(cwd=str(repo))) / "bc-posix-hit"
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["stable_pid_capture"] == "posix-parent-hit"
        assert meta["stable_pid"] == str(os.getppid())

    @pytest.mark.skipif(
        __import__("importlib").util.find_spec("psutil") is None,
        reason="psutil (the POSIX liveness mechanism) not installed",
    )
    def test_posix_parent_miss_name_mismatch(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)
        monkeypatch.setattr(core, "_IS_WINDOWS", False)

        class MissParent:
            def __init__(self, pid):
                pass

            def name(self):
                return "bash"

            def create_time(self):
                return 111.0

        monkeypatch.setattr(core._psutil(), "Process", MissParent)

        ok = core.init("bc-posix-name-mismatch", cwd=str(repo))
        assert ok is True
        sdir = Path(core.sessions_dir(cwd=str(repo))) / "bc-posix-name-mismatch"
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["stable_pid_capture"] == "posix-parent-miss:name-mismatch"
        assert "stable_pid" not in meta

    @pytest.mark.skipif(
        __import__("importlib").util.find_spec("psutil") is None,
        reason="psutil (the POSIX liveness mechanism) not installed",
    )
    def test_posix_parent_miss_psutil_exception(self, tmp_path, monkeypatch):
        import psutil

        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)
        monkeypatch.setattr(core, "_IS_WINDOWS", False)

        def _raise(pid):
            raise psutil.AccessDenied(pid)

        monkeypatch.setattr(core._psutil(), "Process", _raise)

        ok = core.init("bc-posix-access-denied", cwd=str(repo))
        assert ok is True
        sdir = Path(core.sessions_dir(cwd=str(repo))) / "bc-posix-access-denied"
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["stable_pid_capture"] == "posix-parent-miss:AccessDenied"
        assert "stable_pid" not in meta

    @pytest.mark.skipif(
        __import__("importlib").util.find_spec("psutil") is None,
        reason="psutil (the POSIX liveness mechanism) not installed",
    )
    def test_posix_parent_miss_no_create_time(self, tmp_path, monkeypatch):
        """Name resolves to "claude" but create_time() reads None — the
        residual family member distinct from a raised exception and from a
        name mismatch."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)
        monkeypatch.setattr(core, "_IS_WINDOWS", False)

        class NoCreateTimeParent:
            def __init__(self, pid):
                pass

            def name(self):
                return "claude"

            def create_time(self):
                return None

        monkeypatch.setattr(core._psutil(), "Process", NoCreateTimeParent)

        ok = core.init("bc-posix-no-create-time", cwd=str(repo))
        assert ok is True
        sdir = Path(core.sessions_dir(cwd=str(repo))) / "bc-posix-no-create-time"
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["stable_pid_capture"] == "posix-parent-miss:no-create-time"
        assert "stable_pid" not in meta

    # -- refresh path (AC4, AC10) -----------------------------------------

    @pytest.mark.skipif(
        __import__("importlib").util.find_spec("psutil") is None,
        reason="psutil (the POSIX liveness mechanism) not installed",
    )
    def test_refresh_updates_breadcrumb_from_miss_to_hit(self, tmp_path, monkeypatch):
        """AC4: a session that starts failing and later succeeds must not
        read a stale breadcrumb — the refresh path must overwrite it."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)
        monkeypatch.setattr(core, "_IS_WINDOWS", False)
        sid = "bc-posix-refresh-transition"

        class MissParent:
            def __init__(self, pid):
                pass

            def name(self):
                return "bash"

            def create_time(self):
                return 111.0

        monkeypatch.setattr(core._psutil(), "Process", MissParent)
        assert core.init(sid, cwd=str(repo)) is True
        sdir = Path(core.sessions_dir(cwd=str(repo))) / sid
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["stable_pid_capture"] == "posix-parent-miss:name-mismatch"
        assert "stable_pid" not in meta

        class HitParent:
            def __init__(self, pid):
                pass

            def name(self):
                return "claude"

            def create_time(self):
                return 222.0

        monkeypatch.setattr(core._psutil(), "Process", HitParent)
        assert core.init(sid, cwd=str(repo)) is True
        meta2 = json.loads((sdir / "meta.json").read_text())
        assert meta2["stable_pid_capture"] == "posix-parent-hit"
        assert meta2["stable_pid"] == str(os.getppid())

    def test_create_path_writes_stable_pid_capture_field(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)
        monkeypatch.setattr(core, "_IS_WINDOWS", True)
        monkeypatch.setattr(core, "psutil", None)

        ok = core.init("bc-create-path-field", cwd=str(repo))
        assert ok is True
        sdir = Path(core.sessions_dir(cwd=str(repo))) / "bc-create-path-field"
        meta = json.loads((sdir / "meta.json").read_text())
        assert "stable_pid_capture" in meta  # AC1: present from FIRST write

    def test_refresh_path_performs_exactly_one_meta_json_rewrite(self, tmp_path, monkeypatch):
        """AC10: the refresh path must fold the breadcrumb into the
        EXISTING meta.json rewrite, not add a sequential one — assert it by
        counting os.replace calls (the atomic-rewrite commit point shared
        by update_meta_field/update_meta_fields) during the refresh call
        only (the create-path write above is excluded from the count)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)
        sid = "bc-refresh-one-write"
        assert core.init(sid, goal="g", cwd=str(repo)) is True  # create path

        real_replace = os.replace
        calls = []

        def counting_replace(src, dst):
            calls.append((src, dst))
            return real_replace(src, dst)

        monkeypatch.setattr(core.os, "replace", counting_replace)

        assert core.init(sid, goal="g", cwd=str(repo)) is True  # refresh path
        assert len(calls) == 1, (
            f"expected exactly one meta.json rewrite on the refresh path, "
            f"got {len(calls)}: {calls}"
        )


# ---------------------------------------------------------------------------
# update_meta_fields — batch sibling of update_meta_field (added for AC10).
# ---------------------------------------------------------------------------


class TestUpdateMetaFields:
    def test_batches_multiple_fields_in_one_write(self, tmp_path, monkeypatch):
        (tmp_path / "meta.json").write_text(json.dumps({"session_id": "x"}))

        real_replace = os.replace
        calls = []

        def counting_replace(src, dst):
            calls.append((src, dst))
            return real_replace(src, dst)

        monkeypatch.setattr(core.os, "replace", counting_replace)

        assert core.update_meta_fields(
            str(tmp_path), {"a": "1", "b": "2", "c": 3}
        ) is True
        assert len(calls) == 1

        data = json.loads((tmp_path / "meta.json").read_text())
        assert data == {"session_id": "x", "a": "1", "b": "2", "c": "3"}

    def test_empty_fields_is_a_noop_returns_false(self, tmp_path):
        (tmp_path / "meta.json").write_text(json.dumps({"session_id": "x"}))
        assert core.update_meta_fields(str(tmp_path), {}) is False
        data = json.loads((tmp_path / "meta.json").read_text())
        assert data == {"session_id": "x"}  # untouched

    def test_missing_meta_json_returns_false(self, tmp_path):
        assert core.update_meta_fields(str(tmp_path), {"a": "1"}) is False

    def test_any_empty_value_rejects_the_whole_call(self, tmp_path):
        (tmp_path / "meta.json").write_text(json.dumps({"session_id": "x"}))
        with pytest.raises(ValueError):
            core.update_meta_fields(str(tmp_path), {"a": "1", "b": ""})
        # Whole call rejected -- "a" must NOT have been written either.
        data = json.loads((tmp_path / "meta.json").read_text())
        assert data == {"session_id": "x"}

    def test_non_string_values_coerced(self, tmp_path):
        (tmp_path / "meta.json").write_text(json.dumps({"session_id": "x"}))
        core.update_meta_fields(str(tmp_path), {"pid": 12345, "flag": True})
        data = json.loads((tmp_path / "meta.json").read_text())
        assert data["pid"] == "12345"
        assert isinstance(data["pid"], str)
        assert data["flag"] == "True"

    def test_atomic_no_tmp_leftovers(self, tmp_path):
        (tmp_path / "meta.json").write_text(json.dumps({"session_id": "x"}))
        core.update_meta_fields(str(tmp_path), {"a": "1", "b": "2"})
        leftovers = [p for p in tmp_path.iterdir() if p.name != "meta.json"]
        assert leftovers == []

    def test_update_meta_field_still_works_unmodified(self, tmp_path):
        """update_meta_field itself must be unchanged -- it still has other
        callers (AC10 dispatch brief: keep it, do not delete/re-signature)."""
        (tmp_path / "meta.json").write_text(json.dumps({"session_id": "x", "goal": ""}))
        assert core.update_meta_field(str(tmp_path), "goal", "ship it") is True
        assert core.read_meta_field(str(tmp_path), "goal") == "ship it"
