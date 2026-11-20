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


def test_every_path_left_out_carries_a_named_reason():
    out = land_run._land_coverage(
        [PLAN, *CODE], {PLAN}, {"src/a.py": "withheld by run-x"}, PLAN, "r1", dirty=set(CODE),
    )
    assert out["withheld_reasons"]["src/a.py"] == "withheld by run-x"
    assert out["withheld_reasons"]["tests/test_a.py"] == land_run.UNATTRIBUTED_LEFT_REASON
    assert out["withheld_reasons"] == out["withheld_code"]


def test_a_clean_path_already_at_head_is_landed_at_base_not_left_dirty():
    out = land_run._land_coverage([PLAN, *CODE], {PLAN}, {}, PLAN, "r1", dirty={"tests/test_a.py"})
    assert out["land_outcome"] == "partial"
    assert list(out["withheld_reasons"]) == ["tests/test_a.py"]
    assert out["landed_at_base"] == ["src/a.py"]
    assert "src/a.py" not in out["error"]


def test_only_clean_paths_outside_the_commit_is_untyped():
    assert land_run._land_coverage([PLAN, *CODE], {PLAN}, {}, PLAN, "r1", dirty=set()) is None


def test_land_legs_record_carries_withheld_reasons(tmp_path):
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    rid = "ask-20261120T000000-abcdef"
    land_leg_record.write(
        tmp_path, rid, outcome="partial", leg=None, bound_by=None, legs=[], withheld_reasons={"a.py": "why"},
    )
    body = json.loads((record_dir(tmp_path, rid) / land_leg_record.FILE_NAME).read_text())
    assert body["withheld_reasons"] == {"a.py": "why"}


def _verdict(status):
    return {"items": [{"id": "R3", "status": status, "wired": True, "evidence": "phase entry in scripts/example_game_repo_setup.ps1"}]}


def test_post_commit_row_whose_evidence_path_was_withheld_reads_not_met():
    from coordinator_core.ops.dispatch_emit import terminal_commit_ledger as tcl

    for status in ("post_commit", "met"):
        verdict, unmet = tcl.withheld_evidence_unmet(
            _verdict(status), {"scripts/example_game_repo_setup.ps1": "withheld by run-x"}, {}, {},
        )
        item = verdict["items"][0]
        assert item["status"] == "not_met" and item["wired"] is False
        assert "withheld by run-x" in unmet["R3"] and "scripts/example_game_repo_setup.ps1" in item["remaining"][0]


def test_row_serving_the_item_names_its_evidence_paths_by_declared_writes():
    from coordinator_core.ops.dispatch_emit import terminal_commit_ledger as tcl

    verdict = {"items": [{"id": "R3", "status": "post_commit", "wired": True}]}
    _v, unmet = tcl.withheld_evidence_unmet(verdict, {"p.ps1": "why"}, {"C8": ("p.ps1",)}, {"C8": ("R3",)})
    assert list(unmet) == ["R3"]


def test_unrelated_withheld_path_leaves_the_verdict_alone():
    from coordinator_core.ops.dispatch_emit import terminal_commit_ledger as tcl

    verdict = _verdict("post_commit")
    assert tcl.withheld_evidence_unmet(verdict, {"other.py": "why"}, {}, {}) == (verdict, {})


def test_land_legs_record_always_carries_withheld_reasons_even_empty(tmp_path):
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    rid = "ask-20261120T000001-abcdef"
    land_leg_record.write(tmp_path, rid, outcome="landed", leg=None, bound_by=None, legs=[])
    assert json.loads((record_dir(tmp_path, rid) / land_leg_record.FILE_NAME).read_text())["withheld_reasons"] == {}
