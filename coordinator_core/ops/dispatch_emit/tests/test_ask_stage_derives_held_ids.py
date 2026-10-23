"""stage derives `held_ids` on a ledger resume of a manifest recorded before the key existed."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit.ask_stage import derive_held_ids, stage, without_held_rows
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

PLAN_REL = "docs/plans/p.md"
RUN = "r1"


def _row(rid: str, kind: str = "doc-edit") -> str:
    return (
        f"- id: {rid}\n  title: t{rid}\n  change_kind: {kind}\n  surface: docs/x/{rid}.md\n"
        f"  writes:\n    - docs/x/{rid}.md\n  disposition: open\n"
    )


def _plan(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir(exist_ok=True)
    plan = tmp_path / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    rows = _row("A1") + _row("A2") + _row("A3") + _row("A4", "verification")
    plan.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
        encoding="utf-8",
        newline="\n",
    )


def _record(tmp_path: Path, manifest: dict) -> None:
    path = tmp_path / RUN_DIR_ROOT / RUN / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")


_RAW = {
    "A1": {"disposition": "open"},
    "A2": {"disposition": "open"},
    "A3": {"disposition": "coded"},
    "A4": {"disposition": "open", "change_kind": "verification"},
}


def test_pre_change_manifest_yields_the_unrun_executor_rows():
    recorded = {"rows": [{"id": "A1"}], "owed": [], "gated": [], "slot_withheld": []}
    assert derive_held_ids(_RAW, recorded, staged_ids=()) == ("A2", "A3")


def test_staged_and_landed_rows_are_not_held():
    recorded = {"rows": [{"id": "A1"}]}
    assert derive_held_ids(_RAW, recorded, staged_ids=("A2",), landed_ids=("A3",)) == ()


def test_recorded_held_ids_are_kept_not_rederived():
    assert derive_held_ids(_RAW, {"rows": [], "held_ids": ["A9"]}, staged_ids=()) == ("A9",)


def test_no_recorded_manifest_holds_nothing():
    assert derive_held_ids(_RAW, None, staged_ids=()) == ()


def test_ledger_resume_stage_writes_the_derived_held_ids(tmp_path):
    _plan(tmp_path)
    _record(tmp_path, {"rows": [{"id": "A1"}, {"id": "A3"}], "owed": []})

    manifest = stage(tmp_path, run_id=RUN, plan_rel=PLAN_REL, ledger_resume=True, only_rows=["A1"])

    assert manifest.held_ids == ("A2",)
    written = json.loads((tmp_path / RUN_DIR_ROOT / RUN / "manifest.json").read_text(encoding="utf-8"))
    assert written["held_ids"] == ["A2"]


def test_rows_served_by_a_held_chunk_are_not_rulable_but_others_stay():
    raw = {"A2": {"register_rows": ["R1"]}}
    assert without_held_rows(("R1", "R2"), ("A2",), raw) == ("R2",)
    assert without_held_rows(("R1", "R2"), (), raw) == ("R1", "R2")


def test_non_resume_stage_writes_no_held_ids(tmp_path):
    _plan(tmp_path)
    _record(tmp_path, {"rows": [{"id": "A1"}], "owed": []})

    manifest = stage(tmp_path, run_id=RUN, plan_rel=PLAN_REL)

    assert manifest.held_ids == ()
    written = json.loads((tmp_path / RUN_DIR_ROOT / RUN / "manifest.json").read_text(encoding="utf-8"))
    assert "held_ids" not in written
