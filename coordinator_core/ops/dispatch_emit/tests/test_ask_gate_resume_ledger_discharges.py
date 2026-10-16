"""ask_gate._resume_ledger: blocking falls back to rulable ids; em-discharged rows ride the verdict."""

from __future__ import annotations

import yaml

from coordinator_core.ops.dispatch_emit.ask_contract import (
    RESUME_LEDGER_EM_DISCHARGED_FIELD,
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


def test_recorded_blocking_is_kept(tmp_path):
    out = _resume_ledger(SIZING, _halt(["R2"]), tmp_path)
    assert out["blocking"] == ["R2"]
    assert out["rulable_ids"] == ["R2", "R3"]


def test_empty_blocking_falls_back_to_rulable_ids(tmp_path):
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


def test_no_sidecar_gives_empty_discharges(tmp_path):
    assert _resume_ledger(SIZING, _halt(["R2"]), tmp_path)[RESUME_LEDGER_EM_DISCHARGED_FIELD] == {}
