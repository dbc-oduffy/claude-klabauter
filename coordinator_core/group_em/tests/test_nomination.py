"""
Tests for coordinator_core.group_em.nomination -- all five verdict cases (fresh claim, re-entry,
live-incumbent refusal, no-registry-record refusal, and pid-not-running auto-replace), write
atomicity, and that liveness never consults a recorded pid.

Every test points at a tmp_path settings-home / record directory. Writing to the real
`<settings-home>/state/group-em/` would steal the Group-EM from a live peer on this box.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.group_em import nomination
from coordinator_core.session import harness_registry
from coordinator_core.session import liveness as _liveness


@pytest.fixture
def repo_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return str(root)


@pytest.fixture
def record_dir(tmp_path):
    d = tmp_path / "group-em-records"
    d.mkdir()
    return d


def test_no_record_claims_it(repo_root, record_dir):
    result = nomination.claim(repo_root, "sid-new", directory=record_dir)
    assert result == {
        "claimed": True,
        "holder": "sid-new",
        "already_held": False,
        "superseded_incumbent": None,
        "replaced_holder": None,
    }
    on_disk = nomination.read_record(repo_root, record_dir)
    assert on_disk["session_id"] == "sid-new"
    assert "pid" not in on_disk


def test_record_already_names_us_refreshes_and_reports_already_held(repo_root, record_dir):
    nomination.claim(repo_root, "sid-us", directory=record_dir)
    first = nomination.read_record(repo_root, record_dir)["nominated_at"]

    result = nomination.claim(repo_root, "sid-us", directory=record_dir)

    assert result == {
        "claimed": True,
        "holder": "sid-us",
        "already_held": True,
        "superseded_incumbent": None,
        "replaced_holder": None,
    }
    # Refreshed, not merely left alone -- nominated_at exists on both reads.
    assert nomination.read_record(repo_root, record_dir)["nominated_at"] is not None
    assert first is not None


def test_record_names_another_live_does_not_claim(repo_root, record_dir, monkeypatch):
    nomination.claim(repo_root, "sid-incumbent", directory=record_dir)

    monkeypatch.setattr(nomination, "session_live", lambda sid: True)
    monkeypatch.setattr(
        _liveness,
        "_cached_registry_lookup",
        lambda sid: pytest.fail("must not be needed when live"),
    )

    result = nomination.claim(repo_root, "sid-challenger", directory=record_dir)

    assert result["claimed"] is False
    assert result["holder"] == "sid-incumbent"
    assert result["superseded_incumbent"]["session_id"] == "sid-incumbent"
    assert result["superseded_incumbent"]["live"] is True
    assert result["superseded_incumbent"]["live_reason"] == "live"
    assert result["replaced_holder"] is None

    # Never claimed -- record on disk is unchanged.
    on_disk = nomination.read_record(repo_root, record_dir)
    assert on_disk["session_id"] == "sid-incumbent"


def test_record_names_another_no_registry_record_does_not_claim(repo_root, record_dir, monkeypatch):
    """Absence of registry evidence (no row for the incumbent's session_id at all) stays a
    refusal -- this fleet is multi-machine, and no row is indistinguishable from a session
    on another machine or with its messaging gate off. Never auto-replaced."""
    nomination.claim(repo_root, "sid-incumbent", directory=record_dir)

    monkeypatch.setattr(nomination, "session_live", lambda sid: False)
    monkeypatch.setattr(_liveness, "_cached_registry_lookup", lambda sid: None)

    result = nomination.claim(repo_root, "sid-challenger", directory=record_dir)

    assert result["claimed"] is False
    assert result["holder"] == "sid-incumbent"
    assert result["superseded_incumbent"]["live"] is False
    assert result["superseded_incumbent"]["live_reason"] == "no_registry_record"
    assert result["replaced_holder"] is None

    on_disk = nomination.read_record(repo_root, record_dir)
    assert on_disk["session_id"] == "sid-incumbent"


def test_record_names_another_pid_not_running_auto_replaces(repo_root, record_dir, monkeypatch):
    """POSITIVE evidence of death (a harness registry row exists for the incumbent's
    session_id and its pid is not running) auto-replaces -- a dead-but-unreaped record is
    this mode's steady state, not an anomaly. The replacement must be VISIBLE: it lands in
    its own `replaced_holder` field, distinct from `superseded_incumbent` (which stays
    None -- this was not a refusal)."""
    nomination.claim(
        repo_root, "sid-incumbent", peer_name="incumbent-peer", nominated_by="someone",
        directory=record_dir,
    )

    class _Row:
        pid = 999999

    monkeypatch.setattr(nomination, "session_live", lambda sid: False)
    monkeypatch.setattr(_liveness, "_cached_registry_lookup", lambda sid: _Row())

    result = nomination.claim(repo_root, "sid-challenger", directory=record_dir)

    assert result["claimed"] is True
    assert result["holder"] == "sid-challenger"
    assert result["already_held"] is False

    # Not a refusal -- superseded_incumbent stays absent-of-value (None), and the
    # replacement is reported ONLY via its own dedicated field, never folded in here.
    assert result["superseded_incumbent"] is None

    # The visibility contract: `replaced_holder` is populated, names the replaced
    # session, and is a DISTINCT key from `superseded_incumbent`. A test that only
    # checked `claimed is True` would pass a silently-swallowed replacement -- the
    # exact failure mode (a session running under a dead session's Group-EM, caught only
    # by a human noticing a missing statusline glyph) this field exists to prevent.
    assert result["replaced_holder"] is not None
    assert result["replaced_holder"] != result["superseded_incumbent"]
    assert result["replaced_holder"]["session_id"] == "sid-incumbent"
    assert result["replaced_holder"]["peer_name"] == "incumbent-peer"
    assert result["replaced_holder"]["nominated_by"] == "someone"
    assert result["replaced_holder"]["live"] is False
    assert result["replaced_holder"]["live_reason"] == "pid_not_running"

    # The Group-EM was actually claimed -- the record on disk now names the challenger.
    on_disk = nomination.read_record(repo_root, record_dir)
    assert on_disk["session_id"] == "sid-challenger"

    # The trace, not just the ephemeral return dict: re-read the record from disk and
    # confirm the replaced incumbent's identity was persisted there. A caller that
    # discards the return value must still be able to see, on a cold read, that this
    # was a replacement -- never indistinguishable from a repo nobody ever held.
    assert on_disk["replaced_holder_session_id"] == "sid-incumbent"
    assert on_disk["replaced_holder_name"] == "incumbent-peer"
    assert on_disk["replaced_nominated_at"] == result["replaced_holder"]["nominated_at"]
    assert on_disk["replaced_live_reason"] == "pid_not_running"


def test_pid_not_running_replace_leaves_no_trace_on_a_prior_first_claim(
    repo_root, record_dir, monkeypatch
):
    """A record that was never replaced (the ordinary first-claim/self-refresh path)
    must NOT carry any `replaced_*` keys -- additive only, on the replace branch alone."""
    nomination.claim(repo_root, "sid-solo", directory=record_dir)

    on_disk = nomination.read_record(repo_root, record_dir)
    assert "replaced_holder_session_id" not in on_disk
    assert "replaced_holder_name" not in on_disk
    assert "replaced_nominated_at" not in on_disk
    assert "replaced_live_reason" not in on_disk


# Review: coordinatorcode-reviewer (finding #3) -- pin the `_build_record` scoping
# invariant with an on-disk assertion rather than resting on the claim() return dict,
# which would pass even if `replaced_*` leaked into the refresh path.
def test_same_session_refresh_leaves_no_replaced_trace_on_disk(repo_root, record_dir):
    """A same-session refresh (`already_held: True`) must not carry any `replaced_*`
    key forward on disk -- `_build_record` never emits them, and this asserts the
    written record itself, not the `claim()` return value."""
    nomination.claim(repo_root, "sid-us", directory=record_dir)
    result = nomination.claim(repo_root, "sid-us", directory=record_dir)
    assert result["already_held"] is True

    on_disk = nomination.read_record(repo_root, record_dir)
    assert "replaced_holder_session_id" not in on_disk
    assert "replaced_holder_name" not in on_disk
    assert "replaced_nominated_at" not in on_disk
    assert "replaced_live_reason" not in on_disk


def test_reentry_by_holder_still_reports_already_held_true(repo_root, record_dir):
    """Re-entry by the current holder is unchanged by the auto-replace split -- it must
    never be mistaken for a replacement of itself."""
    nomination.claim(repo_root, "sid-us", directory=record_dir)

    result = nomination.claim(repo_root, "sid-us", directory=record_dir)

    assert result["claimed"] is True
    assert result["already_held"] is True
    assert result["superseded_incumbent"] is None
    assert result["replaced_holder"] is None


def test_write_is_atomic_no_tmp_file_left_behind(repo_root, record_dir):
    nomination.claim(repo_root, "sid-a", directory=record_dir)

    leftovers = list(record_dir.glob("*.tmp"))
    assert leftovers == []

    path = nomination._record_path(repo_root, record_dir)
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["session_id"] == "sid-a"


def test_write_atomic_failure_cleans_up_tmp_and_propagates(repo_root, record_dir, monkeypatch):
    def _boom(a, b):
        raise OSError("disk full")

    monkeypatch.setattr(nomination.os, "replace", _boom)

    with pytest.raises(OSError):
        nomination.claim(repo_root, "sid-a", directory=record_dir)

    assert list(record_dir.glob("*.tmp")) == []
    # No partial record was left in place of a prior write either.
    assert not nomination._record_path(repo_root, record_dir).exists()


def test_is_live_never_reads_a_recorded_pid(repo_root, record_dir, monkeypatch):
    """The nomination record carries no `pid` field at all, and even if a caller injected one
    onto a record dict by hand, `is_live` must not consult it -- only `session_id` feeds the
    liveness join.
    """
    record_with_bogus_pid = {
        "version": 1,
        "repo_root": repo_root,
        "session_id": "sid-x",
        "peer_name": None,
        "nominated_at": "2026-08-30T00:00:00Z",
        "nominated_by": None,
        "pid": 1,  # must be ignored entirely
    }

    seen_args = []

    def _fake_session_live(sid):
        seen_args.append(sid)
        return True

    monkeypatch.setattr(nomination, "session_live", _fake_session_live)

    result = nomination.is_live(record_with_bogus_pid)

    assert result.live is True
    assert result.live_reason == "live"
    # Only the session_id was passed through -- never anything pid-shaped.
    assert seen_args == ["sid-x"]


def test_is_live_consults_registry_at_most_once_per_call(repo_root, record_dir, monkeypatch):
    """Finding 8 (overengineering-reviewer): `is_live` must not read the harness registry
    twice to answer one question -- `session_live`'s own Source-0 check and the not-live
    reason label must share ONE registry consult, not two independent `snapshot()` scans.
    Forces `_liveness`'s TTL cache to be genuinely cold (never populated by `session_live`
    for this sid) and counts calls into `harness_registry.snapshot` -- the ONE scan
    `harness_registry.lookup`/`_cached_registry_lookup` are both defined over.
    """
    nomination.claim(repo_root, "sid-incumbent", directory=record_dir)

    # Force the TTL cache cold so this run's single scan is attributable to this call only.
    monkeypatch.setattr(_liveness, "_registry_snapshot_cache", None)
    monkeypatch.setattr(_liveness, "_registry_snapshot_cache_at", None)

    scan_calls = []
    real_snapshot = harness_registry.snapshot

    def _counting_snapshot():
        scan_calls.append(1)
        return real_snapshot()

    monkeypatch.setattr(harness_registry, "snapshot", _counting_snapshot)

    record = nomination.read_record(repo_root, record_dir)
    nomination.is_live(record)

    assert len(scan_calls) == 1


def test_built_record_never_carries_a_pid_field(repo_root, record_dir):
    nomination.claim(repo_root, "sid-a", directory=record_dir)
    record = nomination.read_record(repo_root, record_dir)
    assert "pid" not in record


def test_claim_response_never_carries_a_pid_field(repo_root, record_dir):
    result = nomination.claim(repo_root, "sid-a", directory=record_dir)
    assert "pid" not in json.dumps(result)
