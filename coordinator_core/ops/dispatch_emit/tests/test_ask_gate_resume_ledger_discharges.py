"""ask_gate._resume_ledger: blocking falls back to rulable ids; em-discharged rows ride the verdict."""

from __future__ import annotations

import yaml

from coordinator_core.ops.dispatch_emit.ask_contract import (
    RESUME_LEDGER_EM_DISCHARGED_FIELD,
    RESUME_LEDGER_HALT_SHA_FIELD,
    RESUME_LEDGER_NOTES_FIELD,
    RESUME_LEDGER_REMAINING_FIELD,
    LedgerHalt,
)
from coordinator_core.ops.dispatch_emit.ask_gate import _resume_ledger

PLAN = "docs/plans/p.md"
SIZING = {
    "requirement_register": {
        "rows": [
            {"id": "R1", "status": "met"},
            {"id": "R2", "status": "open"},
            {"id": "R3", "status": "open"},
        ]
    }
}


def _halt(blocking):
    return LedgerHalt(run_id="run-1", plan_rel=PLAN, sizing_rel="state/sizings/s.yaml", blocking=tuple(blocking))


def _sidecar(tmp_path, body):
    path = tmp_path / "docs/plans/p.evidence.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(body), encoding="utf-8")


def test_none_halt_yields_none(tmp_path):
    assert _resume_ledger(SIZING, None, tmp_path) is None


def test_recorded_blocking_is_kept_and_widened_to_unmet_rows(tmp_path):
    out = _resume_ledger(SIZING, _halt(["R2"]), tmp_path)
    assert out["blocking"] == ["R2", "R3"]
    assert out["rulable_ids"] == ["R2", "R3"]


def test_rows_appended_after_the_halt_are_judged_with_its_blocking_ids(tmp_path):
    rows = [{"id": i, "status": "open"} for i in ("T9", "R2", "R3", "R4", "R5")]
    rows.insert(0, {"id": "R1", "status": "met"})
    sizing = {"requirement_register": {"rows": rows}}
    out = _resume_ledger(sizing, _halt(["T9", "R2"]), tmp_path)
    assert out["blocking"] == ["T9", "R2", "R3", "R4", "R5"]


def test_met_and_waived_rows_are_not_rejudged(tmp_path):
    sizing = {"requirement_register": {"rows": [
        {"id": "R1", "status": "met"}, {"id": "R2", "status": "open"}, {"id": "R3", "status": "waived"},
    ]}}
    assert _resume_ledger(sizing, _halt(["R2"]), tmp_path)["blocking"] == ["R2"]


def test_empty_blocking_judges_every_rulable_row(tmp_path):
    assert _resume_ledger(SIZING, _halt([]), tmp_path)["blocking"] == ["R2", "R3"]


def test_em_discharged_projects_text(tmp_path):
    _sidecar(
        tmp_path,
        {
            "em_discharged": {
                "V1": {"recorded_at": "t", "text": "checked by hand"},
                "V2": {"recorded_at": "t"},
                "V3": "not a dict",
            }
        },
    )
    out = _resume_ledger(SIZING, _halt(["R2"]), tmp_path)
    assert out[RESUME_LEDGER_EM_DISCHARGED_FIELD] == {"V1": "checked by hand"}


def test_halt_sha_remaining_and_notes_ride_the_verdict_and_ruled_out_rows_carry_nothing(tmp_path):
    sizing = {"requirement_register": {"rows": [
        {"id": "R1", "status": "not_met"},
        {"id": "R2", "status": "deferred", "ruling": {"ref": "workflow.revise:run-1:out_of_scope"}},
    ]}}
    notes = tmp_path / "state/warp/run-1/notes.jsonl"
    notes.parent.mkdir(parents=True)
    notes.write_text('{"note": "first"}\nnot json\n{"note": 3}\n{"note": "second"}\n', encoding="utf-8")
    sha = "a" * 40
    halt = LedgerHalt(
        run_id="run-1", plan_rel=PLAN, sizing_rel="state/sizings/s.yaml", blocking=("R1", "R2"),
        halt_sha=sha,
        remaining={"R1": {"remaining": ["do x"], "evidence": "e"}, "R2": {"remaining": ["y"], "evidence": ""}},
    )
    out = _resume_ledger(sizing, halt, tmp_path)
    assert out[RESUME_LEDGER_HALT_SHA_FIELD] == sha
    assert out[RESUME_LEDGER_REMAINING_FIELD] == {"R1": {"remaining": ["do x"], "evidence": "e"}}
    assert out[RESUME_LEDGER_NOTES_FIELD] == ["first", "second"]
    assert "R2" not in out["blocking"]


def test_no_notes_file_gives_empty_notes(tmp_path):
    assert _resume_ledger(SIZING, _halt(["R2"]), tmp_path)[RESUME_LEDGER_NOTES_FIELD] == []


def test_no_sidecar_gives_empty_discharges(tmp_path):
    assert _resume_ledger(SIZING, _halt(["R2"]), tmp_path)[RESUME_LEDGER_EM_DISCHARGED_FIELD] == {}
