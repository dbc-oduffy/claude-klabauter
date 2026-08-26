"""
coordinator_core.plan_assemble.predicates.test_triage — one case per
`gates.triage.*` row, plus the absent-input `undetermined` path for each.

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C2
Spec backlink: pln-plan-assemble-admits-instead-o-e441e3, chunk C1
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.plan_assemble.predicates import PredicateContext
from coordinator_core.plan_assemble.predicates import triage


def _context(tmp_path: Path, **overrides) -> PredicateContext:
    defaults = dict(
        repo_root=tmp_path,
        plan_path=None,
        plan_frontmatter=None,
        plan_body=None,
        sizing_object_path=None,
        sizing_frontmatter=None,
        resolved_route="plan",
        caller_flags={},
    )
    defaults.update(overrides)
    return PredicateContext(**defaults)


# --- :30 sizing_object_present ---------------------------------------


def test_sizing_object_present_true(tmp_path):
    sizing_path = tmp_path / "state" / "sizings" / "ask.yaml"
    context = _context(
        tmp_path,
        sizing_object_path=sizing_path,
        sizing_frontmatter={"resized_tshirt": "M"},
    )
    result = triage.sizing_object_present(context)
    assert result == {"present": True, "path": str(sizing_path)}


def test_sizing_object_present_undetermined_when_no_sizing_path(tmp_path):
    result = triage.sizing_object_present(_context(tmp_path))
    assert result["undetermined"] is True
    assert result["reason"]


# --- :32a sizing_object_arrival ----------------------------------------


def test_sizing_object_arrival_fresh_inbound(tmp_path):
    context = _context(tmp_path, caller_flags={"arrival": "fresh_inbound"})
    assert triage.sizing_object_arrival(context) == {"arrival": "fresh_inbound"}


def test_sizing_object_arrival_return_edge(tmp_path):
    context = _context(tmp_path, caller_flags={"arrival": "return_edge"})
    assert triage.sizing_object_arrival(context) == {"arrival": "return_edge"}


def test_sizing_object_arrival_undetermined_when_flag_absent(tmp_path):
    result = triage.sizing_object_arrival(_context(tmp_path))
    assert result["undetermined"] is True


# --- :32b sizing_object_narrative_fields --------------------------------


def test_sizing_object_narrative_fields_verbatim(tmp_path):
    context = _context(
        tmp_path,
        sizing_frontmatter={"intent": "fix a bug", "estimate": "S"},
    )
    result = triage.sizing_object_narrative_fields(context)
    assert result == {"intent": "fix a bug", "estimate": "S", "appetite": None}


def test_sizing_object_narrative_fields_undetermined_when_no_frontmatter(tmp_path):
    result = triage.sizing_object_narrative_fields(_context(tmp_path))
    assert result["undetermined"] is True


# --- :33 route -----------------------------------------------------------


def test_route_surfaces_resolved_route(tmp_path):
    context = _context(tmp_path, resolved_route="spec-dispatch")
    assert triage.route(context) == {"route": "spec-dispatch"}


# --- :34 roadmap_precondition (clean route arm only) ---------------------


@pytest.mark.parametrize(
    "resolved_route,expected",
    [
        ("plan", False),
        ("spec-dispatch", False),
        ("shape", True),
        ("roadmap", True),
        ("pm-decision", True),
    ],
)
def test_roadmap_precondition_disqualified(tmp_path, resolved_route, expected):
    context = _context(tmp_path, resolved_route=resolved_route)
    assert triage.roadmap_precondition(context) == {"disqualified": expected}


# --- :37/:39 sizing_wall_fires --------------------------------------------


def test_sizing_wall_fires_true_when_no_sizing_object(tmp_path):
    assert triage.sizing_wall_fires(_context(tmp_path)) == {"fires": True}


def test_sizing_wall_fires_false_when_sizing_object_parses(tmp_path):
    context = _context(
        tmp_path,
        sizing_object_path=tmp_path / "ask.yaml",
        sizing_frontmatter={"resized_tshirt": "M"},
    )
    assert triage.sizing_wall_fires(context) == {"fires": False}


def test_sizing_wall_fires_true_when_sizing_object_unparseable(tmp_path):
    context = _context(
        tmp_path,
        sizing_object_path=tmp_path / "ask.yaml",
        sizing_frontmatter=None,
    )
    assert triage.sizing_wall_fires(context) == {"fires": True}


# --- :38 sizing_wall_disposition ------------------------------------------


@pytest.mark.parametrize(
    "resolved_route,expected",
    [
        ("plan", "route_to_plan"),
        ("spec-dispatch", "route_to_plan"),
        ("shape", "route_to_shape"),
        ("roadmap", "route_to_roadmap"),
        ("pm-decision", "route_to_pm_decision"),
        ("dispatch", "route_to_dispatch"),
    ],
)
def test_sizing_wall_disposition_table(tmp_path, resolved_route, expected):
    context = _context(tmp_path, resolved_route=resolved_route)
    assert triage.sizing_wall_disposition(context) == {"disposition": expected}


def test_sizing_wall_disposition_undetermined_for_unmapped_route(tmp_path):
    context = _context(tmp_path, resolved_route="not-a-real-route")
    result = triage.sizing_wall_disposition(context)
    assert result["undetermined"] is True


# --- :40 sizing_wall_via_memo ----------------------------------------------


def test_sizing_wall_via_memo_true_from_frontmatter_citation(tmp_path):
    context = _context(
        tmp_path, plan_frontmatter={"source_memo": "2026-08-13-something.md"}
    )
    result = triage.sizing_wall_via_memo(context)
    assert result == {"via_memo": True, "source_memo": "2026-08-13-something.md"}


def test_sizing_wall_via_memo_resolves_citation_to_inbox_path(tmp_path):
    inbox = tmp_path / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "2026-08-13-a-memo.md").write_text("memo body", encoding="utf-8")
    context = _context(
        tmp_path, plan_frontmatter={"source_memo": "2026-08-13-a-memo.md"}
    )
    result = triage.sizing_wall_via_memo(context)
    assert result["via_memo"] is True
    assert result["source_memo"] == "cross-repo/inbox/2026-08-13-a-memo.md"


def test_sizing_wall_via_memo_false_for_uncited_plan_despite_full_inbox(tmp_path):
    # Replaces a test that asserted the opposite and thereby pinned the bug
    # in place: an uncited plan used to inherit whichever memo the inbox
    # happened to yield first. This repo's inbox is non-empty as a matter of
    # course, so that made `via_memo` read True on essentially every plan.
    inbox = tmp_path / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "2026-08-13-unrelated.md").write_text("not ours", encoding="utf-8")
    (inbox / "2026-08-13-also-unrelated.md").write_text("nor this", encoding="utf-8")
    context = _context(tmp_path, plan_frontmatter={})
    assert triage.sizing_wall_via_memo(context) == {
        "via_memo": False,
        "source_memo": None,
    }


def test_sizing_wall_via_memo_keeps_citation_when_memo_already_archived(tmp_path):
    # A cited memo swept to cross-repo/archive/ is still real provenance —
    # the citation stands, it simply does not resolve to an inbox path.
    context = _context(
        tmp_path, plan_frontmatter={"source_memo": "2026-08-13-swept.md"}
    )
    result = triage.sizing_wall_via_memo(context)
    assert result == {"via_memo": True, "source_memo": "2026-08-13-swept.md"}


def test_sizing_wall_via_memo_false_when_no_evidence(tmp_path):
    context = _context(tmp_path, plan_frontmatter={})
    assert triage.sizing_wall_via_memo(context) == {
        "via_memo": False,
        "source_memo": None,
    }


def test_sizing_wall_via_memo_undetermined_when_no_plan_frontmatter(tmp_path):
    result = triage.sizing_wall_via_memo(_context(tmp_path))
    assert result["undetermined"] is True


# --- :42 sizing_wall_carveout -----------------------------------------------


def test_sizing_wall_carveout_handoff_pickup(tmp_path):
    context = _context(tmp_path, caller_flags={"claim_grant": {"verdict": "granted"}})
    assert triage.sizing_wall_carveout(context) == {"carveout": "handoff_pickup"}


def test_sizing_wall_carveout_none(tmp_path):
    context = _context(tmp_path, caller_flags={"claim_grant": None})
    assert triage.sizing_wall_carveout(context) == {"carveout": "none"}


def test_sizing_wall_carveout_undetermined_when_flag_absent(tmp_path):
    result = triage.sizing_wall_carveout(_context(tmp_path))
    assert result["undetermined"] is True


# --- :50 handoff_prescribes_plan --------------------------------------------


def test_handoff_prescribes_plan_true(tmp_path):
    handoff_path = tmp_path / "state" / "handoffs" / "h.md"
    handoff_path.parent.mkdir(parents=True)
    handoff_path.write_text(
        "This handoff prescribes a plan for the next pass.", encoding="utf-8"
    )
    context = _context(
        tmp_path,
        plan_frontmatter={"predecessor_handoff": "state/handoffs/h.md"},
    )
    assert triage.handoff_prescribes_plan(context) == {
        "handoff_prescribes_plan": True
    }


def test_handoff_prescribes_plan_false(tmp_path):
    handoff_path = tmp_path / "state" / "handoffs" / "h.md"
    handoff_path.parent.mkdir(parents=True)
    handoff_path.write_text("Nothing prescriptive here.", encoding="utf-8")
    context = _context(
        tmp_path,
        plan_frontmatter={"predecessor_handoff": "state/handoffs/h.md"},
    )
    assert triage.handoff_prescribes_plan(context) == {
        "handoff_prescribes_plan": False
    }


def test_handoff_prescribes_plan_undetermined_when_no_plan_frontmatter(tmp_path):
    result = triage.handoff_prescribes_plan(_context(tmp_path))
    assert result["undetermined"] is True


def test_handoff_prescribes_plan_undetermined_when_no_predecessor_handoff_key(
    tmp_path,
):
    context = _context(tmp_path, plan_frontmatter={})
    result = triage.handoff_prescribes_plan(context)
    assert result["undetermined"] is True


def test_handoff_prescribes_plan_undetermined_when_handoff_unreadable(tmp_path):
    context = _context(
        tmp_path,
        plan_frontmatter={"predecessor_handoff": "state/handoffs/missing.md"},
    )
    result = triage.handoff_prescribes_plan(context)
    assert result["undetermined"] is True


def test_handoff_prescribes_plan_literal_hit_wins_over_archive(tmp_path):
    """A literal-path hit is used as-is; the archive fallback is never
    consulted when the literal path already resolves."""
    live_path = tmp_path / "state" / "handoffs" / "h.md"
    live_path.parent.mkdir(parents=True)
    live_path.write_text(
        "This handoff prescribes a plan for the next pass.", encoding="utf-8"
    )
    archived_path = tmp_path / "archive" / "handoffs" / "h.md"
    archived_path.parent.mkdir(parents=True)
    archived_path.write_text("Nothing prescriptive here.", encoding="utf-8")
    context = _context(
        tmp_path,
        plan_frontmatter={"predecessor_handoff": "state/handoffs/h.md"},
    )
    assert triage.handoff_prescribes_plan(context) == {
        "handoff_prescribes_plan": True
    }


def test_handoff_prescribes_plan_archive_fallback_fires(tmp_path):
    archived_path = tmp_path / "archive" / "handoffs" / "h.md"
    archived_path.parent.mkdir(parents=True)
    archived_path.write_text(
        "This handoff prescribes a plan for the next pass.", encoding="utf-8"
    )
    context = _context(
        tmp_path,
        plan_frontmatter={"predecessor_handoff": "state/handoffs/h.md"},
    )
    assert triage.handoff_prescribes_plan(context) == {
        "handoff_prescribes_plan": True
    }


def test_handoff_prescribes_plan_archive_fallback_does_not_fire(tmp_path):
    archived_path = tmp_path / "archive" / "handoffs" / "h.md"
    archived_path.parent.mkdir(parents=True)
    archived_path.write_text("Nothing prescriptive here.", encoding="utf-8")
    context = _context(
        tmp_path,
        plan_frontmatter={"predecessor_handoff": "state/handoffs/h.md"},
    )
    assert triage.handoff_prescribes_plan(context) == {
        "handoff_prescribes_plan": False
    }


def test_handoff_prescribes_plan_undetermined_when_genuinely_missing_everywhere(
    tmp_path,
):
    context = _context(
        tmp_path,
        plan_frontmatter={"predecessor_handoff": "state/handoffs/nowhere.md"},
    )
    result = triage.handoff_prescribes_plan(context)
    assert result["undetermined"] is True


def test_handoff_prescribes_plan_undetermined_on_multi_hit_archive_ambiguity(
    tmp_path,
):
    for archive_dir in ("archive/handoffs", "archive/completed"):
        hit = tmp_path / archive_dir / "h.md"
        hit.parent.mkdir(parents=True)
        hit.write_text(
            "This handoff prescribes a plan for the next pass.", encoding="utf-8"
        )
    context = _context(
        tmp_path,
        plan_frontmatter={"predecessor_handoff": "state/handoffs/h.md"},
    )
    result = triage.handoff_prescribes_plan(context)
    assert result["undetermined"] is True


# --- admission (SIZING axis, pln-plan-assemble-admits-instead-o-e441e3) ----


def test_admission_absent_everything_resolves_unsized_no_error(tmp_path):
    """AC1's negative case: no `--plan`, no `--sizing-object` — a baton
    citing nothing is the ordinary unsized case, resolved (never raised)."""
    result = triage.admission(_context(tmp_path))
    assert result == {"value": "unsized", "basis": None, "warning": None}


def test_admission_resolves_execution_from_inbound_plan_frontmatter(tmp_path):
    """AC1: with no `--sizing-object`, the FK is resolved off the inbound
    artifact's own frontmatter via the shared predicate.

    DR-346 (2026-08-21) retired the corpus-walk `origin_plan_id` resolution
    leg — execution now resolves only off a stamped `governing_plan`
    repo-relative path (`sizing_disposition.compute_sizing_disposition`'s
    own precedence), never off `origin_plan_id` alone, which reads
    `unsized` (a stranding, PM-ratified, not a defect). This fixture
    carries both so it keeps exercising a real inbound-frontmatter FK
    resolution under the current contract."""
    plan_path = tmp_path / "docs" / "plans" / "2026-08-20-a.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        "---\ntitle: a plan\nplan_id: pln-a-123456\nstatus: draft\n---\n\nbody\n",
        encoding="utf-8",
    )
    context = _context(
        tmp_path,
        plan_frontmatter={
            "origin_plan_id": "pln-a-123456",
            "governing_plan": "docs/plans/2026-08-20-a.md",
        },
    )
    result = triage.admission(context)
    assert result["value"] == "execution"
    assert "governing_plan=docs/plans/2026-08-20-a.md" in result["basis"]
    assert result["warning"] is None


def test_admission_resolves_sized_from_inbound_plan_frontmatter_sizing_object(
    tmp_path,
):
    sizing_path = tmp_path / "state" / "sizings" / "ask.yaml"
    sizing_path.parent.mkdir(parents=True)
    sizing_path.write_text("route: plan\n", encoding="utf-8")
    context = _context(
        tmp_path,
        plan_frontmatter={"sizing_object": "state/sizings/ask.yaml"},
    )
    result = triage.admission(context)
    assert result == {
        "value": "sized",
        "basis": "sizing_object=state/sizings/ask.yaml",
        "warning": None,
    }


def test_admission_explicit_sizing_object_wins_over_inbound_plan_fk(tmp_path):
    """AC2: an explicitly-passed `--sizing-object` wins over the FK the
    inbound artifact's own frontmatter would otherwise resolve to
    `execution` — the plan-FK precedence never gets a chance to fire."""
    plan_id_path = tmp_path / "docs" / "plans" / "2026-08-20-a.md"
    plan_id_path.parent.mkdir(parents=True)
    plan_id_path.write_text(
        "---\ntitle: a plan\nplan_id: pln-a-123456\nstatus: draft\n---\n\nbody\n",
        encoding="utf-8",
    )
    explicit_sizing_path = tmp_path / "state" / "sizings" / "explicit.yaml"
    explicit_sizing_path.parent.mkdir(parents=True)
    explicit_sizing_path.write_text("route: plan\n", encoding="utf-8")
    context = _context(
        tmp_path,
        plan_frontmatter={"origin_plan_id": "pln-a-123456"},
        sizing_object_path=explicit_sizing_path,
        sizing_frontmatter={"route": "plan"},
    )
    result = triage.admission(context)
    assert result["value"] == "sized"
    assert result["basis"] == "sizing_object=state/sizings/explicit.yaml"
    assert result["warning"] is None


def test_admission_explicit_unresolvable_sizing_object_is_unsized_with_warning(
    tmp_path,
):
    """A `context.sizing_object_path` that does not resolve on disk (a shape
    the CLI's own `--sizing-object` validation never lets through, but the
    predicate itself is exercised against directly here) is a dangling
    citation, not a crash — `unsized` plus a named `warning`."""
    dangling_path = tmp_path / "state" / "sizings" / "does-not-exist.yaml"
    context = _context(tmp_path, sizing_object_path=dangling_path)
    result = triage.admission(context)
    assert result["value"] == "unsized"
    assert result["warning"] is not None
    assert "state/sizings/does-not-exist.yaml" in result["basis"]
