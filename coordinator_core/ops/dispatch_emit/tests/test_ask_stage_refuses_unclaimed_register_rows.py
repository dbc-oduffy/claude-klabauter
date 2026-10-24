"""`ask_stage.stage` refuses a non-ledger stage of a plan whose register_claims omit register rows."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

PLAN_REL = "docs/plans/2026-10-24-unclaimed.md"
SIZING_REL = "state/sizings/2026-10-24-unclaimed.yaml"

SIZING = """schema: sizing-object
name: unclaimed
estimate:
  tshirt: M
route: plan
requirement_register:
  rows:
  - id: R1
    source_text: first
    anchor: relayed
    surface: internal
    status: open
    claimed_by: []
  - id: R2
    source_text: second
    anchor: relayed
    surface: internal
    status: open
    claimed_by: []
  - id: R3
    source_text: third
    anchor: relayed
    surface: internal
    status: waived
    ruling: {by: pm, on: "2026-10-24"}
    claimed_by: []
"""


def _plan(claimed: str) -> str:
    return (
        f"---\ntitle: fixture\nsizing_object: {SIZING_REL}\nregister_claims:\n"
        f"  sizing: {SIZING_REL}\n  rows: {claimed}\n---\n# Fixture\n\n## Tasks\n\n"
        "```yaml plan-tasks\n- id: A1\n  title: row\n  change_kind: code-edit\n"
        "  surface: pkg/a.py\n  writes:\n    - pkg/a.py\n```\n"
    )


def _repo(tmp_path, claimed):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(_plan(claimed), encoding="utf-8", newline="\n")
    (tmp_path / SIZING_REL).write_text(SIZING, encoding="utf-8", newline="\n")


def test_a_plan_omitting_a_register_row_is_refused_before_any_manifest(tmp_path):
    _repo(tmp_path, "[R1]")
    with pytest.raises(ask_stage.AskStageError) as exc:
        ask_stage.stage(tmp_path, run_id="r1", plan_rel=PLAN_REL)
    text = str(exc.value)
    assert text.startswith("REGISTER-ROWS-UNCLAIMED")
    assert "R2" in text and "R3" not in text
    assert f"Refire {SIZING_REL}" in text
    assert not list((tmp_path / RUN_DIR_ROOT).rglob("manifest.json"))


def test_ledger_resume_is_not_refused(tmp_path):
    _repo(tmp_path, "[R1]")
    manifest = ask_stage.stage(tmp_path, run_id="r1", plan_rel=PLAN_REL, ledger_resume=True)
    assert manifest is not None


def test_claim_register_rows_claims_the_omitted_rows_and_passes(tmp_path):
    _repo(tmp_path, "[R1]")
    manifest = ask_stage.stage(tmp_path, run_id="r1", plan_rel=PLAN_REL, claim_register_rows=True)
    assert manifest is not None


def test_a_ruled_out_row_alone_does_not_refuse(tmp_path):
    _repo(tmp_path, "[R1, R2]")
    manifest = ask_stage.stage(tmp_path, run_id="r1", plan_rel=PLAN_REL)
    assert manifest is not None
