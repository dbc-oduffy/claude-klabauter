"""
coordinator_core.ops.tests.test_delegation_check — tests for
coordinator_core.ops.delegation_check (docs/plans/2026-08-28-the-ask-the-
pm-step-gets-an-artifact-to-check.md, chunk C5).

Isolation: every functional test monkeypatches
``fleet_delegation.settings_home`` to a per-test ``tmp_path`` (same
discipline as ``coordinator_core/session/tests/test_fleet_delegation.py``)
so the real ``<home>/.coordinator-claude-settings`` tree is never touched,
and monkeypatches ``fleet_delegation.authorship_verdict`` to a fixed HUMAN
verdict so the writer's own authorship gate is satisfied without a real
ancestry walk.
"""

from __future__ import annotations

import inspect
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from coordinator_core.ops.delegation_check import check_delegation
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
        fd,
        "authorship_verdict",
        lambda start_pid=None: AuthorshipVerdict(Verdict.HUMAN, "posix-parent-miss:name-mismatch"),
    )


def _write_live_grant(**overrides):
    """Write a grant designating THIS process (real, live pid/create_time)
    so ``_designated_live``'s real psutil probe (not a fake) resolves live
    -- the budget/functional tests exercise the real read path end to end.
    """
    import psutil

    this_proc = psutil.Process(os.getpid())
    now = _now()
    kwargs = dict(
        designated_pid=os.getpid(),
        designated_create_time=this_proc.create_time(),
        # A real delegable class, not the name of the step -- see the
        # falsifier's FIXTURE_CLASS note. "ask-the-pm" was the gate's name,
        # never a class, and the write-time allow-list is what caught it.
        classes=["execute-approved-plan"],
        granted_at=_iso(now),
        expires_at=_iso(now + timedelta(hours=1)),
        granted_by="human",
        note="pm said so",
    )
    kwargs.update(overrides)
    ok, reason = fd.write_fleet_delegation(**kwargs)
    assert ok is True, f"fixture grant write must succeed: {reason!r}"


# ---------------------------------------------------------------------------
# TESTABLE OBLIGATION (criterion clause 3 / LEG 3) -- signature shape
# ---------------------------------------------------------------------------


class TestSignatureShape:
    def test_parameter_list_is_exactly_decision_class(self):
        sig = inspect.signature(check_delegation)
        assert list(sig.parameters.keys()) == ["decision_class"]

    def test_extra_authority_kwarg_raises_type_error(self):
        with pytest.raises(TypeError):
            check_delegation("execute-approved-plan", authority="peer-claim")

    def test_extra_claimed_holder_kwarg_raises_type_error(self):
        with pytest.raises(TypeError):
            check_delegation("execute-approved-plan", claimed_holder="peer-session-id")

    def test_extra_session_token_kwarg_raises_type_error(self):
        with pytest.raises(TypeError):
            check_delegation("execute-approved-plan", session_token="tok-123")

    def test_extra_positional_arg_raises_type_error(self):
        with pytest.raises(TypeError):
            check_delegation("execute-approved-plan", "peer-claim")


# ---------------------------------------------------------------------------
# Functional -- no policy of its own, shapes C2's own return value
# ---------------------------------------------------------------------------


class TestCheckDelegation:
    def test_no_grant_file_returns_absent_shape(self):
        result = check_delegation("execute-approved-plan")
        assert result == {"granted": False, "designated": None, "reason": "no-grant"}

    def test_granted_class_returns_granted_with_designated(self, human_verdict):
        import psutil

        this_proc = psutil.Process(os.getpid())
        _write_live_grant()
        result = check_delegation("execute-approved-plan")
        assert result["granted"] is True
        assert result["reason"] == "granted"
        assert result["designated"]["pid"] == os.getpid()
        assert result["designated"]["create_time"] == pytest.approx(this_proc.create_time())

    def test_class_not_in_grant_returns_absent_shape_without_disclosing_designated(self, human_verdict):
        # Both are real allow-listed classes: a grant covering one, asked
        # about the other. A made-up token here would be refused by the
        # writer and this test would assert over an absent file.
        _write_live_grant(classes=["expensive-test-tier"])
        result = check_delegation("execute-approved-plan")
        assert result == {"granted": False, "designated": None, "reason": "no-grant"}

    def test_expired_grant_returns_absent_shape(self, human_verdict):
        now = _now()
        _write_live_grant(
            granted_at=_iso(now - timedelta(minutes=1)),
            expires_at=_iso(now - timedelta(seconds=1)),
        )
        result = check_delegation("execute-approved-plan")
        assert result == {"granted": False, "designated": None, "reason": "no-grant"}

    def test_expired_grant_aged_in_place_is_byte_identical_to_absent(self, human_verdict, tmp_path):
        """THE identity property lives here, not at the session-function
        layer (coordinator_core/session/tests/test_fleet_delegation.py ::
        test_expired_denial_carries_the_record_no_file_does_not) --
        ``check_fleet_delegation`` deliberately keeps the record on an
        expired denial for the human-facing inspection path
        (``coordinator-delegation show``); this op is the agent-facing
        consumer surface, where a session must not be able to tell expired
        from absent. This assertion must not be softened.

        Writes a genuinely LIVE grant through the real writer, then ages it
        out by rewriting ``expires_at`` in place on disk -- the writer
        itself rejects an already-expired ``granted_at``/``expires_at`` pair
        outright, so constructing one directly would leave no file on disk
        and silently pass on two absent reads. The existence guard between
        write and aging is what stops this test decaying the same way its
        session-layer predecessor did.
        """
        no_grant_result = check_delegation("execute-approved-plan")

        now = _now()
        _write_live_grant(
            granted_at=_iso(now),
            expires_at=_iso(now + timedelta(hours=1)),
        )
        grant_file = fd._grant_file()
        assert grant_file.exists(), "vacuity guard: grant file must exist before aging it out"

        import json

        record = json.loads(grant_file.read_text(encoding="utf-8"))
        record["expires_at"] = _iso(now - timedelta(hours=1))
        grant_file.write_text(json.dumps(record), encoding="utf-8")

        expired_result = check_delegation("execute-approved-plan")

        assert expired_result == no_grant_result == {"granted": False, "designated": None, "reason": "no-grant"}

    def test_dead_grantee_returns_absent_shape_without_disclosing_designated(self, human_verdict, monkeypatch):
        # A live-looking grant whose designated process cannot be found --
        # C2 denies it (not-live), and this wrapper must still not disclose
        # the (pid, create_time) pair of the dead grantee.
        _write_live_grant(designated_pid=999999, designated_create_time=1.0)

        class _FakeNoSuchProcess(Exception):
            pass

        class _FakePsutilModule:
            NoSuchProcess = _FakeNoSuchProcess

            def Process(self, pid):
                raise _FakeNoSuchProcess()

        monkeypatch.setattr(fd, "_psutil", lambda: _FakePsutilModule())

        result = check_delegation("execute-approved-plan")
        assert result == {"granted": False, "designated": None, "reason": "no-grant"}

    def test_malformed_record_returns_absent_shape(self, tmp_path):
        (tmp_path / "fleet-delegation.json").write_text("{not json", encoding="utf-8")
        result = check_delegation("execute-approved-plan")
        assert result == {"granted": False, "designated": None, "reason": "no-grant"}

    def test_never_delegable_class_absent_entirely(self):
        # NEVER_DELEGABLE classes can never reach a written grant (C2's own
        # writer rejects them), so this exercises the same absent-grant code
        # path as test_no_grant_file_returns_absent_shape above -- it does
        # NOT distinguish never-delegable handling from an ordinary unwritten
        # class; that guarantee lives in C2's own write-time rejection tests.
        # (Review: code-reviewer, finding on overclaiming comment.)
        result = check_delegation("irreversible-action")
        assert result == {"granted": False, "designated": None, "reason": "no-grant"}


# ---------------------------------------------------------------------------
# BUDGET, ASSERTED NOT ASSUMED (Review: staff-eng (the Staff Engineer), finding 9)
# ---------------------------------------------------------------------------


class TestBudget:
    def test_check_body_median_process_time_under_5ms(self, human_verdict):
        _write_live_grant()

        samples_s = []
        for _ in range(51):
            start = time.process_time()
            check_delegation("execute-approved-plan")
            samples_s.append(time.process_time() - start)

        # Discard the first sample -- import warmth is not charged against
        # the warm-path budget the number describes.
        samples_s = samples_s[1:]
        assert len(samples_s) == 50

        median_ms = sorted(samples_s)[len(samples_s) // 2] * 1000.0
        assert median_ms <= 5.0, f"median check_delegation process time {median_ms:.3f}ms exceeds 5ms budget"
