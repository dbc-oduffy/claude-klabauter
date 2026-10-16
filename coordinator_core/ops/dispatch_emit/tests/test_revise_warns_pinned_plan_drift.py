"""workflow.revise warns, additively, when the sizing's plan pinned register rows the register no
longer matches; no plan or no drift means no warning, and the revision lands either way."""

from __future__ import annotations

import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

RUN = "ask-20261016T000000-abc123"
REL = "state/sizings/2026-10-16-warn.yaml"
PLAN = "docs/plans/2026-10-16-warn.md"


def _repo(tmp_path, *, plan_edge=True, pin_text="build the op"):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    sizing = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
    }
    if plan_edge:
        sizing["plan"] = PLAN
    (tmp_path / REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    fm = {
        "title": "p",
        "register_claims": {"sizing": REL, "rows": ["R1"], "pins": {"R1": rr.row_pin(pin_text)}},
    }
    (tmp_path / PLAN).write_text("---\n" + yaml.safe_dump(fm) + "---\n# p\n", encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL)
    return tmp_path


def _revise(root):
    return rl._handler({"run_id": RUN, "verbatim": "also cover Windows"}, repo_root=root)


def test_drifted_pin_warns_naming_the_row_and_the_pull(tmp_path):
    out = _revise(_repo(tmp_path, pin_text="build a different op"))
    assert out["pin_drift"] == ["R1"]
    assert "R1" in out["warning"] and PLAN in out["warning"] and "will be pulled" in out["warning"]
    assert out["item"] == "R2"


def test_new_row_unclaimed_by_pinned_plan_warns_naming_the_row_and_the_pull(tmp_path):
    out = _revise(_repo(tmp_path))
    assert out["item"] == "R2" and "pin_drift" not in out
    assert out["unclaimed_rows"] == ["R2"]
    assert "R2" in out["warning"] and PLAN in out["warning"] and "will be pulled" in out["warning"]


def test_drift_and_unclaimed_row_both_named_in_one_warning(tmp_path):
    out = _revise(_repo(tmp_path, pin_text="build a different op"))
    assert out["pin_drift"] == ["R1"] and out["unclaimed_rows"] == ["R2"]
    assert "R1" in out["warning"] and "R2" in out["warning"]


def test_all_rows_claimed_with_matching_pins_carry_no_warning(tmp_path):
    out = rl._handler({"run_id": RUN, "note": "no row added"}, repo_root=_repo(tmp_path))
    assert out["ok"] is True
    assert "warning" not in out and "pin_drift" not in out and "unclaimed_rows" not in out


def test_no_plan_edge_carries_no_warning(tmp_path):
    out = _revise(_repo(tmp_path, plan_edge=False, pin_text="other"))
    assert "warning" not in out and out["item"] == "R2"
