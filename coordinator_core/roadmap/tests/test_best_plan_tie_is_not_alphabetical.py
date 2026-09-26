"""`_best_plan` breaks a `(coded, approved)` tie by lifecycle order, not path order.

IBMFR-R05: two plans tied on `(coded, approved)` used to resolve via `max()`'s
first-occurrence-wins behaviour, which tracked the incidental order `hits`
arrived in (alphabetical path order out of `PlanIndex`) rather than which plan
was actually further along. An exact tie (same status too) now declines and
is surfaced via `_plan_link_ambiguity`.
"""

from __future__ import annotations

from coordinator_core.roadmap import plan_gate as pg


def _plan(path: str, status: str) -> dict:
    status = status.strip().lower()
    return {
        "path": path,
        "status": status,
        "title": path,
        "approved": status in pg.PLAN_APPROVED_STATUSES,
        "coded": status in pg.PLAN_CODED_STATUSES,
    }


def test_tie_resolves_by_lifecycle_order_not_alphabetical():
    # "approved" < "executing" alphabetically, but "executing" is further
    # along in the lifecycle. Both are approved-not-coded, so the
    # (coded, approved) key ties and only the lifecycle tiebreak decides.
    earlier_alpha = _plan("plans/a-approved.md", "approved")
    later_alpha = _plan("plans/z-executing.md", "executing")

    winner = pg._best_plan([earlier_alpha, later_alpha], basis="governing_plan")

    assert winner is not None
    assert winner["path"] == "plans/z-executing.md"

    # Reversing input order must not change the outcome.
    winner_reversed = pg._best_plan([later_alpha, earlier_alpha], basis="governing_plan")
    assert winner_reversed["path"] == "plans/z-executing.md"

    # Not ambiguous: the lifecycle tiebreak resolved it cleanly.
    assert pg._plan_link_ambiguity([earlier_alpha, later_alpha], "governing_plan") is None


def test_lifecycle_tie_also_beats_alphabetical_order_when_reversed():
    # Same pair, but named so alphabetical order points the *other* way from
    # lifecycle order, to confirm the tiebreak isn't accidentally re-deriving
    # alphabetical order under a different name.
    alpha_later_lifecycle_ahead = _plan("plans/z-executing.md", "executing")
    alpha_earlier_lifecycle_behind = _plan("plans/a-approved.md", "approved")

    winner = pg._best_plan(
        [alpha_earlier_lifecycle_behind, alpha_later_lifecycle_ahead],
        basis="deliverable_id",
    )

    assert winner["path"] == "plans/z-executing.md"


def test_exact_tie_declines_and_is_reported_ambiguous():
    one = _plan("plans/one.md", "approved")
    two = _plan("plans/two.md", "approved")

    assert pg._best_plan([one, two], basis="governing_plan") is None

    ambiguity = pg._plan_link_ambiguity([one, two], "governing_plan")
    assert ambiguity is not None
    assert ambiguity["basis"] == "governing_plan"
    assert ambiguity["paths"] == ["plans/one.md", "plans/two.md"]


def test_strong_basis_tie_only_names_the_tied_top_not_a_trailing_candidate():
    tied_a = _plan("plans/tied-a.md", "approved")
    tied_b = _plan("plans/tied-b.md", "approved")
    trailing = _plan("plans/trailing-draft.md", "draft")

    winner = pg._best_plan([tied_a, tied_b, trailing], basis="origin_plan_id")
    assert winner is None

    ambiguity = pg._plan_link_ambiguity([tied_a, tied_b, trailing], "origin_plan_id")
    assert ambiguity is not None
    assert ambiguity["paths"] == ["plans/tied-a.md", "plans/tied-b.md"]


def test_coded_beats_approved_regardless_of_status_rank():
    coded = _plan("plans/landed.md", "landed")
    approved_only = _plan("plans/executing.md", "executing")

    winner = pg._best_plan([approved_only, coded], basis="plan_ids")
    assert winner["path"] == "plans/landed.md"
