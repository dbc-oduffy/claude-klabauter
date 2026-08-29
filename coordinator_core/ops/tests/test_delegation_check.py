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
        classes=["ask-the-pm"],
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
            check_delegation("ask-the-pm", authority="peer-claim")

    def test_extra_claimed_holder_kwarg_raises_type_error(self):
        with pytest.raises(TypeError):
            check_delegation("ask-the-pm", claimed_holder="peer-session-id")

    def test_extra_session_token_kwarg_raises_type_error(self):
        with pytest.raises(TypeError):
            check_delegation("ask-the-pm", session_token="tok-123")

    def test_extra_positional_arg_raises_type_error(self):
        with pytest.raises(TypeError):
            check_delegation("ask-the-pm", "peer-claim")


# ---------------------------------------------------------------------------
# Functional -- no policy of its own, shapes C2's own return value
# ---------------------------------------------------------------------------


class TestCheckDelegation:
    def test_no_grant_file_returns_absent_shape(self):
        result = check_delegation("ask-the-pm")
        assert result == {"granted": False, "designated": None, "reason": "no-grant"}

    def test_granted_class_returns_granted_with_designated(self, human_verdict):
        import psutil

        this_proc = psutil.Process(os.getpid())
        _write_live_grant()
        result = check_delegation("ask-the-pm")
        assert result["granted"] is True
        assert result["reason"] == "granted"
        assert result["designated"]["pid"] == os.getpid()
        assert result["designated"]["create_time"] == pytest.approx(this_proc.create_time())

    def test_class_not_in_grant_returns_not_granted(self, human_verdict):
        _write_live_grant(classes=["some-other-class"])
        result = check_delegation("ask-the-pm")
        assert result["granted"] is False
        assert result["reason"] == "not-granted"
        assert result["designated"] is not None

    def test_expired_grant_returns_not_granted(self, human_verdict):
        now = _now()
        _write_live_grant(
            granted_at=_iso(now - timedelta(minutes=1)),
            expires_at=_iso(now - timedelta(seconds=1)),
        )
        result = check_delegation("ask-the-pm")
        assert result["granted"] is False
        assert result["reason"] == "not-granted"

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
            check_delegation("ask-the-pm")
            samples_s.append(time.process_time() - start)

        # Discard the first sample -- import warmth is not charged against
        # the warm-path budget the number describes.
        samples_s = samples_s[1:]
        assert len(samples_s) == 50

        median_ms = sorted(samples_s)[len(samples_s) // 2] * 1000.0
        assert median_ms <= 5.0, f"median check_delegation process time {median_ms:.3f}ms exceeds 5ms budget"
