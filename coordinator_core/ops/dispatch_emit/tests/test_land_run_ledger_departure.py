"""dispatch.land_run lands a run whose ledger verdict records a PM-row departure with its reason: the
reply lists it under ``ledger_departures`` and the commit carries a ``Requirement-Departed`` trailer
(never ``Requirement-Open``); a reasonless departure or an evidence-less PM ``met`` is refused."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import completion_receipt as cr
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_commits_delivered_rows import (  # noqa: F401
    _call,
    _git,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture(autouse=True)
def _loe_probe(repo, monkeypatch):
    """Runs after the seeded repo: its claim-list stub is widened to the production signature."""
    monkeypatch.setattr(cr, "_actual_loe", lambda *a, **k: "S")
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked", lambda *a, **k: ([], [])
    )

_WHY = "the PM's changelog line is unwanted by the owner"


def _verdict(r2: dict, r3: dict | None = None) -> dict:
    return {"items": [
        {"id": "R1", "status": "met", "met_by": ["C1"]},
        {"id": "R2", "status": "departed", **r2},
        {"id": "R3", "status": "met", "met_by": ["C1"], **(r3 or {})},
    ]}


def _trailers(repo, sha: str, key: str) -> list[str]:
    out = _git(repo, "log", "-1", f"--format=%(trailers:key={key},valueonly)", sha)
    return [line for line in out.split("\n") if line]


def test_a_recorded_departure_lands_and_is_listed_with_its_reason(repo):  # noqa: F811
    out = _call(repo, _verdict({"departure_reason": _WHY}))

    assert out["committed"] is True, out
    assert out["ledger_departures"] == [{"id": "R2", "reason": _WHY}]
    assert _trailers(repo, out["sha"], "Requirement-Departed") == [f"R2 {_WHY}"]
    assert _trailers(repo, out["sha"], "Requirement-Open") == []


def test_a_run_with_no_departure_reports_none(repo):  # noqa: F811
    verdict = _verdict({"departure_reason": _WHY})
    verdict["items"][1] = {"id": "R2", "status": "met", "met_by": ["C1"]}
    out = _call(repo, verdict)

    assert out["committed"] is True, out
    assert "ledger_departures" not in out
    assert _trailers(repo, out["sha"], "Requirement-Departed") == []


@pytest.mark.parametrize("reason", [{}, {"departure_reason": "   "}])
def test_a_departure_without_a_reason_is_refused(repo, reason):  # noqa: F811
    before = _git(repo, "rev-parse", "HEAD")
    out = _call(repo, _verdict(reason))

    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    assert [f["id"] for f in out["ledger_failures"]] == ["R2"]
    assert "departed without a recorded reason" in out["ledger_failures"][0]["reason"]
    assert _git(repo, "rev-parse", "HEAD") == before


def test_a_pm_met_row_with_no_evidence_is_refused(repo):  # noqa: F811
    before = _git(repo, "rev-parse", "HEAD")
    verdict = _verdict({"departure_reason": _WHY})
    verdict["items"][0] = {"id": "R1", "status": "met"}
    out = _call(repo, verdict)

    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    assert [f["id"] for f in out["ledger_failures"]] == ["R1"]
    assert _git(repo, "rev-parse", "HEAD") == before
