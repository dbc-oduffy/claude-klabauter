"""
coordinator_core.session.tests.test_liveness — parity tests for
coordinator_core.session.liveness.

Port of: liveness.sh (DoE 6aa77d4b, 2026-07-21).

Oracle bash functions cited per test class:
  - is_session_live       -> _cs_is_session_live
  - session_live          -> _cs_session_live
  - claim_holder_live     -> _cs_claim_holder_live
  - claim_held_by_me      -> _cs_claim_held_by_me
  - live_session_ids      -> cs_live_session_ids (Q24)
  - active_sessions       -> cs_active_sessions

Q20 (golden-diff): the stable-pid Layer-1 path golden-diffs against the REAL
running test process (its own ps -o lstart=) and against the real on-disk
meta.json corpus glob — see TestSessionLiveGoldenDiff and
TestLiveSessionIdsCorpus.

TestSingleLivenessKeyConvergence pins the single-liveness-key invariant (D5,
pcore-03): every consumer (claim_holder_live / live_session_ids /
active_sessions) converges on the ONE session_live decision for a shared
fixture set. TestWindowsCreateTimePath exercises the Windows create_time()
liveness path on POSIX via a platform-seam monkeypatch (psutil's create_time
is cross-platform).

NEGATIVE-SPEC (2026-07-22 parity-retire-fold,
state/review-trail/findings/2026-07-22-parity-retire-fold-plan.md § 7 C1):
this module absorbed the former test_liveness_parity.py, which cross-checked
this module's session_live against the DoE bash oracle
(coordinator/lib/session/liveness.sh's _cs_session_live). That oracle was
retired at 6aa77d4b ("A2-a: delete liveness.sh, cut over coordinator-session.sh
to session-liveness-cli") and its trampoline shell deleted outright at
e34f2484 ("C4a: delete coordinator-session.sh trio"). Do NOT repoint any test
here at session-liveness-cli — it is a pure trampoline into THIS module's own
coordinator_core.session.liveness with no surviving independent
implementation; comparing against it would be self-comparison dressed up as
parity, strictly worse than no cross-check at all (a retired-oracle skip at
least announces its own absence). The two fixtures that carried real residual
value (a recycled-PID-epoch-mismatch case and the 2026-06-23 dead-`pid`-field
trap) were folded into TestSessionLive verbatim as
test_layer1_recycled_pid_epoch_mismatch_is_dead and
test_dead_pid_trap_live_pid_field_never_rescues_stale_session; the
cross-implementation agreement assertion itself (```py_live == bash_live```)
had no oracle left to agree with and was retired, not repointed.

Recipe: scratch/subagent-sandbox/bash-to-python-engine-migration/
recipe-t4a-coordinator-session-hub.md § liveness.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core.session import core, liveness, scope, touch_record
from coordinator_core.session import harness_registry
from coordinator_core.session import holder_evidence as holder_evidence_mod
from coordinator_core.win_portability import (
    no_console_creationflags,
    no_console_passthrough_kwargs,
)

# MOST tests in this file build their repo via `_make_repo(tmp_path)`, spawning
# real git (init/config/add/commit) because the production code under test --
# `core.git_root()` and liveness's session-hub resolution -- reads real git
# state that no mock stands in for. `tmp_path` is function-scoped and tests
# write session state under reused session ids, so the repo fixture stays
# per-test rather than hoisted to module scope. The spawn ratchet's
# `_BASELINE` is shrink-only pre-existing residue and is explicitly not the
# route for this file -- coordinator_core/tests/test_no_new_spawning_tests.py
# Rule 2.
#
# NOT every test, and the exception is load-bearing. `TestLiveSessionIdsCorpus`
# and the Q20 golden-diff path read the REAL on-disk registry
# (`.git/coordinator-sessions/`) on purpose -- see that class's own comment.
# This comment previously claimed the `_make_repo(tmp_path)` isolation held for
# every test in the file, contradicting that class's stated intent one screen
# below. The isolation was documented rather than enforced, and the contradiction
# is what let the following go unnoticed:
#
#   A hub-reading test is not isolated from the ~24 peer sessions mutating the
#   same hub, so it cannot distinguish its own writes from a peer's. Six
#   identical runs of this file on a fixed tree produced 2, 4, 4, 4, 5, 4
#   failures -- five distinct outcomes, nothing changed between them. Inside
#   that noise `test_every_non_uuid_real_child_is_denylisted_or_a_file` was red
#   6/6 on a REAL defect (fixture dirs enumerated as phantom sessions), and the
#   flake is why it read as noise for as long as it did.
#
# CONSEQUENCE, and it binds anything citing this file: a green run of
# test_liveness.py is not evidence. An AC or gate resting on this module must
# name WHICH tests it means. "test_liveness.py passes" is an unsupported claim
# for the hub-reading tests no matter how many times it is observed.
#
# The fix for the hub-reading tests is a fixture hub, which is test-architecture
# work with its own scope, filed at
# state/bug-backlog/2026-08-26-test-fixtures-reached-the-live-session-h-a36a3bf35bc0.yaml.
# It is deliberately NOT attempted here: correcting a false comment is cheap and
# honest, and rewriting four tests' isolation model mid-execution is not.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture(autouse=True)
def _reset_registry_snapshot_cache():
    """Review: staff-eng F5 — `session_live`'s per-process registry-snapshot
    memoization (see `liveness._cached_registry_lookup`) must not leak a
    dict built against one test's `monkeypatch`-ed `registry_dir()` into the
    next test sharing this process. Reset before AND after each test so
    ordering within this file/session never matters."""
    liveness._registry_snapshot_cache = None
    yield
    liveness._registry_snapshot_cache = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, **no_console_passthrough_kwargs())
    return tmp_path


def _write_session(repo, sid, meta: dict):
    sdir = Path(repo) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return sdir


def _session_dir_path(repo, sid):
    return Path(repo) / ".git" / "coordinator-sessions" / sid


def _touch(path, epoch):
    os.utime(path, (epoch, epoch))


def _self_lstart():
    result = subprocess.run(
        ["ps", "-p", str(os.getpid()), "-o", "lstart="],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    lstart = result.stdout.strip()
    assert lstart, "ps -p <self> -o lstart= must succeed on a live test process"
    return lstart


# ---------------------------------------------------------------------------
# is_session_live — pure recency gate
# ---------------------------------------------------------------------------


class TestIsSessionLive:
    def test_live_under_thirty_min(self):
        assert liveness.is_session_live("dummy-pid", 0) is True
        assert liveness.is_session_live("dummy-pid", 60) is True
        assert liveness.is_session_live("dummy-pid", 30 * 60 - 1) is True

    def test_stale_at_or_over_thirty_min(self):
        assert liveness.is_session_live("dummy-pid", 30 * 60) is False
        assert liveness.is_session_live("dummy-pid", 30 * 60 + 1) is False

    def test_non_numeric_elapsed_not_live(self):
        assert liveness.is_session_live("pid", "not-a-number") is False
        assert liveness.is_session_live("pid", "") is False

    def test_negative_elapsed_not_live(self):
        # str(-5) == "-5" fails ^[0-9]+$ -> not live (matches bash regex).
        assert liveness.is_session_live("pid", -5) is False

    def test_pid_is_diagnostic_only_never_gates(self):
        # An obviously-dead pid must NOT change the verdict — recency alone.
        assert liveness.is_session_live(2**31 - 1, 0) is True
        assert liveness.is_session_live(os.getpid(), 30 * 60) is False

    def test_string_numeric_elapsed_accepted(self):
        assert liveness.is_session_live("pid", "100") is True
        assert liveness.is_session_live("pid", str(30 * 60)) is False


# ---------------------------------------------------------------------------
# session_live — two-layer, O(1) single session
# ---------------------------------------------------------------------------


class TestSessionLive:
    def test_empty_sid_not_live(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert liveness.session_live("", cwd=str(repo)) is False

    def test_missing_dir_not_live(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert liveness.session_live("no-such-session", cwd=str(repo)) is False

    def test_layer2_recent_activity_is_live(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "s-recent", {"pid": "999", "last_activity": core.now_iso()})
        assert liveness.session_live("s-recent", cwd=str(repo)) is True

    def test_layer2_old_activity_is_stale(self, tmp_path):
        repo = _make_repo(tmp_path)
        # 2000-01-01 -> way past 30 min.
        _write_session(
            repo, "s-old", {"pid": "999", "last_activity": "2000-01-01T00:00:00Z"}
        )
        assert liveness.session_live("s-old", cwd=str(repo)) is False

    def test_layer2_unparseable_last_activity_not_live(self, tmp_path):
        repo = _make_repo(tmp_path)
        # iso_to_epoch -> 0 -> elapsed huge -> stale.
        _write_session(
            repo, "s-bad", {"pid": "999", "last_activity": "garbage"}
        )
        assert liveness.session_live("s-bad", cwd=str(repo)) is False

    def test_layer2_negative_elapsed_clamped_to_live(self, tmp_path):
        repo = _make_repo(tmp_path)
        # last_activity far in the FUTURE -> negative elapsed -> clamped 0 -> live.
        # (This is the CLAMP behavior that differs from live_session_ids.)
        _write_session(
            repo, "s-future", {"pid": "999", "last_activity": "2099-01-01T00:00:00Z"}
        )
        assert liveness.session_live("s-future", cwd=str(repo)) is True

    def test_layer1_stable_pid_present_lstart_absent_falls_through_to_layer2(self, tmp_path):
        repo = _make_repo(tmp_path)
        # stable_pid present but lstart absent -> A-F1 fall-through to recency.
        _write_session(
            repo,
            "s-partial",
            {"pid": "999", "stable_pid": "12345", "last_activity": core.now_iso()},
        )
        assert liveness.session_live("s-partial", cwd=str(repo)) is True

    def test_layer1_stable_pid_dead_returns_dead_ignoring_recent_recency(self, tmp_path):
        repo = _make_repo(tmp_path)
        # A dead stable_pid with a bogus lstart -> Layer 1 authoritative DEAD,
        # EVEN THOUGH last_activity is fresh (recency NOT consulted).
        _write_session(
            repo,
            "s-dead-stable",
            {
                "pid": "999",
                "stable_pid": str(2**31 - 1),
                "stable_pid_lstart": "Sat Jan  1 00:00:00 2000",
                "stable_pid_start_epoch": "946684800",
                "last_activity": core.now_iso(),
            },
        )
        assert liveness.session_live("s-dead-stable", cwd=str(repo)) is False

    def test_layer1_exception_fails_open_no_fallthrough_to_layer2(self, tmp_path, monkeypatch):
        # Review: staff-eng-review B. A raise from core.stable_pid_alive in
        # session_live's own Layer-1 arm must fail OPEN (True), matching
        # live_session_verdicts' sibling Layer-1 arm's (True, "unknown",
        # None) exactly -- and must NOT fall through to Layer 2, even though
        # last_activity below is stale enough to read DEAD there.
        repo = _make_repo(tmp_path)
        _write_session(
            repo,
            "s-layer1-boom",
            {
                "pid": "999",
                "last_activity": "2000-01-01T00:00:00Z",
                "stable_pid": "12345",
                "stable_pid_lstart": "Sat Jan  1 00:00:00 2000",
                "stable_pid_start_epoch": "946684800",
            },
        )

        def _boom(*a, **k):
            raise RuntimeError("simulated stable_pid_alive failure")

        monkeypatch.setattr(core, "stable_pid_alive", _boom)
        assert liveness.session_live("s-layer1-boom", cwd=str(repo)) is True

    def test_layer1_exception_fails_open_emits_breadcrumb_without_changing_verdict(
        self, tmp_path, monkeypatch
    ):
        # C4a deliverable (3): the fail-open arm must be DETECTABLE (a
        # process-local counter increments) but must NEVER change the
        # returned verdict -- same True as the plain fail-open test above.
        repo = _make_repo(tmp_path)
        _write_session(
            repo,
            "s-layer1-boom-breadcrumb",
            {
                "pid": "999",
                "last_activity": "2000-01-01T00:00:00Z",
                "stable_pid": "12345",
                "stable_pid_lstart": "Sat Jan  1 00:00:00 2000",
                "stable_pid_start_epoch": "946684800",
            },
        )

        def _boom(*a, **k):
            raise RuntimeError("simulated stable_pid_alive failure")

        monkeypatch.setattr(core, "stable_pid_alive", _boom)
        monkeypatch.setattr(liveness, "layer1_unknown_count", 0)
        before = liveness.layer1_unknown_count
        assert liveness.session_live("s-layer1-boom-breadcrumb", cwd=str(repo)) is True
        assert liveness.layer1_unknown_count == before + 1

    def test_layer1_rollback_lever_env_flag_skips_layer1(self, tmp_path, monkeypatch):
        # C4a deliverable (1): COORDINATOR_SESSION_LAYER1_DISABLE, when
        # truthy, must skip Layer 1 entirely and fall through to Layer 2 --
        # a stable_pid that would resolve DEAD in Layer 1 must not gate the
        # verdict when the lever is set; fresh last_activity makes Layer 2
        # read live instead.
        repo = _make_repo(tmp_path)
        _write_session(
            repo,
            "s-layer1-disabled",
            {
                "pid": "999",
                "stable_pid": str(2**31 - 1),
                "stable_pid_lstart": "Sat Jan  1 00:00:00 2000",
                "stable_pid_start_epoch": "946684800",
                "last_activity": core.now_iso(),
            },
        )
        monkeypatch.setenv("COORDINATOR_SESSION_LAYER1_DISABLE", "1")
        assert liveness.session_live("s-layer1-disabled", cwd=str(repo)) is True

    def test_layer1_rollback_lever_unset_leaves_layer1_engaged(self, tmp_path, monkeypatch):
        # Companion negative case: with the lever unset (default), the same
        # fixture as above reads DEAD via Layer 1 unchanged -- proves the
        # lever is the only thing that changed behavior above, not the
        # fixture.
        repo = _make_repo(tmp_path)
        _write_session(
            repo,
            "s-layer1-enabled",
            {
                "pid": "999",
                "stable_pid": str(2**31 - 1),
                "stable_pid_lstart": "Sat Jan  1 00:00:00 2000",
                "stable_pid_start_epoch": "946684800",
                "last_activity": core.now_iso(),
            },
        )
        monkeypatch.delenv("COORDINATOR_SESSION_LAYER1_DISABLE", raising=False)
        assert liveness.session_live("s-layer1-enabled", cwd=str(repo)) is False

    def test_layer1_epoch_only_witness_no_lstart_reaches_layer1_live(self, tmp_path, monkeypatch):
        """dca0e3e80 regression pin: POSIX init() stopped writing
        stable_pid_lstart 2026-07-27 but still writes
        stable_pid_start_epoch. A record carrying stable_pid +
        stable_pid_start_epoch but NO stable_pid_lstart must still reach
        Layer 1 (core.stable_pid_alive) and return basis "stable-pid", not
        silently fall through to the recency window forever."""
        import psutil
        repo = _make_repo(tmp_path)
        ct = int(psutil.Process(os.getpid()).create_time())
        _write_session(
            repo,
            "s-epoch-only",
            {
                # last_activity deliberately STALE to prove Layer 1 -- not
                # recency -- is what makes this live.
                "pid": "999",
                "last_activity": "2000-01-01T00:00:00Z",
                "stable_pid": str(os.getpid()),
                "stable_pid_start_epoch": str(ct),
                # stable_pid_lstart deliberately absent.
            },
        )
        assert liveness.session_live("s-epoch-only", cwd=str(repo)) is True
        verdicts = liveness.live_session_verdicts(cwd=str(repo))
        live, basis, age = verdicts["s-epoch-only"]
        assert live is True
        assert basis == "stable-pid"

    def test_layer1_epoch_only_witness_recycled_epoch_is_dead(self, tmp_path):
        """Companion negative case: epoch-only witness, but the stored epoch
        does NOT match the live process's create_time() -> Layer 1
        authoritative DEAD, even with fresh last_activity (recency not
        consulted)."""
        repo = _make_repo(tmp_path)
        _write_session(
            repo,
            "s-epoch-only-recycled",
            {
                "pid": "999",
                "last_activity": core.now_iso(),
                "stable_pid": str(os.getpid()),
                "stable_pid_start_epoch": "1",
            },
        )
        assert liveness.session_live("s-epoch-only-recycled", cwd=str(repo)) is False

    def test_layer1_neither_witness_still_falls_through_to_layer2(self, tmp_path):
        """A-F1 survives: stable_pid present, BOTH stable_pid_lstart and
        stable_pid_start_epoch absent -> falls through to the Layer-2
        recency window (unchanged from pre-fix behavior)."""
        repo = _make_repo(tmp_path)
        _write_session(
            repo,
            "s-neither-witness",
            {"pid": "999", "stable_pid": "12345", "last_activity": core.now_iso()},
        )
        assert liveness.session_live("s-neither-witness", cwd=str(repo)) is True
        verdicts = liveness.live_session_verdicts(cwd=str(repo))
        live, basis, age = verdicts["s-neither-witness"]
        assert live is True
        assert basis == "recency-window"

    @pytest.mark.skipif(
        os.name == "nt",
        reason="fixture built via POSIX `ps -o lstart=`; no Windows equivalent (see TestWindowsCreateTimePath in this module for the Windows-side coverage)",
    )
    def test_layer1_recycled_pid_epoch_mismatch_is_dead(self, tmp_path):
        """Folded from the retired test_liveness_parity.py (fixture 2,
        'stable_recycled' — 2026-07-22 parity-retire-fold, see the module
        negative-spec below): stable_pid present + MATCHING lstart string but
        MISMATCHED stable_pid_start_epoch (a recycled PID reusing the same
        pid number) -> DEAD, even though last_activity is FRESH. Proves Layer
        1's epoch check, not just the lstart string, gates liveness."""
        repo = _make_repo(tmp_path)
        lstart = _self_lstart()
        epoch = core.lstart_to_epoch(lstart)
        assert epoch > 0
        _write_session(
            repo,
            "s-recycled",
            {
                "pid": "999",
                "last_activity": core.now_iso(),
                "stable_pid": str(os.getpid()),
                "stable_pid_lstart": lstart,
                "stable_pid_start_epoch": str(epoch + 123456),
            },
        )
        assert liveness.session_live("s-recycled", cwd=str(repo)) is False

    def test_dead_pid_trap_live_pid_field_never_rescues_stale_session(self, tmp_path):
        """Folded from the retired test_liveness_parity.py (fixture 5,
        'dead_pid_trap' — 2026-07-22 parity-retire-fold): explicit 2026-06-23
        regression pin. A LIVE os pid in the `pid` field, stale
        last_activity, NO stable_pid -> the `pid` field must NOT rescue the
        session; the correct verdict is DEAD (session_live only ever gates on
        stable_pid / recency, never on the diagnostic-only `pid` field)."""
        repo = _make_repo(tmp_path)
        _write_session(
            repo,
            "trap",
            {"pid": str(os.getpid()), "last_activity": "2000-01-01T00:00:00Z"},
        )
        assert liveness.session_live("trap", cwd=str(repo)) is False


class TestSessionLiveMetalessRecencyFallback:
    """Wrongful-takeover fallback (DoE 642195ba / 88929bea):
    ``session_live``'s Layer 2 must NOT read a meta-less/unparseable session
    dir as instantly-DEAD by defaulting to epoch-0 recency — a mid-write dir
    (meta.json not yet flushed) is a LIVE session, not a takeable one. The
    fallback substitutes the on-disk mtime as the recency SOURCE only; the
    30-min liveness THRESHOLD is unchanged, so genuinely stale dirs still
    read DEAD."""

    def test_no_meta_json_recent_regular_file_is_live(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _session_dir_path(repo, "s-mid-write")
        sdir.mkdir(parents=True)
        # meta.json not yet written; some OTHER regular file was, recently
        # (e.g. a lockfile / partial artifact dropped before meta.json).
        marker = sdir / "lock"
        marker.write_text("x")
        _touch(marker, core.now_epoch())
        assert liveness.session_live("s-mid-write", cwd=str(repo)) is True

    def test_no_meta_json_all_files_old_is_dead(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _session_dir_path(repo, "s-mid-write-stale")
        sdir.mkdir(parents=True)
        marker = sdir / "lock"
        marker.write_text("x")
        old_epoch = core.now_epoch() - (2 * liveness._THIRTY_MIN)
        _touch(marker, old_epoch)
        _touch(sdir, old_epoch)
        assert liveness.session_live("s-mid-write-stale", cwd=str(repo)) is False

    def test_empty_dir_recent_dir_mtime_is_live(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _session_dir_path(repo, "s-empty-fresh")
        sdir.mkdir(parents=True)
        _touch(sdir, core.now_epoch())
        assert liveness.session_live("s-empty-fresh", cwd=str(repo)) is True

    def test_empty_dir_old_dir_mtime_is_dead(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _session_dir_path(repo, "s-empty-stale")
        sdir.mkdir(parents=True)
        _touch(sdir, core.now_epoch() - (2 * liveness._THIRTY_MIN))
        assert liveness.session_live("s-empty-stale", cwd=str(repo)) is False

    def test_unparseable_meta_json_recent_file_is_live(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _session_dir_path(repo, "s-corrupt-meta")
        sdir.mkdir(parents=True)
        (sdir / "meta.json").write_text("{not valid json", encoding="utf-8")
        _touch(sdir / "meta.json", core.now_epoch())
        assert liveness.session_live("s-corrupt-meta", cwd=str(repo)) is True

    def test_unparseable_meta_json_all_old_is_dead(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _session_dir_path(repo, "s-corrupt-meta-stale")
        sdir.mkdir(parents=True)
        meta = sdir / "meta.json"
        meta.write_text("{not valid json", encoding="utf-8")
        old_epoch = core.now_epoch() - (2 * liveness._THIRTY_MIN)
        _touch(meta, old_epoch)
        _touch(sdir, old_epoch)
        assert liveness.session_live("s-corrupt-meta-stale", cwd=str(repo)) is False

    def test_valid_meta_recent_last_activity_still_live_no_regression(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(
            repo, "s-normal-live", {"pid": "1", "last_activity": core.now_iso()}
        )
        assert liveness.session_live("s-normal-live", cwd=str(repo)) is True

    def test_valid_meta_stale_last_activity_still_dead_no_regression(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(
            repo,
            "s-normal-stale",
            {"pid": "1", "last_activity": "2000-01-01T00:00:00Z"},
        )
        assert liveness.session_live("s-normal-stale", cwd=str(repo)) is False

    def test_present_but_unparseable_last_activity_value_still_dead(self, tmp_path):
        # A meta.json that PARSES fine but carries a non-empty, non-ISO
        # last_activity string is NOT covered by this fallback (read_meta_field
        # returns the literal "garbage", not ""), and correctly stays DEAD —
        # unchanged from the existing pre-fix behavior.
        repo = _make_repo(tmp_path)
        sdir = _write_session(
            repo, "s-bad-value", {"pid": "1", "last_activity": "garbage"}
        )
        _touch(sdir, core.now_epoch())
        assert liveness.session_live("s-bad-value", cwd=str(repo)) is False


class TestSessionLiveGoldenDiff:
    """Q20: Layer-1 golden-diffed against the REAL running test process."""

    @pytest.mark.skipif(
        os.name == "nt",
        reason="fixture built via POSIX `ps -o lstart=`; no Windows equivalent (see TestWindowsCreateTimePath in this module for the Windows-side coverage)",
    )
    def test_layer1_live_self_stable_pid(self, tmp_path):
        repo = _make_repo(tmp_path)
        lstart = _self_lstart()
        epoch = core.lstart_to_epoch(lstart)
        assert epoch > 0
        _write_session(
            repo,
            "s-self",
            {
                # last_activity is deliberately STALE to prove Layer 1 wins:
                # the process is alive so recency must NOT be consulted.
                "pid": "999",
                "last_activity": "2000-01-01T00:00:00Z",
                "stable_pid": str(os.getpid()),
                "stable_pid_lstart": lstart,
                "stable_pid_start_epoch": str(epoch),
            },
        )
        assert liveness.session_live("s-self", cwd=str(repo)) is True


# ---------------------------------------------------------------------------
# claim_holder_live
# ---------------------------------------------------------------------------


class TestClaimHolderLive:
    def test_required_arg_raises(self):
        with pytest.raises(ValueError):
            liveness.claim_holder_live("")

    def test_session_id_dir_live_holder(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "holder-live", {"pid": "9", "last_activity": core.now_iso()})
        cdir = tmp_path / "claim-a"
        cdir.mkdir()
        (cdir / "session_id").write_text("holder-live")
        assert liveness.claim_holder_live(str(cdir), cwd=str(repo)) is True

    def test_session_id_dir_stale_holder(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(
            repo, "holder-stale", {"pid": "9", "last_activity": "2000-01-01T00:00:00Z"}
        )
        cdir = tmp_path / "claim-b"
        cdir.mkdir()
        (cdir / "session_id").write_text("holder-stale")
        assert liveness.claim_holder_live(str(cdir), cwd=str(repo)) is False

    def test_session_id_dir_registry_confirmed_live_with_no_local_session_dir(
        self, tmp_path, monkeypatch
    ):
        """The claim-layer shape of the 2026-08-14 fix: a claim dir names a
        holder `session_id` for which THIS repo has no session dir at all
        (never `_write_session`'d) -- previously always `False` (dead-holder
        takeable) regardless of the holder's real state. A confirmed harness-
        registry record now rescues it, matching `claim_artifact`'s own
        takeover gate (`liveness.claim_holder_live(...) and not lease_expired`)."""
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        _write_registry_record(
            registry_dir, "s.json", "holder-elsewhere", os.getpid(), _self_create_time()
        )
        cdir = tmp_path / "claim-elsewhere"
        cdir.mkdir()
        (cdir / "session_id").write_text("holder-elsewhere")
        assert liveness.claim_holder_live(str(cdir), cwd=str(repo)) is True

    def test_session_id_empty_content_not_live(self, tmp_path):
        repo = _make_repo(tmp_path)
        cdir = tmp_path / "claim-empty"
        cdir.mkdir()
        (cdir / "session_id").write_text("")
        assert liveness.claim_holder_live(str(cdir), cwd=str(repo)) is False

    def test_legacy_pid_only_dir_uses_pid_alive_self(self, tmp_path):
        repo = _make_repo(tmp_path)
        cdir = tmp_path / "claim-legacy"
        cdir.mkdir()
        (cdir / "pid").write_text(str(os.getpid()))
        assert liveness.claim_holder_live(str(cdir), cwd=str(repo)) is True

    def test_legacy_pid_only_dir_dead_pid(self, tmp_path):
        repo = _make_repo(tmp_path)
        cdir = tmp_path / "claim-legacy-dead"
        cdir.mkdir()
        (cdir / "pid").write_text(str(2**31 - 1))
        assert liveness.claim_holder_live(str(cdir), cwd=str(repo)) is False

    def test_no_files_at_all_legacy_branch_empty_pid_not_live(self, tmp_path):
        repo = _make_repo(tmp_path)
        cdir = tmp_path / "claim-none"
        cdir.mkdir()
        # No session_id file -> legacy branch; no pid file -> "" -> not live.
        assert liveness.claim_holder_live(str(cdir), cwd=str(repo)) is False


# ---------------------------------------------------------------------------
# claim_held_by_me — identity predicate
# ---------------------------------------------------------------------------


class TestClaimHeldByMe:
    def test_required_arg_raises(self):
        with pytest.raises(ValueError):
            liveness.claim_held_by_me("")

    def test_session_id_match_with_explicit_my_sid(self, tmp_path):
        cdir = tmp_path / "c"
        cdir.mkdir()
        (cdir / "session_id").write_text("me-sid")
        assert liveness.claim_held_by_me(str(cdir), my_sid="me-sid") is True

    def test_session_id_mismatch(self, tmp_path):
        cdir = tmp_path / "c"
        cdir.mkdir()
        (cdir / "session_id").write_text("other-sid")
        assert liveness.claim_held_by_me(str(cdir), my_sid="me-sid") is False

    def test_empty_my_sid_never_matches_session_id_dir(self, tmp_path):
        # my resolves empty AND recorded is non-empty -> False (bash: `-n $my &&`).
        cdir = tmp_path / "c"
        cdir.mkdir()
        (cdir / "session_id").write_text("some-sid")
        # Force resolution to empty by pointing cwd outside any registry and
        # clearing env — resolve_session_id returns "".
        assert liveness.claim_held_by_me(str(cdir), my_sid="", cwd=str(tmp_path)) is False

    def test_my_sid_resolved_when_omitted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "resolved-me")
        cdir = tmp_path / "c"
        cdir.mkdir()
        (cdir / "session_id").write_text("resolved-me")
        assert liveness.claim_held_by_me(str(cdir)) is True

    def test_legacy_pid_only_dir_compares_to_getpid(self, tmp_path):
        cdir = tmp_path / "c"
        cdir.mkdir()
        (cdir / "pid").write_text(str(os.getpid()))
        # $$ compare — this process IS the recorded pid.
        assert liveness.claim_held_by_me(str(cdir), my_sid="ignored") is True

    def test_legacy_pid_only_dir_mismatch(self, tmp_path):
        cdir = tmp_path / "c"
        cdir.mkdir()
        (cdir / "pid").write_text(str(os.getpid() + 1))
        assert liveness.claim_held_by_me(str(cdir), my_sid="ignored") is False


# ---------------------------------------------------------------------------
# live_session_ids (Q24)
# ---------------------------------------------------------------------------


class TestLiveSessionIds:
    def test_empty_outside_repo(self, tmp_path):
        assert liveness.live_session_ids(cwd=str(tmp_path)) == frozenset()

    def test_returns_frozenset(self, tmp_path):
        repo = _make_repo(tmp_path)
        (Path(repo) / ".git" / "coordinator-sessions").mkdir(parents=True)
        assert isinstance(liveness.live_session_ids(cwd=str(repo)), frozenset)

    def test_live_and_stale_partition(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "live-one", {"pid": "1", "last_activity": core.now_iso()})
        _write_session(
            repo, "stale-one", {"pid": "1", "last_activity": "2000-01-01T00:00:00Z"}
        )
        result = liveness.live_session_ids(cwd=str(repo))
        assert result == frozenset({"live-one"})

    def test_skips_archive_and_agents(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, ".archive", {"pid": "1", "last_activity": core.now_iso()})
        _write_session(repo, ".agents", {"pid": "1", "last_activity": core.now_iso()})
        _write_session(repo, "real", {"pid": "1", "last_activity": core.now_iso()})
        assert liveness.live_session_ids(cwd=str(repo)) == frozenset({"real"})

    def test_negative_elapsed_not_clamped_here_so_not_live(self, tmp_path):
        repo = _make_repo(tmp_path)
        # last_activity in the FUTURE -> negative elapsed. UNLIKE session_live's
        # Layer-2 clamp, live_session_ids' non-stable branch does NOT clamp:
        # is_session_live("-N") fails ^[0-9]+$ -> NOT live. (module negative-spec)
        _write_session(
            repo, "future-nostable", {"pid": "1", "last_activity": "2099-01-01T00:00:00Z"}
        )
        assert liveness.live_session_ids(cwd=str(repo)) == frozenset()

    def test_stable_pid_routes_through_two_layer(self, tmp_path):
        repo = _make_repo(tmp_path)
        # dead stable_pid + fresh recency -> Layer 1 DEAD -> not in live set.
        _write_session(
            repo,
            "dead-stable",
            {
                "pid": "1",
                "stable_pid": str(2**31 - 1),
                "stable_pid_lstart": "Sat Jan  1 00:00:00 2000",
                "stable_pid_start_epoch": "946684800",
                "last_activity": core.now_iso(),
            },
        )
        assert liveness.live_session_ids(cwd=str(repo)) == frozenset()


class TestLiveSessionIdsMetalessEnumeration:
    """Second instance of the 05a68dc1 bug class: enumeration used to glob
    ``*/meta.json``, so a session dir that exists but has not yet flushed its
    meta.json was never VISITED, not merely misclassified -- a live session
    was silently absent from the live set. Enumeration now walks every
    subdirectory and routes meta-less dirs through the SAME
    ``_dir_recency_fallback_epoch`` helper ``session_live``'s Layer 2 uses
    (see the module negative-spec)."""

    def test_meta_less_dir_recent_mtime_is_in_live_set(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _session_dir_path(repo, "s-mid-write")
        sdir.mkdir(parents=True)
        marker = sdir / "lock"
        marker.write_text("x")
        _touch(marker, core.now_epoch())
        assert "s-mid-write" in liveness.live_session_ids(cwd=str(repo))

    def test_meta_less_dir_stale_mtime_not_in_live_set(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _session_dir_path(repo, "s-mid-write-stale")
        sdir.mkdir(parents=True)
        marker = sdir / "lock"
        marker.write_text("x")
        old_epoch = core.now_epoch() - (2 * liveness._THIRTY_MIN)
        _touch(marker, old_epoch)
        _touch(sdir, old_epoch)
        assert "s-mid-write-stale" not in liveness.live_session_ids(cwd=str(repo))

    def test_empty_meta_less_dir_recent_dir_mtime_is_live(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _session_dir_path(repo, "s-empty-fresh")
        sdir.mkdir(parents=True)
        _touch(sdir, core.now_epoch())
        assert "s-empty-fresh" in liveness.live_session_ids(cwd=str(repo))

    def test_reserved_non_session_dirs_still_excluded(self, tmp_path):
        repo = _make_repo(tmp_path)
        base = Path(repo) / ".git" / "coordinator-sessions"
        for name in sorted(liveness._NON_SESSION_DIR_NAMES):
            d = base / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "marker").write_text("x")
            _touch(d / "marker", core.now_epoch())
        _write_session(repo, "real", {"pid": "1", "last_activity": core.now_iso()})
        assert liveness.live_session_ids(cwd=str(repo)) == frozenset({"real"})

    def test_normal_session_dirs_unchanged(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "live-one", {"pid": "1", "last_activity": core.now_iso()})
        _write_session(
            repo, "stale-one", {"pid": "1", "last_activity": "2000-01-01T00:00:00Z"}
        )
        assert liveness.live_session_ids(cwd=str(repo)) == frozenset({"live-one"})


class TestLiveSessionIdsCorpus:
    """Q20/Q24 golden-diff against the REAL on-disk meta.json corpus of this
    repo's own session registry: the native pass must not raise and must
    return a frozenset of strings; every returned sid must be a real dir."""

    def test_no_raise_and_shape_against_real_registry(self):
        result = liveness.live_session_ids()
        assert isinstance(result, frozenset)
        assert all(isinstance(s, str) and s for s in result)
        base = Path(core.git_root() or ".", ".git", "coordinator-sessions")
        if base.is_dir():
            for sid in result:
                assert (base / sid).is_dir()
                assert sid not in (".archive", ".agents")

    def test_every_non_uuid_real_child_is_denylisted_or_a_file(self):
        # Regression for the 2026-08-08 phantom-peer defect: `decisions/` and
        # `reconcile-history/` sit alongside the UUID-shaped session dirs in
        # THIS repo's real `.git/coordinator-sessions/` and were, until this
        # test, unfiltered by `_NON_SESSION_DIR_NAMES` -- so
        # `live_session_verdicts`/`live_session_ids` enumerated them as
        # sessions (and `decisions/` in particular reads Layer-2-recent-LIVE,
        # since pickup_assemble actively mtime-touches it).
        #
        # This walks the REAL on-disk registry (not a tmp_path fixture) on
        # purpose: a denylist gains holes exactly when a new infra dir is
        # added next to the sessions without anyone updating this constant,
        # and only a live walk of the actual corpus catches that. Approach
        # (a) (denylist + this real-corpus test) was chosen over inverting to
        # a positive UUID-shape check: this module's own docstring for
        # `_NON_SESSION_DIR_NAMES` records that session ids are
        # caller-supplied and "NOT guaranteed to be UUID-shaped -- test
        # overrides legitimately use non-UUID ids" (e.g. `no-session`), so a
        # positive UUID-only gate would be a behavior change with no way to
        # show non-regression against that documented contract.
        base = Path(core.git_root() or ".", ".git", "coordinator-sessions")
        if not base.is_dir():
            pytest.skip("no real .git/coordinator-sessions/ registry on this box")
        uuid_re = __import__("re").compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        )
        # NEGATIVE-SPEC: do NOT reintroduce a known-test-leak exclusion set here.
        # This check carried one for `sess-abc` until 2026-08-08. Bug-backlog
        # `2026-07-21-test-leaks-a-session-dir-into-the-real-g-5e739049c5c4.yaml`
        # closed on two claims; only one held. Its technical claim is CONFIRMED --
        # the producer, `archive_stamp._session_shape_set_bridge`, is gone
        # (docstring reference only, no definition, no call site), and the
        # surviving writer `session.shape.session_shape_set` is cwd-scoped by its
        # production caller. Its cleanup claim ("stray dir removed") was FALSIFIED:
        # the dir's mtime was 2026-07-20, predating the closure, so it had never
        # been deleted and nothing re-created it. The operator removed it
        # 2026-08-08 and the exclusion went with it.
        #
        # An exclusion here is a hole in exactly the signal this test exists to
        # give. If a stray non-UUID dir appears, delete the dir; do not re-add a
        # name to a passlist to quiet the failure.
        offenders = []
        for entry in base.iterdir():
            if not entry.is_dir():
                continue
            if entry.name in liveness._NON_SESSION_DIR_NAMES:
                continue
            if uuid_re.match(entry.name):
                continue
            offenders.append(entry.name)
        assert offenders == [], (
            "non-UUID-shaped dir(s) under .git/coordinator-sessions/ are not "
            f"in _NON_SESSION_DIR_NAMES and will be enumerated as phantom "
            f"sessions: {offenders!r}"
        )

    def test_resolve_alias_matches_live_session_ids(self):
        # resolve_live_session_ids() is the zero-arg alias core.py:441 imports.
        #
        # NEGATIVE-SPEC: do NOT go back to
        #   `liveness.resolve_live_session_ids() == liveness.live_session_ids()`.
        # That is TWO SEPARATE live reads of the real on-disk
        # `.git/coordinator-sessions/` registry compared against each other.
        # Session liveness is mutable global state -- a peer session can die
        # or appear between the two calls, so the two reads can legitimately
        # disagree even though the alias is wired correctly. That is exactly
        # what made this test flake under full-suite concurrent-session load
        # while passing in isolation.
        #
        # `resolve_live_session_ids` is a thin, literal delegation
        # (`return live_session_ids()`, liveness.py) -- so the alias contract
        # is a call-forwarding contract, not a value-equality contract. Assert
        # that directly via a single controlled stand-in for
        # `live_session_ids`: patch it to return a fixed sentinel, call the
        # alias once, and check it forwards to the sentinel with no
        # arguments. No disk is touched and no live read of mutable state
        # occurs, so there is nothing left to race -- the assertion holds
        # regardless of what any concurrent session is doing to the registry.
        sentinel = frozenset({"sentinel-sid"})
        with mock.patch.object(
            liveness, "live_session_ids", return_value=sentinel
        ) as mocked:
            result = liveness.resolve_live_session_ids()
        mocked.assert_called_once_with()
        assert result is sentinel


# ---------------------------------------------------------------------------
# live_session_verdicts — THE shared per-id seam (C8/AC13/AC-live-verdicts).
# Pins: both arms exactly, the basis vocabulary, the UNCLAMPED negative-
# elapsed invariant (the one a naive per-id loop over session_live would
# silently erase), and live_session_ids' parity with this seam for every
# fixture including that negative-elapsed case.
# (Review: staff-eng Pass 4 Q2, Finding 7; plan C10.)
# ---------------------------------------------------------------------------


class TestLiveSessionVerdicts:
    def test_no_sessions_root_returns_empty_dict(self, tmp_path):
        repo = _make_repo(tmp_path)
        # `.git/coordinator-sessions` is never created — literally absent,
        # not merely empty.
        assert liveness.live_session_verdicts(cwd=str(repo)) == {}

    def test_empty_sessions_dir_returns_empty_dict(self, tmp_path):
        repo = _make_repo(tmp_path)
        (Path(repo) / ".git" / "coordinator-sessions").mkdir(parents=True)
        assert liveness.live_session_verdicts(cwd=str(repo)) == {}

    @pytest.mark.skipif(
        os.name == "nt",
        reason="fixture built via POSIX `ps -o lstart=`; no Windows equivalent",
    )
    def test_layer1_live_basis_stable_pid_age_none(self, tmp_path):
        repo = _make_repo(tmp_path)
        lstart = _self_lstart()
        epoch = core.lstart_to_epoch(lstart)
        assert epoch > 0
        _write_session(
            repo,
            "s-live-stable",
            {
                "pid": "999",
                # Deliberately stale — Layer 1 must win regardless.
                "last_activity": "2000-01-01T00:00:00Z",
                "stable_pid": str(os.getpid()),
                "stable_pid_lstart": lstart,
                "stable_pid_start_epoch": str(epoch),
            },
        )
        verdicts = liveness.live_session_verdicts(cwd=str(repo))
        live, basis, age_sec = verdicts["s-live-stable"]
        assert live is True
        assert basis == "stable-pid"
        assert age_sec is None

    def test_layer1_dead_basis_stable_pid_age_none(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(
            repo,
            "s-dead-stable",
            {
                "pid": "999",
                # Fresh recency must NOT rescue a Layer-1 DEAD verdict.
                "last_activity": core.now_iso(),
                "stable_pid": str(2**31 - 1),
                "stable_pid_lstart": "Sat Jan  1 00:00:00 2000",
                "stable_pid_start_epoch": "946684800",
            },
        )
        verdicts = liveness.live_session_verdicts(cwd=str(repo))
        live, basis, age_sec = verdicts["s-dead-stable"]
        assert live is False
        assert basis == "stable-pid"
        assert age_sec is None

    def test_layer1_exception_resolves_unknown_fail_open(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _write_session(
            repo,
            "s-boom",
            {
                "pid": "999",
                "last_activity": "2000-01-01T00:00:00Z",
                "stable_pid": "12345",
                "stable_pid_lstart": "Sat Jan  1 00:00:00 2000",
                "stable_pid_start_epoch": "946684800",
            },
        )

        def _boom(*a, **k):
            raise RuntimeError("simulated stable_pid_alive failure")

        monkeypatch.setattr(core, "stable_pid_alive", _boom)
        verdicts = liveness.live_session_verdicts(cwd=str(repo))
        live, basis, age_sec = verdicts["s-boom"]
        # Fail OPEN (never asserted-dead) and never a stronger basis than
        # was actually established.
        assert live is True
        assert basis == "unknown"
        assert age_sec is None

    def test_verdict_layer1_exception_fails_open_emits_own_breadcrumb(
        self, tmp_path, monkeypatch
    ):
        # C4a-2 deliverable: the sibling _verdict_for_sdir fail-open arm
        # must be DETECTABLE (its own SEPARATE process-local counter
        # increments) but must NEVER change the returned verdict from the
        # plain fail-open case above.
        repo = _make_repo(tmp_path)
        _write_session(
            repo,
            "s-boom-breadcrumb",
            {
                "pid": "999",
                "last_activity": "2000-01-01T00:00:00Z",
                "stable_pid": "12345",
                "stable_pid_lstart": "Sat Jan  1 00:00:00 2000",
                "stable_pid_start_epoch": "946684800",
            },
        )

        def _boom(*a, **k):
            raise RuntimeError("simulated stable_pid_alive failure")

        monkeypatch.setattr(core, "stable_pid_alive", _boom)
        monkeypatch.setattr(liveness, "verdict_layer1_unknown_count", 0)
        monkeypatch.setattr(liveness, "layer1_unknown_count", 0)
        before = liveness.verdict_layer1_unknown_count
        session_live_before = liveness.layer1_unknown_count
        verdicts = liveness.live_session_verdicts(cwd=str(repo))
        live, basis, age_sec = verdicts["s-boom-breadcrumb"]
        assert live is True
        assert basis == "unknown"
        assert age_sec is None
        assert liveness.verdict_layer1_unknown_count == before + 1
        # The sibling session_live counter is untouched by this arm --
        # proves the two counters are genuinely separate, not aliases.
        assert liveness.layer1_unknown_count == session_live_before

    def test_layer1_fallthrough_lstart_absent_basis_recency_window_clamped(
        self, tmp_path
    ):
        # stable_pid present, stable_pid_lstart ABSENT -> A-F1 fallthrough to
        # the CLAMPED Layer-2 arithmetic (matching session_live's own Layer-2
        # arm), NOT the unclamped non-stable arm below. Future last_activity
        # -> negative elapsed CLAMPED to 0 -> live, age_sec == 0.
        repo = _make_repo(tmp_path)
        _write_session(
            repo,
            "s-partial",
            {
                "pid": "999",
                "last_activity": "2099-01-01T00:00:00Z",
                "stable_pid": "12345",
                # stable_pid_lstart deliberately absent.
            },
        )
        verdicts = liveness.live_session_verdicts(cwd=str(repo))
        live, basis, age_sec = verdicts["s-partial"]
        assert live is True
        assert basis == "recency-window"
        assert age_sec == 0

    def test_layer2_stable_absent_recency_window_basis(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(
            repo, "s-recency", {"pid": "1", "last_activity": core.now_iso()}
        )
        verdicts = liveness.live_session_verdicts(cwd=str(repo))
        live, basis, age_sec = verdicts["s-recency"]
        assert live is True
        assert basis == "recency-window"
        assert isinstance(age_sec, int) and age_sec >= 0

    def test_layer2_negative_elapsed_unclamped_not_live_and_age_negative(
        self, tmp_path
    ):
        """THE invariant a naive per-id loop over session_live would erase
        (module negative-spec): stable_pid ABSENT, last_activity in the
        FUTURE -> elapsed is negative and UNCLAMPED here (unlike
        session_live's Layer-2 clamp) -> is_session_live fails the
        ``^[0-9]+$`` guard -> not live, and age_sec surfaces the RAW
        negative value, not 0."""
        repo = _make_repo(tmp_path)
        _write_session(
            repo,
            "s-future-nostable",
            {"pid": "1", "last_activity": "2099-01-01T00:00:00Z"},
        )
        verdicts = liveness.live_session_verdicts(cwd=str(repo))
        live, basis, age_sec = verdicts["s-future-nostable"]
        assert live is False
        assert basis == "recency-window"
        assert isinstance(age_sec, int) and age_sec < 0
        # session_live's OWN Layer-2 arm clamps the same input to live=True —
        # the two functions provably disagree on this exact fixture, by
        # design (module negative-spec).
        assert liveness.session_live("s-future-nostable", cwd=str(repo)) is True

    def test_meta_less_dir_basis_recency_window_mtime(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _session_dir_path(repo, "s-mid-write")
        sdir.mkdir(parents=True)
        marker = sdir / "lock"
        marker.write_text("x")
        _touch(marker, core.now_epoch())
        verdicts = liveness.live_session_verdicts(cwd=str(repo))
        live, basis, age_sec = verdicts["s-mid-write"]
        assert live is True
        assert basis == "recency-window-mtime"
        assert isinstance(age_sec, int)

    def test_meta_less_dir_stale_basis_recency_window_mtime_not_live(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _session_dir_path(repo, "s-mid-write-stale")
        sdir.mkdir(parents=True)
        marker = sdir / "lock"
        marker.write_text("x")
        old_epoch = core.now_epoch() - (2 * liveness._THIRTY_MIN)
        _touch(marker, old_epoch)
        _touch(sdir, old_epoch)
        verdicts = liveness.live_session_verdicts(cwd=str(repo))
        live, basis, age_sec = verdicts["s-mid-write-stale"]
        assert live is False
        assert basis == "recency-window-mtime"

    def test_reserved_non_session_dirs_excluded_from_verdicts(self, tmp_path):
        repo = _make_repo(tmp_path)
        base = Path(repo) / ".git" / "coordinator-sessions"
        for name in sorted(liveness._NON_SESSION_DIR_NAMES):
            d = base / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "marker").write_text("x")
            _touch(d / "marker", core.now_epoch())
        _write_session(repo, "real", {"pid": "1", "last_activity": core.now_iso()})
        verdicts = liveness.live_session_verdicts(cwd=str(repo))
        assert set(verdicts.keys()) == {"real"}

    def test_verdict_value_shape(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "s", {"pid": "1", "last_activity": core.now_iso()})
        verdicts = liveness.live_session_verdicts(cwd=str(repo))
        assert isinstance(verdicts, dict)
        live, basis, age_sec = verdicts["s"]
        assert isinstance(live, bool)
        assert isinstance(basis, str)
        assert age_sec is None or isinstance(age_sec, int)

    def test_absent_sid_has_no_verdict(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "real", {"pid": "1", "last_activity": core.now_iso()})
        verdicts = liveness.live_session_verdicts(cwd=str(repo))
        assert "no-such-sid" not in verdicts


class TestSessionVerdict:
    """`session_verdict` (Review: staff-eng-review C) — the O(1) per-sid
    entry point over the same ``_verdict_for_sdir`` derivation
    ``live_session_verdicts`` uses, added to drop ``holder_evidence.
    liveness_basis``'s incidental whole-corpus scan."""

    def test_matches_whole_corpus_verdict_recency(self, tmp_path):
        # Review: staff-eng slice-A P2 — this test was misnamed
        # "..._stable_pid" while writing a recency-only session (no
        # stable_pid, no registry record), so it exercised only the
        # recency arm. Renamed to match what it actually covers; the
        # stable-pid arm (the one carrying the refactor's actual risk) is
        # separately pinned below.
        repo = _make_repo(tmp_path)
        _write_session(
            repo,
            "s-recency",
            {"pid": "1", "last_activity": core.now_iso()},
        )
        whole = liveness.live_session_verdicts(cwd=str(repo))["s-recency"]
        single = liveness.session_verdict("s-recency", cwd=str(repo))
        assert single == whole

    def test_matches_whole_corpus_verdict_stable_pid(self, tmp_path):
        # Review: staff-eng slice-A P2 — parity for the stable-pid arm,
        # which the misnamed test above never exercised. Reuses the
        # os.getpid()/create_time() fixture already written for
        # TestLivenessBasisMatchesVerdictSource.
        import psutil

        repo = _make_repo(tmp_path)
        ct = int(psutil.Process(os.getpid()).create_time())
        _write_session(
            repo,
            "s-stable-parity",
            {
                "pid": "999",
                "last_activity": "2000-01-01T00:00:00Z",
                "stable_pid": str(os.getpid()),
                "stable_pid_lstart": "irrelevant-token",
                "stable_pid_start_epoch": str(ct),
            },
        )
        whole = liveness.live_session_verdicts(cwd=str(repo))["s-stable-parity"]
        single = liveness.session_verdict("s-stable-parity", cwd=str(repo))
        assert single == whole
        assert whole[1] == "stable-pid"

    def test_absent_sid_returns_none(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "real", {"pid": "1", "last_activity": core.now_iso()})
        assert liveness.session_verdict("no-such-sid", cwd=str(repo)) is None

    def test_empty_sid_returns_none(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert liveness.session_verdict("", cwd=str(repo)) is None

    def test_traversal_sid_returns_none(self, tmp_path):
        # Review: staff-eng slice-A P2 — core.session_dir is a bare join
        # with no validation; unlike the whole-corpus loop (which only ever
        # sees real child directory names), the per-sid path must reject
        # a traversal-shaped sid explicitly rather than resolving it.
        repo = _make_repo(tmp_path)
        assert liveness.session_verdict("../../etc/passwd", cwd=str(repo)) is None
        assert liveness.session_verdict("foo/../bar", cwd=str(repo)) is None
        assert liveness.session_verdict("foo\\bar", cwd=str(repo)) is None

    @pytest.mark.skipif(
        os.name != "nt",
        reason="pins the Windows-only Path.__truediv__ drive-letter join "
        "escape (a drive-absolute right operand discards the left "
        "entirely) -- on POSIX, Path.__truediv__ treats a backslash as an "
        "ordinary filename character, so the join never escapes `base` and "
        "this assertion's premise does not hold; not a POSIX defect to fix, "
        "this hazard is only real, and only exercisable, on Windows.",
    )
    def test_colon_drive_letter_sid_returns_none(self, tmp_path):
        # Review: coordinator:code-reviewer — a blocklist of `/`, `\`, `..`,
        # NUL did not reject a bare drive-letter/colon component; on
        # Windows, `core.session_dir`'s underlying `Path.__truediv__` join
        # DISCARDS `base` entirely for a drive-letter-bearing sid like
        # "C:evil", a full containment escape out of the sessions corpus
        # (confirmed live: `Path(base) / "C:evil"` resolves to bare
        # "C:evil", never touching `base`). Pin that the join escape is
        # UNGUARDED at `core.session_dir` (so the hazard this fix closes is
        # real, not hypothetical) while `session_verdict` — the guarded
        # entrypoint reached with a sid read off disk — rejects every such
        # sid before ever calling `core.session_dir` on attacker input.
        repo = _make_repo(tmp_path)
        # "C:\\evil" is an unambiguous absolute-with-drive path regardless of
        # process cwd — unlike "C:evil" (drive-relative, resolves against
        # the OS's per-drive cwd and so is environment-dependent to assert
        # on directly), it deterministically demonstrates the raw join
        # escape at `core.session_dir`.
        resolved = core.session_dir("C:\\evil", str(repo))  # abs-path-ok: attack-shaped sid literal, not a machine path citation
        assert resolved == "C:\\evil", (
            f"expected the unguarded join to demonstrate the join escape "
            f"for 'C:\\\\evil'; instead got {resolved!r} — if "
            f"core.session_dir started validating internally, this "
            f"assertion (and its rationale) needs revisiting"
        )
        for bad_sid in ("C:evil", "C:\\evil", "C:/Windows/Temp/x"):
            assert liveness.session_verdict(bad_sid, cwd=str(repo)) is None

    def test_allowlisted_test_fixture_sid_shape_still_accepted(self, tmp_path):
        # The allowlist must stay compatible with a live on-disk corpus
        # containing non-UUID hyphenated test-fixture sids, e.g.
        # "test-session-abc123".
        repo = _make_repo(tmp_path)
        _write_session(
            repo, "test-session-abc123", {"pid": "1", "last_activity": core.now_iso()}
        )
        assert liveness.session_verdict("test-session-abc123", cwd=str(repo)) is not None

    def test_non_session_dir_name_returns_none_without_touching_disk(self, tmp_path):
        # "no-session" would otherwise resolve to a real (bogus) directory
        # via core.session_dir -- the per-sid path must apply
        # _NON_SESSION_DIR_NAMES filtering explicitly, same as the
        # whole-corpus loop gets for free from its own skip check.
        repo = _make_repo(tmp_path)
        assert liveness.session_verdict("no-session", cwd=str(repo)) is None


class TestSessionVerdictHarnessRegistryElsewhere:
    """C1, docs/plans/2026-08-13-liveness-stops-conflating-dead-with-
    elsewhere.md: the no-verdict arm distinguishes a live-in-another-repo
    session from one that does not exist anywhere. `session_live()`'s
    boolean contract must stay byte-for-byte unchanged (AC1) -- these tests
    pin that alongside the new `session_verdict` state (AC2/AC3/AC6)."""

    def test_live_foreign_repo_session_is_distinguishable(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        # No session dir for this sid in `repo` at all -- only a registry
        # record, as if the session were live and working elsewhere.
        _write_registry_record(
            registry_dir,
            "s.json",
            "s-elsewhere",
            os.getpid(),
            _self_create_time(),
            cwd="/some/other/repo",
        )
        verdict = liveness.session_verdict("s-elsewhere", cwd=str(repo))
        assert verdict is not None
        live, basis, elsewhere_cwd = verdict
        assert live is True
        assert basis == "harness-registry-elsewhere"
        assert elsewhere_cwd == "/some/other/repo"

    def test_live_foreign_repo_session_with_no_recorded_cwd(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        _write_registry_record(
            registry_dir, "s.json", "s-elsewhere-no-cwd", os.getpid(), _self_create_time()
        )
        live, basis, elsewhere_cwd = liveness.session_verdict(
            "s-elsewhere-no-cwd", cwd=str(repo)
        )
        assert live is True
        assert basis == "harness-registry-elsewhere"
        assert elsewhere_cwd is None

    def test_genuinely_nonexistent_sid_still_none(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        # Registry dir exists but has no record for this sid at all.
        registry_dir.mkdir(parents=True, exist_ok=True)
        assert liveness.session_verdict("s-nowhere-at-all", cwd=str(repo)) is None

    def test_registry_hit_but_pid_dead_falls_through_to_none(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        # A registry record whose birth-instant does not match any live
        # process -- unconfirmed candidate, must fall through to None
        # rather than being asserted live.
        _write_registry_record(
            registry_dir, "s.json", "s-elsewhere-dead", 2**31 - 1, 946684800, cwd="/dead/repo"
        )
        assert liveness.session_verdict("s-elsewhere-dead", cwd=str(repo)) is None

    def test_live_same_repo_session_unchanged(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        _write_session(
            repo, "s-same-repo-live", {"pid": "1", "last_activity": core.now_iso()}
        )
        assert liveness.session_live("s-same-repo-live", cwd=str(repo)) is True
        live, basis, _age = liveness.session_verdict("s-same-repo-live", cwd=str(repo))
        assert live is True
        assert basis == "recency-window"

    def test_dead_same_repo_session_unchanged(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        _write_session(
            repo,
            "s-same-repo-dead",
            {"pid": "1", "last_activity": "2000-01-01T00:00:00Z"},
        )
        assert liveness.session_live("s-same-repo-dead", cwd=str(repo)) is False
        live, basis, _age = liveness.session_verdict("s-same-repo-dead", cwd=str(repo))
        assert live is False
        assert basis == "recency-window"

    def test_harness_registry_lookup_exception_falls_through_to_none(
        self, tmp_path, monkeypatch
    ):
        def _boom(sid):
            raise RuntimeError("simulated harness_registry internal failure")

        repo = _make_repo(tmp_path)
        monkeypatch.setattr(harness_registry, "lookup", _boom)
        assert liveness.session_verdict("s-elsewhere-boom", cwd=str(repo)) is None


class TestLivenessBasisMatchesVerdictSource:
    """Integration-shaped: on a real tmp_path corpus, the basis
    ``holder_evidence.liveness_basis`` prints must be the basis of the SAME
    arm ``session_verdict``/``live_session_verdicts`` actually resolved --
    nothing currently detects a verdict/basis mismatch on this surface
    (Review: staff-eng-review, tests section)."""

    def test_stable_pid_session_basis_matches_verdict_arm(self, tmp_path):
        import psutil
        repo = _make_repo(tmp_path)
        ct = int(psutil.Process(os.getpid()).create_time())
        _write_session(
            repo,
            "s-stable",
            {
                "pid": "999",
                "last_activity": "2000-01-01T00:00:00Z",
                "stable_pid": str(os.getpid()),
                "stable_pid_lstart": "irrelevant-token",
                "stable_pid_start_epoch": str(ct),
            },
        )
        live, basis, _age = liveness.session_verdict("s-stable", cwd=str(repo))
        assert live is True
        assert basis == "stable-pid"
        reported = holder_evidence_mod.liveness_basis("s-stable", cwd=str(repo))
        assert reported == basis

    def test_recency_session_basis_matches_verdict_arm(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(
            repo, "s-recency-int", {"pid": "1", "last_activity": core.now_iso()}
        )
        live, basis, _age = liveness.session_verdict("s-recency-int", cwd=str(repo))
        assert live is True
        assert basis == "recency-window"
        reported = holder_evidence_mod.liveness_basis("s-recency-int", cwd=str(repo))
        assert reported == basis


class TestLiveSessionIdsMatchesVerdictsSeam:
    """`live_session_ids()` is a thin derived wrapper over
    `live_session_verdicts()` — its output must stay set-identical to today
    for every fixture, INCLUDING the negative-elapsed/clock-skew case (the
    case that fails the moment a naive implementation clamps it away)."""

    def _cases(self, repo):
        _write_session(
            repo, "live-recency", {"pid": "1", "last_activity": core.now_iso()}
        )
        _write_session(
            repo,
            "dead-recency",
            {"pid": "1", "last_activity": "2000-01-01T00:00:00Z"},
        )
        _write_session(
            repo,
            "future-nostable",
            {"pid": "1", "last_activity": "2099-01-01T00:00:00Z"},
        )
        _write_session(
            repo,
            "dead-stable",
            {
                "pid": "1",
                "stable_pid": str(2**31 - 1),
                "stable_pid_lstart": "Sat Jan  1 00:00:00 2000",
                "stable_pid_start_epoch": "946684800",
                "last_activity": core.now_iso(),
            },
        )
        sdir = _session_dir_path(repo, "meta-less-fresh")
        sdir.mkdir(parents=True)
        _touch(sdir, core.now_epoch())

    def test_live_session_ids_matches_verdicts_true_set(self, tmp_path):
        repo = _make_repo(tmp_path)
        self._cases(repo)
        verdicts = liveness.live_session_verdicts(cwd=str(repo))
        expected = frozenset(sid for sid, (live, _b, _a) in verdicts.items() if live)
        assert liveness.live_session_ids(cwd=str(repo)) == expected
        # Pin the concrete membership, not just internal self-consistency —
        # this is the assertion that would fail if `live_session_ids` ever
        # stopped being a thin wrapper (e.g. reverted to its own independent
        # loop) and silently diverged from the seam.
        assert expected == frozenset({"live-recency", "meta-less-fresh"})
        # The negative-elapsed fixture must NOT be live in EITHER function —
        # the invariant this whole test class exists to pin.
        assert "future-nostable" not in liveness.live_session_ids(cwd=str(repo))
        assert "future-nostable" not in expected


# ---------------------------------------------------------------------------
# active_sessions
# ---------------------------------------------------------------------------


class TestActiveSessions:
    def test_empty_outside_repo(self, tmp_path):
        assert liveness.active_sessions(cwd=str(tmp_path)) == []

    def test_no_sessions_dir_message(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert liveness.active_sessions(cwd=str(repo)) == [
            "(no coordinator-sessions dir yet)"
        ]

    def test_empty_sessions_dir_no_active(self, tmp_path):
        repo = _make_repo(tmp_path)
        (Path(repo) / ".git" / "coordinator-sessions").mkdir(parents=True)
        assert liveness.active_sessions(cwd=str(repo)) == ["(no active sessions)"]

    def test_live_line_format(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "sid-live", {"pid": "1", "last_activity": core.now_iso()})
        lines = liveness.active_sessions(cwd=str(repo))
        assert len(lines) == 1
        assert lines[0].startswith("sid-live")
        assert "Live (last activity" in lines[0]
        assert "s ago)" in lines[0]

    def test_stale_line_format_and_reap_threshold_text(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(
            repo, "sid-stale", {"pid": "1", "last_activity": "2000-01-01T00:00:00Z"}
        )
        lines = liveness.active_sessions(cwd=str(repo))
        assert len(lines) == 1
        assert "Stale (last activity" in lines[0]
        assert "reap threshold is 24h" in lines[0]
        assert lines[0].endswith("d ago, reap threshold is 24h)")

    def test_skips_archive_and_agents(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, ".archive", {"pid": "1", "last_activity": core.now_iso()})
        _write_session(repo, ".agents", {"pid": "1", "last_activity": core.now_iso()})
        _write_session(repo, "keep", {"pid": "1", "last_activity": core.now_iso()})
        lines = liveness.active_sessions(cwd=str(repo))
        assert len(lines) == 1
        assert lines[0].startswith("keep")

    def test_sorted_alphabetically(self, tmp_path):
        repo = _make_repo(tmp_path)
        for sid in ("zeta", "alpha", "mike"):
            _write_session(repo, sid, {"pid": "1", "last_activity": core.now_iso()})
        lines = liveness.active_sessions(cwd=str(repo))
        names = [ln.split()[0] for ln in lines]
        assert names == ["alpha", "mike", "zeta"]

    def test_future_activity_clamped_to_zero_shows_live(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(
            repo, "sid-future", {"pid": "1", "last_activity": "2099-01-01T00:00:00Z"}
        )
        lines = liveness.active_sessions(cwd=str(repo))
        # clamp -> "0s ago" label, and session_live (Layer 2 clamp) -> Live.
        assert "0s ago" in lines[0]
        assert "Live" in lines[0]

    def test_minute_bucket_label(self, tmp_path):
        repo = _make_repo(tmp_path)
        five_min_ago = core.now_epoch() - 5 * 60
        from datetime import datetime, timezone

        iso = datetime.fromtimestamp(five_min_ago, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        _write_session(repo, "sid-5m", {"pid": "1", "last_activity": iso})
        lines = liveness.active_sessions(cwd=str(repo))
        assert "m ago" in lines[0]
        assert "Live" in lines[0]

    def test_reserved_non_session_dirs_still_excluded(self, tmp_path):
        repo = _make_repo(tmp_path)
        base = Path(repo) / ".git" / "coordinator-sessions"
        for name in sorted(liveness._NON_SESSION_DIR_NAMES):
            d = base / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "marker").write_text("x")
            _touch(d / "marker", core.now_epoch())
        _write_session(repo, "real", {"pid": "1", "last_activity": core.now_iso()})
        lines = liveness.active_sessions(cwd=str(repo))
        assert len(lines) == 1
        assert lines[0].startswith("real")

    def test_normal_session_dirs_unchanged_with_reserved_dirs_present(self, tmp_path):
        repo = _make_repo(tmp_path)
        for name in sorted(liveness._NON_SESSION_DIR_NAMES):
            d = Path(repo) / ".git" / "coordinator-sessions" / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "marker").write_text("x")
        _write_session(repo, "live-one", {"pid": "1", "last_activity": core.now_iso()})
        _write_session(
            repo, "stale-one", {"pid": "1", "last_activity": "2000-01-01T00:00:00Z"}
        )
        lines = liveness.active_sessions(cwd=str(repo))
        names = [ln.split()[0] for ln in lines]
        assert sorted(names) == ["live-one", "stale-one"]
        for ln in lines:
            if ln.startswith("live-one"):
                assert "Live" in ln
            if ln.startswith("stale-one"):
                assert "Stale" in ln


# ---------------------------------------------------------------------------
# Single-liveness-key invariant — every consumer converges on session_live.
# Folded from the retired test_liveness_parity.py (2026-07-22 parity-retire-
# fold, see the module negative-spec) — merged in wholesale, untouched, since
# it never called the bash oracle in the first place.
# ---------------------------------------------------------------------------


def _convergence_fixture_names():
    return ("stable_matching", "recency_recent", "recency_stale")


def _convergence_fixture_meta(name):
    """Build one of the 3 single-liveness-key convergence fixtures (the
    2 recycled/trap fixtures live as standalone TestSessionLive cases above,
    not here — this class only needs A fixture per liveness bucket, not the
    full 5-fixture parity corpus)."""
    if name == "stable_matching":
        mypid = os.getpid()
        lstart = _self_lstart()
        epoch = core.lstart_to_epoch(lstart)
        assert epoch > 0
        return {
            "pid": "999",
            "last_activity": "2000-01-01T00:00:00Z",
            "stable_pid": str(mypid),
            "stable_pid_lstart": lstart,
            "stable_pid_start_epoch": str(epoch),
        }
    if name == "recency_recent":
        return {"pid": "999", "last_activity": core.now_iso()}
    if name == "recency_stale":
        return {"pid": "999", "last_activity": "2000-01-01T00:00:00Z"}
    raise ValueError(name)


@pytest.mark.skipif(
    os.name == "nt",
    reason="the 'stable_matching' fixture is built via POSIX `ps -o lstart=`; "
    "no Windows equivalent (see TestWindowsCreateTimePath for the Windows-side "
    "coverage of the Layer-1 stable-pid path)",
)
class TestSingleLivenessKeyConvergence:
    """cs_claim_holder_live / cs_reap_stale_claims / cs_sweep_actioned_memos /
    cs_active_sessions / cs_live_session_ids ALL route through the ONE
    session_live decision (D5, pcore-03). reap + sweep route via
    claim_holder_live, so pinning claim_holder_live == session_live covers them
    transitively."""

    def test_convergence_fixture_names_cover_meta_builder(self):
        """Guard against silent under-parametrization (the same hazard the
        retired test_fixture_names_constant_matches_fixtures pinned, folded
        forward here): every name _convergence_fixture_names() emits must be
        buildable by _convergence_fixture_meta, and the covered set must be
        exactly the 3 liveness buckets this class exercises — a name added to
        one without the other would silently drop coverage rather than fail
        loudly."""
        names = _convergence_fixture_names()
        assert set(names) == {"stable_matching", "recency_recent", "recency_stale"}
        for name in names:
            assert _convergence_fixture_meta(name)  # raises ValueError if unmapped

    @pytest.mark.parametrize("name", _convergence_fixture_names())
    def test_all_consumers_agree_with_session_live(self, tmp_path, name):
        repo = _make_repo(tmp_path)
        meta = _convergence_fixture_meta(name)
        sid = "sess-" + name
        _write_session(repo, sid, meta)

        verdict = liveness.session_live(sid, cwd=str(repo))

        # claim_holder_live on a session_id-bearing claim dir -> session_live.
        cdir = tmp_path / ("claim-" + name)
        cdir.mkdir()
        (cdir / "session_id").write_text(sid)
        assert liveness.claim_holder_live(str(cdir), cwd=str(repo)) == verdict

        # live_session_ids membership <-> session_live.
        assert (sid in liveness.live_session_ids(cwd=str(repo))) == verdict

        # active_sessions labels the sid Live iff session_live.
        lines = liveness.active_sessions(cwd=str(repo))
        line = next(ln for ln in lines if ln.startswith(sid))
        assert ("Live" in line) == verdict
        assert ("Stale" in line) != verdict

    def test_claim_holder_live_delegates_to_session_live(self, tmp_path, monkeypatch):
        """Strong 'routes through' pin: flipping session_live flips
        claim_holder_live's verdict — proving no independent liveness logic."""
        repo = _make_repo(tmp_path)
        _write_session(repo, "s", {"pid": "1", "last_activity": core.now_iso()})
        cdir = tmp_path / "c"
        cdir.mkdir()
        (cdir / "session_id").write_text("s")

        monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: True)
        assert liveness.claim_holder_live(str(cdir), cwd=str(repo)) is True
        monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: False)
        assert liveness.claim_holder_live(str(cdir), cwd=str(repo)) is False


# ---------------------------------------------------------------------------
# Windows create_time() path — exercised on POSIX via real psutil (create_time
# is cross-platform) with os.name forced to "nt". Only the init `claude.exe`
# comm-match stays genuinely Windows-only (deferred to the Gate-d dogfood).
# Folded from the retired test_liveness_parity.py (2026-07-22 parity-retire-
# fold, see the module negative-spec) — merged in wholesale, untouched.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("psutil") is None,
    reason="psutil (the Windows liveness mechanism) not installed",
)
class TestWindowsCreateTimePath:
    def _win(self, monkeypatch):
        # Flip the platform seam only — NOT os.name (that would make pathlib
        # pick the un-instantiable WindowsPath on this POSIX host). psutil's
        # create_time() is cross-platform, so the Windows verdict logic runs
        # for real here; only init's `claude.exe` comm-match stays untested.
        monkeypatch.setattr(core, "_IS_WINDOWS", True)

    def test_win_create_time_epoch_live_self(self):
        # _win_create_time_epoch is pure psutil -> works on POSIX too.
        ep = core._win_create_time_epoch(os.getpid())
        assert isinstance(ep, int) and ep > 0

    def test_win_create_time_epoch_dead_pid_is_none(self):
        assert core._win_create_time_epoch(2 ** 31 - 1) is None

    def test_win_matching_is_live(self, tmp_path, monkeypatch):
        import psutil
        self._win(monkeypatch)
        repo = _make_repo(tmp_path)
        ct = int(psutil.Process(os.getpid()).create_time())
        _write_session(
            repo, "w-live",
            {
                "pid": "999",
                "last_activity": "2000-01-01T00:00:00Z",  # stale: Layer 1 must win
                "stable_pid": str(os.getpid()),
                "stable_pid_lstart": str(ct),
                "stable_pid_start_epoch": str(ct),
            },
        )
        assert liveness.session_live("w-live", cwd=str(repo)) is True

    def test_win_recycled_epoch_is_dead(self, tmp_path, monkeypatch):
        import psutil
        self._win(monkeypatch)
        repo = _make_repo(tmp_path)
        ct = int(psutil.Process(os.getpid()).create_time())
        _write_session(
            repo, "w-recycled",
            {
                "pid": "999",
                "last_activity": core.now_iso(),  # fresh: Layer 1 must still win DEAD
                "stable_pid": str(os.getpid()),
                "stable_pid_lstart": str(ct + 999),
                "stable_pid_start_epoch": str(ct + 999),
            },
        )
        assert liveness.session_live("w-recycled", cwd=str(repo)) is False

    def test_win_dead_stable_pid_is_dead(self, tmp_path, monkeypatch):
        self._win(monkeypatch)
        repo = _make_repo(tmp_path)
        _write_session(
            repo, "w-dead",
            {
                "pid": "999",
                "last_activity": core.now_iso(),
                "stable_pid": str(2 ** 31 - 1),
                "stable_pid_lstart": "12345",
                "stable_pid_start_epoch": "12345",
            },
        )
        assert liveness.session_live("w-dead", cwd=str(repo)) is False

    def test_win_legacy_lstart_token_compare(self, tmp_path, monkeypatch):
        """No stored epoch, only the create_time token in stable_pid_lstart ->
        the legacy string-compare arm still resolves identity on Windows."""
        import psutil
        self._win(monkeypatch)
        repo = _make_repo(tmp_path)
        ct = int(psutil.Process(os.getpid()).create_time())
        _write_session(
            repo, "w-legacy",
            {
                "pid": "999",
                "last_activity": "2000-01-01T00:00:00Z",
                "stable_pid": str(os.getpid()),
                "stable_pid_lstart": str(ct),  # token == current create_time int
                # no stable_pid_start_epoch
            },
        )
        assert liveness.session_live("w-legacy", cwd=str(repo)) is True


# ---------------------------------------------------------------------------
# empty-vs-indeterminate, asserted AGAINST compute_scope (not liveness alone)
# — the property that actually matters is compute_scope's fail-closed
# fallback keying on the DISTINCTION between "no sessions dir/unresolvable"
# and "all peers dead" (plan C10 body, AC16). A test that only checks
# `live_session_ids()` in isolation would prove nothing about that gate.
# ---------------------------------------------------------------------------


class TestComputeScopeEmptyVsIndeterminate:
    def test_no_sessions_root_is_determinate_empty_gating_stays_enabled(
        self, tmp_path, capsys
    ):
        """`.git/coordinator-sessions` literally does not exist yet (no
        session has ever inited) -- `live_session_ids()` returns `{}`, and
        `compute_scope`'s own `peer_dir_seen` disambiguator (keyed on
        `os.path.isdir(base)`) must resolve False here, so this stays the
        DETERMINATE empty case: gating is NOT disabled, and no indeterminate
        diagnostic is printed."""
        repo = _make_repo(tmp_path)
        base = Path(core.sessions_dir(cwd=str(repo)))
        assert not base.is_dir()
        assert liveness.live_session_ids(cwd=str(repo)) == frozenset()

        result = scope.compute_scope("mine", cwd=str(repo))
        assert result.my_scope == []
        assert result.skipped == []
        stderr = capsys.readouterr().err
        assert "treating liveness enumeration as indeterminate" not in stderr

    def test_all_peers_dead_is_indeterminate_gating_disabled(self, tmp_path, capsys):
        """A peer session directory genuinely EXISTS on disk, but is dead --
        `live_session_ids()` also returns `{}`, the SAME return shape as the
        no-sessions-root case above. `compute_scope`'s `peer_dir_seen` scan
        must find the peer dir and force this call's liveness enumeration to
        indeterminate (gating disabled), printing the diagnostic and falling
        back to the pre-existing unconditional exclusion -- a dead peer's
        claim on a path THIS session never touched still contests it."""
        repo = _make_repo(tmp_path)
        # "mine" must itself be DEAD too -- if self were live, live_ids
        # would be {"mine"} (non-empty), never reaching this disambiguator
        # at all. core.init() first (so `started_at` exists and Step 2's
        # mtime-fallback augmentation picks up "shared.py" as a candidate),
        # then flip its own last_activity stale.
        core.init("mine", cwd=str(repo))
        mine_sdir = Path(core.sessions_dir(cwd=str(repo))) / "mine"
        core.update_meta_field(str(mine_sdir), "last_activity", "2000-01-01T00:00:00Z")
        # Guard-1 (core.init) stamps stable_pid from the ambient CLAUDE_PID
        # of the live harness session actually running this test suite --
        # an authoritative signal the fixture's stale last_activity above
        # cannot override. Neutralise it the same way last_activity is
        # neutralised above, so Layer 1 has nothing authoritative left to
        # read and "mine" reads dead purely off the (now-stale) recency
        # fallback, matching what this test is actually exercising.
        # update_meta_field() hard-rejects an empty-string value (required-
        # non-empty contract), so the stamp is cleared via a direct
        # meta.json rewrite instead.
        mine_meta_path = mine_sdir / "meta.json"
        mine_meta = json.loads(mine_meta_path.read_text(encoding="utf-8"))
        mine_meta["stable_pid"] = ""
        mine_meta["stable_pid_lstart"] = ""
        mine_meta["stable_pid_start_epoch"] = ""
        mine_meta_path.write_text(
            json.dumps(mine_meta, indent=2) + "\n", encoding="utf-8"
        )

        _write_session(
            repo, "dead-peer", {"pid": "1", "last_activity": "2000-01-01T00:00:00Z"}
        )
        assert liveness.live_session_ids(cwd=str(repo)) == frozenset()

        (repo / "shared.py").write_text("z")
        peer_sdir = _session_dir_path(repo, "dead-peer")
        # The claim must be written in the dialect `compute_scope` actually
        # READS. This fixture wrote a bare-path `touched.txt` until 2026-08-27;
        # AC11 retired that reader, so the claim landed in a file nothing
        # opens and the peer read as claiming nothing -- `attribution` empty,
        # `skipped` empty, the path falling through to `orphans`. The test was
        # asserting a real property against a corpus it could no longer see.
        touch_record.append_event(
            touch_record.sink_path(peer_sdir),
            session_id="dead-peer",
            agent_id=None,
            verb=touch_record.VERB_TOUCH,
            path="shared.py",
        )

        result = scope.compute_scope("mine", cwd=str(repo))
        stderr = capsys.readouterr().err
        assert "treating liveness enumeration as indeterminate" in stderr
        # Gating disabled -> pre-existing unconditional exclusion: the dead
        # peer's claim is NOT released, "shared.py" is still skipped/owned
        # by "dead-peer", never adopted into "mine"'s own scope.
        assert "shared.py" not in result.my_scope
        assert ("shared.py", "dead-peer") in result.skipped


# ---------------------------------------------------------------------------
# holder_evidence._liveness_basis reads off live_session_verdicts() (D5
# single-liveness-key invariant restored — no independent core.stable_pid_alive
# re-derivation outside liveness.py). Also pins the absent-from-map ->
# not-live/"unknown" basis every claim-holder consumer reads.
# ---------------------------------------------------------------------------


class TestHolderEvidenceLivenessBasisSeam:
    def test_module_never_calls_stable_pid_alive_directly(self):
        # Prose mentions of the retired direct-call pattern are fine (the
        # module docstring explains WHY it no longer does this) -- the
        # invariant is no CALL, i.e. no `core.stable_pid_alive(` / bare
        # `stable_pid_alive(` invocation anywhere in the module.
        source = Path(holder_evidence_mod.__file__).read_text(encoding="utf-8")
        assert "stable_pid_alive(" not in source

    def test_liveness_basis_delegates_to_verdicts_seam(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        sentinel = (True, "recency-window", 42)
        with mock.patch.object(
            liveness, "session_verdict", return_value=sentinel
        ) as mocked:
            basis = holder_evidence_mod._liveness_basis("holder-x", cwd=str(repo))
        mocked.assert_called_once_with("holder-x", str(repo))
        assert basis == "recency-window"

    def test_holder_absent_from_map_reads_unknown_basis(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "real", {"pid": "1", "last_activity": core.now_iso()})
        assert "no-session" not in liveness.live_session_verdicts(cwd=str(repo))
        assert holder_evidence_mod._liveness_basis("no-session", cwd=str(repo)) == "unknown"

    def test_archived_sid_absent_from_map_reads_unknown_and_not_live(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _write_session(
            repo, "was-live", {"pid": "1", "last_activity": core.now_iso()}
        )
        archive_dir = Path(core.sessions_dir(cwd=str(repo))) / ".archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        sdir.rename(archive_dir / "was-live-2026-08-03")

        assert "was-live" not in liveness.live_session_verdicts(cwd=str(repo))
        assert liveness.session_live("was-live", cwd=str(repo)) is False
        assert (
            holder_evidence_mod._liveness_basis("was-live", cwd=str(repo)) == "unknown"
        )

    def test_holder_with_no_session_dir_at_all_reads_none_basis_not_unknown(
        self, tmp_path
    ):
        """A holder sid with no session dir on disk short-circuits in
        `holder_evidence` before `_liveness_basis` is ever consulted, so its
        basis reads `None` — the literal `"unknown"` is reserved for a dir
        that DOES exist but carries no verdict (the two tests above). Every
        claim-holder consumer inherits this distinction, so an evidence gap
        is never rendered as a resolved-but-unknown verdict.

        NEGATIVE-SPEC (2026-08-22): this case previously asserted the same
        distinction through `pickup_assemble.compute_competing_claim`'s
        `stale-claim` candidate disposition. That producer was deleted at
        aadef0e23 (chunk C2, docs/plans/2026-08-21-rebuild-the-three-ceremony-
        assemblers.md), whose reverse-reference scan missed this module and
        left it uncollectable at import. The surviving subject is
        `holder_evidence` itself, asserted directly — do NOT repoint this at
        a resurrected sibling-scan producer. Its companion case, which pinned
        that function's per-distinct-sid `live_session_verdicts()` memo
        ("fetched once per call, not per candidate"), is retired outright:
        the memo was internal to the deleted function and no surviving code
        path scans candidates.
        """
        repo = _make_repo(tmp_path)
        assert core.session_dir("no-session", str(repo)) is None or not Path(
            core.session_dir("no-session", str(repo))
        ).is_dir()

        evidence = holder_evidence_mod.holder_evidence("no-session", Path(repo))

        assert evidence["liveness_basis"] is None
        assert liveness.session_live("no-session", cwd=str(repo)) is False


# ---------------------------------------------------------------------------
# harness_registry precedence — C2. Registry is Source 0: LIVE or NO-RECORD,
# never DEAD. Point every fixture at a tmp_path registry_dir — never the
# operator's real `~/.claude/sessions` (AC5/AC7/AC8/AC10 in the C2 brief).
# ---------------------------------------------------------------------------


def _write_registry_record(registry_dir, filename, session_id, pid, epoch, cwd=None):
    registry_dir.mkdir(parents=True, exist_ok=True)
    ticks = int(
        (epoch + harness_registry._FILETIME_EPOCH_OFFSET_SEC)
        * harness_registry._FILETIME_TICKS_PER_SEC
    )
    payload = {"sessionId": session_id, "pid": pid, "procStart": ticks}
    if cwd is not None:
        payload["cwd"] = cwd
    (registry_dir / filename).write_text(json.dumps(payload), encoding="utf-8")


def _self_create_time():
    import psutil

    return psutil.Process(os.getpid()).create_time()


class TestHarnessRegistrySessionLivePrecedence:
    def test_registry_live_wins_over_absent_stable_pid(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        # No stable_pid at all, and stale last_activity -> Layer 2 would say
        # DEAD -- the registry hit must win regardless.
        _write_session(
            repo, "s-registry-live", {"pid": "999", "last_activity": "2000-01-01T00:00:00Z"}
        )
        _write_registry_record(
            registry_dir, "s.json", "s-registry-live", os.getpid(), _self_create_time()
        )
        assert liveness.session_live("s-registry-live", cwd=str(repo)) is True

    def test_registry_live_wins_over_stale_stable_pid(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        # A DEAD stable_pid arm on its own -- registry must still win.
        _write_session(
            repo,
            "s-registry-over-dead-stable",
            {
                "pid": "999",
                "last_activity": "2000-01-01T00:00:00Z",
                "stable_pid": str(2**31 - 1),
                "stable_pid_lstart": "Sat Jan  1 00:00:00 2000",
                "stable_pid_start_epoch": "946684800",
            },
        )
        _write_registry_record(
            registry_dir,
            "s.json",
            "s-registry-over-dead-stable",
            os.getpid(),
            _self_create_time(),
        )
        assert liveness.session_live("s-registry-over-dead-stable", cwd=str(repo)) is True

    def test_registry_live_wins_with_no_local_session_dir_at_all(
        self, tmp_path, monkeypatch
    ):
        """2026-08-14 fix (cross-repo/inbox/2026-08-11-example-market-data-repo-
        em-reclaim-labels-a-live-session-dead-without-checking.md): Source 0
        used to be gated behind `Path(sdir).is_dir()`, so a confirmed-live
        holder with NO session dir visible under `cwd`'s repo at all (a
        foreign-cwd holder, or any resolution mismatch between where the
        holder wrote its meta.json and the `cwd` a caller like
        `claim_holder_live` passes) read instantly DEAD without Source 0
        ever being consulted -- the exact asymmetry `session_verdict`'s own
        `"harness-registry-elsewhere"` no-verdict arm (C1) already fixed for
        ITS callers, but `session_live` (and therefore `claim_holder_live`,
        the claim-takeover primitive) never got the same fix. No
        `_write_session` call in this test at all -- that absence is the
        point."""
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        _write_registry_record(
            registry_dir, "s.json", "s-registry-no-local-dir", os.getpid(), _self_create_time()
        )
        assert liveness.session_live("s-registry-no-local-dir", cwd=str(repo)) is True

    def test_registry_none_falls_through_to_stable_pid_arm_exact_verdict(
        self, tmp_path, monkeypatch
    ):
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        # Registry dir exists but has no record for this sid -> NO-RECORD,
        # falls through to today's Layer-1 DEAD verdict, byte-unchanged.
        _write_session(
            repo,
            "s-dead-stable-no-registry",
            {
                "pid": "999",
                "last_activity": core.now_iso(),
                "stable_pid": str(2**31 - 1),
                "stable_pid_lstart": "Sat Jan  1 00:00:00 2000",
                "stable_pid_start_epoch": "946684800",
            },
        )
        assert (
            liveness.session_live("s-dead-stable-no-registry", cwd=str(repo)) is False
        )

    def test_registry_none_falls_through_to_layer2_recency_window_exact_verdict(
        self, tmp_path, monkeypatch
    ):
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        _write_session(
            repo, "s-recency-no-registry", {"pid": "1", "last_activity": core.now_iso()}
        )
        assert liveness.session_live("s-recency-no-registry", cwd=str(repo)) is True
        _write_session(
            repo,
            "s-recency-stale-no-registry",
            {"pid": "1", "last_activity": "2000-01-01T00:00:00Z"},
        )
        assert (
            liveness.session_live("s-recency-stale-no-registry", cwd=str(repo)) is False
        )

    def test_registry_stale_record_with_no_local_session_dir_still_reads_dead(
        self, tmp_path, monkeypatch
    ):
        """Review: staff-eng F4 — the false-LIVE direction was untested even
        though the entire safety argument for the 2026-08-14 reorder rests
        on it: a registry record that FAILS `stable_pid_alive`'s
        birth-instant compare (here, a `start_epoch` far from the real
        process's actual create time — the recycled-pid/stale-record shape)
        must still read DEAD when there is also no local session dir, not
        fail open just because Source 0 was reached first."""
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        _write_registry_record(
            registry_dir,
            "s.json",
            "s-registry-stale-no-local-dir",
            os.getpid(),
            _self_create_time() - 10_000,  # far outside the 2s tolerance
        )
        assert (
            liveness.session_live("s-registry-stale-no-local-dir", cwd=str(repo))
            is False
        )

    def test_registry_none_falls_through_to_metaless_mtime_substitution(
        self, tmp_path, monkeypatch
    ):
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        sdir = _session_dir_path(repo, "s-mid-write-no-registry")
        sdir.mkdir(parents=True)
        marker = sdir / "lock"
        marker.write_text("x")
        _touch(marker, core.now_epoch())
        assert liveness.session_live("s-mid-write-no-registry", cwd=str(repo)) is True

    def test_registry_absent_directory_falls_through_never_dead(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        # registry_dir itself does not exist on disk at all -- must degrade
        # cleanly to NO-RECORD, not raise, not influence toward DEAD.
        missing = tmp_path / "no-such-registry-dir"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: missing)
        _write_session(
            repo, "s-no-registry-dir", {"pid": "1", "last_activity": core.now_iso()}
        )
        assert liveness.session_live("s-no-registry-dir", cwd=str(repo)) is True

    def test_registry_internal_exception_cannot_propagate(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)

        def _boom():
            raise RuntimeError("simulated harness_registry internal failure")

        monkeypatch.setattr(harness_registry, "snapshot", _boom)
        _write_session(
            repo, "s-registry-boom", {"pid": "1", "last_activity": core.now_iso()}
        )
        # snapshot() raising must not propagate out of session_live -- but
        # session_live calls lookup(), which itself catches internally per
        # C1's own contract (AC11); this belt-and-braces proves the
        # end-to-end path never raises even if that inner catch were removed.
        assert liveness.session_live("s-registry-boom", cwd=str(repo)) is True

    def test_registry_compare_raises_falls_through_to_layer1_exact_verdict(
        self, tmp_path, monkeypatch
    ):
        # Review: staff-eng-review P2 (PM-overridden, treated as accepted).
        # A registry HIT whose core.stable_pid_alive compare raises (e.g.
        # MissingPsutilError) must fall through to Layer 1/2 byte-unchanged
        # -- exact same fail-open posture as live_session_verdicts' sibling
        # registry arm -- never propagate out of session_live, and never
        # itself mean DEAD.
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        _write_session(
            repo,
            "s-registry-compare-boom",
            {
                "pid": "999",
                "last_activity": "2000-01-01T00:00:00Z",
                "stable_pid": str(2**31 - 1),
                "stable_pid_lstart": "Sat Jan  1 00:00:00 2000",
                "stable_pid_start_epoch": "946684800",
            },
        )
        _write_registry_record(
            registry_dir, "s.json", "s-registry-compare-boom", os.getpid(), _self_create_time()
        )

        real_stable_pid_alive = core.stable_pid_alive
        calls = []

        def _boom_once_then_real(*a, **k):
            calls.append(a)
            if len(calls) == 1:
                raise RuntimeError("simulated stable_pid_alive failure")
            return real_stable_pid_alive(*a, **k)

        monkeypatch.setattr(core, "stable_pid_alive", _boom_once_then_real)
        # Today's exact Layer-1 verdict for this stable_pid/lstart/epoch combo
        # is DEAD (recycled/invalid pid) -- the raising registry compare must
        # not prevent reaching that same verdict, and must not raise.
        assert (
            liveness.session_live("s-registry-compare-boom", cwd=str(repo)) is False
        )
        assert len(calls) == 2


class TestHarnessRegistryVerdictsPrecedence:
    def test_registry_basis_and_age_none(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        _write_session(
            repo,
            "s-registry-basis",
            {"pid": "999", "last_activity": "2000-01-01T00:00:00Z"},
        )
        _write_registry_record(
            registry_dir, "s.json", "s-registry-basis", os.getpid(), _self_create_time()
        )
        verdicts = liveness.live_session_verdicts(cwd=str(repo))
        live, basis, age_sec = verdicts["s-registry-basis"]
        assert live is True
        assert basis == "harness-registry"
        assert age_sec is None

    def test_exactly_one_registry_scan_per_invocation(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        for i in range(5):
            _write_session(
                repo, f"s-count-{i}", {"pid": "1", "last_activity": core.now_iso()}
            )
        calls = []
        real_snapshot = harness_registry.snapshot

        def _counting_snapshot():
            calls.append(1)
            return real_snapshot()

        monkeypatch.setattr(harness_registry, "snapshot", _counting_snapshot)
        liveness.live_session_verdicts(cwd=str(repo))
        assert len(calls) == 1

    def test_registry_none_falls_through_stable_pid_verdict_exact(
        self, tmp_path, monkeypatch
    ):
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        _write_session(
            repo,
            "s-dead-stable-verdict",
            {
                "pid": "999",
                "last_activity": core.now_iso(),
                "stable_pid": str(2**31 - 1),
                "stable_pid_lstart": "Sat Jan  1 00:00:00 2000",
                "stable_pid_start_epoch": "946684800",
            },
        )
        verdicts = liveness.live_session_verdicts(cwd=str(repo))
        live, basis, age_sec = verdicts["s-dead-stable-verdict"]
        assert live is False
        assert basis == "stable-pid"
        assert age_sec is None

    def test_registry_internal_exception_cannot_propagate_in_verdicts(
        self, tmp_path, monkeypatch
    ):
        repo = _make_repo(tmp_path)

        def _boom():
            raise RuntimeError("simulated harness_registry internal failure")

        monkeypatch.setattr(harness_registry, "snapshot", _boom)
        _write_session(
            repo, "s-verdict-boom", {"pid": "1", "last_activity": core.now_iso()}
        )
        verdicts = liveness.live_session_verdicts(cwd=str(repo))
        live, basis, age_sec = verdicts["s-verdict-boom"]
        assert live is True
        assert basis == "recency-window"


class TestHarnessRegistryLiveSessionIdsParity:
    """live_session_ids() stays set-identical to today when the registry
    yields NO-RECORD for every candidate -- including the negative-elapsed
    clock-skew case (AC8)."""

    def test_set_identical_with_registry_present_but_no_matching_records(
        self, tmp_path, monkeypatch
    ):
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        # A registry row exists, but keyed to a DIFFERENT sessionId than any
        # fixture below -- must not influence any of these verdicts.
        _write_registry_record(
            registry_dir, "other.json", "unrelated-sid", os.getpid(), _self_create_time()
        )
        _write_session(
            repo, "live-one", {"pid": "1", "last_activity": core.now_iso()}
        )
        _write_session(
            repo, "stale-one", {"pid": "1", "last_activity": "2000-01-01T00:00:00Z"}
        )
        _write_session(
            repo,
            "future-nostable",
            {"pid": "1", "last_activity": "2099-01-01T00:00:00Z"},
        )
        assert liveness.live_session_ids(cwd=str(repo)) == frozenset({"live-one"})

    def test_registry_hit_extends_live_session_ids(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        registry_dir = tmp_path / "registry"
        monkeypatch.setattr(harness_registry, "registry_dir", lambda: registry_dir)
        # This session would read DEAD on Layer 2 alone (stale recency, no
        # stable_pid) -- the registry hit must rescue it into the live set.
        _write_session(
            repo, "s-rescued", {"pid": "1", "last_activity": "2000-01-01T00:00:00Z"}
        )
        _write_registry_record(
            registry_dir, "s.json", "s-rescued", os.getpid(), _self_create_time()
        )
        assert "s-rescued" in liveness.live_session_ids(cwd=str(repo))


# ---------------------------------------------------------------------------
# newest_record_mtime — THE single shared recency-policy implementation
# (AC6, C5, docs/plans/2026-08-25-the-legacy-touch-record-is-retired-by-
# repointing-its-writers.md). Every filename-keyed mtime probe this chunk
# repoints (session_abandoned below, shape.py, stable_pid_watch.py, core.py)
# and C3's fifth probe (dispatch_checks.py :: _rm_peer_claim_of) import and
# assert against THIS function.
# ---------------------------------------------------------------------------


class TestNewestRecordMtime:
    def test_missing_dir_returns_none(self, tmp_path):
        assert liveness.newest_record_mtime(str(tmp_path / "no-such-dir")) is None

    def test_empty_dir_returns_none(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        assert liveness.newest_record_mtime(str(d)) is None

    def test_ac6b_commit_ledger_shaped_dir_with_no_files_yields_no_refresh(
        self, tmp_path
    ):
        # AC6b: a directory holding only `.commit-ledger`-shaped content (no
        # eligible top-level regular file) must yield no liveness refresh --
        # not silently manufacture a recency signal for a non-session dir.
        d = tmp_path / ".commit-ledger"
        d.mkdir()
        assert liveness.newest_record_mtime(str(d)) is None

    def test_em_session_id_txt_alone_returns_none(self, tmp_path):
        d = tmp_path / "s1"
        d.mkdir()
        (d / "em-session-id.txt").write_text("some-sid\n", encoding="utf-8")
        assert liveness.newest_record_mtime(str(d)) is None

    def test_returns_newest_of_multiple_files(self, tmp_path):
        d = tmp_path / "s1"
        d.mkdir()
        (d / "older.txt").write_text("x", encoding="utf-8")
        _touch(d / "older.txt", _STALE_EPOCH)
        (d / "newer.txt").write_text("x", encoding="utf-8")
        newer_epoch = _STALE_EPOCH + 1000
        _touch(d / "newer.txt", newer_epoch)
        assert liveness.newest_record_mtime(str(d)) == newer_epoch

    def test_em_session_id_txt_excluded_even_when_newest(self, tmp_path):
        # The ownership backpointer is written once and never refreshed --
        # it must never be the value newest_record_mtime returns, even when
        # it happens to have the freshest mtime on disk.
        d = tmp_path / "s1"
        d.mkdir()
        (d / "touched.txt").write_text("x", encoding="utf-8")
        _touch(d / "touched.txt", _STALE_EPOCH)
        (d / "em-session-id.txt").write_text("some-sid\n", encoding="utf-8")
        assert liveness.newest_record_mtime(str(d)) == _STALE_EPOCH


# ---------------------------------------------------------------------------
# session_abandoned — additive predicate (C2, docs/plans/2026-08-19-
# abandonment-is-its-own-verdict.md). NEVER a liveness verdict; NEVER a DEAD
# arm. Signal set + threshold per docs/research/2026-08-19-abandonment-
# signal-census.md.
# ---------------------------------------------------------------------------

_STALE = "2000-01-01T00:00:00Z"
_STALE_EPOCH = 946684800


class TestSessionAbandoned:
    def test_empty_sid_not_abandoned(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert liveness.session_abandoned("", cwd=str(repo)) is False

    def test_missing_dir_not_abandoned(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert liveness.session_abandoned("no-such-session", cwd=str(repo)) is False

    def test_stale_last_activity_alone_is_not_abandoned(self, tmp_path):
        # AC2's named negative-spec test, verbatim: a fixture carrying a
        # stale last_activity and nothing else (no touched.txt, no
        # dispatched-agents.txt) must read NOT-abandoned -- the >= 2
        # independent stale signals floor can never be satisfied by one
        # candidate alone, no matter how stale it is.
        repo = _make_repo(tmp_path)
        _write_session(repo, "s-thin", {"pid": "1", "last_activity": _STALE})
        assert liveness.session_abandoned("s-thin", cwd=str(repo)) is False

    def test_fresh_last_activity_alone_is_not_abandoned(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(
            repo, "s-fresh", {"pid": "1", "last_activity": core.now_iso()}
        )
        assert liveness.session_abandoned("s-fresh", cwd=str(repo)) is False

    def test_two_stale_independent_signals_is_abandoned(self, tmp_path):
        # C5, AC6: `dir_record` (`newest_record_mtime`) now folds in
        # meta.json's OWN file mtime alongside any record file it finds, so
        # this fixture backdates meta.json's mtime to match its (already
        # stale) `last_activity` field content -- realistic (both are
        # written together in production) and necessary so meta.json's
        # write-time mtime doesn't itself read as the freshest file in the
        # dir and mask the genuinely stale `touched.txt` signal below.
        repo = _make_repo(tmp_path)
        sdir = _write_session(
            repo, "s-two-stale", {"pid": "1", "last_activity": _STALE}
        )
        _touch(sdir / "meta.json", _STALE_EPOCH)
        (sdir / "touched.txt").write_text("x", encoding="utf-8")
        _touch(sdir / "touched.txt", _STALE_EPOCH)
        assert liveness.session_abandoned("s-two-stale", cwd=str(repo)) is True

    def test_two_stale_independent_signals_new_dialect_alone_is_abandoned(
        self, tmp_path
    ):
        # AC6 widening: a record file under a DIFFERENT name (not
        # `touched.txt`) still feeds `dir_record` -- the newest-file-in-dir
        # policy, not a re-derived literal, so a future record rename can
        # only DEFER this signal, never DISABLE it.
        repo = _make_repo(tmp_path)
        sdir = _write_session(
            repo, "s-two-stale-new", {"pid": "1", "last_activity": _STALE}
        )
        _touch(sdir / "meta.json", _STALE_EPOCH)
        (sdir / "touch-record.jsonl").write_text("x", encoding="utf-8")
        _touch(sdir / "touch-record.jsonl", _STALE_EPOCH)
        assert liveness.session_abandoned("s-two-stale-new", cwd=str(repo)) is True

    def test_two_stale_independent_signals_both_dialects_is_abandoned(
        self, tmp_path
    ):
        # "Each dialect alone and both together" (C5 brief): both the old
        # and a new-dialect record file present, both stale -- still
        # abandoned, same as either alone.
        repo = _make_repo(tmp_path)
        sdir = _write_session(
            repo, "s-two-stale-both", {"pid": "1", "last_activity": _STALE}
        )
        _touch(sdir / "meta.json", _STALE_EPOCH)
        (sdir / "touched.txt").write_text("x", encoding="utf-8")
        _touch(sdir / "touched.txt", _STALE_EPOCH)
        (sdir / "touch-record.jsonl").write_text("x", encoding="utf-8")
        _touch(sdir / "touch-record.jsonl", _STALE_EPOCH)
        assert liveness.session_abandoned("s-two-stale-both", cwd=str(repo)) is True

    def test_fresh_new_dialect_record_alone_is_not_abandoned(self, tmp_path):
        # Mirrors test_stale_last_activity_but_fresh_touched_txt_is_not_
        # abandoned, but for a record file under the new dialect's name --
        # the primary freshest-signal gate must recognise it exactly like
        # `touched.txt`.
        repo = _make_repo(tmp_path)
        sdir = _write_session(
            repo, "s-mixed-new", {"pid": "1", "last_activity": _STALE}
        )
        _touch(sdir / "meta.json", _STALE_EPOCH)
        (sdir / "touch-record.jsonl").write_text("x", encoding="utf-8")
        # freshly-written file -> fresh mtime (now), no _touch() backdate.
        assert liveness.session_abandoned("s-mixed-new", cwd=str(repo)) is False

    def test_stale_last_activity_but_fresh_touched_txt_is_not_abandoned(self, tmp_path):
        # The primary gate (freshest-of-signals) fires before the >= 2 floor
        # is even consulted: one fresh signal is dispositive.
        repo = _make_repo(tmp_path)
        sdir = _write_session(
            repo, "s-mixed", {"pid": "1", "last_activity": _STALE}
        )
        (sdir / "touched.txt").write_text("x", encoding="utf-8")
        # freshly-written file -> fresh mtime (now), no _touch() backdate.
        assert liveness.session_abandoned("s-mixed", cwd=str(repo)) is False

    def test_stale_last_activity_and_stale_dispatched_agents_is_abandoned(self, tmp_path):
        # See test_two_stale_independent_signals_is_abandoned's comment on
        # why meta.json's own mtime must be backdated alongside its
        # (already stale) `last_activity` field content.
        repo = _make_repo(tmp_path)
        sdir = _write_session(
            repo, "s-dispatch-stale", {"pid": "1", "last_activity": _STALE}
        )
        _touch(sdir / "meta.json", _STALE_EPOCH)
        (sdir / "dispatched-agents.txt").write_text("x", encoding="utf-8")
        _touch(sdir / "dispatched-agents.txt", _STALE_EPOCH)
        assert liveness.session_abandoned("s-dispatch-stale", cwd=str(repo)) is True

    def test_meta_less_stale_touched_txt_is_abandoned(self, tmp_path):
        # No meta.json at all -- touched.txt alone is dispositive, per C1's
        # "Meta-less sid, decided" finding. No >= 2 floor applies (there is
        # structurally only ever one candidate for this population).
        repo = _make_repo(tmp_path)
        sdir = _session_dir_path(repo, "s-metaless-stale")
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "touched.txt").write_text("x", encoding="utf-8")
        _touch(sdir / "touched.txt", _STALE_EPOCH)
        assert liveness.session_abandoned("s-metaless-stale", cwd=str(repo)) is True

    def test_meta_less_fresh_touched_txt_is_not_abandoned(self, tmp_path):
        repo = _make_repo(tmp_path)
        sdir = _session_dir_path(repo, "s-metaless-fresh")
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "touched.txt").write_text("x", encoding="utf-8")
        assert liveness.session_abandoned("s-metaless-fresh", cwd=str(repo)) is False

    def test_meta_less_no_touched_txt_falls_back_to_dir_mtime(self, tmp_path):
        # No meta.json, no touched.txt -- falls back to
        # _dir_recency_fallback_epoch, which is fresh (dir just created), so
        # not abandoned.
        repo = _make_repo(tmp_path)
        sdir = _session_dir_path(repo, "s-metaless-empty")
        sdir.mkdir(parents=True, exist_ok=True)
        assert liveness.session_abandoned("s-metaless-empty", cwd=str(repo)) is False

    def test_unparseable_last_activity_does_not_count_as_a_stale_signal(
        self, tmp_path
    ):
        # Review: coordinator:code-reviewer P2 (coordinatorcode-reviewer-
        # 1da5144e.md) -- core.iso_to_epoch returns 0 on BOTH empty input
        # and parse failure, so an unparseable-but-present last_activity
        # (e.g. "not-a-timestamp") used to score epoch 0 (maximally stale)
        # and count as one of the two independent stale signals. This
        # fixture has a corrupted last_activity AND a genuinely stale
        # touched.txt -- if the corrupted field still counted, that would
        # satisfy the >= 2 floor and read ABANDONED. It must not: a
        # corrupted field contributes NO candidate, so only one real
        # signal (touched.txt) is stale, and the floor is never reached.
        repo = _make_repo(tmp_path)
        sdir = _write_session(
            repo, "s-corrupt-ts", {"pid": "1", "last_activity": "not-a-timestamp"}
        )
        (sdir / "touched.txt").write_text("x", encoding="utf-8")
        _touch(sdir / "touched.txt", _STALE_EPOCH)
        assert liveness.session_abandoned("s-corrupt-ts", cwd=str(repo)) is False

    def test_live1_disabled_lever_has_no_effect(self, tmp_path, monkeypatch):
        # session_abandoned never reads the rollback lever -- toggling it
        # must not change a verdict this function already committed to.
        repo = _make_repo(tmp_path)
        sdir = _write_session(
            repo, "s-lever", {"pid": "1", "last_activity": _STALE}
        )
        _touch(sdir / "meta.json", _STALE_EPOCH)
        (sdir / "touched.txt").write_text("x", encoding="utf-8")
        _touch(sdir / "touched.txt", _STALE_EPOCH)
        before = liveness.session_abandoned("s-lever", cwd=str(repo))
        monkeypatch.setenv("COORDINATOR_SESSION_LAYER1_DISABLE", "1")
        after = liveness.session_abandoned("s-lever", cwd=str(repo))
        assert before is after is True


# ---------------------------------------------------------------------------
# AC1 tripwire: session_abandoned is additive. Capture session_live,
# live_session_ids, live_session_verdicts and session_verdict's outputs
# BEFORE any session_abandoned call, exercise session_abandoned across the
# same corpus/fixtures, then re-capture and assert byte-for-byte equality.
# A diff that changes any of the four functions' return values for any
# input fails this criterion (AC1).
# ---------------------------------------------------------------------------


class TestAC1CharacterizationUnchanged:
    def _four_function_snapshot(self, repo, sids):
        return {
            "session_live": {sid: liveness.session_live(sid, cwd=str(repo)) for sid in sids},
            "live_session_ids": liveness.live_session_ids(cwd=str(repo)),
            "live_session_verdicts": liveness.live_session_verdicts(cwd=str(repo)),
            "session_verdict": {
                sid: liveness.session_verdict(sid, cwd=str(repo)) for sid in sids
            },
        }

    def test_fixture_matrix_unchanged_by_session_abandoned(self, tmp_path):
        repo = _make_repo(tmp_path)
        # A small matrix spanning session_live's own documented arms: Layer-2
        # live, Layer-2 stale, Layer-1 dead-with-fresh-recency, meta-less,
        # and the abandonment predicate's own thin/rich-evidence cases.
        _write_session(repo, "m-live", {"pid": "1", "last_activity": core.now_iso()})
        _write_session(repo, "m-stale", {"pid": "1", "last_activity": _STALE})
        _write_session(
            repo,
            "m-layer1-dead",
            {
                "pid": "1",
                "stable_pid": str(2**31 - 1),
                "stable_pid_lstart": "Sat Jan  1 00:00:00 2000",
                "stable_pid_start_epoch": "946684800",
                "last_activity": core.now_iso(),
            },
        )
        thin_sdir = _write_session(repo, "m-thin", {"pid": "1", "last_activity": _STALE})
        rich_sdir = _write_session(repo, "m-rich", {"pid": "1", "last_activity": _STALE})
        (rich_sdir / "touched.txt").write_text("x", encoding="utf-8")
        _touch(rich_sdir / "touched.txt", _STALE_EPOCH)
        metaless_sdir = _session_dir_path(repo, "m-metaless")
        metaless_sdir.mkdir(parents=True, exist_ok=True)
        (metaless_sdir / "touched.txt").write_text("x", encoding="utf-8")
        _touch(metaless_sdir / "touched.txt", _STALE_EPOCH)

        sids = [
            "m-live", "m-stale", "m-layer1-dead", "m-thin", "m-rich",
            "m-metaless", "no-such-session",
        ]

        before = self._four_function_snapshot(repo, sids)

        # Exercise the new predicate across every sid in the matrix --
        # calling it must not mutate meta.json, touched.txt, or any state
        # the four functions above read.
        for sid in sids:
            liveness.session_abandoned(sid, cwd=str(repo))

        after = self._four_function_snapshot(repo, sids)

        assert before == after
        # Pin the values themselves too, not just before==after equality --
        # a regression that shifted BOTH captures identically would still
        # pass a bare equality check.
        assert before["session_live"]["m-live"] is True
        assert before["session_live"]["m-stale"] is False
        assert before["session_live"]["m-layer1-dead"] is False
        assert before["session_live"]["m-thin"] is False
        assert before["session_live"]["m-rich"] is False
        assert before["session_live"]["no-such-session"] is False

    def test_live_corpus_unchanged_by_session_abandoned(self, tmp_path):
        # Golden-diff against REAL corpus SHAPES (Q20 pattern, see
        # TestLiveSessionIdsCorpus above): the four functions must produce
        # byte-for-byte identical output whether or not session_abandoned is
        # called on the same sids in between.
        #
        # WHY THIS COPIES THE CORPUS INSTEAD OF READING IT IN PLACE. This test
        # asserts a NEGATIVE — that calling `session_abandoned` mutates nothing
        # the other four functions read — by diffing two snapshots. Taken
        # against the live hub on a box running dozens of concurrent sessions,
        # peers write `meta.json`/`touched.txt` and sessions die BETWEEN the two
        # snapshots, so `before != after` for reasons that have nothing to do
        # with the call under test. The comparison cannot isolate its own
        # variable. Measured before this change: six identical runs of this
        # module produced FIVE distinct outcomes, and this test failed 2/6 with
        # nothing in the tree changing.
        #
        # A moving red is worse than a stable one — it trains readers to
        # discount the file, which is how a genuine 6/6 red here
        # (`test_every_non_uuid_real_child_is_denylisted_or_a_file`, reporting
        # seven phantom sessions in the live hub) went unread.
        #
        # The intent — exercise real record SHAPES, not synthetic ones — is
        # preserved: this copies live session dirs into a private fixture hub
        # and diffs against that. What is given up is only concurrency with
        # peers, which was never the property under test.
        real = Path(core.git_root() or ".", ".git", "coordinator-sessions")
        if not real.is_dir():
            pytest.skip("no real .git/coordinator-sessions/ registry on this box")

        repo = _make_repo(tmp_path)
        hub = Path(repo) / ".git" / "coordinator-sessions"
        hub.mkdir(parents=True, exist_ok=True)
        sids = []
        for p in sorted(real.iterdir()):
            if len(sids) >= 50:
                break
            if not p.is_dir() or p.name.startswith("."):
                continue
            try:
                shutil.copytree(p, hub / p.name)
            except OSError:
                # A peer can delete or rewrite a dir mid-copy; skipping it is
                # correct — this test needs real SHAPES, not any specific sid.
                continue
            sids.append(p.name)
        if not sids:
            pytest.skip("no copyable session dirs in the real registry")

        def _verdict_shape(v):
            """A verdict with its AGE dropped.

            ``session_verdict`` returns ``(live, basis, idle_seconds)``, and
            ``idle_seconds`` advances with the wall clock whether or not
            anything touched the corpus. Comparing it across two snapshots
            taken microseconds apart makes this test fail on a second
            boundary: observed 57188 vs 57189, which is the clock ticking,
            not ``session_abandoned`` mutating anything. The property under
            test is that the VERDICT and its BASIS are unchanged; the age is
            asserted separately, with a tolerance, below.
            """
            return v[:2] if isinstance(v, tuple) else v

        def _ages(verdicts):
            return {
                sid: v[2]
                for sid, v in verdicts.items()
                if isinstance(v, tuple) and len(v) > 2 and isinstance(v[2], (int, float))
            }

        def snap():
            verdicts = {sid: liveness.session_verdict(sid, cwd=str(repo)) for sid in sids}
            return {
                "session_live": {
                    sid: liveness.session_live(sid, cwd=str(repo)) for sid in sids
                },
                "live_session_ids": liveness.live_session_ids(cwd=str(repo)),
                "live_session_verdicts": liveness.live_session_verdicts(cwd=str(repo)),
                "session_verdict": {
                    sid: _verdict_shape(v) for sid, v in verdicts.items()
                },
            }, _ages(verdicts)

        before, before_ages = snap()

        for sid in sids:
            liveness.session_abandoned(sid, cwd=str(repo))

        after, after_ages = snap()

        assert before == after

        # The age may advance (the clock runs); it must never go BACKWARDS or
        # jump, which is what a corpus mutation would look like here.
        for sid, before_age in before_ages.items():
            after_age = after_ages.get(sid)
            assert after_age is not None, (
                f"{sid} lost its idle-age reading across session_abandoned()"
            )
            assert 0 <= after_age - before_age <= 60, (
                f"{sid} idle age moved {before_age} -> {after_age} across "
                f"session_abandoned() — that is a corpus mutation, not the clock"
            )


class TestSharedStablePidIsNotAConfidentLiveness:
    """`stable_pid` is not unique per session, and a verdict derived from a
    shared one is not a verdict about either session holding it.

    Root: `state/bug-backlog/2026-08-31-liveness-by-stable-pid-cannot-tell-two-
    sessions-apart.yaml`. Two sessions launched under one ancestor terminal
    carry the same `stable_pid`; while EITHER is alive, BOTH read live, so a
    dead holder's path claim never frees -- `clear_claim_if_dead` keeps the row
    while any holder reads live, and `coordinator-safe-commit` refuses with a
    remedy ("coordinate with the holder BY NAME") that is unfollowable because
    a gone session has already left the harness registry.

    These pin the DETECTION only. `live` stays True on a shared handle
    deliberately: refusing-and-keeping is the correct conservative behaviour for
    every current caller, so this change makes the answer honest without
    changing what anyone does with it. A test that asserted `live is False`
    here would be pinning a caller decision this chunk does not make.
    """

    def _session(self, base, sid, stable_pid):
        sdir = base / sid
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "meta.json").write_text(
            json.dumps(
                {
                    "stable_pid": stable_pid,
                    "stable_pid_start_epoch": 1,
                    "last_activity": "2026-09-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        return sdir

    def test_shared_pid_is_reported_shared_and_unshared_is_not(self, tmp_path):
        base = tmp_path / "sessions"
        self._session(base, "aaaaaaaa-1111-2222-3333-444444444444", "5000")
        self._session(base, "bbbbbbbb-1111-2222-3333-444444444444", "5000")
        self._session(base, "cccccccc-1111-2222-3333-444444444444", "6000")

        shared = liveness._shared_stable_pids(str(base))

        assert "5000" in shared, (
            "a stable_pid carried by two sessions must be reported shared -- "
            "this is the whole discriminator"
        )
        assert "6000" not in shared, (
            "a stable_pid carried by exactly one session is a confident handle "
            "and must NOT be degraded to shared"
        )

    def test_empty_and_missing_dirs_are_not_shared(self, tmp_path):
        assert liveness._shared_stable_pids(str(tmp_path / "nope")) == frozenset()
        assert liveness._shared_stable_pids(None) == frozenset()

    def test_cache_revalidates_when_a_session_appears(self, tmp_path):
        base = tmp_path / "sessions"
        self._session(base, "aaaaaaaa-1111-2222-3333-444444444444", "5000")
        assert liveness._shared_stable_pids(str(base)) == frozenset()

        # A second session claiming the same handle must flip the answer. If
        # the memo keyed on path alone, this would keep returning the stale
        # empty set and the defect would survive its own fix.
        self._session(base, "bbbbbbbb-1111-2222-3333-444444444444", "5000")
        os.utime(base, None)

        assert "5000" in liveness._shared_stable_pids(str(base))

    def test_stray_non_session_dirs_cannot_manufacture_a_shared_handle(self, tmp_path):
        """A leftover directory must not degrade a live session's handle.

        Found on the real corpus while landing this: `.git/coordinator-sessions/`
        carries `sess-1` and `test-session` (test leftovers that
        `TestLiveSessionIdsCorpus` independently flags as phantom sessions), and
        `sess-1` carries the same `stable_pid` as a LIVE session. Counting it
        would report that live session's handle as shared -- an indeterminate
        manufactured entirely by corpus pollution, in a predicate whose whole
        purpose is to stop liveness answers being about the wrong thing.

        `_NON_SESSION_DIR_NAMES` does not cover these; being absent from it is
        precisely what the corpus test complains about. So shape is the
        discriminator, not the denylist.
        """
        base = tmp_path / "sessions"
        real = "11111111-2222-3333-4444-555555555555"
        self._session(base, real, "7000")
        self._session(base, "sess-1", "7000")
        self._session(base, "test-session", "7000")

        assert liveness._shared_stable_pids(str(base)) == frozenset(), (
            "only UUID-shaped session dirs may count toward sharing -- two "
            "stray dirs must not make a single live session's handle read "
            "indeterminate"
        )
