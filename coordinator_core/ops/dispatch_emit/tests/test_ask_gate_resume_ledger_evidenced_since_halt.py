"""ask_gate._resume_ledger: a carried gap whose serving row gained evidence after the halt is not re-planned."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_LEDGER_REMAINING_FIELD, LedgerHalt
from coordinator_core.ops.dispatch_emit.ask_gate import _resume_ledger

PLAN = "docs/plans/p.md"
RUN = "run-1"
HALTED_AT = "2026-11-20T15:12:29Z"
SIZING = {"requirement_register": {"rows": [{"id": "R1", "status": "open"}]}}
GAP = {"remaining": ["post-change bench needs a G-EM GPU slot"], "evidence": "no post-change bench exists"}


def _repo(tmp_path: Path, evidence_at: str | None, stage: str = "halted") -> None:
    plan = tmp_path / PLAN
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        "---\nrun_id: f\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C2\n  title: T\n  disposition: open\n  writes:\n    - a.py\n  register_rows: [R1]\n```\n",
        encoding="utf-8", newline="\n",
    )
    if evidence_at:
        (tmp_path / "docs/plans/p.evidence.yaml").write_text(
            yaml.safe_dump({"rows": {"C2": [{"recorded_at": evidence_at, "text": "end bench done"}]}}), encoding="utf-8"
        )
    rec = tmp_path / "state/warp" / RUN
    rec.mkdir(parents=True)
    (rec / "run-stage.json").write_text(json.dumps({"stage": stage, "last_activity_at": HALTED_AT}), encoding="utf-8")


def _halt() -> LedgerHalt:
    return LedgerHalt(
        run_id=RUN, plan_rel=PLAN, sizing_rel="state/sizings/s.yaml", blocking=("R1",),
        halt_sha="a" * 40, remaining={"R1": GAP}, unlanded=("C2",),
    )


def test_evidence_recorded_after_halt_clears_the_carried_gap(tmp_path: Path) -> None:
    _repo(tmp_path, "2026-11-20T15:15:21Z")
    out = _resume_ledger(SIZING, _halt(), tmp_path)
    assert out[RESUME_LEDGER_REMAINING_FIELD]["R1"] == {"remaining": []}
    assert out["blocking"] == ["R1"]


def test_evidence_recorded_before_halt_keeps_the_gap(tmp_path: Path) -> None:
    _repo(tmp_path, "2026-11-20T15:00:00Z")
    assert _resume_ledger(SIZING, _halt(), tmp_path)[RESUME_LEDGER_REMAINING_FIELD]["R1"] == GAP


def test_no_evidence_keeps_the_gap(tmp_path: Path) -> None:
    _repo(tmp_path, None)
    assert _resume_ledger(SIZING, _halt(), tmp_path)[RESUME_LEDGER_REMAINING_FIELD]["R1"] == GAP


def test_unstamped_halt_instant_keeps_the_gap(tmp_path: Path) -> None:
    _repo(tmp_path, "2026-11-20T15:15:21Z", stage="executing")
    assert _resume_ledger(SIZING, _halt(), tmp_path)[RESUME_LEDGER_REMAINING_FIELD]["R1"] == GAP
