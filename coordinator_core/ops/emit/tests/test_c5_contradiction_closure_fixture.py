"""C5: re-emit and diff — proves the 6 named contradictory-status forks close under C4.

Purpose (RAG-bait): AC8 requires the 6 contradictory-status instances named by the
fork-population oracle (`state/audits/2026-08-01-deliverable-id-fork-population.md`) to
resolve to a single ``deliverable_status`` after C4 (the shared canonicalization
read-model) lands — verified by re-emitting and diffing, never by bare assertion.

Three named steps (plan § C5 body), all present in this module:
  1. **Freeze the fixture.** Real source-record field values (deliverable_id,
     deployment_state/status, shipped_sha) transcribed from the actual handoff/plan
     frontmatter for each of the 6 pairs — not invented data, and not a live re-scan of
     the corpus (which mutates constantly and would rot this test within days). Frozen
     at 2026-08-02; see each ``_PAIR_*`` block below for its source path(s).
  2. **Baseline.** Emit (``deliverable_status._compute_map``) over the fixture with an
     EMPTY equivalence map. Per the EM decision recorded in this plan's Execution Notes
     (C5 dispatch brief), an empty map reproduces pre-C4 behaviour exactly — absence of
     an entry canonicalizes every id to itself (`canonicalize`'s own documented
     negative-spec) — so this is a genuine, deterministic stand-in for "emission before
     C4's fix" without checking out old code.
  3. **Post-fix.** Emit over the SAME fixture with the declared map (a frozen transcript
     of the 6 pairs' entries from ``state/deliverable-equivalence.yaml``, C3b) — assert
     each pair collapses to ONE canonical key (the DD#1 winner id) with ONE status.

Spec backlink: docs/plans/2026-08-01-deliverable-id-fork-remediation.md § C5 (AC8)
Evidence: state/audits/2026-08-01-deliverable-id-fork-population.md,
  state/audits/2026-08-01-deliverable-id-fork-population-itemized.md,
  state/deliverable-equivalence.yaml (C3b, commit 4b2506754819)

Negative-spec: this module does NOT edit ``deliverable_status.py``, does NOT read the
live ``state/deliverable-equivalence.yaml`` artifact (its own subset is frozen here so
a future edit to that artifact cannot silently invalidate this proof), and does NOT
touch any live artifact frontmatter — that is C6's surface, which must run strictly
AFTER this chunk (see plan § C5 body: C6 landing first would attribute its own
frontmatter edits to C4's collapse and invalidate this diff).

Scope note (Review: code-reviewer 5230f7ac, Finding 3): this file is the
``deliverable_status.py`` leg of AC8 only — one of ``canonicalize()``'s four consumers.
The plan's C5 Execution Note requires AC8's proof to span every consumer, precisely so
a canonicalization that works in emission but breaks a rollup join cannot ship green off
a single-consumer proof. See ``coordinator_core/ops/tests/test_deliverable_rollup.py``
and ``coordinator_core/ops/fleet/tests/test_migrate_handoff_vocabulary.py`` for the
other legs.
"""

from __future__ import annotations

from coordinator_core.ops.emit import deliverable_status

# --------------------------------------------------------------------------------------
# Step 1: frozen fixture — real source-record field values for the 6 named
# contradictory-status instances, transcribed from their actual frontmatter (paths noted
# per pair). Only the fields _handoff_phase/_plan_phase/_roadmap_phase actually read are
# carried — deliverable_id plus the phase-determining status fields.
# --------------------------------------------------------------------------------------

# --- Pair 1: sat-01 (the oracle's clearest instance) ---
# Winner stub: archive/handoffs/2026-07/2026-07-17_160000_roadmap-sat-01.md
#   deployment_state: continued (continued_into set) -> _handoff_phase = "in-progress"
# Loser plan: docs/plans/2026-07-28-sat-01-sovereign-tracker-substrate.md
#   status: implemented -> _plan_phase = "shipped"
# Loser execution handoff: archive/handoffs/2026-07/2026-07-28-sat-01-sovereign-tracker-substrate.md
#   deployment_state: shipped -> _handoff_phase = "shipped"
_SAT01_WINNER = "dlv-sat-01"
_SAT01_LOSER = "dlv-sat-01-sovereign-tracker-substrate-locke-02c8bc"
_PAIR_SAT01_HANDOFFS = [
    {"deliverable_id": _SAT01_WINNER, "deployment_state": "continued", "shipped_sha": None},
    {"deliverable_id": _SAT01_LOSER, "deployment_state": "shipped", "shipped_sha": None},
]
_PAIR_SAT01_PLANS = [
    {"deliverable_id": _SAT01_LOSER, "status": "implemented", "shipped_sha": None},
]

# --- Pair 2: registration-quad (2 loser legs) ---
# Winner handoff: archive/handoffs/2026-07/2026-07-25_001104_registration-quad-completeness-gate.md
#   deployment_state: continued -> "in-progress"
# Loser plan: docs/plans/2026-07-25-registration-quad-completeness-gate.md
#   status: implemented -> "shipped"
# Loser execution handoff: archive/handoffs/2026-07/2026-07-25-execute-the-registration-quad-completene.md
#   deployment_state: shipped -> "shipped"
_REGQUAD_WINNER = "dlv-registration-quad-completeness-gate-daf4d1"
_REGQUAD_LOSER_PLAN = "dlv-registration-quad-completeness-gate-four-f1eef6"
_REGQUAD_LOSER_HANDOFF = "dlv-execute-the-registration-quad-completene-7c1a94"
_PAIR_REGQUAD_HANDOFFS = [
    {"deliverable_id": _REGQUAD_WINNER, "deployment_state": "continued", "shipped_sha": None},
    {"deliverable_id": _REGQUAD_LOSER_HANDOFF, "deployment_state": "shipped", "shipped_sha": None},
]
_PAIR_REGQUAD_PLANS = [
    {"deliverable_id": _REGQUAD_LOSER_PLAN, "status": "implemented", "shipped_sha": None},
]

# --- Pair 3: structured-sibling-evidence-gates ---
# Winner spinoff handoff: archive/handoffs/2026-07/2026-07-26-structured-sibling-evidence-gates-make-a.md
#   deployment_state: closed, closed_reason: displaced -> "abandoned"
# Loser plan: docs/plans/2026-07-26-structured-sibling-evidence-gates.md
#   status: implemented -> "shipped"
_SIBLING_WINNER = "dlv-structured-sibling-evidence-gates-make-a-60680d"
_SIBLING_LOSER = "dlv-structured-sibling-evidence-gates-machin-fdfba6"
_PAIR_SIBLING_HANDOFFS = [
    {"deliverable_id": _SIBLING_WINNER, "deployment_state": "closed", "shipped_sha": None},
]
_PAIR_SIBLING_PLANS = [
    {"deliverable_id": _SIBLING_LOSER, "status": "implemented", "shipped_sha": None},
]

# --- Pair 4: pickup ("4 ids, 4 statuses" — 1 winner + 3 loser legs) ---
# Winner handoff: state/handoffs/2026-07-24_134006_pickup-skill-code-driven-branch-result.md
#   deployment_state: ready_to_fire -> "in-progress"
# Loser1 plan: docs/plans/2026-07-24-pickup-code-computed-decision-surface.md
#   status: draft -> "proposed"
# Loser2 handoff: state/handoffs/2026-07-24_140030_a3983c55-a9ec-45ee-b7ff-a448b01090c2.md
#   deployment_state: ready_to_fire -> "in-progress"
# Loser3 plan: docs/plans/2026-07-24-pickup-as-a-fully-assembled-decision-sur.md
#   status: implemented -> "shipped"
_PICKUP_WINNER = "dlv-pickup-skill-code-driven-branch-result-acd867"
_PICKUP_LOSER_1 = "dlv-pickup-as-a-code-computed-decision-surfa-3f5b23"
_PICKUP_LOSER_2 = "dlv-pickup-code-computed-decision-surface-pl-7fb6b1"
_PICKUP_LOSER_3 = "dlv-pickup-as-a-fully-assembled-decision-sur-872849"
_PAIR_PICKUP_HANDOFFS = [
    {"deliverable_id": _PICKUP_WINNER, "deployment_state": "ready_to_fire", "shipped_sha": None},
    {"deliverable_id": _PICKUP_LOSER_2, "deployment_state": "ready_to_fire", "shipped_sha": None},
]
_PAIR_PICKUP_PLANS = [
    {"deliverable_id": _PICKUP_LOSER_1, "status": "draft", "shipped_sha": None},
    {"deliverable_id": _PICKUP_LOSER_3, "status": "implemented", "shipped_sha": None},
]

# --- Pair 5: unclaimed-dirty-file-adoption ---
# Winner spinoff handoff: archive/handoffs/2026-07/2026-07-31-auto-commit-unclaimed-file-adoption.md
#   deployment_state: continued -> "in-progress"
# Loser plan: archive/specs/2026-07/2026-07-31-unclaimed-dirty-file-adoption.md
#   status: implemented -> "shipped"
# Loser execution handoff: archive/handoffs/2026-07/2026-07-31-unclaimed-dirty-file-adoption.md
#   deployment_state: shipped -> "shipped"
_UNCLAIMED_WINNER = "dlv-stop-the-session-end-auto-commit-from-ad-d93d7a"
_UNCLAIMED_LOSER = "dlv-stop-compute-scope-adopting-unclaimed-di-2be30d"
_PAIR_UNCLAIMED_HANDOFFS = [
    {"deliverable_id": _UNCLAIMED_WINNER, "deployment_state": "continued", "shipped_sha": None},
    {"deliverable_id": _UNCLAIMED_LOSER, "deployment_state": "shipped", "shipped_sha": None},
]
_PAIR_UNCLAIMED_PLANS = [
    {"deliverable_id": _UNCLAIMED_LOSER, "status": "implemented", "shipped_sha": None},
]

# --- Pair 6: claude-klabauter-driven-ceremony-redesign ---
# Winner spinoff handoff: archive/handoffs/2026-07/2026-07-23_000222_claude_klabauter-driven-ceremony-redesign.md
#   deployment_state: continued -> "in-progress"
# Loser plan: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md
#   status: draft -> "proposed"
_CEREMONY_WINNER = "dlv-claude-klabauter-driven-ceremony-redesign-765da4"
_CEREMONY_LOSER = "dlv-claude-klabauter-driven-ceremony-redesign-engine-o-2d5cc0"
_PAIR_CEREMONY_HANDOFFS = [
    {"deliverable_id": _CEREMONY_WINNER, "deployment_state": "continued", "shipped_sha": None},
]
_PAIR_CEREMONY_PLANS = [
    {"deliverable_id": _CEREMONY_LOSER, "status": "draft", "shipped_sha": None},
]

# The 6 named pairs, each as (label, winner_id, set-of-all-ids-in-the-group).
_ALL_SIX_PAIRS = [
    ("sat-01", _SAT01_WINNER, {_SAT01_WINNER, _SAT01_LOSER}),
    ("registration-quad", _REGQUAD_WINNER, {_REGQUAD_WINNER, _REGQUAD_LOSER_PLAN, _REGQUAD_LOSER_HANDOFF}),
    ("structured-sibling-evidence-gates", _SIBLING_WINNER, {_SIBLING_WINNER, _SIBLING_LOSER}),
    ("pickup", _PICKUP_WINNER, {_PICKUP_WINNER, _PICKUP_LOSER_1, _PICKUP_LOSER_2, _PICKUP_LOSER_3}),
    ("unclaimed-dirty-file-adoption", _UNCLAIMED_WINNER, {_UNCLAIMED_WINNER, _UNCLAIMED_LOSER}),
    ("claude-klabauter-driven-ceremony-redesign", _CEREMONY_WINNER, {_CEREMONY_WINNER, _CEREMONY_LOSER}),
]

_ALL_HANDOFFS = (
    _PAIR_SAT01_HANDOFFS
    + _PAIR_REGQUAD_HANDOFFS
    + _PAIR_SIBLING_HANDOFFS
    + _PAIR_PICKUP_HANDOFFS
    + _PAIR_UNCLAIMED_HANDOFFS
    + _PAIR_CEREMONY_HANDOFFS
)
_ALL_PLANS = (
    _PAIR_SAT01_PLANS
    + _PAIR_REGQUAD_PLANS
    + _PAIR_SIBLING_PLANS
    + _PAIR_PICKUP_PLANS
    + _PAIR_UNCLAIMED_PLANS
    + _PAIR_CEREMONY_PLANS
)
_ALL_ROADMAPS: list[dict] = []

# --------------------------------------------------------------------------------------
# The declared equivalence map subset for these 6 pairs — a frozen transcript of the
# matching entries in state/deliverable-equivalence.yaml (C3b, commit 4b2506754819) at
# the time this fixture was authored. Frozen rather than loaded live so a future edit to
# the real artifact cannot silently invalidate this proof (see module docstring).
# --------------------------------------------------------------------------------------
_DECLARED_EQUIVALENCE_MAP = {
    _SAT01_LOSER: _SAT01_WINNER,
    _REGQUAD_LOSER_PLAN: _REGQUAD_WINNER,
    _REGQUAD_LOSER_HANDOFF: _REGQUAD_WINNER,
    _SIBLING_LOSER: _SIBLING_WINNER,
    _PICKUP_LOSER_1: _PICKUP_WINNER,
    _PICKUP_LOSER_2: _PICKUP_WINNER,
    _PICKUP_LOSER_3: _PICKUP_WINNER,
    _UNCLAIMED_LOSER: _UNCLAIMED_WINNER,
    _CEREMONY_LOSER: _CEREMONY_WINNER,
}


class TestBaselineReproducesContradiction:
    """Step 2: an EMPTY equivalence map is the pre-C4 stand-in (§ module docstring) —
    each of the 6 pairs must show up as MULTIPLE independently-computed groups, at
    least one of which disagrees in status. A baseline that does not reproduce the
    contradiction proves nothing (plan § C5 body's own instruction)."""

    def test_all_six_pairs_split_into_multiple_groups_pre_fix(self) -> None:
        dlv_map = deliverable_status._compute_map(
            _ALL_HANDOFFS, _ALL_PLANS, _ALL_ROADMAPS, equivalence_map={}
        )

        for label, _winner, group_ids in _ALL_SIX_PAIRS:
            present = group_ids & dlv_map.keys()
            assert len(present) > 1, (
                f"{label}: expected multiple independent pre-fix groups, "
                f"found {present} in {dlv_map}"
            )

    def test_sat01_baseline_genuinely_disagrees(self) -> None:
        """sat-01 is the oracle's clearest instance: in-progress under the winner,
        shipped under the loser, simultaneously — assert that disagreement directly."""
        dlv_map = deliverable_status._compute_map(
            _ALL_HANDOFFS, _ALL_PLANS, _ALL_ROADMAPS, equivalence_map={}
        )

        assert dlv_map[_SAT01_WINNER] == "in-progress"
        assert dlv_map[_SAT01_LOSER] == "shipped"
        assert dlv_map[_SAT01_WINNER] != dlv_map[_SAT01_LOSER]

    def test_registration_quad_baseline_genuinely_disagrees(self) -> None:
        dlv_map = deliverable_status._compute_map(
            _ALL_HANDOFFS, _ALL_PLANS, _ALL_ROADMAPS, equivalence_map={}
        )

        assert dlv_map[_REGQUAD_WINNER] == "in-progress"
        assert dlv_map[_REGQUAD_LOSER_PLAN] == "shipped"
        assert dlv_map[_REGQUAD_LOSER_HANDOFF] == "shipped"
        assert dlv_map[_REGQUAD_WINNER] != dlv_map[_REGQUAD_LOSER_PLAN]

    def test_structured_sibling_baseline_genuinely_disagrees(self) -> None:
        dlv_map = deliverable_status._compute_map(
            _ALL_HANDOFFS, _ALL_PLANS, _ALL_ROADMAPS, equivalence_map={}
        )

        assert dlv_map[_SIBLING_WINNER] == "abandoned"
        assert dlv_map[_SIBLING_LOSER] == "shipped"
        assert dlv_map[_SIBLING_WINNER] != dlv_map[_SIBLING_LOSER]

    def test_pickup_baseline_genuinely_disagrees(self) -> None:
        dlv_map = deliverable_status._compute_map(
            _ALL_HANDOFFS, _ALL_PLANS, _ALL_ROADMAPS, equivalence_map={}
        )

        assert dlv_map[_PICKUP_WINNER] == "in-progress"
        assert dlv_map[_PICKUP_LOSER_1] == "proposed"
        assert dlv_map[_PICKUP_LOSER_2] == "in-progress"
        assert dlv_map[_PICKUP_LOSER_3] == "shipped"
        # 4 ids, not all agreeing — the oracle's own "4 ids, 4 statuses" framing.
        assert len({dlv_map[i] for i in (_PICKUP_WINNER, _PICKUP_LOSER_1, _PICKUP_LOSER_2, _PICKUP_LOSER_3)}) > 1

    def test_unclaimed_dirty_file_adoption_baseline_genuinely_disagrees(self) -> None:
        dlv_map = deliverable_status._compute_map(
            _ALL_HANDOFFS, _ALL_PLANS, _ALL_ROADMAPS, equivalence_map={}
        )

        assert dlv_map[_UNCLAIMED_WINNER] == "in-progress"
        assert dlv_map[_UNCLAIMED_LOSER] == "shipped"
        assert dlv_map[_UNCLAIMED_WINNER] != dlv_map[_UNCLAIMED_LOSER]

    def test_claude_klabauter_ceremony_redesign_baseline_genuinely_disagrees(self) -> None:
        dlv_map = deliverable_status._compute_map(
            _ALL_HANDOFFS, _ALL_PLANS, _ALL_ROADMAPS, equivalence_map={}
        )

        assert dlv_map[_CEREMONY_WINNER] == "in-progress"
        assert dlv_map[_CEREMONY_LOSER] == "proposed"
        assert dlv_map[_CEREMONY_WINNER] != dlv_map[_CEREMONY_LOSER]


class TestPostFixCollapsesToOneStatus:
    """Step 3: the declared map (C4's canonicalize(), wired via _compute_map per C4b)
    must collapse each of the 6 pairs to exactly ONE canonical key — the DD#1 winner —
    with exactly ONE status. This is the AC8 assertion: re-emitting over the SAME
    fixture with the fix applied resolves the contradiction, proven by diff against the
    baseline above, not by assertion alone."""

    def test_all_six_pairs_collapse_to_single_winner_key(self) -> None:
        dlv_map = deliverable_status._compute_map(
            _ALL_HANDOFFS, _ALL_PLANS, _ALL_ROADMAPS, equivalence_map=_DECLARED_EQUIVALENCE_MAP
        )

        for label, winner, group_ids in _ALL_SIX_PAIRS:
            present = group_ids & dlv_map.keys()
            assert present == {winner}, (
                f"{label}: expected the group to collapse onto the sole winner key "
                f"{winner!r}, found {present} in {dlv_map}"
            )

    def test_sat01_resolves_to_single_shipped_status(self) -> None:
        dlv_map = deliverable_status._compute_map(
            _ALL_HANDOFFS, _ALL_PLANS, _ALL_ROADMAPS, equivalence_map=_DECLARED_EQUIVALENCE_MAP
        )

        assert set(dlv_map.keys()) & {_SAT01_WINNER, _SAT01_LOSER} == {_SAT01_WINNER}
        assert dlv_map[_SAT01_WINNER] == "shipped"

    def test_registration_quad_resolves_to_single_shipped_status(self) -> None:
        dlv_map = deliverable_status._compute_map(
            _ALL_HANDOFFS, _ALL_PLANS, _ALL_ROADMAPS, equivalence_map=_DECLARED_EQUIVALENCE_MAP
        )

        assert dlv_map[_REGQUAD_WINNER] == "shipped"

    def test_structured_sibling_resolves_to_single_shipped_status(self) -> None:
        dlv_map = deliverable_status._compute_map(
            _ALL_HANDOFFS, _ALL_PLANS, _ALL_ROADMAPS, equivalence_map=_DECLARED_EQUIVALENCE_MAP
        )

        assert dlv_map[_SIBLING_WINNER] == "shipped"

    def test_pickup_resolves_to_single_shipped_status(self) -> None:
        dlv_map = deliverable_status._compute_map(
            _ALL_HANDOFFS, _ALL_PLANS, _ALL_ROADMAPS, equivalence_map=_DECLARED_EQUIVALENCE_MAP
        )

        assert dlv_map[_PICKUP_WINNER] == "shipped"

    def test_unclaimed_dirty_file_adoption_resolves_to_single_shipped_status(self) -> None:
        dlv_map = deliverable_status._compute_map(
            _ALL_HANDOFFS, _ALL_PLANS, _ALL_ROADMAPS, equivalence_map=_DECLARED_EQUIVALENCE_MAP
        )

        assert dlv_map[_UNCLAIMED_WINNER] == "shipped"

    def test_claude_klabauter_ceremony_redesign_resolves_to_single_in_progress_status(self) -> None:
        dlv_map = deliverable_status._compute_map(
            _ALL_HANDOFFS, _ALL_PLANS, _ALL_ROADMAPS, equivalence_map=_DECLARED_EQUIVALENCE_MAP
        )

        assert dlv_map[_CEREMONY_WINNER] == "in-progress"
