"""A ledger resume after review_blocked admits the run's revise register rows as remaining work and leaves landed spine rows unstaged."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_stage, resume_snapshot
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, stage
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage_resume_added_rows import PLAN_REL, RUN, _plan, _row

LANDED = ["C1", "C2", "C3", "C4", "C5", "C6"]
REVISE = ["R1", "R2", "R3"]
SIZING = {"requirement_register": {"rows": [{"id": i, "status": "open"} for i in REVISE]}}


@pytest.fixture(autouse=True)
def _sizing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ask_stage, "_run_sizing", lambda root, rel: SIZING)


def _ledger_stage(tmp_path: Path, only_rows: list[str], *, ledger_resume: bool = True) -> dict:
    stage(tmp_path, run_id=RUN, plan_rel=PLAN_REL, only_rows=only_rows, ledger_resume=ledger_resume)
    return _manifest(tmp_path)


def _manifest(tmp_path: Path) -> dict:
    return json.loads((record_dir(tmp_path, RUN) / "manifest.json").read_text(encoding="utf-8"))


def test_revise_rows_absent_from_the_spine_are_admitted_and_landed_rows_stay_unstaged(tmp_path: Path) -> None:
    _plan(tmp_path, "".join(_row(r) for r in LANDED))
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, LANDED, REVISE, reasons={r: "landed" for r in LANDED})
    manifest = _ledger_stage(tmp_path, REVISE)
    assert manifest["rows"] == []
    assert manifest["retired_resume_rows"] == REVISE


def test_admission_does_not_need_the_gate_snapshot(tmp_path: Path) -> None:
    _plan(tmp_path, "".join(_row(r) for r in LANDED))
    manifest = _ledger_stage(tmp_path, REVISE)
    assert manifest["retired_resume_rows"] == REVISE


def test_revise_rows_present_in_the_spine_are_staged_alone(tmp_path: Path) -> None:
    _plan(tmp_path, "".join(_row(r) for r in [*LANDED, *REVISE]))
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, LANDED, REVISE, reasons={r: "landed" for r in LANDED})
    manifest = _ledger_stage(tmp_path, REVISE)
    assert {r["id"] for r in manifest["rows"]} == set(REVISE)
    assert manifest["retired_resume_rows"] == []


def test_an_id_that_is_neither_spine_nor_revise_row_still_refuses(tmp_path: Path) -> None:
    _plan(tmp_path, "".join(_row(r) for r in LANDED))
    with pytest.raises(AskStageError, match="C99"):
        stage(tmp_path, run_id=RUN, plan_rel=PLAN_REL, only_rows=[*REVISE, "C99"], ledger_resume=True)


def test_a_non_ledger_resume_still_refuses_a_revise_row_absent_from_the_spine(tmp_path: Path) -> None:
    _plan(tmp_path, "".join(_row(r) for r in LANDED))
    with pytest.raises(AskStageError, match="R1"):
        stage(tmp_path, run_id=RUN, plan_rel=PLAN_REL, only_rows=REVISE, ledger_resume=False)
