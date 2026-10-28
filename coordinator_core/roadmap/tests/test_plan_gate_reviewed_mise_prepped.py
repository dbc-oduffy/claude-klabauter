"""A reviewed plan carrying a mise_prepped stamp opens the planning gate; the execution gate stays closed."""
from coordinator_core.roadmap import gate_v2
from coordinator_core.roadmap.tests.test_gate_v2 import baton, by_id, plan, root  # noqa: F401


def _report(root):
    return by_id(gate_v2.compute_gate_report(root))


def test_reviewed_plan_with_mise_prepped_stamp_opens_planning_gate_only(root):
    plan(root, "up", status="reviewed", deliverable_id="d-up", title="up", mise_prepped_sha="81d34e0f01")
    baton(root, "up", deliverable_id="d-up")
    baton(root, "down", blocked_by=["up"])
    row = _report(root)["down"]
    assert row["planning_gate"] == {"open": True, "blocking": []}
    assert row["execution_gate"]["open"] is False
    assert row["execution_gate"]["blocking"] == [{"blocker": "up", "disposition": "plan-approved"}]


def test_reviewed_plan_without_stamp_keeps_planning_gate_closed(root):
    plan(root, "up", status="reviewed", deliverable_id="d-up", title="up")
    baton(root, "up", deliverable_id="d-up")
    baton(root, "down", blocked_by=["up"])
    row = _report(root)["down"]
    assert row["planning_gate"]["open"] is False
    assert row["planning_gate"]["blocking"] == [{"blocker": "up", "disposition": "plan-drafted"}]


def test_build_plan_index_reads_the_stamp(root):
    from coordinator_core.roadmap import plan_gate as pg

    plan(root, "up", status="reviewed", deliverable_id="d-up", title="up", mise_prepped_sha="81d34e0f01")
    plan(root, "bare", status="reviewed", deliverable_id="d-bare", title="bare")
    by_path = pg.build_plan_index(root).by_path
    assert by_path["docs/plans/up.md"]["approved"] is True
    assert by_path["docs/plans/bare.md"]["approved"] is False


def test_governing_plan_outside_the_index_reads_the_stamp(root):
    (root / "elsewhere").mkdir()
    (root / "elsewhere/up.md").write_text(
        "---\ntitle: up\nstatus: reviewed\nmise_prepped_sha: 81d34e0f01\n---\nbody\n", encoding="utf-8"
    )
    baton(root, "up", governing_plan="elsewhere/up.md")
    baton(root, "down", blocked_by=["up"])
    assert _report(root)["down"]["planning_gate"]["open"] is True


def test_draft_plan_with_stamp_stays_closed(root):
    plan(root, "up", status="draft", deliverable_id="d-up", title="up", mise_prepped_sha="81d34e0f01")
    baton(root, "up", deliverable_id="d-up")
    baton(root, "down", blocked_by=["up"])
    assert _report(root)["down"]["planning_gate"]["open"] is False
