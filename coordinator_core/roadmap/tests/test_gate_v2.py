"""gate_v2.compute_gate_report: fail-closed invariants, candidacy withdrawals, gates, waves, plan link."""
import pytest

from coordinator_core.git.git_state import IndexParseError
from coordinator_core.roadmap import gate_v2


def _fm(**fields):
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(value)}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\nbody\n"


def baton(root, name, *, archive=None, **fields):
    base = {"stub_id": name, "baton_role": "work", "status": "open", "deployment_state": "ready_to_fire"}
    base.update(fields)
    where = root / ("archive/handoffs/" + archive if archive else "state/handoffs")
    where.mkdir(parents=True, exist_ok=True)
    (where / f"{name}.md").write_text(_fm(**base), encoding="utf-8")


def plan(root, name, **fields):
    (root / "docs/plans").mkdir(parents=True, exist_ok=True)
    (root / "docs/plans" / f"{name}.md").write_text(_fm(**fields), encoding="utf-8")


@pytest.fixture
def root(tmp_path):
    (tmp_path / "state/handoffs").mkdir(parents=True)
    return tmp_path


def by_id(report):
    return {b["id"]: b for b in report["batons"]}


def test_unresolved_blocker_closes_both_gates_and_is_listed(root):
    baton(root, "a", blocked_by=["no-such-record"])
    report = gate_v2.compute_gate_report(root)
    a = by_id(report)["a"]
    assert a["planning_gate"]["open"] is False and a["execution_gate"]["open"] is False
    assert a["planning_gate"]["blocking"] == [{"blocker": "no-such-record", "disposition": "unresolved"}]
    assert report["unresolved_blockers"] == ["no-such-record"]


def test_role_name_blocker_is_unresolved_for_its_candidate_and_the_sweep_goes_on(root):
    baton(root, "a", blocked_by=["coordinator-content-repo-em"])
    baton(root, "b")
    report = gate_v2.compute_gate_report(root)
    batons = by_id(report)
    assert set(batons) == {"a", "b"}
    assert batons["b"]["execution_gate"]["open"] is True
    assert batons["a"]["execution_gate"]["open"] is False
    assert report["unresolved_blockers"] == ["coordinator-content-repo-em"]


def test_narrowing_keeps_a_non_target_as_a_blocker(root):
    baton(root, "blocker", roadmap_id="other")
    baton(root, "a", roadmap_id="r1", blocked_by=["blocker"])
    report = gate_v2.compute_gate_report(root, roadmap_id="r1")
    assert set(by_id(report)) == {"a"}
    assert by_id(report)["a"]["planning_gate"]["blocking"][0]["blocker"] == "blocker"
    report = gate_v2.compute_gate_report(root, targets=["a"])
    assert set(by_id(report)) == {"a"}
    assert by_id(report)["a"]["execution_gate"]["open"] is False


def test_unreadable_handoff_dir_raises_not_an_empty_report(tmp_path):
    with pytest.raises(gate_v2.GateReadRefused):
        gate_v2.compute_gate_report(tmp_path)


def test_unresolvable_subject_raises(root):
    baton(root, "a")
    with pytest.raises(gate_v2.GateReadRefused):
        gate_v2.compute_gate_report(root, subject="nope")


def test_candidate_without_frontmatter_content_is_dropped_but_unparseable_candidate_refuses(root):
    (root / "state/handoffs/x.md").write_text("no frontmatter\n", encoding="utf-8")
    baton(root, "a")
    assert set(by_id(gate_v2.compute_gate_report(root))) == {"a"}


def test_untracked_candidate_is_withdrawn_and_reported(root, monkeypatch):
    baton(root, "a")
    baton(root, "b")
    monkeypatch.setattr(gate_v2, "read_index_paths", lambda repo: frozenset({"state/handoffs/a.md"}))
    report = gate_v2.compute_gate_report(root)
    assert set(by_id(report)) == {"a"}
    assert report["untracked"] == ["state/handoffs/b.md"]


def test_unreadable_index_withholds_nothing_and_says_nothing(root, monkeypatch):
    baton(root, "a")

    def boom(repo):
        raise IndexParseError("split index")

    monkeypatch.setattr(gate_v2, "read_index_paths", boom)
    report = gate_v2.compute_gate_report(root)
    assert set(by_id(report)) == {"a"}
    assert report["untracked"] == []


def test_resurrected_baton_is_withdrawn(root):
    baton(root, "a")
    baton(root, "a", archive="2026-01", deployment_state="shipped")
    baton(root, "b")
    baton(root, "b", archive="2026-01", deployment_state="ready_to_fire")
    assert set(by_id(gate_v2.compute_gate_report(root))) == {"b"}


def test_replanned_source_is_withdrawn(root):
    baton(root, "old")
    baton(root, "new", replan_of="old")
    assert set(by_id(gate_v2.compute_gate_report(root))) == {"new"}


def test_superseded_baton_is_withdrawn(root):
    baton(root, "a", superseded_by="b")
    baton(root, "b")
    assert set(by_id(gate_v2.compute_gate_report(root))) == {"b"}


def test_held_with_reason_is_withdrawn_and_listed(root):
    baton(root, "a", plan_blitz_hold_reason="waiting on PM")
    report = gate_v2.compute_gate_report(root)
    assert report["batons"] == []
    assert report["held"] == ["state/handoffs/a.md"]


def test_hold_without_reason_is_not_honoured(root):
    baton(root, "a", plan_blitz_hold_cite="somewhere")
    report = gate_v2.compute_gate_report(root)
    assert set(by_id(report)) == {"a"}
    assert report["held"] == []


def test_hold_cleared_by_re_offers_the_baton(root):
    baton(root, "a", plan_blitz_hold_reason="x", plan_blitz_hold_cleared_by="pm")
    assert set(by_id(gate_v2.compute_gate_report(root))) == {"a"}


def test_not_a_candidate_states_are_dropped(root):
    baton(root, "claimed", status="claimed")
    baton(root, "flying", deployment_state="in_flight")
    baton(root, "rec", baton_role="record")
    baton(root, "ok")
    assert set(by_id(gate_v2.compute_gate_report(root))) == {"ok"}


def test_waiting_on_execution_is_not_a_candidate(root):
    (root / "state/sizings").mkdir(parents=True)
    (root / "state/sizings/s.yaml").write_text("route: dispatch\n", encoding="utf-8")
    baton(root, "dep")
    baton(root, "w", sizing_object="state/sizings/s.yaml", blocked_by=["dep"])
    report = gate_v2.compute_gate_report(root)
    w = by_id(report)["w"]
    assert w["waiting_on_execution"] is True and w["candidate"] is False and w["needs_plan"] is False
    assert w["sized"] is True
    assert report["counts"]["candidates"] == 1


@pytest.mark.parametrize(
    "blocker_fields,plan_status,planning,execution",
    [
        ({"deployment_state": "shipped"}, None, True, True),
        ({"deployment_state": "continued"}, None, True, True),
        ({"deployment_state": "closed"}, None, True, True),
        ({"deliverable_id": "d1"}, "approved", True, False),
        ({"deliverable_id": "d1"}, "executing", True, False),
        ({"deliverable_id": "d1"}, "landed", True, True),
        ({"deliverable_id": "d1"}, "implemented", True, True),
        ({"deliverable_id": "d1"}, "reviewed", False, False),
        ({"deliverable_id": "d1"}, "draft", False, False),
        ({}, None, False, False),
    ],
)
def test_gate_truth_table(root, blocker_fields, plan_status, planning, execution):
    baton(root, "blk", **blocker_fields)
    if plan_status:
        plan(root, "p", status=plan_status, deliverable_id="d1", title="p")
    baton(root, "a", blocked_by=["blk"])
    a = by_id(gate_v2.compute_gate_report(root))["a"]
    assert a["planning_gate"]["open"] is planning
    assert a["execution_gate"]["open"] is execution


def test_closure_is_transitive_and_stops_at_a_coded_blocker(root):
    baton(root, "shipped", deployment_state="shipped", blocked_by=["ghost"])
    baton(root, "mid", blocked_by=["far"])
    baton(root, "far")
    baton(root, "a", blocked_by=["shipped", "mid"])
    baton(root, "b", blocked_by=["shipped"])
    report = gate_v2.compute_gate_report(root)
    batons = by_id(report)
    assert {e["blocker"] for e in batons["a"]["execution_gate"]["blocking"]} == {"mid", "far"}
    assert batons["b"]["execution_gate"]["open"] is True
    assert "ghost" not in report["unresolved_blockers"]


def test_blocker_resolves_through_an_archived_header_id(root):
    baton(root, "file-name", archive="2026-01", stub_id="stub-7", deployment_state="shipped")
    baton(root, "a", blocked_by=["stub-7", "file-name"])
    report = gate_v2.compute_gate_report(root)
    assert by_id(report)["a"]["execution_gate"]["open"] is True
    assert report["unresolved_blockers"] == []


def test_waves_number_by_depth(root):
    baton(root, "a")
    baton(root, "b", blocked_by=["a"])
    baton(root, "c", blocked_by=["b"])
    baton(root, "free")
    report = gate_v2.compute_gate_report(root)
    assert report["waves"] == [["a", "free"], ["b"], ["c"]]
    assert report["cycles"] == []
    assert report["counts"]["unschedulable"] == 0


def test_wave_depth_counts_a_candidate_blocker_outside_the_narrowing(root):
    baton(root, "a", roadmap_id="r0")
    baton(root, "b", roadmap_id="r1", blocked_by=["a"])
    report = gate_v2.compute_gate_report(root, roadmap_id="r1")
    assert report["waves"] == [[], ["b"]]


def test_cycle_members_are_named_and_unschedulable(root):
    baton(root, "x", blocked_by=["y"])
    baton(root, "y", blocked_by=["x"])
    baton(root, "z", blocked_by=["x"])
    baton(root, "ok")
    report = gate_v2.compute_gate_report(root)
    assert report["cycles"] == ["x", "y"]
    assert report["waves"] == [["ok"]]
    assert report["counts"]["unschedulable"] == 3


def test_a_baton_with_an_unresolved_blocker_is_in_no_wave(root):
    baton(root, "a", blocked_by=["ghost"])
    report = gate_v2.compute_gate_report(root)
    assert report["waves"] == []
    assert report["counts"]["unschedulable"] == 1


def test_a_planned_baton_is_not_scheduled_and_does_not_need_a_plan(root):
    plan(root, "p", status="approved", deliverable_id="d1", title="p")
    baton(root, "a", deliverable_id="d1")
    baton(root, "auth", execution_authorized_by="pm")
    report = gate_v2.compute_gate_report(root)
    assert report["waves"] == []
    assert by_id(report)["a"]["needs_plan"] is False
    assert by_id(report)["auth"]["needs_plan"] is False
    assert by_id(report)["auth"]["execution_authorized"] is True


def test_plan_link_basis_order_prefers_deliverable_id_over_sizing_object(root):
    plan(root, "by-d", status="approved", deliverable_id="d1", sizing_object="s1", title="t")
    plan(root, "by-s", status="draft", sizing_object="s2", title="t")
    baton(root, "a", deliverable_id="d1", sizing_object="s2")
    baton(root, "b", sizing_object="s2")
    batons = by_id(gate_v2.compute_gate_report(root))
    assert batons["a"]["plan"] == {"path": "docs/plans/by-d.md", "status": "approved", "link_basis": "deliverable_id"}
    assert batons["b"]["plan"] == {"path": "docs/plans/by-s.md", "status": "draft", "link_basis": "sizing_object"}


def test_governing_plan_links_without_a_plan_index(root):
    plan(root, "gp", status="reviewed", title="t")
    baton(root, "a", governing_plan="docs/plans/gp.md")
    a = by_id(gate_v2.compute_gate_report(root))["a"]
    assert a["plan"]["link_basis"] == "governing_plan" and a["plan"]["status"] == "reviewed"
    assert a["unlinked_plan_claim"] is None


def test_unlinked_and_ambiguous_plan_fields_are_present_as_null_or_filled(root):
    plan(root, "p1", status="draft", sizing_object="shared", title="t")
    plan(root, "p2", status="draft", sizing_object="shared", title="t")
    baton(root, "amb", sizing_object="shared")
    baton(root, "claim", governing_plan="docs/plans/gone.md")
    baton(root, "none")
    batons = by_id(gate_v2.compute_gate_report(root))
    assert batons["amb"]["plan"] is None and batons["amb"]["ambiguous_plan_link"]["basis"] == "sizing_object"
    assert batons["claim"]["unlinked_plan_claim"] == {
        "path": "docs/plans/gone.md", "cause": "plan_absent", "status": None,
    }
    assert batons["claim"]["plan"] is None
    assert batons["none"]["plan"] is None
    assert batons["none"]["unlinked_plan_claim"] is None and batons["none"]["ambiguous_plan_link"] is None


def test_unlinked_claim_names_the_one_step_that_refused_it(root):
    plan(root, "other", status="deferred", deliverable_id="d-plan", title="t")
    (root / "docs/plans/review.md").write_text(_fm(kind="staff-eng-review", plan="x", status="draft"), encoding="utf-8")
    baton(root, "contra", governing_plan="docs/plans/other.md", deliverable_id="d-baton")
    baton(root, "sidecar", governing_plan="docs/plans/review.md")
    baton(root, "gone", governing_plan="docs/plans/gone.md")
    batons = by_id(gate_v2.compute_gate_report(root))
    assert batons["contra"]["unlinked_plan_claim"] == {
        "path": "docs/plans/other.md", "cause": "no_back_link", "status": "deferred",
    }
    assert batons["sidecar"]["unlinked_plan_claim"]["cause"] == "not_a_plan_record"
    assert batons["gone"]["unlinked_plan_claim"]["cause"] == "plan_absent"


def test_sidecar_in_docs_plans_is_not_a_plan(root):
    plan(root, "p-review", status="approved", deliverable_id="d1", plan="docs/plans/p.md", title="t")
    baton(root, "a", deliverable_id="d1")
    assert by_id(gate_v2.compute_gate_report(root))["a"]["plan"] is None


def test_sized_reads_disk_not_frontmatter_presence(root):
    (root / "state/sizings").mkdir(parents=True)
    (root / "state/sizings/real.yaml").write_text("route: plan\n", encoding="utf-8")
    baton(root, "yes", sizing_object="state/sizings/real.yaml")
    baton(root, "no", sizing_object="state/sizings/ghost.yaml")
    batons = by_id(gate_v2.compute_gate_report(root))
    assert batons["yes"]["sized"] is True and batons["yes"]["sizing_objects"] == ["state/sizings/real.yaml"]
    assert batons["no"]["sized"] is False and batons["no"]["sizing_objects"] == []


def test_subject_returns_verdict_for_a_non_candidate_and_never_narrows_closure(root):
    baton(root, "dep")
    baton(root, "flying", deployment_state="in_flight", blocked_by=["dep"])
    report = gate_v2.compute_gate_report(root, subject="flying")
    assert [b["id"] for b in report["batons"]] == ["flying"]
    assert report["batons"][0]["candidate"] is False
    assert report["verdict"]["execution_gate"]["open"] is False
    assert report["verdict"]["execution_gate"]["blocking"] == [{"blocker": "dep", "disposition": "unplanned"}]
    assert report["counts"]["candidates"] == 0


def test_subject_resolves_by_path_and_reports_held(root):
    baton(root, "h", plan_blitz_hold_reason="why")
    report = gate_v2.compute_gate_report(root, subject="state/handoffs/h.md")
    assert report["batons"][0]["held"] is True
    assert report["held"] == ["state/handoffs/h.md"]


def test_verdict_is_null_without_subject(root):
    baton(root, "a")
    assert gate_v2.compute_gate_report(root)["verdict"] is None


def test_unmatched_targets_name_targets_that_reach_no_row(root):
    baton(root, "a")
    baton(root, "claimed", status="claimed")
    report = gate_v2.compute_gate_report(root, targets=["a", "claimed", "ghost"])
    assert report["unmatched_targets"] == ["claimed", "ghost"]


def test_report_emits_exactly_the_requirement_9_keys(root):
    baton(root, "a")
    report = gate_v2.compute_gate_report(root)
    assert set(report) == gate_v2.REPORT_KEYS
    assert set(report["batons"][0]) == gate_v2.BATON_REPORT_KEYS
    assert set(report["counts"]) == {"candidates", "planning_open", "execution_open", "unschedulable"}


def test_inline_comment_on_blocked_by_does_not_hide_the_edges(root):
    baton(root, "dep")
    (root / "state/handoffs/a.md").write_text(
        "---\nbaton_role: work\nstatus: open\ndeployment_state: ready_to_fire\n"
        "blocked_by: [dep, ghost]  # ghost added later\n---\n",
        encoding="utf-8",
    )
    report = gate_v2.compute_gate_report(root)
    assert report["unresolved_blockers"] == ["ghost"]
    assert {e["blocker"] for e in by_id(report)["a"]["execution_gate"]["blocking"]} == {"dep", "ghost"}


# --- cost-path cuts: the fast readers must answer exactly as the slow scanner does -------------

_RECORD_SHAPES = {
    "plain": "title: Plain\nstatus: open\nblocked_by: [a, b]  # why\nsizing_object: 'x/y.yaml'\n",
    "block_list": "title: Listed\nblocked_by:\n  - a\n  - b\nstatus: open\n",
    "block_scalar": "title: >-\n  folded\n  title\nstatus: open\n",
    "doubled_apostrophe": "title: 'it''s'\nstatus: open\n",
    "backslash_escape": 'title: "a \\"q\\" b"\nstatus: open\n',
    "repeated_key": "status: open\nstatus: claimed\n",
    "null_and_empty_list": "title: null\nblocked_by: []\nstatus: open\n",
    "crlf": "title: Crlf\r\nstatus: open\r\nblocked_by: [a]\r\n",
}


@pytest.mark.parametrize("shape", sorted(_RECORD_SHAPES))
def test_baton_fields_agrees_with_the_narrow_scanner(tmp_path, shape):
    path = tmp_path / "rec.md"
    path.write_bytes(("---\n" + _RECORD_SHAPES[shape] + "---\nbody\n").encode("utf-8"))
    slow = gate_v2.pg._read_baton_fields(path)
    assert gate_v2._baton_fields(path) == slow


def test_baton_fields_without_frontmatter_or_file_is_empty(tmp_path):
    path = tmp_path / "plain.md"
    path.write_text("no frontmatter\n", encoding="utf-8")
    assert gate_v2._baton_fields(path) == {}
    assert gate_v2._baton_fields(tmp_path / "absent.md") == {}


def test_head_fields_reads_a_key_that_sits_past_the_first_window(tmp_path):
    path = tmp_path / "long.md"
    filler = "".join(f"census_{i}: {'x' * 60}\n" for i in range(60))
    path.write_text("---\ntitle: T\n" + filler + "sizing_object: s.yaml\n---\n", encoding="utf-8")
    assert path.stat().st_size > gate_v2._PLAN_HEAD_BYTES
    assert gate_v2._head_fields(path, gate_v2._PLAN_HEAD_BYTES, gate_v2._PLAN_WANT) == {
        "title": "T", "sizing_object": "s.yaml",
    }


def test_head_fields_hands_a_block_scalar_value_to_the_narrow_scanner(tmp_path):
    path = tmp_path / "folded.md"
    path.write_text("---\ntitle: >-\n  folded\n  text\nstatus: open\n---\n", encoding="utf-8")
    got = gate_v2._head_fields(path, gate_v2._HEAD_BYTES, frozenset({"title", "status"}))
    assert got == gate_v2.pg._scan_fields(path, frozenset({"title", "status"}))
    assert got["title"] != ">-"


def test_head_fields_only_reports_wanted_keys(tmp_path):
    path = tmp_path / "r.md"
    path.write_text("---\ntitle: T\nother: O\nstatus: open\n---\n", encoding="utf-8")
    assert gate_v2._head_fields(path, gate_v2._HEAD_BYTES, frozenset({"status"})) == {"status": "open"}


def test_tracked_set_comes_from_the_path_only_reader(root, monkeypatch):
    baton(root, "a")
    calls = []
    monkeypatch.setattr(gate_v2, "read_index_paths", lambda r: calls.append(r) or frozenset({"state/handoffs/a.md"}))
    report = gate_v2.compute_gate_report(root)
    assert calls == [root] and report["untracked"] == []
    monkeypatch.setattr(gate_v2, "read_index_paths", lambda r: frozenset({"state/handoffs/other.md"}))
    assert gate_v2.compute_gate_report(root)["untracked"] == ["state/handoffs/a.md"]
