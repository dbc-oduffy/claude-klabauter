"""Receipt writers are all-or-none over run_truth_contract.MANDATORY_STAGES (the 7 per-stage receipts).

`stage_receipts.write_receipt` must write and read back every one of the 7 stages; a stage it refuses, or a
stage list that is not 7, goes red. The call-site pin goes red when production code names some but not all
7 stages in a `dispatch.write_receipt` call: writers land all at once or not at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_truth_contract as rtc
from coordinator_core.ops.dispatch_emit import stage_receipts

EXPECTED_STAGES = ("sizing", "plan_review", "execute", "review", "kira", "terminal_judge", "reacher")


def test_the_registry_names_exactly_the_seven_stages():
    assert rtc.MANDATORY_STAGES == EXPECTED_STAGES


@pytest.mark.parametrize("stage", EXPECTED_STAGES)
def test_the_writer_is_registered_for_every_stage(stage, tmp_path):
    stage_receipts.write_receipt(tmp_path / "run-x", stage, "ok", None)
    got = stage_receipts.read_receipt(tmp_path / "run-x", stage)
    assert got is not None and got.stage == stage


def test_all_seven_present_and_none_missing_after_writing_each(tmp_path):
    run_dir = tmp_path / "run-x"
    for stage in EXPECTED_STAGES:
        stage_receipts.write_receipt(run_dir, stage, "ok", None)
    assert stage_receipts.present(run_dir) == EXPECTED_STAGES
    assert stage_receipts.missing(run_dir, None, None) == ()


def test_a_stage_outside_the_registry_is_refused(tmp_path):
    with pytest.raises(ValueError):
        stage_receipts.write_receipt(tmp_path / "run-x", "not_a_stage", "ok", None)


_CALL = re.compile(r"dispatch\.write_receipt.{0,400}?[\"']?stage[\"']?\s*[:=]\s*[\"'](\w+)[\"']", re.S)


def test_production_writer_call_sites_name_no_stage_or_all_seven():
    pkg = Path(rtc.__file__).resolve().parents[2]
    named = set()
    for src in pkg.rglob("*.py"):
        if "tests" in src.parts or src.name.startswith("test_") or src.name == "stage_receipts.py":
            continue
        named.update(_CALL.findall(src.read_text(encoding="utf-8", errors="replace")))
    assert named in (set(), set(EXPECTED_STAGES)), sorted(named)
