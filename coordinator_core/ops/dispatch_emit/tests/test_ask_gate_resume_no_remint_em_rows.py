"""A ledger resume emits since_sha and never re-mints an em-performed row the EM already discharged."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.ask_contract import (
    RESUME_LEDGER_REMAINING_FIELD,
    RESUME_LEDGER_SINCE_SHA_FIELD,
    LedgerHalt,
)
from coordinator_core.ops.dispatch_emit.ask_gate import _resume_ledger
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, _refuse_reminted_em_rows, stage

PLAN_REL = "docs/plans/p.md"
SIZING = {"requirement_register": {"rows": [{"id": "R1", "status": "open"}, {"id": "R2", "status": "open"}]}}


def _row(rid: str, regs: str, performer: str | None) -> str:
    perf = f"  performer: {performer}\n" if performer else ""
    return (
        f"- id: {rid}\n  title: t{rid}\n  change_kind: doc-edit\n  surface: docs/x/{rid}.md\n{perf}"
        f"  register_rows: {regs}\n  writes:\n    - docs/x/{rid}.md\n  disposition: open\n"
    )


def _plan(tmp_path: Path, rows: list[str], discharged: tuple[str, ...] = ()) -> Path:
    (tmp_path / ".git").mkdir(exist_ok=True)
    plan = tmp_path / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{''.join(rows)}```\n",
        encoding="utf-8",
        newline="\n",
    )
    if discharged:
        side = plan.with_name("p.evidence.yaml")
        side.write_text(
            yaml.safe_dump({"em_discharged": {r: {"recorded_at": "t", "text": "done by hand"} for r in discharged}}),
            encoding="utf-8",
        )
    return plan


def _halt(sha: str | None = None) -> LedgerHalt:
    return LedgerHalt(
        run_id="run-1", plan_rel=PLAN_REL, sizing_rel="state/sizings/s.yaml", blocking=("R1",),
        halt_sha="b" * 40, since_sha=sha,
        remaining={"R1": {"remaining": ["confirm and commit"], "evidence": "e"}},
    )


def test_since_sha_rides_the_verdict_beside_halt_sha(tmp_path):
    _plan(tmp_path, [_row("C25", "[R1]", "em")])
    out = _resume_ledger(SIZING, _halt("a" * 40), tmp_path)
    assert out[RESUME_LEDGER_SINCE_SHA_FIELD] == "a" * 40
    assert _resume_ledger(SIZING, _halt(None), tmp_path)[RESUME_LEDGER_SINCE_SHA_FIELD] is None


def test_discharged_em_performed_serving_row_owes_no_replan(tmp_path):
    _plan(tmp_path, [_row("C25", "[R1]", "em")], discharged=("C25",))
    rem = _resume_ledger(SIZING, _halt(), tmp_path)[RESUME_LEDGER_REMAINING_FIELD]
    assert rem["R1"]["remaining"] == []


def test_undischarged_em_performed_serving_row_still_owes(tmp_path):
    _plan(tmp_path, [_row("C25", "[R1]", "em")])
    rem = _resume_ledger(SIZING, _halt(), tmp_path)[RESUME_LEDGER_REMAINING_FIELD]
    assert rem["R1"]["remaining"] == ["confirm and commit"]


def test_a_non_em_serving_row_keeps_the_owed_remaining(tmp_path):
    _plan(tmp_path, [_row("C25", "[R1]", "em"), _row("C26", "[R1]", None)], discharged=("C25",))
    rem = _resume_ledger(SIZING, _halt(), tmp_path)[RESUME_LEDGER_REMAINING_FIELD]
    assert rem["R1"]["remaining"] == ["confirm and commit"]


MIXED = [_row("C25", "[R1]", "em"), _row("C26", "[R1]", None), _row("C28", "[R1]", "em")]


def test_stage_refuses_an_em_performed_duplicate_of_a_discharged_row(tmp_path):
    _plan(tmp_path, MIXED, discharged=("C25",))
    with pytest.raises(AskStageError, match=r"re-mints em-performed row\(s\) \['C28'\]"):
        stage(tmp_path, run_id="r1", plan_rel=PLAN_REL, only_rows=["C26", "C28"], ledger_resume=True)


def test_stage_admits_the_non_em_row_alone(tmp_path):
    _plan(tmp_path, MIXED, discharged=("C25",))
    manifest = stage(tmp_path, run_id="r1", plan_rel=PLAN_REL, only_rows=["C26"], ledger_resume=True)
    assert [r.id for r in manifest.rows] == ["C26"]


def test_stage_without_ledger_resume_never_applies_the_refusal(tmp_path):
    _plan(tmp_path, MIXED, discharged=("C25",))
    manifest = stage(tmp_path, run_id="r1", plan_rel=PLAN_REL, only_rows=["C26", "C28"])
    assert {r.id for r in manifest.rows} >= {"C26"}


def _dep_row(rid: str, regs: str, performer: str | None, deps: list[str]) -> str:
    return _row(rid, regs, performer) + "  depends_on:\n" + "".join(f"    - chunk: {d}\n" for d in deps)


def test_em_row_depending_transitively_on_a_discharged_row_is_staged(tmp_path):
    rows = [
        _row("S2", "[R1]", "em"),
        _dep_row("B1", "[R2]", None, ["S2"]),
        _dep_row("D1", "[R2]", None, ["B1"]),
        _dep_row("R1x", "[R1]", "em", ["D1"]),
    ]
    plan = _plan(tmp_path, rows, discharged=("S2",))
    raw = {r["id"]: r for r in yaml.safe_load(plan.read_text().split("```yaml plan-tasks\n")[1].split("```")[0])}
    _refuse_reminted_em_rows(plan, raw, frozenset({"R1x"}))
    with pytest.raises(AskStageError, match=r"depends_on"):
        _refuse_reminted_em_rows(plan, {**raw, "R1x": {**raw["R1x"], "depends_on": []}}, frozenset({"R1x"}))


def test_guard_still_admits_an_em_row_serving_a_new_register_row(tmp_path):
    plan = _plan(tmp_path, [_row("C25", "[R1]", "em"), _row("C28", "[R1, R2]", "em")], discharged=("C25",))
    raw = {r["id"]: r for r in yaml.safe_load(plan.read_text().split("```yaml plan-tasks\n")[1].split("```")[0])}
    _refuse_reminted_em_rows(plan, raw, frozenset({"C28"}))
    with pytest.raises(AskStageError):
        _refuse_reminted_em_rows(plan, {**raw, "C28": {**raw["C28"], "register_rows": ["R1"]}}, frozenset({"C28"}))
