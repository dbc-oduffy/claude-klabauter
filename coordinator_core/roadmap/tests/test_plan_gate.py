"""
coordinator_core/roadmap/tests/test_plan_gate.py — the two-gate resolver.

Subject: `coordinator_core.roadmap.plan_gate`, which answers "may planning
start?" and "may execution start?" as two separate questions over one
`blocked_by` edge set.

The claim under test, stated once so every case below reads against it: a
blocker holds EXECUTION until its work lands, but holds PLANNING only until its
plan clears review. Every test here either pins that divergence, pins one of the
fail-closed directions around it (an unresolved edge, a cycle, a sidecar
mistaken for a plan), or pins the cost of computing it.

Zero spawns; every case builds its corpus in `tmp_path`. The one exception is
`test_narrow_scan_agrees_with_the_general_parser`, which reads this repo's own
records — a parity oracle needs a corpus it did not author, and a hand-built
fixture would only prove the scanner agrees with the shapes its author thought
of.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from coordinator_core.roadmap import plan_gate as pg


def _write(path: Path, frontmatter: str, body: str = "body\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter.strip()}\n---\n\n{body}", encoding="utf-8")
    return path


def _baton(root: Path, stub_id: str, **fields) -> Path:
    lines = [
        "kind: roadmap-baton",
        f"title: {fields.pop('title', stub_id)}",
        f"stub_id: {stub_id}",
        f"status: {fields.pop('status', 'open')}",
        f"deployment_state: {fields.pop('deployment_state', 'ready_to_fire')}",
        "baton_role: work",
    ]
    for key, value in fields.items():
        if isinstance(value, (list, tuple)):
            lines.append(f"{key}: [{', '.join(value)}]")
        else:
            lines.append(f"{key}: {value}")
    return _write(root / "state" / "handoffs" / f"{stub_id}.md", "\n".join(lines))


def _plan(root: Path, slug: str, status: str, **fields) -> str:
    lines = [f"title: {slug}", f"status: {status}"]
    for key, value in fields.items():
        lines.append(f"{key}: {value}")
    _write(root / "docs" / "plans" / f"{slug}.md", "\n".join(lines))
    return f"docs/plans/{slug}.md"


def _by_id(report, ident):
    for baton in report["batons"]:
        if baton["id"] == ident:
            return baton
    raise AssertionError(f"{ident} absent from report: {[b['id'] for b in report['batons']]}")


@pytest.mark.parametrize(
    "plan_status, planning_open, execution_open, disposition",
    [
        ("draft", False, False, pg.BLOCKER_PLAN_DRAFTED),
        ("reviewed", False, False, pg.BLOCKER_PLAN_DRAFTED),
        ("approved", True, False, pg.BLOCKER_PLAN_APPROVED),
        ("executing", True, False, pg.BLOCKER_PLAN_APPROVED),
        ("landed", True, True, pg.BLOCKER_CODED),
        ("implemented", True, True, pg.BLOCKER_CODED),
        ("deferred", False, False, pg.BLOCKER_PLAN_DRAFTED),
        ("abandoned", False, False, pg.BLOCKER_PLAN_DRAFTED),
        ("superseded", False, False, pg.BLOCKER_PLAN_DRAFTED),
    ],
)
def test_plan_status_drives_the_two_gates_apart(
    tmp_path, plan_status, planning_open, execution_open, disposition
):
    plan_path = _plan(tmp_path, "blocker-plan", plan_status, plan_id="pln-blocker")
    _baton(tmp_path, "blocker-1", governing_plan=plan_path, origin_plan_id="pln-blocker")
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    report = pg.assemble_plan_gate(tmp_path)
    dependent = _by_id(report, "dependent-1")

    assert dependent["planning_gate"]["open"] is planning_open
    assert dependent["execution_gate"]["open"] is execution_open
    assert dependent["blockers"][0]["disposition"] == disposition


@pytest.mark.parametrize("state", sorted(pg.BATON_CODED_STATES))
def test_terminal_deployment_states_open_both_gates(tmp_path, state):
    _baton(tmp_path, "blocker-1", deployment_state=state)
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")

    assert dependent["planning_gate"]["open"] is True
    assert dependent["execution_gate"]["open"] is True
    assert dependent["blockers"][0]["disposition"] == pg.BLOCKER_CODED


def test_an_unplanned_blocker_holds_both_gates(tmp_path):
    _baton(tmp_path, "blocker-1")
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")

    assert dependent["planning_gate"]["open"] is False
    assert dependent["execution_gate"]["open"] is False
    assert dependent["blockers"][0]["disposition"] == pg.BLOCKER_UNPLANNED


def test_an_unresolvable_blocker_closes_both_gates_and_is_named(tmp_path):
    """A gate that fails OPEN on a typo authorises exactly the work the edge
    existed to hold. The id must also be REPORTED — a closed gate with no named
    cause is indistinguishable from a real dependency, and the author would go
    looking for a baton that does not exist."""
    _baton(tmp_path, "dependent-1", blocked_by=["nothing-on-disk"])

    report = pg.assemble_plan_gate(tmp_path)
    dependent = _by_id(report, "dependent-1")

    assert dependent["planning_gate"]["open"] is False
    assert dependent["execution_gate"]["open"] is False
    assert dependent["blockers"][0]["disposition"] == pg.BLOCKER_UNRESOLVED
    assert {"baton": "dependent-1", "blocker": "nothing-on-disk"} in report["unresolved_blockers"]
    assert dependent["planning_wave"] is None


def test_a_cycle_is_named_rather_than_silently_dropped(tmp_path):
    _baton(tmp_path, "a-1", blocked_by=["b-1"])
    _baton(tmp_path, "b-1", blocked_by=["a-1"])

    report = pg.assemble_plan_gate(tmp_path)

    assert report["cycles"], "a two-node cycle must be reported"
    assert set(report["cycles"][0]) == {"a-1", "b-1"}
    assert _by_id(report, "a-1")["planning_wave"] is None
    assert _by_id(report, "b-1")["planning_wave"] is None


def test_a_review_sidecar_is_not_mistaken_for_the_plan_it_reviews(tmp_path):
    _write(
        tmp_path / "docs" / "plans" / "thing.staff-eng-review.md",
        "kind: staff-eng-review\nplan: docs/plans/thing.md\n"
        "status: approved\nplan_id: pln-thing\nverdict: REQUIRES_CHANGES",
    )
    _baton(tmp_path, "blocker-1", origin_plan_id="pln-thing")
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")

    assert dependent["blockers"][0]["disposition"] == pg.BLOCKER_UNPLANNED
    assert dependent["planning_gate"]["open"] is False


def test_a_blocker_that_is_in_flight_is_unschedulable_not_wave_zero(tmp_path):
    _baton(tmp_path, "blocker-1", deployment_state="in_flight")
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")

    assert dependent["planning_wave"] is None
    assert dependent["planning_gate"]["open"] is False


def test_unschedulable_names_its_batons_and_what_holds_each(tmp_path):
    _baton(tmp_path, "blocker-1", deployment_state="in_flight")
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    report = pg.assemble_plan_gate(tmp_path)

    assert report["counts"]["unschedulable"] == len(report["unschedulable"])
    row = next(r for r in report["unschedulable"] if r["id"] == "dependent-1")
    assert row["held_by"] == ["blocker-1"]


def test_a_blocker_naming_no_baton_is_reported_as_what_holds_the_row(tmp_path):
    _baton(tmp_path, "pm-held-1", blocked_by=["pm-decision:seats-vs-reauth"])

    report = pg.assemble_plan_gate(tmp_path)

    row = next(r for r in report["unschedulable"] if r["id"] == "pm-held-1")
    assert row["held_by"] == ["pm-decision:seats-vs-reauth"]
    assert _by_id(report, "pm-held-1")["candidate"] is True
    assert all("pm-held-1" not in wave for wave in report["waves"])


def test_a_transitively_held_row_names_the_blocker_that_holds_it(tmp_path):
    _baton(tmp_path, "outside-1", deployment_state="in_flight")
    _baton(tmp_path, "middle-1", blocked_by=["outside-1"])
    _baton(tmp_path, "far-1", blocked_by=["middle-1"])

    report = pg.assemble_plan_gate(tmp_path)

    far = next(r for r in report["unschedulable"] if r["id"] == "far-1")
    assert far["held_by"] == ["middle-1"]
    assert not any(r["held_by"] == [] for r in report["unschedulable"])


def test_waves_follow_the_planning_gate_not_the_execution_gate(tmp_path):
    """The scheduling claim: a chain of depth 3 plans in 3 rounds, because each
    round's approvals open the next round's planning gates. Under the old
    single-gate reading this chain took 3 LANDINGS, not 3 reviews."""
    _baton(tmp_path, "a-1")
    _baton(tmp_path, "b-1", blocked_by=["a-1"])
    _baton(tmp_path, "c-1", blocked_by=["b-1"])
    _baton(tmp_path, "d-1", blocked_by=["a-1"])

    report = pg.assemble_plan_gate(tmp_path)

    assert report["waves"] == [["a-1"], ["b-1", "d-1"], ["c-1"]]
    assert _by_id(report, "c-1")["planning_wave"] == 2
    assert _by_id(report, "b-1")["execution_gate"]["open"] is False


def test_an_already_approved_blocker_collapses_its_dependent_to_wave_zero(tmp_path):
    plan_path = _plan(tmp_path, "done-plan", "approved")
    _baton(tmp_path, "blocker-1", governing_plan=plan_path)
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    report = pg.assemble_plan_gate(tmp_path)

    assert _by_id(report, "dependent-1")["planning_wave"] == 0
    assert report["waves"][0] == ["dependent-1"]
    assert _by_id(report, "blocker-1")["needs_plan"] is False
    assert _by_id(report, "blocker-1")["planning_wave"] is None


def test_a_baton_with_a_draft_plan_still_needs_one(tmp_path):
    """`needs_plan` keys on APPROVAL, not on a file existing. A draft plan is
    exactly the case a blitz should pick up and drive through review."""
    plan_path = _plan(tmp_path, "draft-plan", "draft")
    _baton(tmp_path, "subject-1", governing_plan=plan_path)

    baton = _by_id(pg.assemble_plan_gate(tmp_path), "subject-1")
    assert baton["needs_plan"] is True
    assert baton["planning_wave"] == 0


def test_targets_narrow_the_candidate_set_but_not_the_gates(tmp_path):
    _baton(tmp_path, "blocker-1")
    _baton(tmp_path, "wanted-1", blocked_by=["blocker-1"])
    _baton(tmp_path, "ignored-1")

    report = pg.assemble_plan_gate(tmp_path, targets=["wanted-1"])

    assert [b["id"] for b in report["batons"]] == ["wanted-1"]
    wanted = _by_id(report, "wanted-1")
    assert wanted["planning_gate"]["open"] is False
    assert wanted["blockers"][0]["disposition"] == pg.BLOCKER_UNPLANNED
    assert report["scanned"]["batons"] == 3


def test_a_multi_blocker_baton_waits_for_its_latest_blocker(tmp_path):
    _baton(tmp_path, "a-1")
    _baton(tmp_path, "b-1", blocked_by=["a-1"])
    _baton(tmp_path, "c-1", blocked_by=["a-1", "b-1"])

    assert _by_id(pg.assemble_plan_gate(tmp_path), "c-1")["planning_wave"] == 2


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({}, True),
        ({"status": "claimed"}, False),
        ({"deployment_state": "in_flight"}, False),
        ({"deployment_state": "shipped"}, False),
        ({"deployment_state": "awaiting_gate", "gate_dependency": "something"}, True),
    ],
)
def test_candidate_selection(tmp_path, fields, expected):
    _baton(tmp_path, "subject-1", **fields)
    report = pg.assemble_plan_gate(tmp_path, subject="subject-1")
    assert _by_id(report, "subject-1")["candidate"] is expected


@pytest.mark.parametrize(
    "link_field, plan_field, value",
    [
        ("origin_plan_id", "plan_id", "pln-x"),
        ("deliverable_id", "deliverable_id", "dlv-x"),
        ("sizing_object", "sizing_object", "state/sizings/szo-x.yaml"),
    ],
)
def test_each_plan_link_basis_resolves(tmp_path, link_field, plan_field, value):
    _plan(tmp_path, "linked", "approved", **{plan_field: value})
    _baton(tmp_path, "blocker-1", **{link_field: value})
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")
    assert dependent["blockers"][0]["disposition"] == pg.BLOCKER_PLAN_APPROVED


def test_a_sizing_object_links_across_the_path_vs_bare_id_spelling(tmp_path):
    _plan(tmp_path, "linked", "approved", sizing_object="szo-x")
    _baton(tmp_path, "blocker-1", sizing_object="state/sizings/szo-x.yaml")
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")
    assert dependent["blockers"][0]["disposition"] == pg.BLOCKER_PLAN_APPROVED


def test_the_most_advanced_of_several_linked_plans_wins(tmp_path):
    _plan(tmp_path, "early", "draft", deliverable_id="dlv-x")
    _plan(tmp_path, "later", "approved", deliverable_id="dlv-x")
    _baton(tmp_path, "blocker-1", deliverable_id="dlv-x")
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")
    assert dependent["blockers"][0]["disposition"] == pg.BLOCKER_PLAN_APPROVED


def test_a_shared_sizing_object_does_not_hand_a_blocker_a_siblings_plan(tmp_path):
    """The weak-basis fail-open, reported by example-cockpit-repo 2026-09-12.

    Four batons minted from one sizing all cite it, so a `sizing_object` hit set
    is every sibling's plan. Reducing it with `max(coded, approved)` gave a
    blocker whose own plan was `reviewed` the disposition of a sibling's
    `approved` plan, and the dependent's PLANNING gate opened on work the edge
    existed to hold. Declining the reduction is the closed answer."""
    _plan(tmp_path, "blockers-own", "reviewed", sizing_object="szo-wave")
    _plan(tmp_path, "siblings", "approved", sizing_object="szo-wave")
    _baton(tmp_path, "blocker-1", sizing_object="szo-wave")
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"], sizing_object="szo-wave")

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")
    blocker = dependent["blockers"][0]
    assert blocker["disposition"] == pg.BLOCKER_UNPLANNED
    assert blocker["plan"] is None
    assert dependent["planning_gate"]["open"] is False
    assert dependent["execution_gate"]["open"] is False


def test_an_ambiguous_weak_link_is_named_rather_than_read_as_no_plan(tmp_path):
    _plan(tmp_path, "one", "approved", sizing_object="szo-wave")
    _plan(tmp_path, "two", "draft", sizing_object="szo-wave")
    _baton(tmp_path, "subject-1", sizing_object="szo-wave")

    subject = _by_id(pg.assemble_plan_gate(tmp_path, subject="subject-1"), "subject-1")
    assert subject["plan"] is None
    ambiguity = subject["ambiguous_plan_link"]
    assert ambiguity["basis"] == "sizing_object"
    assert ambiguity["paths"] == ["docs/plans/one.md", "docs/plans/two.md"]
    assert "governing_plan" in ambiguity["repair"]


def test_a_strong_basis_still_reduces_a_multi_hit_set(tmp_path):
    _plan(tmp_path, "early", "draft", deliverable_id="dlv-x")
    _plan(tmp_path, "later", "approved", deliverable_id="dlv-x")
    _baton(tmp_path, "subject-1", deliverable_id="dlv-x")

    subject = _by_id(pg.assemble_plan_gate(tmp_path, subject="subject-1"), "subject-1")
    assert subject["plan"]["path"] == "docs/plans/later.md"
    assert subject["ambiguous_plan_link"] is None


def test_a_single_weak_basis_hit_still_links(tmp_path):
    _plan(tmp_path, "only", "approved", sizing_object="szo-wave")
    _baton(tmp_path, "blocker-1", sizing_object="szo-wave")
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")
    assert dependent["blockers"][0]["disposition"] == pg.BLOCKER_PLAN_APPROVED


def test_a_blocker_may_be_named_by_handoff_id_as_well_as_stub_id(tmp_path):
    _baton(tmp_path, "blocker-1", handoff_id="hnd-blocker-aaaaaa", deployment_state="shipped")
    _baton(tmp_path, "dependent-1", blocked_by=["hnd-blocker-aaaaaa"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")
    assert dependent["blockers"][0]["disposition"] == pg.BLOCKER_CODED


def test_an_archived_blocker_still_resolves(tmp_path):
    _write(
        tmp_path / "archive" / "handoffs" / "2026-01" / "old.md",
        "kind: roadmap-baton\ntitle: old\nstub_id: blocker-1\n"
        "status: open\ndeployment_state: shipped\nbaton_role: work",
    )
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    report = pg.assemble_plan_gate(tmp_path)
    dependent = _by_id(report, "dependent-1")

    assert dependent["blockers"][0]["disposition"] == pg.BLOCKER_CODED
    assert dependent["planning_gate"]["open"] is True


def test_a_live_baton_wins_an_id_collision_with_an_archived_namesake(tmp_path):
    _write(
        tmp_path / "archive" / "handoffs" / "2026-01" / "old.md",
        "kind: roadmap-baton\ntitle: old\nstub_id: blocker-1\n"
        "status: open\ndeployment_state: shipped\nbaton_role: work",
    )
    _baton(tmp_path, "blocker-1")
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")
    assert dependent["blockers"][0]["disposition"] == pg.BLOCKER_UNPLANNED


def _normalise(value):
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return [str(item) for item in value]
    return value


@pytest.mark.parametrize("subtree", [("state", "handoffs"), ("archive", "handoffs")])
def test_narrow_scan_agrees_with_the_general_parser(subtree):
    root = Path(__file__).resolve().parents[3]
    paths = pg._iter_record_paths(root, subtree, recursive=(subtree[0] == "archive"))
    if not paths:
        pytest.skip(f"{'/'.join(subtree)} is empty in this checkout")

    corrupted_by_the_general_parser = {"shipped_in"}

    disagreements = []
    for path in paths:
        narrow = pg._read_baton_fields(path)
        general = pg._read_frontmatter_head(path)
        for field in pg._BATON_FIELDS - corrupted_by_the_general_parser:
            got, want = _normalise(narrow.get(field)), _normalise(general.get(field))
            if got != want:
                disagreements.append((path.name, field, got, want))

    assert not disagreements, (
        f"{len(disagreements)} field(s) read differently by the narrow scanner "
        f"and the general parser; first 5: {disagreements[:5]}"
    )


#: RETURNED text — trailing blank body lines are always dropped — so this
_BLOCK_SCALAR_FORMS = ["|", "|-", "|+", ">", ">-", ">+"]


def test_narrow_scan_agrees_on_a_block_scalar_shape_per_scanned_key(tmp_path):
    preset = {"kind", "title", "stub_id", "status", "deployment_state", "baton_role"}
    lines = [
        "kind: roadmap-baton",
        "title: block-scalar-fixture",
        "stub_id: block-scalar-fixture",
        "status: open",
        "deployment_state: ready_to_fire",
        "baton_role: work",
    ]
    scanned = sorted(pg._BATON_FIELDS - preset)
    for index, key in enumerate(scanned):
        form = _BLOCK_SCALAR_FORMS[index % len(_BLOCK_SCALAR_FORMS)]
        lines.append(f"{key}: {form}")
        lines.append(f"  line one of {key}")
        lines.append(f"  line two of {key}")
    path = _write(tmp_path / "state" / "handoffs" / "block-scalar-fixture.md", "\n".join(lines))

    narrow = pg._read_baton_fields(path)
    general = pg._read_frontmatter_head(path)

    disagreements = [
        (key, _normalise(narrow.get(key)), _normalise(general.get(key)))
        for key in scanned
        if _normalise(narrow.get(key)) != _normalise(general.get(key))
    ]
    assert not disagreements, (
        f"{len(disagreements)} field(s) read differently on a block scalar; "
        f"first 5: {disagreements[:5]}"
    )


def test_a_trailing_comment_on_an_inline_list_does_not_swallow_the_edges(tmp_path):
    _baton(tmp_path, "a-1", deployment_state="shipped")
    _baton(tmp_path, "b-1", deployment_state="shipped")
    _write(
        tmp_path / "state" / "handoffs" / "dependent-1.md",
        "kind: roadmap-baton\ntitle: d\nstub_id: dependent-1\nstatus: open\n"
        "deployment_state: ready_to_fire\nbaton_role: work\n"
        "blocked_by: [a-1, b-1]  # b-1 added later: consumes a-1's primitive",
    )

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")
    assert dependent["blocked_by"] == ["a-1", "b-1"]
    assert dependent["planning_gate"]["open"] is True


def test_a_null_scalar_is_absence_not_the_string_null(tmp_path):
    _plan(tmp_path, "decoy", "approved", plan_id="null")
    _baton(tmp_path, "blocker-1", origin_plan_id="null")
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")
    assert dependent["blockers"][0]["disposition"] == pg.BLOCKER_UNPLANNED


def test_a_leading_html_comment_does_not_hide_the_frontmatter(tmp_path):
    path = tmp_path / "state" / "handoffs" / "commented.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!-- generated; do not hand-edit -->\n---\nkind: roadmap-baton\n"
        "title: c\nstub_id: commented-1\nstatus: open\n"
        "deployment_state: ready_to_fire\nbaton_role: work\n---\n\nbody\n",
        encoding="utf-8",
    )
    report = pg.assemble_plan_gate(tmp_path)
    assert _by_id(report, "commented-1")["candidate"] is True


def test_whole_tree_scan_holds_the_brightline():
    root = Path(__file__).resolve().parents[3]
    start = time.process_time()
    report = pg.assemble_plan_gate(root)
    elapsed_ms = (time.process_time() - start) * 1000

    assert report["scanned"]["batons"] > 0, "empty scan proves nothing about cost"
    assert report["index_unreadable"] is None, "the index read is inside this budget, not skipped"
    assert elapsed_ms < 500, (
        f"assemble_plan_gate took {elapsed_ms:.0f}ms process time over "
        f"{report['scanned']} — over the 500ms brightline. Cut the real cost; "
        f"do not raise this number."
    )


def _index_holds(monkeypatch, *stub_ids):
    paths = frozenset(f"state/handoffs/{s}.md" for s in stub_ids)
    monkeypatch.setattr(pg, "_tracked_paths", lambda root: (paths, None))


def test_an_untracked_baton_is_named_and_held_out_of_every_wave(tmp_path, monkeypatch):
    _baton(tmp_path, "settled")
    _baton(tmp_path, "minting")
    _index_holds(monkeypatch, "settled")

    report = pg.assemble_plan_gate(tmp_path)

    assert report["waves"] == [["settled"]]
    assert [row["id"] for row in report["untracked"]] == ["minting"]
    assert report["counts"]["untracked"] == 1
    assert report["counts"]["candidates"] == 1
    assert _by_id(report, "settled")["tracked"] is True


def test_a_dependent_of_an_untracked_baton_waits_rather_than_planning_past_it(
    tmp_path, monkeypatch
):
    _baton(tmp_path, "minting")
    _baton(tmp_path, "dependent", blocked_by=["minting"])
    _index_holds(monkeypatch, "dependent")

    report = pg.assemble_plan_gate(tmp_path)

    assert report["waves"] == []
    assert _by_id(report, "dependent")["planning_wave"] is None


def test_an_unknowable_index_withholds_nothing_and_says_why(tmp_path, monkeypatch):
    _baton(tmp_path, "solo")
    monkeypatch.setattr(pg, "_tracked_paths", lambda root: (None, "IndexParseError: split index"))

    report = pg.assemble_plan_gate(tmp_path)

    assert report["waves"] == [["solo"]]
    assert report["untracked"] == []
    assert report["index_unreadable"] == "IndexParseError: split index"
    assert _by_id(report, "solo")["tracked"] is None


def test_a_tree_with_no_git_index_withholds_nothing(tmp_path):
    _baton(tmp_path, "solo")

    report = pg.assemble_plan_gate(tmp_path)

    assert report["waves"] == [["solo"]]
    assert report["index_unreadable"] == "no git index at this worktree"


def _archived(root: Path, stub_id: str, state: str, month: str = "2026-08") -> Path:
    return _write(
        root / "archive" / "handoffs" / month / f"{stub_id}.md",
        f"kind: roadmap-baton\ntitle: {stub_id}\nstub_id: {stub_id}\n"
        f"status: open\ndeployment_state: {state}\nbaton_role: work",
    )


def test_a_live_copy_of_a_shipped_archived_baton_is_named_and_withheld(tmp_path):
    _baton(tmp_path, "zombie")
    _archived(tmp_path, "zombie", "shipped")
    _baton(tmp_path, "alive")

    report = pg.assemble_plan_gate(tmp_path)

    assert report["waves"] == [["alive"]]
    assert report["resurrected"] == [
        {
            "id": "zombie",
            "path": "state/handoffs/zombie.md",
            "archived_path": "archive/handoffs/2026-08/zombie.md",
            "archived_state": "shipped",
        }
    ]
    assert report["counts"]["resurrected"] == 1


def test_a_shared_basename_with_no_shared_id_is_not_a_resurrection(tmp_path):
    _baton(tmp_path, "live-one")
    archived = _archived(tmp_path, "someone-else", "shipped")
    archived.rename(archived.with_name("live-one.md"))

    report = pg.assemble_plan_gate(tmp_path)

    assert report["waves"] == [["live-one"]]
    assert report["resurrected"] == []


def test_a_non_terminal_archived_copy_says_nothing_about_which_is_stale(tmp_path):
    _baton(tmp_path, "twin")
    _archived(tmp_path, "twin", "ready_to_fire")

    report = pg.assemble_plan_gate(tmp_path)

    assert report["waves"] == [["twin"]]
    assert report["resurrected"] == []


def test_the_archive_is_not_scanned_when_nothing_needs_it(tmp_path):
    for index in range(3):
        _write(
            tmp_path / "archive" / "handoffs" / "2026-01" / f"old-{index}.md",
            f"kind: roadmap-baton\ntitle: o\nstub_id: archived-{index}\n"
            "status: open\ndeployment_state: shipped\nbaton_role: work",
        )
    _baton(tmp_path, "solo-1")

    report = pg.assemble_plan_gate(tmp_path)
    assert report["scanned"]["batons"] == 1, "archive was walked with no edge asking for it"


def test_a_replanned_pair_yields_exactly_one_candidate(tmp_path):
    _baton(tmp_path, "b-1")
    _baton(
        tmp_path,
        "hnd-replan-b-1-abc123",
        kind="spinoff",
        replan_of="b-1",
        forked_from="state/handoffs/b-1.md",
    )

    report = pg.assemble_plan_gate(tmp_path)

    candidates = {b["id"] for b in report["batons"] if b["candidate"]}
    assert candidates == {"hnd-replan-b-1-abc123"}, candidates
    assert report["replanned"] == [
        {"id": "b-1", "path": "state/handoffs/b-1.md", "title": "b-1"}
    ]
    assert report["counts"]["replanned"] == 1


def test_a_pre_fix_pair_with_no_replan_of_is_still_deduped_by_forked_from(tmp_path):
    _baton(tmp_path, "b-2")
    _baton(
        tmp_path,
        "hnd-replan-b-2-def456",
        kind="spinoff",
        forked_from="state/handoffs/b-2.md",
    )

    report = pg.assemble_plan_gate(tmp_path)

    candidates = {b["id"] for b in report["batons"] if b["candidate"]}
    assert candidates == {"hnd-replan-b-2-def456"}, candidates


def test_kind_plan_is_admitted_because_the_template_emits_it():
    assert pg.is_plan_record({"kind": "plan"}) is True
    assert pg.is_plan_record({}) is True
    assert pg.is_plan_record({"kind": "staff-eng-review"}) is False
    assert pg.is_plan_record({"plan": "docs/plans/x.md"}) is False
    assert pg.is_plan_record({"kind": "plan", "plan": "docs/plans/x.md"}) is False


def test_a_baton_naming_a_plan_no_basis_links_is_reported_not_silently_unplanned(tmp_path):
    plan_rel = _plan(tmp_path, "2026-09-09-governed", "approved")
    _baton(tmp_path, "unlinked-01", plan=plan_rel)

    row = _by_id(pg.assemble_plan_gate(tmp_path), "unlinked-01")

    assert row["plan"] is None
    assert row["needs_plan"] is True
    claim = row["unlinked_plan_claim"]
    assert claim is not None, "the broken link must be named, not inferred from needs_plan"
    assert claim["field"] == "plan"
    assert claim["path"] == plan_rel
    assert "governing_plan" in claim["repair"]


def test_the_claim_is_absent_once_a_declared_basis_links(tmp_path):
    plan_rel = _plan(tmp_path, "2026-09-09-governed", "approved")
    _baton(tmp_path, "linked-01", plan=plan_rel, governing_plan=plan_rel)

    row = _by_id(pg.assemble_plan_gate(tmp_path), "linked-01")

    assert row["plan"]["link_basis"] == "governing_plan"
    assert row["unlinked_plan_claim"] is None


def test_a_baton_with_no_plan_at_all_carries_a_null_claim(tmp_path):
    _baton(tmp_path, "bare-01")

    row = _by_id(pg.assemble_plan_gate(tmp_path), "bare-01")

    assert "unlinked_plan_claim" in row
    assert row["unlinked_plan_claim"] is None


def test_a_plan_path_that_does_not_exist_is_not_a_claim(tmp_path):
    _baton(tmp_path, "dangling-01", plan="docs/plans/2026-09-09-not-on-disk.md")

    row = _by_id(pg.assemble_plan_gate(tmp_path), "dangling-01")

    assert row["unlinked_plan_claim"] is None


def test_an_execution_parked_s_blocker_opens_its_dependents_planning_gate(tmp_path):
    """The S lane parks a spec and never takes the approval, so the plan stays `draft`.

    `needs_plan` already keys on `handoff_phase: execution` for exactly this reason —
    "keying on plan status ALONE would re-plan it on every later sweep, forever". The
    blocker ladder read only plan status, so a parked S ranked `plan-drafted`, below
    `_PLANNING_SATISFIED`, and held every DEPENDENT's planning gate shut permanently:
    the blocker will never be approved (the S lane does not approve) and is not yet
    coded (nothing has shipped). No sweep could open it.

    Measured 2026-09-10 on example-cockpit-repo: tmrg-07, tmrg-09 and tmrg-10 were all
    unplannable behind a tmrg-06 adjudicated ready to execute three days earlier.
    """
    plan = _plan(tmp_path, "the-parked-spec", "draft")
    _baton(
        tmp_path,
        "blocker-1",
        governing_plan=plan,
        handoff_phase="execution",
        execution_authorized_by="plan-blitz",
        execution_authorized_at="2026-09-10T00:00:00Z",
        execution_authorized_sha="deadbeef",
    )
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")

    assert dependent["planning_gate"]["open"] is True
    assert dependent["planning_gate"]["blocking"] == []


def test_an_execution_parked_s_blocker_still_holds_the_EXECUTION_gate_shut(tmp_path):
    """PLAN_APPROVED, never CODED — this is the whole two-gate rule.

    The EM adjudicated the blocker ready to RUN, which is at least as strong as a
    review-approved plan, but the work has not shipped. Ranking it CODED would open a
    dependent's EXECUTION gate against work that does not exist yet, which is the more
    expensive of the two errors.
    """
    plan = _plan(tmp_path, "the-parked-spec", "draft")
    _baton(
        tmp_path,
        "blocker-1",
        governing_plan=plan,
        handoff_phase="execution",
        execution_authorized_by="plan-blitz",
        execution_authorized_at="2026-09-10T00:00:00Z",
        execution_authorized_sha="deadbeef",
    )
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")

    assert dependent["execution_gate"]["open"] is False
    held = dependent["execution_gate"]["blocking"][0]
    assert held["disposition"] == pg.BLOCKER_PLAN_APPROVED
    assert "handoff_phase: execution" in held["reason"]


def test_an_unparked_draft_blocker_is_unchanged(tmp_path):
    plan = _plan(tmp_path, "just-a-draft", "draft")
    _baton(tmp_path, "blocker-1", governing_plan=plan)
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")

    assert dependent["planning_gate"]["open"] is False
    assert dependent["planning_gate"]["blocking"][0]["disposition"] == pg.BLOCKER_PLAN_DRAFTED


def test_the_phase_stamp_alone_does_not_open_a_dependents_planning_gate(tmp_path):
    plan = _plan(tmp_path, "a-draft", "draft")
    _baton(tmp_path, "blocker-1", governing_plan=plan, handoff_phase="execution")
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")

    assert dependent["planning_gate"]["open"] is False
    assert dependent["planning_gate"]["blocking"][0]["disposition"] == pg.BLOCKER_PLAN_DRAFTED


@pytest.mark.parametrize("shelved", ["deferred", "abandoned", "superseded"])
def test_a_parked_blocker_whose_plan_was_later_shelved_does_not_open_the_gate(tmp_path, shelved):
    """`PLAN_APPROVED_STATUSES` excludes these deliberately — a shelved plan publishes no
    decisions a dependent can build on. The park stamp is sticky (`authorize_execution`
    refuses a re-stamp and nothing clears it), so a plan shelved AFTER the park would
    otherwise hold the gate open permanently on a plan its own author withdrew.
    """
    plan = _plan(tmp_path, "a-shelved-plan", shelved)
    _baton(
        tmp_path,
        "blocker-1",
        governing_plan=plan,
        handoff_phase="execution",
        execution_authorized_by="plan-blitz",
        execution_authorized_at="2026-09-10T00:00:00Z",
        execution_authorized_sha="deadbeef",
    )
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")

    assert dependent["planning_gate"]["open"] is False


def test_a_parked_blocker_whose_plan_link_is_gone_says_so(tmp_path):
    _baton(
        tmp_path,
        "blocker-1",
        governing_plan="docs/plans/moved-or-deleted.md",
        handoff_phase="execution",
        execution_authorized_by="plan-blitz",
        execution_authorized_at="2026-09-10T00:00:00Z",
        execution_authorized_sha="deadbeef",
    )
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    report = pg.assemble_plan_gate(tmp_path)
    dependent = _by_id(report, "dependent-1")
    held = dependent["planning_gate"]["blocking"][0]

    assert held["disposition"] == pg.BLOCKER_UNPLANNED
    assert dependent["planning_gate"]["open"] is False
    assert "no sweep will plan it" in held["reason"]
    assert _by_id(report, "blocker-1")["needs_plan"] is False


def test_an_unparked_baton_with_no_plan_keeps_the_plain_wording(tmp_path):
    _baton(tmp_path, "blocker-1")
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    report = pg.assemble_plan_gate(tmp_path)
    held = _by_id(report, "dependent-1")["planning_gate"]["blocking"][0]

    assert held["reason"] == "no plan links to this baton"
    assert _by_id(report, "blocker-1")["needs_plan"] is True


def _sizing(root: Path, slug: str, route: str) -> str:
    """A sizing object carrying only the key the gate reads.

    The trailing enum comment is the SCAFFOLD'S OWN shape, not decoration: the
    generator emits `route: plan  # dispatch | spec-dispatch | ...`, and a
    reader that takes the comment as part of the value finds no route on any
    real sizing. Every fixture here carries it for that reason.
    """
    rel = f"state/sizings/{slug}.yaml"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "schema: sizing-object\n"
        f"route: {route}  # dispatch | spec-dispatch | shape | plan | roadmap\n"
        "estimate: XS\n",
        encoding="utf-8",
    )
    return rel


def test_an_xs_waiting_on_its_blockers_execution_is_not_a_planning_candidate(tmp_path):
    """example-retrieval-repo's cq-17, 2026-09-11 — dispatched and declined three times.

    Its blocker's plan is APPROVED, so the planning gate is open and it landed
    in `waves[0]`; its blocker is not CODED, so the execution gate is shut and
    nothing it could be dispatched to do can start. A dispatch-route baton has
    no plan to write, so a planning wave had nothing to give it.
    """
    plan_path = _plan(tmp_path, "blocker-plan", "approved")
    _baton(tmp_path, "blocker-1", governing_plan=plan_path)
    _baton(
        tmp_path,
        "cq-17",
        blocked_by=["blocker-1"],
        sizing_object=_sizing(tmp_path, "szo-cq-17", "dispatch"),
    )

    report = pg.assemble_plan_gate(tmp_path)
    assert all("cq-17" not in wave for wave in report["waves"])
    row = _by_id(pg.assemble_plan_gate(tmp_path, subject="cq-17"), "cq-17")
    assert row["candidate"] is False
    assert row["waiting_on_execution"] is True

    held = report["waiting_on_execution"]
    assert [r["baton"] for r in held] == ["cq-17"]
    assert held[0]["route"] == "dispatch"
    assert held[0]["blocking"] == ["blocker-1"]
    assert "waiting on a blocker to be CODED" in held[0]["reason"]
    assert report["counts"]["waiting_on_execution"] == 1


def test_a_spec_dispatch_baton_is_withheld_on_the_same_grounds(tmp_path):
    plan_path = _plan(tmp_path, "blocker-plan", "approved")
    _baton(tmp_path, "blocker-1", governing_plan=plan_path)
    _baton(
        tmp_path,
        "s-lane-1",
        blocked_by=["blocker-1"],
        sizing_object=_sizing(tmp_path, "szo-s-lane", "spec-dispatch"),
    )

    report = pg.assemble_plan_gate(tmp_path)
    assert [r["baton"] for r in report["waiting_on_execution"]] == ["s-lane-1"]
    assert report["waiting_on_execution"][0]["route"] == "spec-dispatch"


def test_a_plan_route_baton_in_the_same_gate_state_stays_a_candidate(tmp_path):
    plan_path = _plan(tmp_path, "blocker-plan", "approved")
    _baton(tmp_path, "blocker-1", governing_plan=plan_path)
    _baton(
        tmp_path,
        "m-lane-1",
        blocked_by=["blocker-1"],
        sizing_object=_sizing(tmp_path, "szo-m-lane", "plan"),
    )

    report = pg.assemble_plan_gate(tmp_path)
    row = _by_id(report, "m-lane-1")

    assert row["candidate"] is True
    assert row["waiting_on_execution"] is False
    assert row["execution_gate"]["open"] is False
    assert report["counts"]["waiting_on_execution"] == 0


def test_an_open_execution_gate_keeps_even_a_dispatch_baton_a_candidate(tmp_path):
    _baton(tmp_path, "blocker-1", deployment_state="shipped")
    _baton(
        tmp_path,
        "cq-17",
        blocked_by=["blocker-1"],
        sizing_object=_sizing(tmp_path, "szo-cq-17", "dispatch"),
    )

    report = pg.assemble_plan_gate(tmp_path)
    row = _by_id(report, "cq-17")

    assert row["execution_gate"]["open"] is True
    assert row["waiting_on_execution"] is False
    assert row["candidate"] is True


def test_an_unsized_baton_is_untouched_by_this_pass(tmp_path):
    """COVERAGE, not cost, is this predicate's limit — measured on claude-klabauter:
    242 candidates carry 5 sizing citations between them.

    A baton with no sizing object has no route to read, so it cannot be
    classified here and must not be guessed at. It is already reported
    `unsized`, and a sizing scout is what fixes that.
    """
    plan_path = _plan(tmp_path, "blocker-plan", "approved")
    _baton(tmp_path, "blocker-1", governing_plan=plan_path)
    _baton(tmp_path, "unsized-1", blocked_by=["blocker-1"])

    report = pg.assemble_plan_gate(tmp_path)
    row = _by_id(report, "unsized-1")

    assert row["sized"] is False
    assert row["waiting_on_execution"] is False
    assert row["candidate"] is True


def test_an_external_gate_on_a_baton_is_reported_as_inert(tmp_path):
    _baton(tmp_path, "inert-1", external_gate="[{owner_repo: example-game-repo}]")

    report = pg.assemble_plan_gate(tmp_path)

    assert report["counts"]["inert_fields"] == 1
    row = report["inert_fields"][0]
    assert row["baton"] == "inert-1"
    assert row["fields"] == ["external_gate"]


def test_an_inert_field_is_reported_but_never_acted_on(tmp_path):
    _baton(tmp_path, "inert-1", external_gate="[{owner_repo: example-game-repo}]")

    report = pg.assemble_plan_gate(tmp_path)

    assert report["counts"]["candidates"] == 1
    assert report["counts"]["held"] == 0


def test_a_held_baton_is_reported_with_its_reason_not_offered_as_a_candidate(tmp_path):
    _baton(
        tmp_path,
        "held-1",
        plan_blitz_hold_reason="PM took this personally; DR-2048 rules it GO but not yet",
        plan_blitz_hold_cite="DR-2048 §2",
        plan_blitz_hold_until="the structural-index gift ships",
    )

    report = pg.assemble_plan_gate(tmp_path)
    assert all("held-1" not in wave for wave in report["waves"])
    assert report["counts"]["held"] == 1

    row = report["held"][0]
    assert row["baton"] == "held-1"
    assert row["cite"] == "DR-2048 §2"
    assert row["until"] == "the structural-index gift ships"
    assert "PM took this personally" in row["reason"]

    subject = _by_id(pg.assemble_plan_gate(tmp_path, subject="held-1"), "held-1")
    assert subject["held"] is True
    assert subject["candidate"] is False


def test_a_hold_with_no_reason_is_not_honoured(tmp_path):
    """An unexplained suppression is the thing this REPLACES, not a lighter
    version of it. A cite or an until without a reason leaves the baton a
    candidate rather than removing it on nobody's stated authority."""
    _baton(tmp_path, "half-held-1", plan_blitz_hold_cite="DR-2048", plan_blitz_hold_until="later")

    report = pg.assemble_plan_gate(tmp_path)
    assert report["counts"]["held"] == 0
    assert _by_id(report, "half-held-1")["candidate"] is True


def test_a_hold_is_not_an_edge_and_does_not_hold_its_dependents(tmp_path):
    _baton(tmp_path, "held-1", plan_blitz_hold_reason="not yet")
    _baton(tmp_path, "dependent-1", blocked_by=["held-1"])

    report = pg.assemble_plan_gate(tmp_path)
    dependent = _by_id(report, "dependent-1")

    # The dependent is held by `held-1` being UNPLANNED, which it genuinely is —
    assert dependent["blocked_by"] == ["held-1"]
    assert [b["blocker"] for b in dependent["blockers"]] == ["held-1"]
    assert report["counts"]["held"] == 1


def test_a_baton_two_rules_would_withdraw_is_reported_under_the_first(tmp_path, monkeypatch):
    """The precedence `_WITHDRAWALS` declares, which nothing pinned while it
    was six in-place passes: each pass skipped what an earlier one had already
    withdrawn, so "which reason the reader is told" was decided by the order
    the passes happened to be written in. First match wins, top to bottom —
    untracked before held — and a baton is named ONCE, never in two buckets."""
    _baton(tmp_path, "settled")
    _baton(tmp_path, "both", plan_blitz_hold_reason="PM said not yet")
    _index_holds(monkeypatch, "settled")

    report = pg.assemble_plan_gate(tmp_path)

    assert [row["id"] for row in report["untracked"]] == ["both"]
    assert report["held"] == []
    assert _by_id(pg.assemble_plan_gate(tmp_path, subject="both"), "both")["candidate"] is False


def test_an_ordinary_baton_carries_held_false(tmp_path):
    _baton(tmp_path, "plain-1")

    report = pg.assemble_plan_gate(tmp_path)
    assert _by_id(report, "plain-1")["held"] is False
    assert report["held"] == []


def test_a_block_scalar_hold_reason_renders_as_its_text_not_its_indicator(tmp_path):
    """klabauter#49 — the narrow scanner used to read a block-scalar value's
    INDICATOR (`>-`, `|`, …) as the value itself, so a `plan_blitz_hold_reason`
    authored as a folded block scalar reported the literal two-character
    string ">-" as the reason a held baton's own text never was.
    """
    _write(
        tmp_path / "state" / "handoffs" / "held-1.md",
        "kind: roadmap-baton\ntitle: held-1\nstub_id: held-1\nstatus: open\n"
        "deployment_state: ready_to_fire\nbaton_role: work\n"
        "plan_blitz_hold_reason: >-\n"
        "  PM took this personally; DR-2048\n"
        "  rules it GO but not yet\n"
        "plan_blitz_hold_cite: DR-2048 §2",
    )

    report = pg.assemble_plan_gate(tmp_path)
    assert report["counts"]["held"] == 1

    row = report["held"][0]
    assert row["baton"] == "held-1"
    assert ">-" not in row["reason"]
    assert "PM took this personally; DR-2048" in row["reason"]
    assert "rules it GO but not yet" in row["reason"]

    fields = pg._read_baton_fields(tmp_path / "state" / "handoffs" / "held-1.md")
    assert fields["plan_blitz_hold_reason"] == (
        pg._read_frontmatter_head(tmp_path / "state" / "handoffs" / "held-1.md")[
            "plan_blitz_hold_reason"
        ]
    )


def _xl_sizing(root: Path, slug: str, *, xl_exit: str) -> str:
    rel = f"state/sizings/{slug}.yaml"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "schema: sizing-object\n"
        "route: pm-decision  # dispatch | spec-dispatch | shape | plan | roadmap\n"
        "detents: []\n"
        "fork: null\n"
        f"xl_exit: {xl_exit}\n"
        "status: routed\n",
        encoding="utf-8",
    )
    return rel


def test_an_accepted_xl_exit_resolves_pm_decision_to_plan(tmp_path):
    """example-market-data-repo, 2026-09-11: a sizing resolved on 2026-08-05 —
    appetite raised, `xl_exit: accept_multi_session` assented, the EM's split
    DECLINED — still routed pm-decision in every wave since, so each wave
    re-asked a question answered in the file it had just read. The PM called
    that class of escalation hedging."""
    rel = _xl_sizing(tmp_path, "resolved", xl_exit="accept_multi_session")
    assert pg._sizing_route(tmp_path, [rel]) == "plan"


@pytest.mark.parametrize("exit_value", ["split", "shape", "roadmap"])
def test_the_other_xl_exits_stay_at_the_gate(tmp_path, exit_value):
    rel = _xl_sizing(tmp_path, f"exit-{exit_value}", xl_exit=exit_value)
    assert pg._sizing_route(tmp_path, [rel]) == "pm-decision"


def test_an_unset_xl_exit_is_not_an_acceptance(tmp_path):
    rel = _xl_sizing(tmp_path, "unset", xl_exit="null")
    assert pg._sizing_route(tmp_path, [rel]) == "pm-decision"


def test_a_nested_route_key_does_not_shadow_the_top_level_one(tmp_path):
    rel = "state/sizings/nested.yaml"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "schema: sizing-object\n"
        "em_analysis:\n"
        "  route: dispatch\n"
        "route: plan  # dispatch | spec-dispatch | shape | plan | roadmap\n",
        encoding="utf-8",
    )
    assert pg._sizing_route(tmp_path, [rel]) == "plan"


def test_an_owner_declared_gate_is_reported_without_withholding(tmp_path):
    _baton(
        tmp_path,
        "gated-1",
        deployment_state="awaiting_gate",
        gate_dependency="reddit api credential, requested 2026-09-04",
    )

    report = pg.assemble_plan_gate(tmp_path)

    row = next(r for r in report["gated"] if r["baton"] == "gated-1")
    assert "reddit api credential" in row["dependency"]
    assert row["candidate"] is True
    assert _by_id(report, "gated-1")["gated"] is True
    assert report["counts"]["gated"] == 1


def test_a_gate_with_no_dependency_is_not_reported(tmp_path):
    _baton(tmp_path, "gated-2", deployment_state="awaiting_gate")

    report = pg.assemble_plan_gate(tmp_path)

    assert report["gated"] == []
    assert _by_id(report, "gated-2")["gated"] is False


def test_an_ungated_baton_carries_gated_false(tmp_path):
    _baton(tmp_path, "plain-2")

    report = pg.assemble_plan_gate(tmp_path)

    assert _by_id(report, "plain-2")["gated"] is False
    assert report["counts"]["gated"] == 0


def test_a_comment_after_a_quoted_scalar_is_still_a_comment():
    assert pg._unquote('""  # both gates discharged 2026-08-29') == ""
    assert pg._unquote('"a real gate"  # deprecated') == "a real gate"
    assert pg._unquote("'it''s gated'  # note") == "it's gated"
    assert pg._unquote('"a # inside the quotes"') == "a # inside the quotes"
    assert pg._unquote('"unterminated  # not ours to truncate') == (
        '"unterminated  # not ours to truncate'
    )


def _shared_id_baton(root: Path, filename: str, stub_id: str, **fields) -> Path:
    """A baton at an arbitrary FILENAME carrying a caller-chosen `stub_id`.

    `_baton` names the file after the stub, so it cannot express the case under
    test here — two live records sharing one id, which is exactly what a
    succession chain and a roadmap stub's fan-out both produce by design.
    """
    lines = [
        "kind: roadmap-baton",
        f"title: {fields.pop('title', filename)}",
        f"stub_id: {stub_id}",
        f"status: {fields.pop('status', 'open')}",
        f"deployment_state: {fields.pop('deployment_state', 'ready_to_fire')}",
        "baton_role: work",
    ]
    for key, value in fields.items():
        lines.append(f"{key}: {value}")
    return _write(root / "state" / "handoffs" / f"{filename}.md", "\n".join(lines))


def test_shared_wave_slot_names_every_baton_collapsed_into_one_id(tmp_path):
    _shared_id_baton(tmp_path, "older-record", "shared-1")
    _shared_id_baton(tmp_path, "newer-record", "shared-1")
    _shared_id_baton(tmp_path, "solo-record", "solo-1")

    report = pg.assemble_plan_gate(tmp_path)

    assert report["counts"]["shared_wave_slot"] == 1
    (row,) = report["shared_wave_slot"]
    assert set(row) == {"id", "wave", "members"}
    assert row["id"] == "shared-1"
    assert row["wave"] == _by_id(report, "shared-1")["planning_wave"]
    assert row["members"] == [
        {"path": "state/handoffs/newer-record.md", "title": "newer-record"},
        {"path": "state/handoffs/older-record.md", "title": "older-record"},
    ]

    assert report["counts"]["candidates"] == 3
    assert sum(len(wave) for wave in report["waves"]) == 2


def test_shared_wave_slot_is_empty_when_every_candidate_id_is_unique(tmp_path):
    _shared_id_baton(tmp_path, "first-record", "unique-1")
    _shared_id_baton(tmp_path, "second-record", "unique-2")

    report = pg.assemble_plan_gate(tmp_path)

    assert report["shared_wave_slot"] == []
    assert report["counts"]["shared_wave_slot"] == 0
    assert report["counts"]["candidates"] == 2
