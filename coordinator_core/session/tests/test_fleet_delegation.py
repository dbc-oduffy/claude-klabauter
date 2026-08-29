"""
coordinator_core.session.tests.test_fleet_delegation — tests for
coordinator_core.session.fleet_delegation (docs/plans/2026-08-28-the-ask-
the-pm-step-gets-an-artifact-to-check.md, chunk C2).

Isolation: every test monkeypatches ``fleet_delegation.settings_home`` to a
per-test ``tmp_path`` so the real ``<home>/.coordinator-claude-settings``
tree is never touched, and monkeypatches ``fleet_delegation.authorship_verdict``
to a fixed HUMAN/AGENT/UNRESOLVED verdict so the writer's own authorship gate
is exercised without a real ancestry walk (that walk is C1's own test
surface, not this module's).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core.session import fleet_delegation as fd
from coordinator_core.session.grant_authorship import AuthorshipVerdict, Verdict


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _isolate_settings_home(tmp_path, monkeypatch):
    monkeypatch.setattr(fd, "settings_home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def human_verdict(monkeypatch):
    monkeypatch.setattr(
        fd, "authorship_verdict", lambda start_pid=None: AuthorshipVerdict(Verdict.HUMAN, "posix-parent-miss:name-mismatch")
    )


@pytest.fixture
def agent_verdict(monkeypatch):
    monkeypatch.setattr(
        fd, "authorship_verdict", lambda start_pid=None: AuthorshipVerdict(Verdict.AGENT, "posix-parent-hit")
    )


@pytest.fixture
def unresolved_verdict(monkeypatch):
    monkeypatch.setattr(
        fd, "authorship_verdict", lambda start_pid=None: AuthorshipVerdict(Verdict.UNRESOLVED, "walk-miss:psutil-absent")
    )


def _write_ok(**overrides):
    now = _now()
    kwargs = dict(
        designated_pid=1234,
        designated_create_time=1000.5,
        classes=["some-class"],
        granted_at=_iso(now),
        expires_at=_iso(now + timedelta(hours=1)),
        granted_by="human",
        note="pm said so",
    )
    kwargs.update(overrides)
    return fd.write_fleet_delegation(**kwargs)


class _FakeLiveProcess:
    def __init__(self, create_time):
        self._create_time = create_time

    def create_time(self):
        return self._create_time


class _FakeNoSuchProcess(Exception):
    pass


class _FakeAccessDenied(Exception):
    pass


class _FakePsutilModule:
    NoSuchProcess = _FakeNoSuchProcess
    AccessDenied = _FakeAccessDenied

    def __init__(self, process_factory):
        self._process_factory = process_factory

    def Process(self, pid):
        return self._process_factory(pid)


def _install_fake_psutil(monkeypatch, process_factory):
    monkeypatch.setattr(fd, "_psutil", lambda: _FakePsutilModule(process_factory))


# ---------------------------------------------------------------------------
# write_fleet_delegation — HARD CONSTRAINTS
# ---------------------------------------------------------------------------


class TestWriteFleetDelegation:
    def test_rejects_when_authorship_is_agent(self, agent_verdict, tmp_path):
        ok, reason = _write_ok()
        assert ok is False
        assert "authorship-refused:agent" in reason
        assert not (tmp_path / "fleet-delegation.json").exists()

    def test_rejects_when_authorship_is_unresolved(self, unresolved_verdict, tmp_path):
        ok, reason = _write_ok()
        assert ok is False
        assert "authorship-refused:unresolved" in reason
        assert not (tmp_path / "fleet-delegation.json").exists()

    def test_rejects_missing_expires_at(self, human_verdict, tmp_path):
        ok, reason = _write_ok(expires_at=None)
        assert ok is False
        assert "expires_at is required" in reason
        assert not (tmp_path / "fleet-delegation.json").exists()

    def test_rejects_granted_at_too_far_in_past(self, human_verdict, tmp_path):
        now = _now()
        ok, reason = _write_ok(
            granted_at=_iso(now - timedelta(minutes=10)),
            expires_at=_iso(now + timedelta(hours=1)),
        )
        assert ok is False
        assert "granted_at" in reason
        assert not (tmp_path / "fleet-delegation.json").exists()

    def test_rejects_future_granted_at_even_within_12h_window(self, human_verdict, tmp_path):
        """A future granted_at is itself a rejection, even when
        expires_at - granted_at <= 12h (Review: staff-eng (the Staff Engineer), finding 4)."""
        now = _now()
        future_granted = now + timedelta(hours=6)
        ok, reason = _write_ok(
            granted_at=_iso(future_granted),
            expires_at=_iso(future_granted + timedelta(hours=1)),
        )
        assert ok is False
        assert "granted_at" in reason
        assert not (tmp_path / "fleet-delegation.json").exists()

    def test_rejects_expires_at_more_than_12h_after_wall_clock(self, human_verdict, tmp_path):
        now = _now()
        ok, reason = _write_ok(
            granted_at=_iso(now),
            expires_at=_iso(now + timedelta(hours=12, minutes=1)),
        )
        assert ok is False
        assert "expires_at" in reason
        assert not (tmp_path / "fleet-delegation.json").exists()

    def test_12h_ceiling_anchors_to_wall_clock_not_granted_at(self, human_verdict, tmp_path):
        """expires_at must be measured against wall clock at write time, never
        against a caller-supplied granted_at that is itself within tolerance
        of now but stale enough that granted_at + 12h would wrongly pass."""
        now = _now()
        # granted_at within the 5-min tolerance of now, but expires_at is
        # 12h + a bit past NOW (not past granted_at) -- must reject.
        ok, reason = _write_ok(
            granted_at=_iso(now - timedelta(minutes=1)),
            expires_at=_iso(now + timedelta(hours=12, minutes=5)),
        )
        assert ok is False
        assert "expires_at" in reason

    def test_rejects_granted_by_not_human(self, human_verdict, tmp_path):
        ok, reason = _write_ok(granted_by="ceremony")
        assert ok is False
        assert "granted_by" in reason
        assert not (tmp_path / "fleet-delegation.json").exists()

    def test_rejects_never_delegable_class(self, human_verdict, tmp_path):
        ok, reason = _write_ok(classes=["scope-change", "some-class"])
        assert ok is False
        assert "not delegable" in reason
        assert not (tmp_path / "fleet-delegation.json").exists()

    def test_every_never_delegable_member_is_rejected(self, human_verdict, tmp_path):
        for cls in fd.NEVER_DELEGABLE:
            ok, reason = _write_ok(classes=[cls])
            assert ok is False, cls
            assert not (tmp_path / "fleet-delegation.json").exists()

    def test_accepts_a_valid_grant_and_round_trips(self, human_verdict, tmp_path):
        ok, reason = _write_ok()
        assert ok is True
        assert reason is None

        record = fd.read_fleet_delegation()
        assert record["schema_version"] == 1
        assert record["designated"] == {"pid": 1234, "create_time": 1000.5}
        assert record["classes"] == ["some-class"]
        assert record["granted_by"] == "human"
        assert record["authorship"]["verdict"] == "human"
        assert record["note"] == "pm said so"

    def test_overwrite_is_atomic_and_leaves_no_temp_file(self, human_verdict, tmp_path):
        _write_ok()
        ok, _ = _write_ok(classes=["another-class"])
        assert ok is True
        record = fd.read_fleet_delegation()
        assert record["classes"] == ["another-class"]
        leftovers = [p for p in tmp_path.iterdir() if p.name != "fleet-delegation.json"]
        assert leftovers == []


# ---------------------------------------------------------------------------
# read_fleet_delegation — raw reader
# ---------------------------------------------------------------------------


class TestReadFleetDelegation:
    def test_absent_file_returns_none(self, tmp_path):
        assert fd.read_fleet_delegation() is None

    def test_malformed_json_returns_none(self, tmp_path):
        (tmp_path / "fleet-delegation.json").write_text("{not json", encoding="utf-8")
        assert fd.read_fleet_delegation() is None

    def test_non_object_json_returns_none(self, tmp_path):
        (tmp_path / "fleet-delegation.json").write_text("[1, 2]", encoding="utf-8")
        assert fd.read_fleet_delegation() is None


# ---------------------------------------------------------------------------
# check_fleet_delegation — the authorization predicate
# ---------------------------------------------------------------------------


class TestCheckFleetDelegation:
    def test_no_file_is_absent(self, tmp_path):
        granted, record = fd.check_fleet_delegation("some-class")
        assert (granted, record) == (False, None)

    def test_expired_and_missing_are_identical_by_value(self, human_verdict, tmp_path, monkeypatch):
        """The whole of owed item (3): expired reads BYTE-IDENTICAL to the
        no-file case -- not merely falsey, the same (bool, None) tuple by
        value. This assertion must not be softened."""
        no_file_result = fd.check_fleet_delegation("some-class")

        now = _now()
        _write_ok(
            granted_at=_iso(now - timedelta(hours=2)),
            expires_at=_iso(now - timedelta(hours=1)),
        )
        _install_fake_psutil(monkeypatch, lambda pid: _FakeLiveProcess(1000.5))

        expired_result = fd.check_fleet_delegation("some-class")

        assert expired_result == no_file_result == (False, None)

    def test_grants_when_class_covered_and_designated_is_live(self, human_verdict, tmp_path, monkeypatch):
        _write_ok(classes=["target-class"], designated_pid=555, designated_create_time=42.0)
        _install_fake_psutil(monkeypatch, lambda pid: _FakeLiveProcess(42.0))

        granted, record = fd.check_fleet_delegation("target-class")

        assert granted is True
        assert record["classes"] == ["target-class"]

    def test_denies_when_class_not_covered(self, human_verdict, tmp_path, monkeypatch):
        _write_ok(classes=["target-class"])
        _install_fake_psutil(monkeypatch, lambda pid: _FakeLiveProcess(1000.5))

        granted, record = fd.check_fleet_delegation("other-class")

        assert granted is False
        assert record is not None  # record still surfaced for audit/denial quoting

    def test_denies_when_authorship_is_not_human(self, human_verdict, tmp_path, monkeypatch):
        _write_ok(classes=["target-class"])
        # Tamper the stored record's authorship after the fact -- the reader
        # never re-verifies authorship, it reads whatever the writer stored.
        grant_file = tmp_path / "fleet-delegation.json"
        record = json.loads(grant_file.read_text(encoding="utf-8"))
        record["authorship"] = {"verdict": "agent", "reason": "tampered"}
        grant_file.write_text(json.dumps(record), encoding="utf-8")
        _install_fake_psutil(monkeypatch, lambda pid: _FakeLiveProcess(1000.5))

        granted, record = fd.check_fleet_delegation("target-class")

        assert granted is False

    def test_denies_unknown_schema_version(self, human_verdict, tmp_path, monkeypatch):
        _write_ok(classes=["target-class"])
        grant_file = tmp_path / "fleet-delegation.json"
        record = json.loads(grant_file.read_text(encoding="utf-8"))
        record["schema_version"] = 2
        grant_file.write_text(json.dumps(record), encoding="utf-8")
        _install_fake_psutil(monkeypatch, lambda pid: _FakeLiveProcess(1000.5))

        granted, _record = fd.check_fleet_delegation("target-class")

        assert granted is False

    def test_denies_when_designated_pid_recycled_different_create_time(self, human_verdict, tmp_path, monkeypatch):
        _write_ok(classes=["target-class"], designated_pid=555, designated_create_time=42.0)
        # A different process now owns pid 555 (create_time mismatch) --
        # must read not-live, never "close enough".
        _install_fake_psutil(monkeypatch, lambda pid: _FakeLiveProcess(9999.0))

        granted, record = fd.check_fleet_delegation("target-class")

        assert granted is False
        assert record is not None

    def test_denies_when_designated_process_not_found(self, human_verdict, tmp_path, monkeypatch):
        _write_ok(classes=["target-class"])

        def _raise(pid):
            raise _FakeNoSuchProcess()

        _install_fake_psutil(monkeypatch, _raise)

        granted, _record = fd.check_fleet_delegation("target-class")

        assert granted is False

    def test_liveness_probe_access_denied_fails_closed(self, human_verdict, tmp_path, monkeypatch):
        _write_ok(classes=["target-class"])

        def _raise(pid):
            raise _FakeAccessDenied()

        _install_fake_psutil(monkeypatch, _raise)

        granted, _record = fd.check_fleet_delegation("target-class")

        assert granted is False

    def test_liveness_probe_os_error_fails_closed(self, human_verdict, tmp_path, monkeypatch):
        _write_ok(classes=["target-class"])

        def _raise(pid):
            raise OSError("disk full")

        _install_fake_psutil(monkeypatch, _raise)

        granted, _record = fd.check_fleet_delegation("target-class")

        assert granted is False

    def test_liveness_probe_arbitrary_exception_fails_closed(self, human_verdict, tmp_path, monkeypatch):
        _write_ok(classes=["target-class"])

        def _raise(pid):
            raise ValueError("something unexpected")

        _install_fake_psutil(monkeypatch, _raise)

        granted, _record = fd.check_fleet_delegation("target-class")

        assert granted is False

    def test_liveness_probe_psutil_absent_fails_closed(self, human_verdict, tmp_path, monkeypatch):
        _write_ok(classes=["target-class"])
        monkeypatch.setattr(fd, "_psutil", lambda: None)

        granted, _record = fd.check_fleet_delegation("target-class")

        assert granted is False

    def test_malformed_designated_field_fails_closed(self, human_verdict, tmp_path, monkeypatch):
        _write_ok(classes=["target-class"])
        grant_file = tmp_path / "fleet-delegation.json"
        record = json.loads(grant_file.read_text(encoding="utf-8"))
        record["designated"] = "not-a-dict"
        grant_file.write_text(json.dumps(record), encoding="utf-8")
        _install_fake_psutil(monkeypatch, lambda pid: _FakeLiveProcess(1000.5))

        granted, _record = fd.check_fleet_delegation("target-class")

        assert granted is False


class TestAuthorshipVerdictWiring:
    """Smoke test for the real (unmocked) ``fd.authorship_verdict`` binding —
    every other writer test in this module monkeypatches ``fd.authorship_verdict``
    to a fixed verdict (module docstring), so nothing else here would catch a
    rename or signature drift in ``grant_authorship.authorship_verdict``.
    (Review: code-reviewer, finding 3.)
    """

    def test_write_refuses_under_real_authorship_walk_from_pytest(self, tmp_path):
        # Deliberately does NOT patch fd.authorship_verdict — exercises the
        # real import binding end-to-end. Running under pytest, the calling
        # process is agent-driven (AGENT or UNRESOLVED, never HUMAN), so the
        # writer must refuse — proving both that the binding resolves to the
        # real grant_authorship.authorship_verdict and that its default
        # authorship_start_pid=None wiring is intact.
        ok, reason = _write_ok()

        assert ok is False
        assert reason.startswith("authorship-refused:")
