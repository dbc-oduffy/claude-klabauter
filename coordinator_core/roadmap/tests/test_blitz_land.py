"""
coordinator_core/roadmap/tests/test_blitz_land.py — landing a wave without an EM.

Subject: `coordinator_core.roadmap.blitz_land`, which executes a wave result's
verdicts so the loop between waves needs no operator.

The claim under test: a wave that ran unattended must also LAND unattended, and
the one step that must never be left to a human is the plan→baton link — because
an unlinked approval is a silent no-op that reads as success.

Zero spawns; every case builds its corpus in `tmp_path`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.roadmap import blitz_land as bl
from coordinator_core.roadmap import plan_gate as pg


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir(exist_ok=True)
    return tmp_path


def _baton(root: Path, stub_id: str, **fields) -> str:
    lines = [
        "kind: roadmap-baton", f"title: {stub_id}", f"stub_id: {stub_id}",
        "status: open", "deployment_state: ready_to_fire", "baton_role: work",
    ]
    for k, v in fields.items():
        lines.append(f"{k}: [{', '.join(v)}]" if isinstance(v, (list, tuple)) else f"{k}: {v}")
    p = root / "state" / "handoffs" / f"{stub_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + "\n".join(lines) + "\n---\n\nbody\n", encoding="utf-8")
    return f"state/handoffs/{stub_id}.md"


def _plan(root: Path, slug: str, status: str, **fields) -> str:
    lines = [f"title: {slug}", f"status: {status}"] + [f"{k}: {v}" for k, v in fields.items()]
    p = root / "docs" / "plans" / f"{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + "\n".join(lines) + "\n---\n\nbody\n", encoding="utf-8")
    return f"docs/plans/{slug}.md"


def _status(root: Path, rel: str) -> str:
    return pg._scan_fields(root / rel, frozenset({"status"})).get("status")


# ---------------------------------------------------------------------------
# The link is repaired before the stamp, never after
# ---------------------------------------------------------------------------


def test_an_unlinked_plan_is_linked_and_then_stamped(tmp_path):
    """The defect this module exists for. An approval that resolves to nothing
    leaves the baton `needs_plan: true` forever, and the symptom is
    indistinguishable from untouched work."""
    root = _repo(tmp_path)
    baton = _baton(root, "b-1", deliverable_id="dlv-b-1")
    plan = _plan(root, "the-plan", "draft")  # no FK of any kind

    before = pg.assemble_plan_gate(root)
    assert pg_by(before, "b-1")["needs_plan"] is True
    assert pg_by(before, "b-1")["plan"] is None

    out = bl.land_wave(root, {"waveIndex": 0, "ready": [{"batonId": "b-1", "planPath": plan}]})

    assert out["approved"][0]["link_repaired"] is True
    assert out["approved"][0]["stamped"] is True
    assert _status(root, plan) == "approved"

    after = pg.assemble_plan_gate(root)
    assert pg_by(after, "b-1")["needs_plan"] is False
    assert pg_by(after, "b-1")["plan"]["link_basis"] == "governing_plan"


def pg_by(report, ident):
    for b in report["batons"]:
        if b["id"] == ident:
            return b
    raise AssertionError(f"{ident} not in report")


def test_an_already_linked_plan_is_not_repaired(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(root, "the-plan", "draft", deliverable_id="dlv-b-1")
    _baton(root, "b-1", deliverable_id="dlv-b-1")

    out = bl.land_wave(root, {"waveIndex": 0, "ready": [{"batonId": "b-1", "planPath": plan}]})

    assert out["approved"][0]["link_repaired"] is False
    assert _status(root, plan) == "approved"


def test_a_baton_already_owning_a_different_plan_is_refused(tmp_path):
    """A landing ADOPTS an unlinked plan; it never re-owns a linked one. Silently
    repointing would let a wave steal a baton from a plan somebody else authored."""
    root = _repo(tmp_path)
    other = _plan(root, "other-plan", "draft")
    _baton(root, "b-1", governing_plan=other)
    mine = _plan(root, "my-plan", "draft")

    out = bl.land_wave(root, {"waveIndex": 0, "ready": [{"batonId": "b-1", "planPath": mine}]})

    assert not out["approved"]
    assert "governing_plan" in out["refused"][0]["reason"]
    assert _status(root, mine) == "draft", "refused landing must not have stamped"


def test_a_missing_plan_refuses_rather_than_stamping(tmp_path):
    root = _repo(tmp_path)
    _baton(root, "b-1")
    out = bl.land_wave(
        root, {"waveIndex": 0, "ready": [{"batonId": "b-1", "planPath": "docs/plans/ghost.md"}]}
    )
    assert not out["approved"] and "does not exist" in out["refused"][0]["reason"]


# ---------------------------------------------------------------------------
# Idempotence and non-regression
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["approved", "executing", "landed", "implemented"])
def test_landing_never_walks_a_plan_backwards(tmp_path, status):
    """A re-landed wave must not drag `implemented` back to `approved` — that
    would re-open an execution gate that has already legitimately closed."""
    root = _repo(tmp_path)
    plan = _plan(root, "the-plan", status, deliverable_id="dlv-b-1")
    _baton(root, "b-1", deliverable_id="dlv-b-1")

    out = bl.land_wave(root, {"waveIndex": 0, "ready": [{"batonId": "b-1", "planPath": plan}]})

    assert out["approved"][0]["stamped"] is False
    assert _status(root, plan) == status


def test_landing_twice_is_stable(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(root, "the-plan", "draft")
    _baton(root, "b-1")
    result = {"waveIndex": 0, "ready": [{"batonId": "b-1", "planPath": plan}]}

    bl.land_wave(root, result)
    second = bl.land_wave(root, result)

    assert second["approved"][0]["stamped"] is False
    assert _status(root, plan) == "approved"


def test_a_pulled_plan_is_reported_and_left_alone(tmp_path):
    """`pulled` is the EM leaving a plan where it is, deliberately."""
    root = _repo(tmp_path)
    plan = _plan(root, "the-plan", "draft", deliverable_id="dlv-b-1")
    _baton(root, "b-1", deliverable_id="dlv-b-1")

    out = bl.land_wave(root, {"waveIndex": 0, "pulled": [{"batonId": "b-1", "planPath": plan}]})

    assert out["pulled"] == ["b-1"]
    assert _status(root, plan) == "draft"


# ---------------------------------------------------------------------------
# The XS lane — the wave did the work; the landing stamps it terminal
# ---------------------------------------------------------------------------


def test_a_dispatch_ready_verdict_closes_the_baton_terminal(tmp_path):
    """XS has no plan, so nothing is approved — what closes it is the baton
    reaching a terminal deployment_state with a resolvable shipped_in. Until that
    stamp lands, the work is done and every surface counting open batons still
    reads it as unstarted."""
    root = _repo(tmp_path)
    baton = _baton(root, "b-1")

    out = bl.land_wave(
        root,
        {"waveIndex": 0, "ready": [{"batonId": "b-1", "route": "dispatch"}]},
        shipped_in="0983062abc",
    )

    assert out["closed"][0]["closed"] is True
    assert not out["approved"] and not out["refused"]
    fm = pg._read_baton_fields(root / baton)
    assert fm["deployment_state"] == "shipped"
    assert fm["shipped_in"] == "0983062abc"


def test_closing_a_dispatched_baton_without_a_sha_is_refused(tmp_path):
    """This module does not commit, so a stamp written before the commit cites
    nothing. Refusing is the only honest option — a citation to a SHA that does not
    exist is worse than an unclosed baton, because it looks discharged."""
    root = _repo(tmp_path)
    _baton(root, "b-1")

    out = bl.land_wave(root, {"waveIndex": 0, "ready": [{"batonId": "b-1", "route": "dispatch"}]})

    assert not out["closed"]
    assert "shipped_in" in out["refused"][0]["reason"]


def test_a_dispatch_verdict_is_not_refused_for_lacking_a_planPath(tmp_path):
    """Regression: the first live XS wave was refused with "verdict carries no
    planPath" — correct fail-closed behaviour against an unimplemented lane, but
    an XS is CORRECT not to have a plan and must not be judged against one."""
    root = _repo(tmp_path)
    _baton(root, "b-1")

    out = bl.land_wave(
        root,
        {"waveIndex": 0, "ready": [{"batonId": "b-1", "route": "dispatch"}]},
        shipped_in="deadbee",
    )

    assert not any("planPath" in r["reason"] for r in out["refused"])


def test_closing_opens_a_dependents_execution_gate_not_merely_its_planning_gate(tmp_path):
    """The XS lane's terminal stamp is stronger than an approval: `coded` satisfies
    BOTH gates, where an approved plan satisfies only planning."""
    root = _repo(tmp_path)
    _baton(root, "blocker-1")
    _baton(root, "dependent-1", blocked_by=["blocker-1"])

    before = pg.assemble_plan_gate(root)
    assert pg_by(before, "dependent-1")["execution_gate"]["open"] is False

    bl.land_wave(
        root,
        {"waveIndex": 0, "ready": [{"batonId": "blocker-1", "route": "dispatch"}]},
        shipped_in="0983062",
    )

    after = pg.assemble_plan_gate(root)
    dep = pg_by(after, "dependent-1")
    assert dep["planning_gate"]["open"] is True
    assert dep["execution_gate"]["open"] is True


def test_re_landing_does_not_re_close_a_terminal_baton(tmp_path):
    root = _repo(tmp_path)
    _baton(root, "b-1")
    result = {"waveIndex": 0, "ready": [{"batonId": "b-1", "route": "dispatch"}]}

    bl.land_wave(root, result, shipped_in="0983062")
    second = bl.land_wave(root, result, shipped_in="1234567")

    assert second["closed"][0]["closed"] is False
    assert "already terminal" in second["closed"][0]["note"]


# ---------------------------------------------------------------------------
# The S lane — parked spec, execution-ready, no EM hands on the record
# ---------------------------------------------------------------------------


def test_an_s_lane_ready_verdict_parks_the_spec_and_stamps_execution_ready(tmp_path):
    """An S is a straight dispatch, not a decision-weight plan. Handing one back as
    an un-actioned baton is what made sizing fight the skill: if calling something S
    condemned it to the queue, the honest S got inflated to M."""
    root = _repo(tmp_path)
    plan = _plan(root, "the-spec", "draft")
    baton = _baton(root, "b-1")

    out = bl.land_wave(
        root,
        {"waveIndex": 0, "ready": [{"batonId": "b-1", "planPath": plan, "route": "spec-dispatch"}]},
    )

    assert out["execution_ready"][0]["execution_ready"] is True
    assert not out["approved"], "an S lane item takes the stamp, not the approval"

    fm = pg._read_baton_fields(root / baton)
    assert fm["handoff_phase"] == "execution"
    assert fm["governing_plan"] == plan
    for field in ("execution_authorized_by", "execution_authorized_at",
                  "execution_authorized_sha", "execution_authorized_note"):
        assert fm.get(field), f"{field} missing — H-CROSS-EXEC-1 requires all four"


def test_the_execution_stamp_validates_against_the_real_handoff_schema(tmp_path):
    """H-CROSS-EXEC-1 refuses `handoff_phase: execution` without all four stamp
    fields present and non-empty. A stamp this module writes must survive the
    engine's own validator, not merely look complete."""
    from coordinator_core.frontmatter.schema_validate import (
        parse_frontmatter,
        validate_frontmatter,
    )

    root = _repo(tmp_path)
    plan = _plan(root, "the-spec", "draft")
    # Schema-complete fixture on purpose: a minimal one would fail on its own
    # missing `created`/`branch`/`summary` and tell us nothing about the stamp.
    baton = _baton(
        root, "b-1",
        created="2026-09-05",
        branch='"work/test"',
        predecessor="none",
        category="infra",
        summary='"A schema-complete fixture so this test measures the stamp, not the fixture."',
        roadmap_id="rm-1", blocks=[], blocked_by=[], wave="1",
    )
    bl.land_wave(
        root,
        {"waveIndex": 0, "ready": [{"batonId": "b-1", "planPath": plan, "route": "spec-dispatch"}]},
    )

    schema = Path(bl.__file__).parents[1] / "frontmatter" / "schemas" / "handoff.schema.json"
    fm = parse_frontmatter((root / baton).read_text(encoding="utf-8"))["frontmatter"]
    assert not validate_frontmatter(fm, str(schema))


def test_the_authorized_sha_binds_the_plan_body_git_style(tmp_path):
    """`/pickup` recomputes this witness. If it is not a real git blob hash of the
    plan body, a plan edited after authorization still matches and the stale
    authorization is carried silently instead of surfacing."""
    root = _repo(tmp_path)
    plan = _plan(root, "the-spec", "draft")
    _baton(root, "b-1")

    out = bl.land_wave(
        root,
        {"waveIndex": 0, "ready": [{"batonId": "b-1", "planPath": plan, "route": "spec-dispatch"}]},
    )

    body = (root / plan).read_bytes()
    import hashlib

    expected = hashlib.sha1(f"blob {len(body)}\0".encode() + body).hexdigest()
    assert out["execution_ready"][0]["authorized_sha"] == expected


def test_the_note_is_attributed_to_the_wave_not_to_the_pm(tmp_path):
    """The note field is self-attesting about who named execution. A session
    writing a sentence that reads like a PM utterance is the one way this stamp
    could lie, and it would lie in the direction of manufactured authority."""
    root = _repo(tmp_path)
    plan = _plan(root, "the-spec", "draft")
    baton = _baton(root, "b-1")

    bl.land_wave(
        root,
        {"waveIndex": 0, "ready": [{"batonId": "b-1", "planPath": plan, "route": "spec-dispatch"}]},
    )

    note = pg._read_baton_fields(root / baton)["execution_authorized_note"]
    assert "plan-blitz" in note and "readiness gate" in note


def test_re_landing_does_not_re_stamp_an_execution_ready_baton(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(root, "the-spec", "draft")
    _baton(root, "b-1")
    result = {"waveIndex": 0, "ready": [{"batonId": "b-1", "planPath": plan, "route": "spec-dispatch"}]}

    bl.land_wave(root, result)
    second = bl.land_wave(root, result)

    assert second["execution_ready"][0]["execution_ready"] is False
    assert "already stamped" in second["execution_ready"][0]["note"]


def test_a_non_s_route_still_takes_the_ordinary_approval(tmp_path):
    """Only spec-dispatch parks. An M/L plan's approval is what opens a dependent's
    planning gate, and swapping it for an execution stamp would break the wave loop."""
    root = _repo(tmp_path)
    plan = _plan(root, "the-plan", "draft")
    baton = _baton(root, "b-1")

    out = bl.land_wave(
        root, {"waveIndex": 0, "ready": [{"batonId": "b-1", "planPath": plan, "route": "plan"}]}
    )

    assert out["approved"] and not out["execution_ready"]
    assert _status(root, plan) == "approved"
    assert pg._read_baton_fields(root / baton).get("handoff_phase") is None


# ---------------------------------------------------------------------------
# Replan minting
# ---------------------------------------------------------------------------


def test_a_replan_verdict_mints_a_baton_carrying_the_brief_verbatim(tmp_path):
    """The brief was written for a session with no context; summarising it here
    would compress the one artifact whose purpose is to survive that boundary."""
    root = _repo(tmp_path)
    _baton(root, "b-1")
    brief = "CONTEXT FOR A SESSION WITH NO PRIOR CONTEXT.\n\nThe premise failed because X."

    out = bl.land_wave(
        root,
        {"waveIndex": 0, "replan": [{"batonId": "b-1", "replanBrief": brief}]},
        branch="work/test",
    )

    minted = root / out["minted"][0]["path"]
    text = minted.read_text(encoding="utf-8")
    assert brief in text
    assert "forked_from" in text and "b-1" in text


def test_the_minted_baton_validates_against_the_real_handoff_schema(tmp_path):
    """The gap that let a schema-invalid baton reach disk on the first live landing:
    minting was tested for CONTENT (brief verbatim, forked_from) and never for
    SHAPE. `handoff_id` is pattern-pinned to ^hnd-<slug>-[0-9a-f]{6}$ and was being
    written without the suffix."""
    from coordinator_core.frontmatter.schema_validate import (
        parse_frontmatter,
        validate_frontmatter,
    )

    root = _repo(tmp_path)
    _baton(root, "b-1")
    out = bl.land_wave(
        root,
        {"waveIndex": 0, "replan": [{"batonId": "b-1", "replanBrief": "why it failed"}]},
        branch="work/test",
    )

    schema = Path(bl.__file__).parents[1] / "frontmatter" / "schemas" / "handoff.schema.json"
    fm = parse_frontmatter((root / out["minted"][0]["path"]).read_text(encoding="utf-8"))["frontmatter"]
    errors = validate_frontmatter(fm, str(schema))
    assert not errors, errors


def test_the_minted_filename_does_not_double_its_prefix(tmp_path):
    root = _repo(tmp_path)
    _baton(root, "b-1")
    out = bl.land_wave(
        root, {"waveIndex": 0, "replan": [{"batonId": "b-1", "replanBrief": "why"}]}
    )
    assert "replan-replan" not in out["minted"][0]["path"]


def test_the_minted_baton_re_enters_the_gate_as_work(tmp_path):
    """The loop only closes if the minted baton is visible to the next wave."""
    root = _repo(tmp_path)
    _baton(root, "b-1")

    out = bl.land_wave(root, {"waveIndex": 0, "replan": [{"batonId": "b-1", "replanBrief": "why"}]})

    after = pg.assemble_plan_gate(root)
    ids = {b["id"] for b in after["batons"] if b["needs_plan"]}
    assert out["minted"][0]["handoff_id"] in ids or any(
        "replan" in i for i in ids
    ), f"minted baton absent from the next wave: {sorted(ids)}"


# ---------------------------------------------------------------------------
# Payload shape — the wrong object must not look like an empty wave
# ---------------------------------------------------------------------------


def test_the_task_output_envelope_is_unwrapped(tmp_path):
    """The harness writes a workflow's return value inside
    {"summary":…, "agentCount":…, "result":{…}} and its notification points at that
    file. Handing it over whole is the natural mistake, not an exotic one."""
    root = _repo(tmp_path)
    plan = _plan(root, "the-plan", "draft")
    _baton(root, "b-1")

    out = bl.land_wave(
        root,
        {"summary": "a wave", "agentCount": 7,
         "result": {"waveIndex": 0, "ready": [{"batonId": "b-1", "planPath": plan}]}},
    )

    assert out["approved"][0]["stamped"] is True


def test_a_payload_with_no_verdict_keys_is_refused_not_treated_as_empty(tmp_path):
    """The failure this guards is silent success: without the check, every loop
    iterates nothing and the landing returns approved:[], minted:[], refused:[] —
    complete-looking output for work never done, and indistinguishable from a wave
    where the EM genuinely pulled everything. Measured: it happened on the first
    live landing, when the task-output envelope was passed whole."""
    root = _repo(tmp_path)
    _baton(root, "b-1")

    with pytest.raises(bl.LandingRefused, match="not a plan-blitz wave result"):
        bl.land_wave(root, {"summary": "a wave", "agentCount": 7, "logs": []})


def test_a_genuinely_empty_wave_still_lands(tmp_path):
    """An all-pulled wave is a real outcome and must not be confused with a bad
    payload — `pulled` present-but-empty is the discriminator."""
    root = _repo(tmp_path)
    _baton(root, "b-1")

    out = bl.land_wave(root, {"waveIndex": 0, "ready": [], "pulled": [], "replan": []})

    assert out["approved"] == [] and out["refused"] == []
    assert out["next_wave"]["waveIndex"] == 1


# ---------------------------------------------------------------------------
# next_wave — the half that makes it a loop
# ---------------------------------------------------------------------------


def test_next_wave_is_computed_after_the_writes_not_before(tmp_path):
    """The approvals just written are exactly what changes the next wave. Reusing
    the pre-landing read would hand the driver the same wave twice, forever."""
    root = _repo(tmp_path)
    plan = _plan(root, "blocker-plan", "draft")
    _baton(root, "blocker-1")
    _baton(root, "dependent-1", blocked_by=["blocker-1"])

    before = pg.assemble_plan_gate(root)
    assert pg_by(before, "dependent-1")["planning_gate"]["open"] is False

    out = bl.land_wave(
        root, {"waveIndex": 0, "ready": [{"batonId": "blocker-1", "planPath": plan}]}
    )

    ids = [b["id"] for b in out["next_wave"]["batons"]]
    assert ids == ["dependent-1"], f"dependent did not enter wave 1: {ids}"
    assert out["next_wave"]["waveIndex"] == 1


def test_next_wave_entries_are_shaped_for_the_workflow_and_carry_planPath(tmp_path):
    """A hand-built fire array is where `planPath` silently becomes null for a
    baton that already has a plan — which is how a wave duplicates a plan."""
    root = _repo(tmp_path)
    existing = _plan(root, "already-there", "draft", deliverable_id="dlv-d-1")
    _baton(root, "d-1", deliverable_id="dlv-d-1")

    out = bl.land_wave(root, {"waveIndex": 0, "ready": []})

    entry = next(b for b in out["next_wave"]["batons"] if b["id"] == "d-1")
    assert set(entry) == {"id", "path", "title", "sized", "planPath", "executionOpen"}
    assert entry["planPath"].endswith(existing)
    assert Path(entry["path"]).is_absolute()


def test_fire_args_carry_the_execution_gate_separately_from_planning(tmp_path):
    """An XS whose blockers are planned-but-not-coded may be PLANNED in a wave and
    must not be EXECUTED in it. The wave can only honour that if it is handed both
    gates — one flag serving both questions would run code against blockers that do
    not exist yet, which is the precise failure the two-gate split prevents."""
    root = _repo(tmp_path)
    blocker_plan = _plan(root, "blocker-plan", "approved", deliverable_id="dlv-blocker")
    _baton(root, "blocker-1", deliverable_id="dlv-blocker")
    _baton(root, "dependent-1", blocked_by=["blocker-1"])

    out = bl.land_wave(root, {"waveIndex": 0, "ready": []})
    entry = next(b for b in out["next_wave"]["batons"] if b["id"] == "dependent-1")

    # Planning is open (the blocker's plan is approved) but execution is not.
    assert entry["executionOpen"] is False
    assert pg_by(pg.assemble_plan_gate(root), "dependent-1")["planning_gate"]["open"] is True
    assert blocker_plan  # the approved plan is what opened planning, and only planning


def test_fire_args_report_execution_open_once_the_blocker_is_coded(tmp_path):
    root = _repo(tmp_path)
    _baton(root, "blocker-1", deployment_state="shipped")
    _baton(root, "dependent-1", blocked_by=["blocker-1"])

    out = bl.land_wave(root, {"waveIndex": 0, "ready": []})
    entry = next(b for b in out["next_wave"]["batons"] if b["id"] == "dependent-1")

    assert entry["executionOpen"] is True


def test_next_wave_respects_the_batch_limit_and_reports_the_remainder(tmp_path):
    """A wave is not a fire unit — 205 batons in one call is a machine-wide event."""
    root = _repo(tmp_path)
    for i in range(12):
        _baton(root, f"b-{i:02d}")

    out = bl.land_wave(root, {"waveIndex": 0, "ready": []}, limit=8)

    assert len(out["next_wave"]["batons"]) == 8
    assert out["next_wave"]["remaining"] == 4


# ---------------------------------------------------------------------------
# A pivot is refused at the landing, independently of the workflow
# ---------------------------------------------------------------------------
#
# The workflow reconciles a pivoted plan to `replan` before a wave result reaches
# here. These cases pin the SECOND check: the first one lives in a `.mjs` an agent
# edits, and a rule with exactly one enforcement point loses it the first time
# someone rewrites that file.


@pytest.mark.parametrize("verdict", ["PIVOT", "pivot", "REJECTED"])
def test_a_ready_verdict_a_reviewer_pivoted_is_refused(tmp_path, verdict):
    root = _repo(tmp_path)
    _baton(root, "b-1", deliverable_id="dlv-b-1")
    plan = _plan(root, "the-plan", "draft", deliverable_id="dlv-b-1")

    out = bl.land_wave(
        root,
        {
            "waveIndex": 0,
            "ready": [
                {
                    "batonId": "b-1",
                    "planPath": plan,
                    "reviewVerdicts": [
                        {"reviewer": "staff-eng", "verdict": "BLOCKED"},
                        {"reviewer": "eng-director", "verdict": verdict},
                    ],
                }
            ],
        },
    )

    assert out["approved"] == []
    assert len(out["refused"]) == 1
    # The refusal names the reviewer, not just the rule: a landing report that says
    # "refused" without naming who disagreed cannot be reconciled against the trail.
    assert "eng-director" in out["refused"][0]["reason"]
    # Not stamped is the whole point — the plan stays where the reviewer left it.
    assert _status(root, plan) == "draft"


def test_a_refused_pivot_leaves_the_baton_replannable_rather_than_stranded(tmp_path):
    """The self-healing half. A refusal writes nothing, so the baton still has no
    approved plan and the next sweep plans it fresh — which is what a pivot asked
    for. A refusal that also closed the baton would strand it."""
    root = _repo(tmp_path)
    _baton(root, "b-1", deliverable_id="dlv-b-1")
    plan = _plan(root, "the-plan", "draft", deliverable_id="dlv-b-1")

    bl.land_wave(
        root,
        {
            "waveIndex": 0,
            "ready": [
                {
                    "batonId": "b-1",
                    "planPath": plan,
                    "reviewVerdicts": [{"reviewer": "r", "verdict": "PIVOT"}],
                }
            ],
        },
    )

    after = pg.assemble_plan_gate(root)
    assert pg_by(after, "b-1")["needs_plan"] is True


def test_a_ready_verdict_whose_reviews_all_hold_the_direction_still_lands(tmp_path):
    """BLOCKED is not a pivot. A plan whose every review was BLOCKED had its findings
    applied by the integrator and is an ordinary approval — treating severity as a
    route is exactly the conflation this vocabulary exists to remove."""
    root = _repo(tmp_path)
    _baton(root, "b-1", deliverable_id="dlv-b-1")
    plan = _plan(root, "the-plan", "draft", deliverable_id="dlv-b-1")

    out = bl.land_wave(
        root,
        {
            "waveIndex": 0,
            "ready": [
                {
                    "batonId": "b-1",
                    "planPath": plan,
                    "reviewVerdicts": [
                        {"reviewer": "a", "verdict": "BLOCKED"},
                        {"reviewer": "b", "verdict": "OK"},
                    ],
                }
            ],
        },
    )

    assert out["refused"] == []
    assert _status(root, plan) == "approved"


def test_a_wave_result_carrying_no_review_verdicts_is_admitted_not_refused(tmp_path):
    """Absence is not a pivot. Failing closed on a MISSING field would refuse every
    wave result written before the field existed, none of which carried a pivot."""
    root = _repo(tmp_path)
    _baton(root, "b-1", deliverable_id="dlv-b-1")
    plan = _plan(root, "the-plan", "draft", deliverable_id="dlv-b-1")

    out = bl.land_wave(root, {"waveIndex": 0, "ready": [{"batonId": "b-1", "planPath": plan}]})

    assert out["refused"] == []
    assert _status(root, plan) == "approved"


def test_pivoting_reviewers_ignores_malformed_entries(tmp_path):
    assert bl.pivoting_reviewers({}) == []
    assert bl.pivoting_reviewers({"reviewVerdicts": None}) == []
    assert bl.pivoting_reviewers({"reviewVerdicts": ["PIVOT"]}) == []
    assert bl.pivoting_reviewers({"reviewVerdicts": [{"verdict": "PIVOT"}]}) == [
        "(unnamed reviewer)"
    ]


def test_a_pivot_does_not_block_the_other_lanes_in_the_same_wave(tmp_path):
    """A pivot on one baton is not a wave-level halt. The user's own framing: the
    pipeline must not exit because a reviewer rejected one plan."""
    root = _repo(tmp_path)
    _baton(root, "b-1", deliverable_id="dlv-b-1")
    _baton(root, "b-2", deliverable_id="dlv-b-2")
    pivoted = _plan(root, "pivoted-plan", "draft", deliverable_id="dlv-b-1")
    good = _plan(root, "good-plan", "draft", deliverable_id="dlv-b-2")

    out = bl.land_wave(
        root,
        {
            "waveIndex": 0,
            "ready": [
                {
                    "batonId": "b-1",
                    "planPath": pivoted,
                    "reviewVerdicts": [{"reviewer": "r", "verdict": "PIVOT"}],
                },
                {"batonId": "b-2", "planPath": good},
            ],
        },
    )

    assert [a["baton"] for a in out["approved"]] == ["state/handoffs/b-2.md"]
    assert [r["baton"] for r in out["refused"]] == ["b-1"]
    assert _status(root, good) == "approved"
    assert _status(root, pivoted) == "draft"
