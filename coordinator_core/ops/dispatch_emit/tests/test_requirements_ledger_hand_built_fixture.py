"""requirements_ledger: a row whose acceptance evidence hand-built its fixture never reads met."""

from __future__ import annotations

import pytest

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.tests.test_requirements_ledger_design_rows import REL, run  # noqa: F401


def _item(item_id, **extra):
    return {"id": item_id, "status": "met", "wired": True, **extra}


def _verdict(ledger, target, **extra):
    items = [{"id": i.id, "status": "met"} for i in ledger.items if i is not target]
    return {"items": [*items, _item(target.id, **extra)]}


def _met_ledger(run):
    root, ledger, _design, others = run
    return root, ledger, others[0]


def test_hand_built_fixture_evidence_fails_the_gate_and_names_the_production_entry_point(run):
    root, ledger, target = _met_ledger(run)
    verdict = _verdict(ledger, target, evidence_entry_point=rr.EVIDENCE_HAND_BUILT)
    out = rl.judge(ledger, verdict, repo_root=root)
    assert not out["pass"]
    reasons = [f["reason"] for f in out["failures"] if f["id"] == target.id]
    assert any("production entry point" in r for r in reasons), reasons
    assert all(o["id"] != target.id for o in out["open"])


@pytest.mark.parametrize("extra", [{"evidence_entry_point": rr.EVIDENCE_PRODUCTION}, {}])
def test_production_or_absent_evidence_entry_point_stays_met(run, extra):
    root, ledger, target = _met_ledger(run)
    out = rl.judge(ledger, _verdict(ledger, target, **extra), repo_root=root)
    assert not [f for f in out["failures"] if f["id"] == target.id], out


def _status(root, row_id):
    rows = rr.read_register((root / REL).read_text(encoding="utf-8")).rows
    return next(r for r in rows if r["id"] == row_id)


def test_apply_verdict_lands_hand_built_fixture_evidence_partial_with_no_met_by(run):
    root, ledger, target = _met_ledger(run)
    rl.apply_verdict(
        root, REL, _verdict(ledger, target, evidence_entry_point=rr.EVIDENCE_HAND_BUILT),
        run_id="run-1", sha="abc",
    )
    row = _status(root, target.id)
    assert row["status"] == rr.STATUS_PARTIAL
    assert not row.get("met_by")


@pytest.mark.parametrize("extra", [{"evidence_entry_point": rr.EVIDENCE_PRODUCTION}, {}])
def test_apply_verdict_production_or_absent_lands_met(run, extra):
    root, ledger, target = _met_ledger(run)
    rl.apply_verdict(root, REL, _verdict(ledger, target, **extra), run_id="run-1", sha="abc")
    row = _status(root, target.id)
    assert row["status"] == rr.STATUS_MET
    assert row["met_by"] == ["run-1@abc"]
