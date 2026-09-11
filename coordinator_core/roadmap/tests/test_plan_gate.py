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


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# The central claim: one edge, two gates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "plan_status, planning_open, execution_open, disposition",
    [
        # Pre-ratification: the blocker's decisions are not published yet, so
        # neither gate opens. This is the row that keeps the new planning gate
        # from collapsing into "any plan will do".
        ("draft", False, False, pg.BLOCKER_PLAN_DRAFTED),
        ("reviewed", False, False, pg.BLOCKER_PLAN_DRAFTED),
        # The seam. `approved` publishes the blocker's decisions; a dependent
        # can plan against them, and cannot yet call the code.
        ("approved", True, False, pg.BLOCKER_PLAN_APPROVED),
        ("executing", True, False, pg.BLOCKER_PLAN_APPROVED),
        # Landed: the code exists, so both gates open.
        ("landed", True, True, pg.BLOCKER_CODED),
        ("implemented", True, True, pg.BLOCKER_CODED),
        # Shelved: a deferred plan publishes nothing a dependent can build on,
        # so it must NOT read as approved just because it passed through review.
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
    """`deployment_state` is read directly, with NO plan on disk — a baton that
    reached a terminal state has satisfied its dependents whether or not anyone
    ever wrote it a plan. Batons predating the plan convention are the common
    case here, and requiring a plan link would report every one of them as
    holding its dependents shut forever."""
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


# ---------------------------------------------------------------------------
# Fail-closed directions
# ---------------------------------------------------------------------------


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
    """`docs/plans/` holds review sidecars beside the plans they review. A
    sidecar admitted as a plan would answer "is this baton's plan approved?"
    with a status that is not the plan's."""
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
    """An `in_flight` blocker is not a candidate, so no planning wave will
    clear it. Its dependent must get wave `None` — assigning it a wave would
    invite a caller to fire against a gate that this pass cannot open."""
    _baton(tmp_path, "blocker-1", deployment_state="in_flight")
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")

    assert dependent["planning_wave"] is None
    assert dependent["planning_gate"]["open"] is False


def test_unschedulable_names_its_batons_and_what_holds_each(tmp_path):
    """`counts.unschedulable` says how many; the report must also say WHICH and
    WHY. A count with no subjects cannot be acted on and cannot be reconciled
    against the trail -- a driver reading only the number cannot tell a baton
    that is being deliberately held from one that quietly vanished."""
    _baton(tmp_path, "blocker-1", deployment_state="in_flight")
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    report = pg.assemble_plan_gate(tmp_path)

    assert report["counts"]["unschedulable"] == len(report["unschedulable"])
    row = next(r for r in report["unschedulable"] if r["id"] == "dependent-1")
    assert row["held_by"] == ["blocker-1"]


def test_a_blocker_naming_no_baton_is_reported_as_what_holds_the_row(tmp_path):
    """The encoding for "something outside this repo holds this" -- a PM
    decision, a licensing call -- is a `blocked_by` entry that resolves to no
    baton. The row stays a visible candidate and never enters a wave, and the
    report names the blocker so the hold is legible rather than mysterious."""
    _baton(tmp_path, "pm-held-1", blocked_by=["pm-decision:seats-vs-reauth"])

    report = pg.assemble_plan_gate(tmp_path)

    row = next(r for r in report["unschedulable"] if r["id"] == "pm-held-1")
    assert row["held_by"] == ["pm-decision:seats-vs-reauth"]
    assert _by_id(report, "pm-held-1")["candidate"] is True
    assert all("pm-held-1" not in wave for wave in report["waves"])


def test_a_transitively_held_row_names_the_blocker_that_holds_it(tmp_path):
    """A blocker can be a perfectly good candidate and still hold its dependent
    out of every wave, by being unscheduled itself. Reporting only blockers that
    resolve to no baton returned `held_by: []` for a row that genuinely could
    not be scheduled -- the same count-with-no-subject defect, one level down.
    An empty `held_by` must mean nothing holds the row, never that something
    does and the report cannot say what."""
    _baton(tmp_path, "outside-1", deployment_state="in_flight")
    _baton(tmp_path, "middle-1", blocked_by=["outside-1"])
    _baton(tmp_path, "far-1", blocked_by=["middle-1"])

    report = pg.assemble_plan_gate(tmp_path)

    far = next(r for r in report["unschedulable"] if r["id"] == "far-1")
    assert far["held_by"] == ["middle-1"]
    assert not any(r["held_by"] == [] for r in report["unschedulable"])


# ---------------------------------------------------------------------------
# Wave assignment
# ---------------------------------------------------------------------------


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
    # Nothing has landed, so every execution gate below the root stays shut.
    assert _by_id(report, "b-1")["execution_gate"]["open"] is False


def test_an_already_approved_blocker_collapses_its_dependent_to_wave_zero(tmp_path):
    """A blocker whose plan already cleared review is not work this blitz has to
    do, so its dependent starts at wave 0 rather than queueing behind it — and
    the blocker itself is NOT in the wave, because it needs no plan.

    Both halves matter. On DoE-claude's first live run the second half was
    missing and 18 batons carrying approved plans sat in wave 0, where a blitz
    would have re-planned every one of them."""
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
    """Targeted mode: an EM or the PM picks the batons. A non-target stays a
    fully-resolved BLOCKER — narrowing the question must never narrow what the
    answer is computed from, or a gate reports open because the thing holding it
    shut was filtered out."""
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


# ---------------------------------------------------------------------------
# Candidate selection and plan linking
# ---------------------------------------------------------------------------


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
    """A baton citing `state/sizings/szo-x.yaml` and a plan citing `szo-x` are
    citing the same object. Joining on the raw strings misses the pair, and the
    miss reads as 'this baton has no plan' — which would send an already-planned
    baton back through a planning wave."""
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


def test_a_blocker_may_be_named_by_handoff_id_as_well_as_stub_id(tmp_path):
    """handoff.schema.json admits both spellings in `blocked_by`. Indexing only
    `stub_id` reports every handoff_id edge as `unresolved`."""
    _baton(tmp_path, "blocker-1", handoff_id="hnd-blocker-aaaaaa", deployment_state="shipped")
    _baton(tmp_path, "dependent-1", blocked_by=["hnd-blocker-aaaaaa"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")
    assert dependent["blockers"][0]["disposition"] == pg.BLOCKER_CODED


def test_an_archived_blocker_still_resolves(tmp_path):
    """The archive is scanned lazily, so this is the case that proves the
    laziness is a skipped-empty-scan and not a dropped edge."""
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
    _baton(tmp_path, "blocker-1")  # live, unplanned
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")
    assert dependent["blockers"][0]["disposition"] == pg.BLOCKER_UNPLANNED


# ---------------------------------------------------------------------------
# Narrow-scanner parity
# ---------------------------------------------------------------------------


def _normalise(value):
    """Collapse the two readers' type conventions so only real disagreements show."""
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
    """The narrow scanner is a performance decision (~7x) with a correctness
    surface, so it is held to the general parser's reading of the SAME bytes,
    field by field, over this repo's whole live corpus.

    Read against a corpus rather than a fixture on purpose: a hand-built fixture
    only proves the scanner handles the shapes its author thought of, and every
    divergence found while writing it (a trailing `#` comment on an inline
    `blocked_by` list; a `null` scalar read as the four-letter string) came from
    a real record nobody would have invented.
    """
    root = Path(__file__).resolve().parents[3]
    paths = pg._iter_record_paths(root, subtree, recursive=(subtree[0] == "archive"))
    if not paths:
        pytest.skip(f"{'/'.join(subtree)} is empty in this checkout")

    # `shipped_in` is excluded, and the exclusion is the finding rather than a
    # concession: YAML reads a leading-zero SHA like `0983062` as a NUMBER, so the
    # general parser returns 983062.0 — a corrupted hash with its leading zero gone
    # and a decimal point added. The narrow scanner returns the string, which is
    # correct. Comparing them here would assert the wrong reading. The general
    # parser's coercion is a real defect in `coordinator_core.dag._parse_frontmatter`,
    # reported rather than fixed from here: it is a shared primitive with callers
    # this workstream has not surveyed.
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


def test_a_trailing_comment_on_an_inline_list_does_not_swallow_the_edges(tmp_path):
    """Regression: `blocked_by: [a-1, b-1]  # why` parsed as a single scalar id,
    so both real edges vanished and the gate reported `unresolved`."""
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


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_whole_tree_scan_holds_the_brightline():
    """500ms end-to-end, process time, over this repo's real corpus — the
    brightline (DR-344), not a suspension bar.

    Process time rather than wall clock: wall clock measures peer load on a box
    running ~50 sessions, and calibrating to it would let a busy box excuse a
    slow op or a quiet one hide a fast-growing one.
    """
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


# ---------------------------------------------------------------------------
# A baton still being minted is not a candidate
# ---------------------------------------------------------------------------


def _index_holds(monkeypatch, *stub_ids):
    paths = frozenset(f"state/handoffs/{s}.md" for s in stub_ids)
    monkeypatch.setattr(pg, "_tracked_paths", lambda root: (paths, None))


def test_an_untracked_baton_is_named_and_held_out_of_every_wave(tmp_path, monkeypatch):
    """example-cockpit-repo, 2026-09-11: four handoffs minted `pickup_ready` before
    their commit reached wave 0 while their author was still writing them."""
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
    """An unborn or non-git tree has no membership to read — every other test in
    this file runs in one, which is what pins the fail-open direction."""
    _baton(tmp_path, "solo")

    report = pg.assemble_plan_gate(tmp_path)

    assert report["waves"] == [["solo"]]
    assert report["index_unreadable"] == "no git index at this worktree"


# ---------------------------------------------------------------------------
# A live copy of an archived, closed baton is not a candidate
# ---------------------------------------------------------------------------


def _archived(root: Path, stub_id: str, state: str, month: str = "2026-08") -> Path:
    return _write(
        root / "archive" / "handoffs" / month / f"{stub_id}.md",
        f"kind: roadmap-baton\ntitle: {stub_id}\nstub_id: {stub_id}\n"
        f"status: open\ndeployment_state: {state}\nbaton_role: work",
    )


def test_a_live_copy_of_a_shipped_archived_baton_is_named_and_withheld(tmp_path):
    """example-store-repo, 2026-09-11: a merge that took HEAD over a closure put the
    pre-close copies back in state/handoffs, and three reached wave 0."""
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
    """The laziness is load-bearing: claude-klabauter's archive holds ~3x its live tree,
    and parsing it cost more than the rest of this module put together."""
    for index in range(3):
        _write(
            tmp_path / "archive" / "handoffs" / "2026-01" / f"old-{index}.md",
            f"kind: roadmap-baton\ntitle: o\nstub_id: archived-{index}\n"
            "status: open\ndeployment_state: shipped\nbaton_role: work",
        )
    _baton(tmp_path, "solo-1")

    report = pg.assemble_plan_gate(tmp_path)
    assert report["scanned"]["batons"] == 1, "archive was walked with no edge asking for it"


def test_kind_plan_is_admitted_because_the_template_emits_it():
    """`kind: plan` is a PLAN, not a sidecar — the template emits it.

    `is_plan_record`'s discriminator once read a bare `kind:` as sidecar-ness, on the
    premise that plan.schema.json declares no `kind`. True of the schema, false of the
    corpus: DoE's `coordinator/templates/plans/plan.md.tmpl` emits `kind: plan`, so 41 of
    283 records carried it and every one was indexed as a sidecar. That is the second
    failure `is_plan_record`'s own docstring names — an already-planned baton reported as
    unplanned, fed back into a planning wave that writes a second plan for work that has
    one — and it fired silently, because the query answers, and answers empty.
    """
    assert pg.is_plan_record({"kind": "plan"}) is True
    assert pg.is_plan_record({}) is True
    # A sidecar is still a sidecar, by either discriminator.
    assert pg.is_plan_record({"kind": "staff-eng-review"}) is False
    assert pg.is_plan_record({"plan": "docs/plans/x.md"}) is False
    # A back-pointer still wins: a record that points AT a plan is not that plan.
    assert pg.is_plan_record({"kind": "plan", "plan": "docs/plans/x.md"}) is False


# ---------------------------------------------------------------------------
# The unlinked plan claim
# ---------------------------------------------------------------------------


def test_a_baton_naming_a_plan_no_basis_links_is_reported_not_silently_unplanned(tmp_path):
    """`needs_plan: true` means a blitz has work to do; a broken link means it does not.

    `plan:` is undeclared in handoff.schema.json, so records carry it freely while `link_plans`
    reads `governing_plan`. A baton naming a real plan there resolves to no link, reports
    `needs_plan: true`, and is re-planned by every later sweep — beside an approved plan for the
    same work, with any execution record attaching to nothing. Measured once in example-retrieval-repo
    against a PM-authorized plan, where it had been true for weeks and announced nothing.
    """
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
    """The repair the claim names actually clears it — otherwise the field is a permanent
    complaint rather than a routable finding."""
    plan_rel = _plan(tmp_path, "2026-09-09-governed", "approved")
    _baton(tmp_path, "linked-01", plan=plan_rel, governing_plan=plan_rel)

    row = _by_id(pg.assemble_plan_gate(tmp_path), "linked-01")

    assert row["plan"]["link_basis"] == "governing_plan"
    assert row["unlinked_plan_claim"] is None


def test_a_baton_with_no_plan_at_all_carries_a_null_claim(tmp_path):
    """Present-as-null, never absent. An omitted key and "no claim" would be one value, and this
    field exists to make a silent case loud."""
    _baton(tmp_path, "bare-01")

    row = _by_id(pg.assemble_plan_gate(tmp_path), "bare-01")

    assert "unlinked_plan_claim" in row
    assert row["unlinked_plan_claim"] is None


def test_a_plan_path_that_does_not_exist_is_not_a_claim(tmp_path):
    """The claim is that a REAL plan went unlinked. A dangling path is a different defect with a
    different repair — fix the path, not the link basis — and reporting it here would send an
    author to write `governing_plan:` pointing at a file that is not there."""
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
    # The carve-out is keyed on the execution stamp, not on being a draft: an ordinary
    # drafted plan with no authorization must still hold the planning gate shut.
    plan = _plan(tmp_path, "just-a-draft", "draft")
    _baton(tmp_path, "blocker-1", governing_plan=plan)
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    dependent = _by_id(pg.assemble_plan_gate(tmp_path), "dependent-1")

    assert dependent["planning_gate"]["open"] is False
    assert dependent["planning_gate"]["blocking"][0]["disposition"] == pg.BLOCKER_PLAN_DRAFTED


def test_the_phase_stamp_alone_does_not_open_a_dependents_planning_gate(tmp_path):
    """`handoff_phase: execution` is the fleet-wide plan->execute seam, not an S-park.

    `scan_batons` reads EVERY record under state/handoffs/ with no `kind` filter, and
    ordinary session handoffs carry that stamp — 19 of them in claude-klabauter's own corpus, none
    an S-park. Keying the carve-out on the phase alone would open a dependent's planning
    gate on a record that never went through `blitz_land.authorize_execution`, while the
    reason line asserted a park nothing had checked.
    """
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
    """`unplanned` normally means a sweep will plan it. Here it never will.

    `needs_plan` keys on the park alone, with no plan dependency, so an execution-parked
    baton whose `governing_plan` was moved or deleted reports `needs_plan: False` — no
    sweep picks it up — while this lane reports its dependents as ordinarily blocked. The
    dependents are jammed permanently and the wording gives an operator no way to tell
    that from work still queued.

    The disposition and both gates are unchanged: a blocker with no plan publishes
    nothing to build on, and opening a gate here would be the more expensive error. Only
    the diagnosis changes, because the repair is a human restoring the link.
    """
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
    # And the blocker really is invisible to sweeps, which is what makes it permanent.
    assert _by_id(report, "blocker-1")["needs_plan"] is False


def test_an_unparked_baton_with_no_plan_keeps_the_plain_wording(tmp_path):
    # Ordinary queued work must not inherit the parked diagnosis — a sweep WILL plan this.
    _baton(tmp_path, "blocker-1")
    _baton(tmp_path, "dependent-1", blocked_by=["blocker-1"])

    report = pg.assemble_plan_gate(tmp_path)
    held = _by_id(report, "dependent-1")["planning_gate"]["blocking"][0]

    assert held["reason"] == "no plan links to this baton"
    assert _by_id(report, "blocker-1")["needs_plan"] is True


# ---------------------------------------------------------------------------
# waiting_on_execution — a baton with no planning content
# ---------------------------------------------------------------------------


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
    # Withheld, so it is out of `batons` like every other non-candidate. A
    # driver asking "why is cq-17 not in the wave?" names it as the `subject`,
    # which narrows the REPORT without narrowing the gates — and that is the
    # one read where the per-row flag is reachable.
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
    """The S lane parks a light spec, not a plan — same absence of planning
    content as XS, so the same verdict."""
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
    """The negative verdict, and the reason this keys on ROUTE rather than on
    the cheaper "no plan and a shut execution gate".

    An M or L waiting on a blocker's execution is exactly the baton a wave
    SHOULD plan now — the planning is the useful work available while the
    blocker is coded. Withholding it would trade cq-17's silence for a worse
    one.
    """
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
    """Route alone never withholds. A dispatch baton whose blockers are all
    CODED is dispatchable NOW, and the whole point of the wave is to reach it."""
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


# ---------------------------------------------------------------------------
# held — a baton somebody decided must not fire
# ---------------------------------------------------------------------------


def test_an_external_gate_on_a_baton_is_reported_as_inert(tmp_path):
    """example-game-workbench-repo-b8, 2026-09-11: two independent plan-blitz wave EMs
    recommended writing `external_gate` onto a BATON to stop a host-gated one
    recycling. It is a plan spine-row field; candidacy never consults it, so the
    write is well-formed frontmatter that changes nothing — and the next wave's EM
    reads the field and concludes the question is settled, which is worse than the
    open defect. `_scan_fields`' "absent from the set reads as absent" is
    indistinguishable, from the author's side, from having written the right thing.
    """
    _baton(tmp_path, "inert-1", external_gate="[{owner_repo: example-game-repo}]")

    report = pg.assemble_plan_gate(tmp_path)

    assert report["counts"]["inert_fields"] == 1
    row = report["inert_fields"][0]
    assert row["baton"] == "inert-1"
    assert row["fields"] == ["external_gate"]


def test_an_inert_field_is_reported_but_never_acted_on(tmp_path):
    """Suppressing the baton here would give the mistaken write exactly the effect
    its author wanted, which makes the wrong spelling work and buries the defect
    for good. The real mechanism is `blocked_by: [host:...]`."""
    _baton(tmp_path, "inert-1", external_gate="[{owner_repo: example-game-repo}]")

    report = pg.assemble_plan_gate(tmp_path)

    assert report["counts"]["candidates"] == 1
    assert report["counts"]["held"] == 0


def test_a_held_baton_is_reported_with_its_reason_not_offered_as_a_candidate(tmp_path):
    """example-retrieval-repo, 2026-09-11 — the friction that cost them the most.

    5 of 8 wave-0 candidates had a recorded reason not to fire, and 4 had been
    re-fired across five runs since 09-06: the reason lived in a DR or a run
    report, nothing joined it to the gate, and every session re-derived it.
    """
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
    """DR-2048's refusal, pinned.

    A hold suppresses ONE baton's candidacy. Spelling it as a `blocked_by` edge
    would also shut every dependent's gates — which is why DR-2048 refuses the
    edge, and why this must not quietly reintroduce one.
    """
    _baton(tmp_path, "held-1", plan_blitz_hold_reason="not yet")
    _baton(tmp_path, "dependent-1", blocked_by=["held-1"])

    report = pg.assemble_plan_gate(tmp_path)
    dependent = _by_id(report, "dependent-1")

    # The dependent is held by `held-1` being UNPLANNED, which it genuinely is —
    # not by the hold, which contributes no edge of its own.
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
    """The negative verdict: the flag is present-as-False, never absent, so a
    reader never has to tell "not held" from "this gate is too old to say"."""
    _baton(tmp_path, "plain-1")

    report = pg.assemble_plan_gate(tmp_path)
    assert _by_id(report, "plain-1")["held"] is False
    assert report["held"] == []


# ---------------------------------------------------------------------------
# pm-decision is a question, and xl_exit is where the answer lands
# ---------------------------------------------------------------------------


def _xl_sizing(root: Path, slug: str, *, xl_exit: str) -> str:
    """An XL sizing at route pm-decision, with the PM's exit as written."""
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
    """Only accepting one coherent multi-session job leaves a plan to write.
    Split and shape send the baton back for re-scoping; roadmap sends it to an
    initiative. None of those is this wave planning this plan."""
    rel = _xl_sizing(tmp_path, f"exit-{exit_value}", xl_exit=exit_value)
    assert pg._sizing_route(tmp_path, [rel]) == "pm-decision"


def test_an_unset_xl_exit_is_not_an_acceptance(tmp_path):
    """`null` is a legitimate open state and NEVER means the multi-session exit
    was accepted by default — the schema says so in its own words, and a gate
    that read it as consent would decide the PM's question for them."""
    rel = _xl_sizing(tmp_path, "unset", xl_exit="null")
    assert pg._sizing_route(tmp_path, [rel]) == "pm-decision"


def test_a_nested_route_key_does_not_shadow_the_top_level_one(tmp_path):
    """`route:` under some other block is that block's key, not the sizing's."""
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


# ---------------------------------------------------------------------------
# An owner's declared gate is not an edge, and used to be invisible
# ---------------------------------------------------------------------------


def test_an_owner_declared_gate_is_reported_without_withholding(tmp_path):
    """example-cockpit-repo, 2026-09-11. The owner stamped awaiting_gate,
    pickup_ready false, and a gate_dependency naming the credential. All three
    were readable and the gate said nothing about any of them, because both
    computed gates resolve `blocked_by` edges and a declaration is not one.

    Reported, not withheld: a gate on FIRING is not a gate on planning, which
    `test_candidate_selection` pins independently. A baton that must not be
    planned at all carries plan_blitz_hold_reason instead."""
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
    """awaiting_gate alone says a gate exists but not what it is, and a row
    naming no dependency is a row a reader cannot act on."""
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
    """Two live records carry `gate_dependency: ""  # both gates discharged ...`.
    The quote-aware branch returned the whole line for those, so the narrow
    scanner read a discharged gate as a live one and disagreed with the general
    parser — the one thing it may never do."""
    assert pg._unquote('""  # both gates discharged 2026-08-29') == ""
    assert pg._unquote('"a real gate"  # deprecated') == "a real gate"
    assert pg._unquote("'it''s gated'  # note") == "it's gated"
    assert pg._unquote('"a # inside the quotes"') == "a # inside the quotes"
    assert pg._unquote('"unterminated  # not ours to truncate') == (
        '"unterminated  # not ours to truncate'
    )
