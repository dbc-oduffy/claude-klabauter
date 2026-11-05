"""A ledger re-plan omits, never refuses, an em-performed row an em-discharged row already serves."""

from __future__ import annotations

from pathlib import Path

import yaml

from coordinator_core.ops.dispatch_emit.ask_stage import _drop_reminted_em_rows, stage
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts

PLAN_REL = "docs/plans/p.md"


def _row(rid: str, regs: str, performer: str | None, deps: tuple[str, ...] = ()) -> str:
    perf = f"  performer: {performer}\n" if performer else ""
    dep = ("  depends_on:\n" + "".join(f"    - chunk: {d}\n" for d in deps)) if deps else ""
    return (
        f"- id: {rid}\n  title: t{rid}\n  change_kind: doc-edit\n  surface: docs/x/{rid}.md\n{perf}"
        f"  register_rows: {regs}\n  writes:\n    - docs/x/{rid}.md\n{dep}  disposition: open\n"
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
        plan.with_name("p.evidence.yaml").write_text(
            yaml.safe_dump({"em_discharged": {r: {"recorded_at": "t", "text": "by hand"} for r in discharged}}),
            encoding="utf-8",
        )
    return plan


def _raw(plan: Path) -> dict:
    block = plan.read_text().split("```yaml plan-tasks\n")[1].split("```")[0]
    return {r["id"]: r for r in yaml.safe_load(block)}


MIXED = [_row("C25", "[R1]", "em"), _row("C26", "[R1]", None), _row("C28", "[R1]", "em")]


def test_stage_omits_the_reminted_em_row_and_reports_it(tmp_path):
    _plan(tmp_path, MIXED, discharged=("C25",))
    write_run_scripts(tmp_path, "r1")
    manifest = stage(tmp_path, run_id="r1", plan_rel=PLAN_REL, only_rows=["C26", "C28"], ledger_resume=True)
    assert [r.id for r in manifest.rows] == ["C26"]
    assert "C28" in manifest.retired_resume_rows


def test_dropping_function_returns_the_served_row(tmp_path):
    plan = _plan(tmp_path, MIXED, discharged=("C25",))
    assert _drop_reminted_em_rows(plan, _raw(plan), frozenset({"C26", "C28"})) == ("C28",)


def test_row_chained_to_the_discharged_row_is_kept(tmp_path):
    plan = _plan(tmp_path, [_row("S2", "[R1]", "em"), _row("R1x", "[R1]", "em", ("S2",))], discharged=("S2",))
    assert _drop_reminted_em_rows(plan, _raw(plan), frozenset({"R1x"})) == ()


def test_em_row_serving_a_new_register_row_is_kept(tmp_path):
    plan = _plan(tmp_path, [_row("C25", "[R1]", "em"), _row("C28", "[R1, R2]", "em")], discharged=("C25",))
    assert _drop_reminted_em_rows(plan, _raw(plan), frozenset({"C28"})) == ()


def test_nothing_discharged_drops_nothing(tmp_path):
    plan = _plan(tmp_path, MIXED)
    assert _drop_reminted_em_rows(plan, _raw(plan), frozenset({"C25", "C28"})) == ()
