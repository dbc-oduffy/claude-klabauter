"""gate_v2._info: a baton whose own disposition is coded reports both gates open, nothing blocking."""
from coordinator_core.roadmap import gate_v2
from coordinator_core.roadmap.tests.test_gate_v2 import baton, by_id, plan, root  # noqa: F401


def _blockers(root):
    plan(root, "drafted-plan", status="draft", deliverable_id="d-drafted", title="drafted")
    baton(root, "drafted", deliverable_id="d-drafted")


def _row(root, ident):
    return gate_v2.compute_gate_report(root, subject=ident)["batons"][0]


def test_baton_with_implemented_plan_reports_open_gates_despite_blockers(root):
    _blockers(root)
    plan(root, "landed-plan", status="implemented", deliverable_id="d-self", title="landed")
    baton(root, "landed", deliverable_id="d-self", blocked_by=["drafted", "no-such-record"])
    row = by_id(gate_v2.compute_gate_report(root))["landed"]
    assert row["planning_gate"] == {"open": True, "blocking": []}
    assert row["execution_gate"] == {"open": True, "blocking": []}


def test_coded_baton_contributes_no_unresolved_blockers(root):
    baton(root, "landed", deployment_state="shipped", blocked_by=["no-such-record"])
    report = gate_v2.compute_gate_report(root, subject="landed")
    row = report["batons"][0]
    assert row["planning_gate"]["open"] is True and row["execution_gate"]["open"] is True
    assert report["unresolved_blockers"] == []


def test_baton_with_shipped_deployment_state_reports_open_gates(root):
    _blockers(root)
    baton(root, "landed", deployment_state="shipped", blocked_by=["drafted", "no-such-record"])
    row = _row(root, "landed")
    assert row["planning_gate"] == {"open": True, "blocking": []}
    assert row["execution_gate"] == {"open": True, "blocking": []}


def test_non_coded_baton_with_the_same_blockers_stays_closed(root):
    _blockers(root)
    baton(root, "open-one", blocked_by=["drafted", "no-such-record"])
    row = by_id(gate_v2.compute_gate_report(root))["open-one"]
    assert row["planning_gate"]["open"] is False and row["execution_gate"]["open"] is False
    assert {e["blocker"] for e in row["planning_gate"]["blocking"]} == {"drafted", "no-such-record"}


def test_coding_a_row_of_a_reviewed_plan_clears_needs_plan(root):
    from coordinator_core.ops.dispatch_emit.plan_row_coding import flip_rows_coded

    rows = "- id: C1\n  title: t\n  change_kind: code-edit\n  surface: a.py\n  disposition: open\n  deferred: false\n"
    plan_file = root / "docs/plans/rp.md"
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text(
        f"---\nstatus: reviewed\ntitle: rp\n---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8"
    )
    baton(root, "a", governing_plan="docs/plans/rp.md")
    before = _row(root, "a")
    assert before["needs_plan"] is True and before["plan"]["status"] == "reviewed"
    text, flipped = flip_rows_coded(plan_file.read_text(encoding="utf-8"), {"C1"}, "a" * 40)
    assert flipped == ["C1"]
    plan_file.write_text(text, encoding="utf-8")
    after = _row(root, "a")
    assert after["needs_plan"] is False and after["plan"]["status"] == "executing"


def test_blocker_with_executing_plan_opens_planning_and_closes_execution(root):
    plan(root, "run-plan", status="executing", deliverable_id="d-run", title="run")
    baton(root, "running", deliverable_id="d-run")
    baton(root, "dependant", blocked_by=["running"])
    row = by_id(gate_v2.compute_gate_report(root))["dependant"]
    assert row["planning_gate"] == {"open": True, "blocking": []}
    assert row["execution_gate"] == {
        "open": False,
        "blocking": [{"blocker": "running", "disposition": "plan-approved"}],
    }
