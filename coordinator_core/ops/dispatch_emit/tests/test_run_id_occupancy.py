"""`run_id_occupancy`: tranche numbering continues past every -tN output, and a script whose
run id has landed or run is reported spent."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit.run_id_occupancy import (
    family_spent_reason,
    next_tranche_number,
    spent_run_reason,
)

BASE = "20261021T182421-0ee696ba"


def _touch(d: Path, *names: str) -> None:
    for name in names:
        (d / name).write_text("x", encoding="utf-8")


def test_empty_dir_is_one(tmp_path):
    assert next_tranche_number(tmp_path, BASE, BASE) == 1


def test_records_alone_number_as_before(tmp_path):
    _touch(tmp_path, f"{BASE}.md", f"{BASE}-t1.md", f"{BASE}-t2.md")
    assert next_tranche_number(tmp_path, BASE, BASE) == 3


def test_outputs_without_the_record_still_occupy_t1(tmp_path):
    _touch(tmp_path, f"{BASE}-t1-p1.workflow.mjs", f"{BASE}-t1-p1.spine.md")
    assert next_tranche_number(tmp_path, BASE, BASE) == 2


def test_receipt_and_land_scripts_count(tmp_path):
    _touch(tmp_path, f"{BASE}-t3-p2-land.workflow.mjs.emitted.json")
    assert next_tranche_number(tmp_path, BASE, BASE) == 4


def test_gap_is_never_reused(tmp_path):
    _touch(tmp_path, f"{BASE}-t1.md", f"{BASE}-t4-p1.spine.md")
    assert next_tranche_number(tmp_path, BASE, BASE) == 5


def test_other_family_ignored(tmp_path):
    _touch(tmp_path, "20260101T000000-aaaaaaaa-t9.md", f"{BASE}-r2-t5.md")
    assert next_tranche_number(tmp_path, BASE, BASE) == 1


def _script(d: Path, name: str, receipt: dict | None) -> Path:
    path = d / name
    path.write_text("// s\n", encoding="utf-8")
    if receipt is not None:
        (d / (name + ".emitted.json")).write_text(json.dumps(receipt), encoding="utf-8")
    return path


def test_landed_receipt_is_spent_and_names_the_receipt(tmp_path):
    s = _script(tmp_path, f"{BASE}-t1-p1.workflow.mjs", {"landed": {"sha": "a", "at": "t"}})
    reason = spent_run_reason(s)
    assert reason and f"{s.name}.emitted.json" in reason


def test_unlanded_receipt_without_evidence_is_not_spent(tmp_path):
    s = _script(tmp_path, f"{BASE}-t1-p1.workflow.mjs", {"sha256": "a"})
    assert spent_run_reason(s) is None


def test_no_receipt_is_not_spent(tmp_path):
    assert spent_run_reason(_script(tmp_path, f"{BASE}-t1-p1.workflow.mjs", None)) is None


def test_resume_and_land_siblings_are_spent_and_named(tmp_path):
    for kind in ("resume", "land2"):
        d = tmp_path / kind
        d.mkdir()
        s = _script(d, f"{BASE}-t1-p1.workflow.mjs", {"sha256": "a"})
        sib = f"{BASE}-t1-p1-{kind}.workflow.mjs"
        _touch(d, sib)
        reason = spent_run_reason(s)
        assert reason and sib in reason


def test_sibling_of_another_part_is_not_evidence(tmp_path):
    s = _script(tmp_path, f"{BASE}-t1-p1.workflow.mjs", {"sha256": "a"})
    _touch(tmp_path, f"{BASE}-t1-p2-resume.workflow.mjs")
    assert spent_run_reason(s) is None


def test_family_spent_reason_scans_parts(tmp_path):
    _script(tmp_path, f"{BASE}-t1-p1.workflow.mjs", {"sha256": "a"})
    _script(tmp_path, f"{BASE}-t1-p2.workflow.mjs", {"landed": {"sha": "a", "at": "t"}})
    assert family_spent_reason(tmp_path, f"{BASE}-t1")
    assert family_spent_reason(tmp_path, f"{BASE}-t2") is None
