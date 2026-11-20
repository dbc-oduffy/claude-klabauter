"""A land whose commit omits the run's measured code paths is not plain `landed`."""

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import land_leg_record, land_run

PLAN = "docs/plans/2026-11-20-x-68c477.md"
CODE = ["src/a.py", "tests/test_a.py"]


def test_plan_doc_only_commit_is_landed_no_code_and_names_paths():
    out = land_run._land_coverage(
        [PLAN, *CODE], {PLAN}, {"src/a.py": "withheld as foreign"}, PLAN, "r1",
    )
    assert out["land_outcome"] == "landed_no_code"
    assert out["ok"] is False
    assert set(out["withheld_code"]) == set(CODE)
    assert "withheld as foreign" in out["withheld_code"]["src/a.py"]
    assert out["next_action"]["op"] == "dispatch.record_halted_run_landed"


def test_all_code_landed_is_untyped():
    assert land_run._land_coverage([PLAN, *CODE], {PLAN, *CODE}, {}, PLAN, "r1") is None


def test_partial_land_is_typed_partial_with_withheld_list():
    out = land_run._land_coverage([PLAN, *CODE], {PLAN, "src/a.py"}, {}, PLAN, "r1")
    assert out["land_outcome"] == "partial"
    assert list(out["withheld_code"]) == ["tests/test_a.py"]
    assert "ok" not in out


def test_bookkeeping_only_run_is_untyped():
    assert land_run._land_coverage([PLAN, "state/warp/r/x.json"], set(), {}, PLAN, "r1") is None


def test_admitted_land_records_typed_outcome(tmp_path, monkeypatch):
    reply = {"committed": True, "land_outcome": "landed_no_code"}
    monkeypatch.setattr(land_run, "_dispatch_mode", lambda *a, **k: reply)
    monkeypatch.setattr(land_run, "_chain_landed_tranche", lambda *a, **k: None)
    seen = {}
    monkeypatch.setattr(land_leg_record, "write", lambda root, rid, **kw: seen.update(kw))
    land_run._dispatch_admitted("product", tmp_path, tmp_path, {"run_id": "r1"}, None)
    assert seen["outcome"] == "landed_no_code"
