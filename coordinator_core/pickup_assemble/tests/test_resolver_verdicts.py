"""
coordinator_core.pickup_assemble.tests.test_resolver_verdicts

P026-C4 (docs/plans/2026-09-07-a-claim-is-written-twice-and-nothing-
compares-them.md), AC16's report-only half: all four live
`_session_core.resolve_session_id` call sites in `pickup_assemble/__init__.py`
keep `resolve_session_id` permanently and report (never refuse) a
warm-uncarried resolution -- none of them writes a durable claim, each
resolves "is this session me?" for a comparison.

Sites covered:
  - `_adopt_into_baton`          -- fail-open by its own documented posture
  - `compute_liveness_signal`    -- already carried-first; cold fallback only
  - `_primary_held_disposition`  -- already carried-first; cold fallback only
  - `_finish_unification_claims` -- resolves self-identity for a comparison

AC16 is not gated on the klabauter publish (report-only sites cannot stop
the fleet) -- these pins hold regardless of C5's own gate.
"""
from __future__ import annotations

import pytest

import coordinator_core.pickup_assemble as pa
from coordinator_core.session import core as _session_core

pytestmark = [pytest.mark.cadence]

_SERVER_OWNER = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"


class TestReportWarmUncarriedResolve:
    """Direct coverage of `pa._report_warm_uncarried_resolve`."""

    def test_fires_when_warm_and_uncarried(self, monkeypatch, capsys):
        for var in _session_core.SESSION_ENV_PRECEDENCE:
            monkeypatch.setenv(var, _SERVER_OWNER)
        with _session_core.warm_served_request():
            pa._report_warm_uncarried_resolve("some_site")
        err = capsys.readouterr().err
        assert "pickup_assemble.some_site" in err
        assert "report-only, not refused" in err

    def test_silent_when_carried(self, monkeypatch, capsys):
        for var in _session_core.SESSION_ENV_PRECEDENCE:
            monkeypatch.setenv(var, _SERVER_OWNER)
        with _session_core.warm_served_request():
            with _session_core.session_identity_override(_SERVER_OWNER):
                pa._report_warm_uncarried_resolve("some_site")
        assert capsys.readouterr().err == ""

    def test_silent_when_cold(self, monkeypatch, capsys):
        for var in _session_core.SESSION_ENV_PRECEDENCE:
            monkeypatch.setenv(var, _SERVER_OWNER)
        pa._report_warm_uncarried_resolve("some_site")
        assert capsys.readouterr().err == ""


class TestFinishUnificationClaimsReportsButNeverRefuses:
    """`_finish_unification_claims` resolves self-identity to ask
    `list_claims_by_session` whether it already holds the successor. A
    warm-uncarried resolution reports (AC16) and the function still
    proceeds -- it never refuses, for the same reason the release sites in
    `session/claims.py` do not."""

    def test_reports_and_still_claims_successor_under_warm_uncarried(
        self, tmp_path, monkeypatch, capsys
    ):
        claimed = []
        released = []
        monkeypatch.setattr(
            pa._claims,
            "list_claims_by_session",
            lambda sid, cwd=None: [],
        )
        monkeypatch.setattr(
            pa._claims,
            "claim_handoff",
            lambda basename, cwd=None: claimed.append(basename),
        )
        monkeypatch.setattr(
            pa._claims,
            "release_artifact",
            lambda cls, basename, cwd=None: released.append((cls, basename)),
        )
        for var in _session_core.SESSION_ENV_PRECEDENCE:
            monkeypatch.setenv(var, _SERVER_OWNER)

        with _session_core.warm_served_request():
            pa._finish_unification_claims(
                tmp_path, ["state/handoffs/parent.md"], "state/handoffs/successor.md"
            )

        err = capsys.readouterr().err
        assert "pickup_assemble._finish_unification_claims" in err
        assert claimed == ["successor.md"], (
            "P026-C4: this site reports a warm-uncarried resolution but must "
            "not refuse to claim the successor"
        )
        assert released == [("handoff", "parent.md")]
