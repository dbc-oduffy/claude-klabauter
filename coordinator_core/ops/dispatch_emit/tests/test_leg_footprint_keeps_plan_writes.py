"""A path a plan row declares in `writes` reaches the ledger-remaining leg's footprint."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_contract import LEG_DISPATCH
from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import plan_rows_serving, remaining_leg_plan

_FM = '---\nregister_claims:\n  rows: ["R1"]\n---\n'


def _plan(tmp_path, writes_yaml):
    p = tmp_path / "plan.md"
    p.write_text(f"{_FM}# P\n\n## Tasks\n\n```yaml plan-tasks\n- id: C1\n  disposition: open\n  {writes_yaml}\n```\n")
    return p


def test_scalar_writes_is_one_path_not_its_characters(tmp_path):
    assert plan_rows_serving(_plan(tmp_path, "writes: pkg/new.py"), ["R1"]) == ["pkg/new.py"]


def test_list_writes_unchanged(tmp_path):
    assert plan_rows_serving(_plan(tmp_path, "writes: [a.py, b.py]"), ["R1"]) == ["a.py", "b.py"]


def test_leg_keeps_plan_write_in_declared_form_and_tracked_spelling(tmp_path):
    spelling = lambda paths: {p: ("pkg/Mod.py" if p == "Pkg/mod.py" else None) for p in paths}
    plan = remaining_leg_plan(
        {"R1": {"remaining": ["do the thing"]}}, ["finish"], [], ["R1"], [], None, tmp_path,
        plan_writes=["./pkg/new.py", "Pkg\\mod.py".replace("\\", "/")], spelling=spelling,
    )
    assert plan["action"] == LEG_DISPATCH
    assert plan["writes"] == ["pkg/new.py", "pkg/Mod.py"]
