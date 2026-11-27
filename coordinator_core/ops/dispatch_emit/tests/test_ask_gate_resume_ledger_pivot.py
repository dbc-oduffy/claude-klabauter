"""ask_gate._resume_ledger sets `pivot` from the halting run's halted.json, and only for a PIVOT halt."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import (
    ALL_LANDED_HALT_KIND,
    RECORD_DIR_ROOT,
    RESUME_LEDGER_PIVOT_FIELD,
    LedgerHalt,
)
from coordinator_core.ops.dispatch_emit.ask_gate import _resume_ledger
from coordinator_core.ops.dispatch_emit.sizing_fire import ALL_LANDED_NO_RUN

PLAN = "docs/plans/p.md"
SIZING = {"requirement_register": {"rows": [{"id": "R1", "status": "open"}]}}
RUN = "run-1"


def _halt(run_id: str = RUN, **kw) -> LedgerHalt:
    return LedgerHalt(run_id=run_id, plan_rel=PLAN, sizing_rel="state/sizings/s.yaml", blocking=("R1",), **kw)


def _halted(tmp_path: Path, body: object, run_id: str = RUN) -> None:
    path = tmp_path / RECORD_DIR_ROOT / run_id / "halted.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")


def _blitz(verdict: str, reviewer: str = "sid") -> dict:
    return {"halted": "ledger-check", "blitz": {"pulled": [{"reviews": [{"reviewer": reviewer, "verdict": verdict}]}]}}


def test_a_pivot_review_in_the_halt_record_sets_the_pivot_field(tmp_path: Path) -> None:
    _halted(tmp_path, _blitz("PIVOT"))
    assert _resume_ledger(SIZING, _halt(), tmp_path)[RESUME_LEDGER_PIVOT_FIELD] is True


def test_the_top_level_pivot_marker_sets_the_pivot_field(tmp_path: Path) -> None:
    _halted(tmp_path, {"halted": "ledger-check", RESUME_LEDGER_PIVOT_FIELD: True})
    assert _resume_ledger(SIZING, _halt(), tmp_path)[RESUME_LEDGER_PIVOT_FIELD] is True


@pytest.mark.parametrize("body", [_blitz("BLOCKED"), _blitz("OK"), _blitz("PIVOT", reviewer="premise-check"), {"halted": "ledger-check"}])
def test_any_other_halt_omits_the_pivot_key(tmp_path: Path, body: dict) -> None:
    _halted(tmp_path, body)
    assert RESUME_LEDGER_PIVOT_FIELD not in _resume_ledger(SIZING, _halt(), tmp_path)


@pytest.mark.parametrize("body", ["{not json", "[]"])
def test_an_unreadable_halt_record_omits_the_pivot_key(tmp_path: Path, body: str) -> None:
    _halted(tmp_path, body)
    assert RESUME_LEDGER_PIVOT_FIELD not in _resume_ledger(SIZING, _halt(), tmp_path)


def test_an_absent_halt_record_omits_the_pivot_key(tmp_path: Path) -> None:
    assert RESUME_LEDGER_PIVOT_FIELD not in _resume_ledger(SIZING, _halt(), tmp_path)


def test_the_no_run_sentinel_reads_no_record(tmp_path: Path) -> None:
    _halted(tmp_path, _blitz("PIVOT"), run_id=ALL_LANDED_NO_RUN)
    out = _resume_ledger(SIZING, _halt(ALL_LANDED_NO_RUN, halted=ALL_LANDED_HALT_KIND), tmp_path)
    assert RESUME_LEDGER_PIVOT_FIELD not in out
