"""
Tests for coordinator_core.group_em.nomination -- all four verdict cases, write atomicity, and
that liveness never consults a recorded pid.

Every test points at a tmp_path settings-home / record directory. Writing to the real
`<settings-home>/state/group-em/` would steal the crown from a live peer on this box.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.group_em import nomination
from coordinator_core.session import harness_registry


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
    }
    # Refreshed, not merely left alone -- nominated_at exists on both reads.
    assert nomination.read_record(repo_root, record_dir)["nominated_at"] is not None
    assert first is not None


def test_record_names_another_live_does_not_claim(repo_root, record_dir, monkeypatch):
    nomination.claim(repo_root, "sid-incumbent", directory=record_dir)

    monkeypatch.setattr(nomination, "session_live", lambda sid: True)
    monkeypatch.setattr(
        harness_registry, "lookup", lambda sid: pytest.fail("must not be needed when live")
    )

    result = nomination.claim(repo_root, "sid-challenger", directory=record_dir)

    assert result["claimed"] is False
    assert result["holder"] == "sid-incumbent"
    assert result["superseded_incumbent"]["session_id"] == "sid-incumbent"
    assert result["superseded_incumbent"]["live"] is True
    assert result["superseded_incumbent"]["live_reason"] == "live"

    # Never claimed -- record on disk is unchanged.
    on_disk = nomination.read_record(repo_root, record_dir)
    assert on_disk["session_id"] == "sid-incumbent"


def test_record_names_another_dead_does_not_claim_either(repo_root, record_dir, monkeypatch):
    nomination.claim(repo_root, "sid-incumbent", directory=record_dir)

    monkeypatch.setattr(nomination, "session_live", lambda sid: False)
    monkeypatch.setattr(harness_registry, "lookup", lambda sid: None)

    result = nomination.claim(repo_root, "sid-challenger", directory=record_dir)

    assert result["claimed"] is False
    assert result["holder"] == "sid-incumbent"
    assert result["superseded_incumbent"]["live"] is False
    assert result["superseded_incumbent"]["live_reason"] == "no_registry_record"

    on_disk = nomination.read_record(repo_root, record_dir)
    assert on_disk["session_id"] == "sid-incumbent"


def test_record_names_another_dead_pid_not_running_reason(repo_root, record_dir, monkeypatch):
    nomination.claim(repo_root, "sid-incumbent", directory=record_dir)

    class _Row:
        pid = 999999

    monkeypatch.setattr(nomination, "session_live", lambda sid: False)
    monkeypatch.setattr(harness_registry, "lookup", lambda sid: _Row())

    result = nomination.claim(repo_root, "sid-challenger", directory=record_dir)

    assert result["claimed"] is False
    assert result["superseded_incumbent"]["live"] is False
    assert result["superseded_incumbent"]["live_reason"] == "pid_not_running"


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


def test_built_record_never_carries_a_pid_field(repo_root, record_dir):
    nomination.claim(repo_root, "sid-a", directory=record_dir)
    record = nomination.read_record(repo_root, record_dir)
    assert "pid" not in record


def test_claim_response_never_carries_a_pid_field(repo_root, record_dir):
    result = nomination.claim(repo_root, "sid-a", directory=record_dir)
    assert "pid" not in json.dumps(result)
