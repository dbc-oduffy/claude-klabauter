"""Incident shape of the cockpit ff286a run: R1 is met_by a sha, R2 (a design-registry row) is met_by
its review file, whose landing sha the revise note and the row's evidence name. R2 stays met on the
ledger resume and the run reaches the commit leg. Built under tmp_path; no cockpit path is read."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_all_landed_ledger import (
    _RESUME,
    _labels,
    _run,
    _script,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (  # noqa: F401
    readers_cfg,
    stage_root,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_WORK = "f594d0a96"
_REVIEW_SHA = "67fde3387"
_REVIEW = "docs/design/gsd/reviews/2026-10-23-tiled-chrome-overlap-fix-review.md"
_ANCHOR = "design-registry:gsd-tiled-chrome"
_NO_SHA = "met_by: the commit sha that carries the credited work"


def _resume(*, notes, r2_evidence):
    return {
        "run_id": "ask-prior", "plan": _RESUME["plan"], "blocking": ["R1", "R2"], "halt_sha": "a" * 40,
        "unlanded": [], "revise_notes": notes,
        "remaining": {
            "R1": {"remaining": ["update the sizing"], "evidence": "no sha here"},
            "R2": {"remaining": ["update the sizing"], "evidence": r2_evidence},
        },
    }


def _verdict(r2_met_by):
    return {"items": [
        {"id": "R1", "status": "met", "wired": True, "met_by": [_WORK]},
        {"id": "R2", "status": "met", "wired": True, "anchor": _ANCHOR, "met_by": r2_met_by},
    ]}


def _r2(out):
    return next(i for i in out["result"]["halted_by"]["items"] if i["id"] == "R2")


def test_a_design_row_met_by_its_review_file_stays_met_when_the_revise_note_names_the_shas(monkeypatch, tmp_path):
    review = tmp_path / _REVIEW
    review.parent.mkdir(parents=True)
    review.write_text("senior-front-end review\n", encoding="utf-8")
    resume = _resume(
        notes=[f"R1 met_by {_WORK}; R2 met_by {_WORK} and {_REVIEW_SHA}"],
        r2_evidence=f"the review in {_WORK}",
    )
    out = _run(_script(monkeypatch), verify=_verdict([_REVIEW]), resume=resume)
    assert out["result"]["halted_by"] is None, out["result"]["halted_by"]
    assert "commit" in _labels(out)


def test_a_file_met_by_credited_only_by_the_row_evidence_stays_met(monkeypatch):
    resume = _resume(notes=["R1 and R2 are ruled met by the landed work"], r2_evidence=f"the review in {_WORK}")
    out = _run(_script(monkeypatch), verify=_verdict([_REVIEW]), resume=resume)
    assert out["result"]["halted_by"] is None, out["result"]["halted_by"]
    assert "commit" in _labels(out)


def test_a_file_met_by_with_no_sha_in_note_or_evidence_is_still_demoted(monkeypatch):
    resume = _resume(notes=["R1 and R2 are ruled met by the landed work"], r2_evidence="no sha here")
    out = _run(_script(monkeypatch), verify=_verdict([_REVIEW]), resume=resume)
    assert out["result"]["halted_by"]["halted"] == "ledger-check"
    assert _r2(out)["status"] == "not_met"
    assert _r2(out)["remaining"] == [_NO_SHA]
    assert "commit" not in _labels(out)


def test_a_design_row_with_no_met_by_is_still_demoted_for_the_missing_file(monkeypatch):
    resume = _resume(notes=[f"R2 met_by {_WORK}"], r2_evidence=f"the review in {_WORK}")
    out = _run(_script(monkeypatch), verify=_verdict("none"), resume=resume)
    assert _r2(out)["status"] == "not_met"
    assert _r2(out)["remaining"] == ["met_by: the senior-front-end review or ratification file of the design board"]
    assert "commit" not in _labels(out)
