"""
coordinator_core.reconcile.tests.test_gate_eval — C3 unified gate evaluator fixtures.

Spec backlink: pln-claude-klabauter-auto-reconcile-pass-off-425848 § C3

Covers the plan's required scenario matrix:
  - all blocked_by shipped -> clear
  - one-of-two shipped -> narrow (NOT clear) — the tc-4 regression guard
  - a blocked_by member abandoned -> surface
  - mixed blocked_by:[shipped, abandoned] -> narrow-mutates to [abandoned] AND
    carries a surface signal (the Staff Engineer F1 composite)
  - asymmetric blocks/blocked_by graph -> fail-loud (surface)
  - prose gate (non-roadmap) resolvable -> clear
  - prose gate unresolvable -> surface
  - prose gate resolving to >1 candidate witness -> surface

Also covers the 2026-07-20 claude-central-em false-positive memo, Defect 1:
  - symmetric blocks/blocked_by graph across an `id`-vs-`stub_id` namespace
    mismatch (the handoff-under-evaluation carries only a path-stem `id`, the
    blocker's `blocks:[...]` list names the durable `stub_id`) -> must NOT
    surface as asymmetry.
  - a genuinely dangling (unresolvable) `blocked_by` ref -> must surface with
    a distinct evidence line, not silently fall through to `not-cleared`.

C3 gate_evidence extension (docs/plans/2026-07-26-structured-sibling-
evidence-gates.md § C3) additionally covers the D2 precedence matrix
(prose-only, evidence-only, both-with-covers_prose, both-without-
covers_prose, neither), the gate_evidence AND-reduce (any indeterminate leg
wins, a `human` leg is permanently indeterminate), and `kind: deadline`'s
distinct `review-due` status excluded from the AND-reduce toward `freed`
(D3a).

C5 (`continued_into` terminus-chase) coverage, modeled on the REAL
`lifecycle-vocab` corpus shape (`coordinator-claude/archive/handoffs/2026-07/
2026-07-08_144205_roadmap-lvv-05.md`, `state/handoffs/2026-07-08_144206_
roadmap-lvv-06.md`, and the dr084 successor `archive/handoffs/2026-07/
2026-07-22_152437_dr084-skill-layer-dual-read.md`) rather than a synthetic
shape — this is the exact case the PM's manual gate-drain caught in
`1faa5047` (2026-07-26): lvv-05 is `deployment_state: continued`,
`continued_into: hnd-dual-read-claimed-by-consumed--7e3c06`; at drain time its
continuation was still open, so lvv-06/lvv-07/lvv-08 (all `blocked_by:
[lvv-05, ...]`, and lvv-05's own `blocks: [lvv-06, lvv-07, lvv-08]`) must NOT
clear. Covers:
  - terminus still open -> narrow+surface composite, never clear (the named
    lvv-05/lvv-06 case).
  - terminus genuinely `shipped` -> the edge clears; evidence names BOTH hops
    (blocker id + terminus id) while `cleared_by_shas`/`cleared_blocker_ids`
    stay 1:1-paired against the ORIGINAL blocked_by id, never the terminus.
  - chain exceeds the depth cap -> surface, does not loop.
  - chain cycles -> surface, reusing the visited-set-guard discipline (a
    chase-scoped guard, distinct from `_evaluate_structured_gate`'s own
    sibling-edge visited set).
  - `continued_into` as a `state/handoffs/...` path (pre-existing successors
    carry no handoff_id) resolved via basename match against the caller's
    already-collected index — never re-read off disk.
  - an unresolvable `continued_into` (handoff_id or path) -> surface, never a
    clear.
  - the asymmetry check (`_has_asymmetry`) is NOT tripped by a chased
    terminus lacking any `blocks:[...]` back-reference to the original
    dependent — only the NAMED blocker's own `blocks:` is consulted.

Spec backlink: pln-structured-sibling-evidence-ga-6e2ceb § C3
"""

from __future__ import annotations

import pytest

from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT

from coordinator_core.reconcile.gate_eval import (
    consumes_gate_evidence,
    evaluate_gate,
    evaluate_gate_triage,
)


def _roadmap_handoff(handoff_id: str, blocked_by, blocks=None) -> dict:
    # Sets BOTH `id` and `handoff_id` to the same durable `hnd-...` value: the
    # collector only ever synthesizes `id` from a path stem (never `hnd-...`
    # shaped) in production, so an `hnd-...` fixture id must also populate the
    # `handoff_id` field or C2c's prefix-discriminated index misses it entirely
    # (routes to the `handoff_id` sub-index, finds nothing there).
    return {
        "id": handoff_id,
        "handoff_id": handoff_id,
        "kind": "spinoff-roadmap",
        "deployment_state": "awaiting_gate",
        "blocked_by": blocked_by,
        "blocks": blocks or [],
    }


def _blocker(handoff_id: str, deployment_state: str, shipped_in=None, blocks=None) -> dict:
    d = {
        "id": handoff_id,
        "handoff_id": handoff_id,
        "kind": "spinoff-roadmap",
        "deployment_state": deployment_state,
        "blocks": blocks or [],
    }
    if shipped_in:
        d["shipped_in"] = shipped_in
    return d


class TestAllShippedClear:
    def test_all_blocked_by_shipped_clears(self) -> None:
        dependent = _roadmap_handoff("hnd-dep-000001", blocked_by=["hnd-tc1-000001"])
        tc1 = _blocker("hnd-tc1-000001", "shipped", shipped_in="a" * 40, blocks=["hnd-dep-000001"])

        result = evaluate_gate(dependent, [dependent, tc1])

        assert result["verdict"] == "clear"
        assert result["cleared_by_shas"] == ["a" * 40]
        assert result["remaining_blockers"] == []
        assert result["also_surface"] is False


class TestPartialSatisfactionNarrows:
    def test_one_of_two_shipped_narrows_not_clears(self) -> None:
        # tc-4 regression guard: blocked_by:[tc-1, tc-5], only tc-1 shipped.
        dependent = _roadmap_handoff(
            "hnd-tc4-000001", blocked_by=["hnd-tc1-000001", "hnd-tc5-000001"]
        )
        tc1 = _blocker("hnd-tc1-000001", "shipped", shipped_in="b" * 40, blocks=["hnd-tc4-000001"])
        tc5 = _blocker("hnd-tc5-000001", "awaiting_gate", blocks=["hnd-tc4-000001"])

        result = evaluate_gate(dependent, [dependent, tc1, tc5])

        assert result["verdict"] == "narrow"
        assert result["verdict"] != "clear"
        assert result["cleared_by_shas"] == ["b" * 40]
        assert result["remaining_blockers"] == ["hnd-tc5-000001"]
        assert result["also_surface"] is False


class TestAbandonedBlockerSurfaces:
    def test_single_abandoned_blocker_surfaces(self) -> None:
        dependent = _roadmap_handoff("hnd-dep-000002", blocked_by=["hnd-dead-000001"])
        dead = _blocker("hnd-dead-000001", "abandoned", blocks=["hnd-dep-000002"])

        result = evaluate_gate(dependent, [dependent, dead])

        assert result["verdict"] == "surface"
        assert result["cleared_by_shas"] == []
        assert result["remaining_blockers"] == ["hnd-dead-000001"]


class TestAbandonedWithStillOpenNoShippedSurfaces:
    """Slice-A review Finding 3 (P2): blocked_by:[abandoned, still_open] with NO
    shipped member must return `surface`, not `narrow` — there is no edge to
    actually narrow (cleared_by_shas would be empty)."""

    def test_abandoned_and_still_open_no_shipped_surfaces_not_narrow(self) -> None:
        dependent = _roadmap_handoff(
            "hnd-dep-000009", blocked_by=["hnd-dead-000009", "hnd-open-000009"]
        )
        dead = _blocker("hnd-dead-000009", "abandoned", blocks=["hnd-dep-000009"])
        still_open = _blocker("hnd-open-000009", "in_flight", blocks=["hnd-dep-000009"])

        result = evaluate_gate(dependent, [dependent, dead, still_open])

        assert result["verdict"] == "surface"
        assert result["cleared_by_shas"] == []
        assert set(result["remaining_blockers"]) == {"hnd-dead-000009", "hnd-open-000009"}
        assert result["also_surface"] is False


class TestMixedShippedAbandonedComposite:
    def test_mixed_blocked_by_narrows_to_abandoned_and_surfaces(self) -> None:
        dependent = _roadmap_handoff(
            "hnd-dep-000003", blocked_by=["hnd-tc1-000002", "hnd-dead-000002"]
        )
        tc1 = _blocker("hnd-tc1-000002", "shipped", shipped_in="c" * 40, blocks=["hnd-dep-000003"])
        dead = _blocker("hnd-dead-000002", "abandoned", blocks=["hnd-dep-000003"])

        result = evaluate_gate(dependent, [dependent, tc1, dead])

        assert result["verdict"] == "narrow"
        assert result["remaining_blockers"] == ["hnd-dead-000002"]
        assert result["cleared_by_shas"] == ["c" * 40]
        assert result["also_surface"] is True


class TestAsymmetricGraphFailsLoud:
    def test_blocks_blocked_by_asymmetry_surfaces(self) -> None:
        dependent = _roadmap_handoff("hnd-dep-000004", blocked_by=["hnd-tc1-000003"])
        # tc1 does NOT list hnd-dep-000004 in its own `blocks` -> asymmetry.
        tc1 = _blocker("hnd-tc1-000003", "shipped", shipped_in="d" * 40, blocks=[])

        result = evaluate_gate(dependent, [dependent, tc1])

        assert result["verdict"] == "surface"
        assert result["cleared_by_shas"] == []
        assert "asymmetry" in result["evidence"][0]


class TestProseGateResolvable:
    def test_prose_gate_resolvable_clears(self) -> None:
        handoff = {
            "id": "hnd-prose-000001",
            "kind": "session-handoff",
            "deployment_state": "awaiting_gate",
            "gate_dependency": "widget-engine-migration",
        }
        witness = {
            "id": "hnd-widget-000001",
            "deployment_state": "shipped",
            "shipped_in": "e" * 40,
        }

        result = evaluate_gate(handoff, [handoff], witness_candidates=[witness])

        assert result["verdict"] == "clear"
        assert result["cleared_by_shas"] == ["e" * 40]


class TestProseGateUnresolvable:
    def test_prose_gate_with_no_witness_surfaces(self) -> None:
        handoff = {
            "id": "hnd-prose-000002",
            "kind": "session-handoff",
            "deployment_state": "awaiting_gate",
            "gate_dependency": "some-vague-subsystem",
        }

        result = evaluate_gate(handoff, [handoff], witness_candidates=[])

        assert result["verdict"] == "surface"
        assert result["remaining_blockers"] == []


class TestProseGateAmbiguousCandidates:
    def test_prose_gate_multiple_candidates_surfaces(self) -> None:
        handoff = {
            "id": "hnd-prose-000003",
            "kind": "session-handoff",
            "deployment_state": "awaiting_gate",
            "gate_dependency": "engine-migration",
        }
        witness_a = {"id": "hnd-a-000001", "deployment_state": "shipped", "shipped_in": "f" * 40}
        witness_b = {"id": "hnd-b-000001", "deployment_state": "awaiting_gate"}

        result = evaluate_gate(handoff, [handoff], witness_candidates=[witness_a, witness_b])

        assert result["verdict"] == "surface"
        assert result["cleared_by_shas"] == []
        assert "ambiguous" in result["evidence"][0]


class TestEmptyBlockedByNoProseVacuouslyClears:
    """C4 reconciliation: this previously asserted `verdict == "surface"`
    (old behavior — empty `blocked_by` fell through to the PROSE fallback
    path, which surfaces on zero witness_candidates). `evaluate_gate` now
    routes an empty-`blocked_by`-and-no-prose handoff through the SAME
    vacuous-`clear` branch `evaluate_gate_triage` already used (for-all over
    an empty set), rather than the prose fallback — updated deliberately per
    the C4 dispatch brief, not a silent premise change: this is also the
    fix for a spinoff with `predecessor`/`origin_*` lineage fields and
    nothing else gating it (see module docstring "LINEAGE IS NOT GATING")."""

    def test_roadmap_handoff_with_empty_blocked_by_and_no_prose_clears(self) -> None:
        handoff = {
            "id": "hnd-empty-000001",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
        }

        result = evaluate_gate(handoff, [handoff], witness_candidates=[])

        assert result["verdict"] == "clear"
        assert result["remaining_blockers"] == []


class TestDanglingBlockedByRefSurfaces:
    """2026-07-20 claude-central-em false-positive memo, Defect 1 recommendation:
    an unresolvable `blocked_by` id is a genuine data defect (a dangling ref),
    not a benign steady state. `not-cleared` is deliberately NOT surfaced by
    `handoff_reconcile.py` — falling through to it here would silently swallow
    a real problem. Superseded assertion: this previously asserted
    `verdict == "not-cleared"`; updated deliberately per the memo."""

    def test_unresolvable_blocker_id_now_surfaces_with_dangling_evidence(self) -> None:
        dependent = _roadmap_handoff("hnd-dep-000005", blocked_by=["hnd-ghost-000001"])

        result = evaluate_gate(dependent, [dependent])

        assert result["verdict"] == "surface"
        assert result["remaining_blockers"] == ["hnd-ghost-000001"]
        assert any("dangling" in e for e in result["evidence"])


class TestPlaceholderShapedBlockedByDoesNotResolve:
    """Review: code-reviewer (Finding 1, P1) — `_HANDOFF_ID_PATTERN` is the
    actual runtime matcher deciding whether a `blocked_by` id resolves against
    a live `handoff_id`; `gate_eval.py` never calls schema validation, so the
    `handoff.schema.json` placeholder-id narrow alone does not stop a
    placeholder-shaped id from being indexed and resolved here. A
    scaffold-minted, well-formed-looking `hnd-placeholder-replace-with-...`
    blocker must surface as dangling, never silently resolve/clear."""

    def test_placeholder_shaped_blocker_id_surfaces_as_dangling_not_clear(self) -> None:
        # Blocker is a real, `shipped` handoff on disk — but its id is
        # placeholder-shaped, so `_HANDOFF_ID_PATTERN` refuses to index it at
        # all (same as a genuinely absent/never-existed blocker id from the
        # resolver's point of view): this is what makes the id "dangling"
        # despite the underlying handoff being real and terminal.
        placeholder_id = "hnd-placeholder-replace-with-one-l-5f04ba"
        dependent = _roadmap_handoff("hnd-dep-000005", blocked_by=[placeholder_id])

        result = evaluate_gate(dependent, [dependent])

        assert result["verdict"] == "surface"
        assert result["remaining_blockers"] == [placeholder_id]
        assert any("dangling" in e for e in result["evidence"])


class TestNamespaceMismatchSymmetricGraphDoesNotSurface:
    """2026-07-20 claude-central-em false-positive memo, Defect 1: the handoff
    under evaluation carries a path-stem `id` (injected by `_collect_open_handoffs`
    when the roadmap stub's frontmatter has no `id:` field of its own) alongside
    its durable `stub_id`; the blocker's `blocks:[...]` list names the durable
    `stub_id`, not the path-stem id. This symmetric graph must NOT surface as
    asymmetry — the previous single-key comparison fired on every such edge."""

    def test_stub_id_vs_path_stem_id_symmetric_graph_does_not_surface(self) -> None:
        dependent = {
            "id": "2026-07-17_160001_roadmap-sat-02",
            "stub_id": "sat-02",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": ["sat-01"],
            "blocks": [],
        }
        blocker = {
            "id": "sat-01",
            "stub_id": "sat-01",
            "kind": "spinoff-roadmap",
            "deployment_state": "shipped",
            "shipped_in": "a" * 40,
            "blocks": ["sat-02"],
        }

        result = evaluate_gate(dependent, [dependent, blocker])

        assert result["verdict"] == "clear"
        assert result["cleared_by_shas"] == ["a" * 40]


class TestNarrowWithUnresolvedIdAlsoSurfaces:
    """Defect 1 narrow-path parity: a `narrow` verdict whose remaining_blockers
    includes a dangling (unresolvable) id must ALSO carry `also_surface=True`,
    mirroring the abandoned-id composite (TestMixedShippedAbandonedComposite)."""

    def test_shipped_plus_unresolved_narrows_and_also_surfaces(self) -> None:
        dependent = _roadmap_handoff(
            "hnd-dep-000010", blocked_by=["hnd-tc1-000010", "hnd-ghost-000010"]
        )
        tc1 = _blocker("hnd-tc1-000010", "shipped", shipped_in="9" * 40, blocks=["hnd-dep-000010"])

        result = evaluate_gate(dependent, [dependent, tc1])

        assert result["verdict"] == "narrow"
        assert result["remaining_blockers"] == ["hnd-ghost-000010"]
        assert result["cleared_by_shas"] == ["9" * 40]
        assert result["also_surface"] is True


class TestScanIncompleteUnresolvedDoesNotClearNamesScanGap:
    """2026-07-22 fail-open close: `handoff_reconcile.py`'s `_collect_all_handoffs_for_gate_index`
    (94d8251f) surfaces `scan_incomplete`/`scan_errors` when the archive/handoffs/
    subtree behind the gate index couldn't be fully scanned. An unresolvable
    `blocked_by` id under that condition must still NOT clear — it stays surfaced —
    but the evidence must name the scan gap rather than assert a confirmed dangling
    ref, since the id may simply live under the unscanned subtree."""

    def test_scan_incomplete_unresolvable_blocker_surfaces_with_scan_gap_reason(self) -> None:
        dependent = _roadmap_handoff("hnd-dep-000011", blocked_by=["hnd-ghost-000011"])

        result = evaluate_gate(
            dependent,
            [dependent],
            scan_incomplete=True,
            scan_errors=["archive/handoffs/2026-06: PermissionError"],
        )

        assert result["verdict"] == "surface"
        assert result["cleared_by_shas"] == []
        assert result["remaining_blockers"] == ["hnd-ghost-000011"]
        assert any("scan" in e and "archive/handoffs/2026-06" in e for e in result["evidence"])
        assert not any("dangling" in e for e in result["evidence"])


class TestScanIncompletePositivelyResolvedStillClears:
    """Positive resolution off a partial index is not diminished by
    `scan_incomplete` — finding a shipped blocker proves it exists regardless of
    what else the scan missed."""

    def test_scan_incomplete_all_shipped_still_clears(self) -> None:
        dependent = _roadmap_handoff("hnd-dep-000012", blocked_by=["hnd-tc1-000012"])
        tc1 = _blocker("hnd-tc1-000012", "shipped", shipped_in="1" * 40, blocks=["hnd-dep-000012"])

        result = evaluate_gate(
            dependent,
            [dependent, tc1],
            scan_incomplete=True,
            scan_errors=["archive/handoffs/2026-05: PermissionError"],
        )

        assert result["verdict"] == "clear"
        assert result["cleared_by_shas"] == ["1" * 40]
        assert result["remaining_blockers"] == []
        assert result["also_surface"] is False


class TestScanCompleteBaselineUnchanged:
    """scan_incomplete defaults to False — the pre-existing dangling-ref framing
    (data defect, not a scan gap) is unchanged when the caller doesn't pass it."""

    def test_scan_complete_unresolvable_blocker_keeps_dangling_reason(self) -> None:
        dependent = _roadmap_handoff("hnd-dep-000013", blocked_by=["hnd-ghost-000013"])

        result = evaluate_gate(dependent, [dependent])

        assert result["verdict"] == "surface"
        assert result["remaining_blockers"] == ["hnd-ghost-000013"]
        assert any("dangling" in e for e in result["evidence"])


class TestCycleGuard:
    def test_self_referential_blocked_by_does_not_infinite_loop(self) -> None:
        dependent = _roadmap_handoff("hnd-cyc-000001", blocked_by=["hnd-cyc-000001"])

        result = evaluate_gate(dependent, [dependent])

        # Bounded one-level walk with a visited-set guard; self-reference resolves
        # to itself (not shipped) rather than recursing — still-not-cleared, not a
        # crash or infinite loop.
        assert result["verdict"] in {"not-cleared", "surface"}


# ---------------------------------------------------------------------------
# C4 — reconciliation: evaluate_gate now shares _is_structured_gate with
# evaluate_gate_triage, widened to ANY kind, with prose-dominance parity.
# ---------------------------------------------------------------------------


class TestWidenedEligibilityNonRoadmapKindClears:
    """AC5, scenario 1: a `kind: spinoff` baton (NOT `spinoff-roadmap`) with a
    shipped blocker CLEARS — the widened, kind-independent eligibility."""

    def test_non_roadmap_kind_with_shipped_blocker_clears(self) -> None:
        dependent = {
            "id": "hnd-widen-000001",
            "handoff_id": "hnd-widen-000001",
            "kind": "spinoff",
            "deployment_state": "awaiting_gate",
            "blocked_by": ["hnd-widen-blocker-000001"],
            "blocks": [],
        }
        blocker = {
            "id": "hnd-widen-blocker-000001",
            "handoff_id": "hnd-widen-blocker-000001",
            "kind": "spinoff",
            "deployment_state": "shipped",
            "shipped_in": "1" * 40,
        }

        result = evaluate_gate(dependent, [dependent, blocker])

        assert result["verdict"] == "clear"
        assert result["cleared_by_shas"] == ["1" * 40]
        assert result["remaining_blockers"] == []


class TestProseDominanceNeverClearsMutatingPath:
    """AC5, scenario 2: a baton with BOTH a non-empty `blocked_by` AND a
    non-empty `gate_dependency` does NOT clear even with all structured
    members shipped — reconciled parity with `evaluate_gate_triage`'s own
    precedence rule (strang-03 shape: the prose gate is the real
    precondition, `blocked_by` merely tracks sibling pattern-proofs)."""

    def test_both_blocked_by_and_gate_dependency_all_shipped_does_not_clear(self) -> None:
        dependent = _roadmap_handoff(
            "hnd-dominance-000001", blocked_by=["hnd-tc1-dominance-000001"]
        )
        dependent["gate_dependency"] = "claude-klabauter action layer live; cutover landed"
        tc1 = _blocker(
            "hnd-tc1-dominance-000001", "shipped", shipped_in="2" * 40,
            blocks=["hnd-dominance-000001"],
        )

        result = evaluate_gate(dependent, [dependent, tc1])

        assert result["verdict"] != "clear"
        assert result["verdict"] == "surface"
        assert result["cleared_by_shas"] == []


class TestBlockingNotesNoLongerDominatesSatisfiedStructuredSet:
    """C4 (docs/plans/2026-08-03-gate-dependency-template-emission-spec.md
    § C4, AC4.1): demoted per the schema's own declared `blocking_notes`
    contract ("Advisory prose, NEVER read by the resolver — inert by
    construction") — a non-empty `blocking_notes` (with NO `gate_dependency`)
    no longer overrides a SATISFIED structured `blocked_by` graph. When every
    structured member is shipped, the structured walk's own verdict (`clear`)
    stands regardless of `blocking_notes`. This flips the prior
    `TestBlockingNotesDominatesSatisfiedStructuredSet` expectation — that IS
    the C4 fix (today's `surface` was itself the defect)."""

    def test_blocking_notes_present_no_gate_dependency_all_shipped_clears(self) -> None:
        dependent = _roadmap_handoff(
            "hnd-blocking-notes-000001", blocked_by=["hnd-tc1-blocking-notes-000001"]
        )
        dependent["blocking_notes"] = "Windows machine required for AC7 verification"
        tc1 = _blocker(
            "hnd-tc1-blocking-notes-000001", "shipped", shipped_in="3" * 40,
            blocks=["hnd-blocking-notes-000001"],
        )

        result = evaluate_gate(dependent, [dependent, tc1])

        assert result["verdict"] == "clear"
        assert result["cleared_by_shas"] == ["3" * 40]


class TestBlockingNotesDominatesVacuousEmptyBlockedBy:
    """The Windows-box regression shape this chunk exists to fix: `awaiting_
    gate`, `blocked_by: []`, non-empty `blocking_notes` -> NOT clear. Before
    this fix, the vacuous-empty branch checked only `gate_dependency` and
    silently cleared a real, unmet, human-checkable gate."""

    def test_empty_blocked_by_with_blocking_notes_does_not_vacuously_clear(self) -> None:
        handoff = {
            "id": "hnd-windows-gate-000001",
            "handoff_id": "hnd-windows-gate-000001",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
            "blocking_notes": (
                "Windows machine required for AC7 verification — no baton, "
                "advisory only"
            ),
        }

        result = evaluate_gate(handoff, [handoff])

        assert result["verdict"] != "clear"
        assert result["verdict"] == "surface"


class TestEmptyBlockedByEmptyBlockingNotesStillVacuouslyClears:
    """Guard against overcorrection: the change must not make every ungated
    baton sticky — an empty (or absent) `blocking_notes` alongside an empty
    `blocked_by` and no `gate_dependency` still vacuously clears."""

    def test_empty_blocked_by_absent_blocking_notes_clears(self) -> None:
        handoff = {
            "id": "hnd-vacuous-blocking-notes-000001",
            "handoff_id": "hnd-vacuous-blocking-notes-000001",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
        }

        result = evaluate_gate(handoff, [handoff])

        assert result["verdict"] == "clear"

    def test_empty_blocked_by_empty_string_blocking_notes_clears(self) -> None:
        handoff = {
            "id": "hnd-vacuous-blocking-notes-000002",
            "handoff_id": "hnd-vacuous-blocking-notes-000002",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
            "blocking_notes": "",
        }

        result = evaluate_gate(handoff, [handoff])

        assert result["verdict"] == "clear"


class TestWhitespaceOnlyBlockingNotesIsEmptyNotAGate:
    """A whitespace-only `blocking_notes` must not park a baton forever —
    it is treated as empty, exactly as `gate_dependency` already is."""

    def test_whitespace_only_blocking_notes_still_vacuously_clears(self) -> None:
        handoff = {
            "id": "hnd-whitespace-blocking-notes-000001",
            "handoff_id": "hnd-whitespace-blocking-notes-000001",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
            "blocking_notes": "   ",
        }

        result = evaluate_gate(handoff, [handoff])

        assert result["verdict"] == "clear"


class TestAsymmetryGuardSkipsNonRoadmapKindBlocker:
    """Blast-radius fix: `_has_asymmetry` assumed every blocker authors a
    `blocks:` back-reference (a roadmap-kind convention). Widening
    eligibility to ANY kind means a non-roadmap-kind blocker that never
    adopted that convention must NOT fire a false-positive asymmetry."""

    def test_non_roadmap_kind_blocker_with_no_blocks_field_does_not_surface(self) -> None:
        dependent = {
            "id": "hnd-asym-guard-000001",
            "handoff_id": "hnd-asym-guard-000001",
            "kind": "spinoff",
            "deployment_state": "awaiting_gate",
            "blocked_by": ["hnd-asym-guard-blocker-000001"],
            "blocks": [],
        }
        # Blocker is a non-roadmap kind and carries NO `blocks:` field at all
        # (the realistic shape for a kind that never adopted the convention).
        blocker = {
            "id": "hnd-asym-guard-blocker-000001",
            "handoff_id": "hnd-asym-guard-blocker-000001",
            "kind": "session-handoff",
            "deployment_state": "shipped",
            "shipped_in": "4" * 40,
        }

        result = evaluate_gate(dependent, [dependent, blocker])

        assert result["verdict"] == "clear"
        assert result["cleared_by_shas"] == ["4" * 40]


# ---------------------------------------------------------------------------
# evaluate_gate_triage — freed / still-blocked / indeterminate three-way,
# added for the stale-awaiting_gate batch-audit use case.
# ---------------------------------------------------------------------------

#: The "dead" (terminal-but-not-shipped) states, derived from the SAME
#: schema-backed enum `gate_eval.py` itself reads — HANDOFF_TERMINAL_DEPLOYMENT
#: minus "shipped". Iterating this set (rather than hard-coding
#: "abandoned"/"continued"/"closed" literals) means a future enum widening is
#: automatically exercised by these tests, not silently missed.
_DEAD_STATES = sorted(HANDOFF_TERMINAL_DEPLOYMENT - {"shipped"})


class TestTriageAllShippedFreed:
    def test_all_blocked_by_shipped_is_freed(self) -> None:
        dependent = _roadmap_handoff("hnd-tri-000001", blocked_by=["hnd-b1-000001"])
        b1 = _blocker("hnd-b1-000001", "shipped", shipped_in="a" * 40, blocks=["hnd-tri-000001"])

        result = evaluate_gate_triage(dependent, [dependent, b1])

        assert result["status"] == "freed"
        assert result["shipped_ids"] == ["hnd-b1-000001"]
        assert result["still_open_ids"] == []
        assert result["dead_ids"] == []
        assert result["unresolved_ids"] == []
        assert result["has_prose_gate"] is False


class TestTriageEmptyBlockedByVacuouslyFreed:
    def test_empty_blocked_by_no_prose_is_freed(self) -> None:
        handoff = _roadmap_handoff("hnd-tri-000002", blocked_by=[])

        result = evaluate_gate_triage(handoff, [handoff])

        assert result["status"] == "freed"
        assert "vacuous" in result["reason"]
        assert result["blocked_by"] == []


class TestTriagePartialSatisfactionStillBlocked:
    def test_one_of_two_shipped_is_still_blocked_not_freed(self) -> None:
        dependent = _roadmap_handoff(
            "hnd-tri-000003", blocked_by=["hnd-b1-000003", "hnd-b2-000003"]
        )
        b1 = _blocker("hnd-b1-000003", "shipped", shipped_in="b" * 40, blocks=["hnd-tri-000003"])
        b2 = _blocker("hnd-b2-000003", "awaiting_gate", blocks=["hnd-tri-000003"])

        result = evaluate_gate_triage(dependent, [dependent, b1, b2])

        assert result["status"] == "still-blocked"
        assert result["shipped_ids"] == ["hnd-b1-000003"]
        assert result["still_open_ids"] == ["hnd-b2-000003"]


class TestTriageBlockerItselfAwaitingGateStillBlocked:
    """Explicit dispatch-brief case: a blocker that is itself still
    awaiting_gate — genuinely still in the pipeline, not a data anomaly."""

    def test_blocker_awaiting_gate_is_still_blocked(self) -> None:
        dependent = _roadmap_handoff("hnd-tri-000004", blocked_by=["hnd-b1-000004"])
        b1 = _blocker("hnd-b1-000004", "awaiting_gate", blocks=["hnd-tri-000004"])

        result = evaluate_gate_triage(dependent, [dependent, b1])

        assert result["status"] == "still-blocked"
        assert result["still_open_ids"] == ["hnd-b1-000004"]


class TestTriageUnresolvableBlockerIndeterminate:
    """Explicit dispatch-brief case: a blocker id that exists nowhere in the
    live+archived index — the machine cannot confirm shipped-ness, so this is
    indeterminate, never freed and never confidently still-blocked."""

    def test_unresolvable_blocker_is_indeterminate(self) -> None:
        dependent = _roadmap_handoff("hnd-tri-000005", blocked_by=["hnd-ghost-000005"])

        result = evaluate_gate_triage(dependent, [dependent])

        assert result["status"] == "indeterminate"
        assert result["unresolved_ids"] == ["hnd-ghost-000005"]
        assert any("resolve" in result["reason"] or "resolved" in result["reason"] for _ in [0])


class TestTriageDeadBlockerIndeterminate:
    """Every terminal-but-not-shipped state (abandoned/continued/closed, derived
    from the schema-backed enum) makes the dependent indeterminate, never
    freed — the dependent's premise may be moot and needs a human look."""

    def test_dead_blocker_states_are_indeterminate(self) -> None:
        for dead_state in _DEAD_STATES:
            dependent = _roadmap_handoff(
                f"hnd-tri-dead-{dead_state}", blocked_by=[f"hnd-b-{dead_state}"]
            )
            blocker = _blocker(
                f"hnd-b-{dead_state}", dead_state, blocks=[f"hnd-tri-dead-{dead_state}"]
            )

            result = evaluate_gate_triage(dependent, [dependent, blocker])

            assert result["status"] == "indeterminate", dead_state
            assert result["dead_ids"] == [f"hnd-b-{dead_state}"], dead_state


class TestTriageAsymmetryIndeterminate:
    def test_blocks_blocked_by_asymmetry_is_indeterminate(self) -> None:
        dependent = _roadmap_handoff("hnd-tri-000006", blocked_by=["hnd-b1-000006"])
        # b1 does NOT list hnd-tri-000006 in its own `blocks` -> asymmetry.
        b1 = _blocker("hnd-b1-000006", "shipped", shipped_in="c" * 40, blocks=[])

        result = evaluate_gate_triage(dependent, [dependent, b1])

        assert result["status"] == "indeterminate"
        assert "asymmetry" in result["reason"]


class TestTriageProseGateDominanceReroutesToReviewDueWhenStructuredAllShipped:
    """C2 (docs/plans/2026-08-03-gate-dependency-template-emission-spec.md
    § C2 chunk, CONTRADICTION CARVE-OUT): a handoff carrying BOTH blocked_by
    AND a prose gate_dependency, with EVERY structured blocked_by member
    shipped, now re-routes onto `review-due` — NOT `indeterminate`. This
    flips the prior expectation — that IS the C2 fix: before it, this exact
    shape (the strang-03/sat-07 corpus case, prose stale under an
    all-shipped structured graph) was indistinguishable from an ordinary
    unresolved prose gate, forcing a human to re-discover the staleness by
    hand on every triage pass instead of it surfacing as actionable. `status`
    is still never `freed` — a human still decides whether the stale prose
    is safe to retire (`handoff.transition gate-recheck --cleared`); only
    the signal strength changes, from "nothing to see" to "this needs a
    look." See `evaluate_gate`'s own `contradiction` key (module docstring
    "C1") for the mutating-path twin of this same re-route."""

    def test_prose_gate_present_with_all_structured_shipped_is_review_due(self) -> None:
        dependent = {
            "id": "hnd-tri-000007",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "gate_dependency": "claude-klabauter action layer live; cutover landed",
            "blocked_by": ["hnd-b1-000007"],
            "blocks": [],
        }
        b1 = _blocker("hnd-b1-000007", "shipped", shipped_in="d" * 40, blocks=["hnd-tri-000007"])

        result = evaluate_gate_triage(dependent, [dependent, b1])

        assert result["status"] == "review-due"
        assert result["has_prose_gate"] is True
        # Structured classification is never even reached — shipped_ids stays
        # the base-case empty list, not ["hnd-b1-000007"], demonstrating the
        # precedence check runs BEFORE the structured walk.
        # Review: code-reviewer (Finding 2, nit) — restored, still true.
        assert result["shipped_ids"] == []


class TestTriageProseOnlyIndeterminate:
    """A handoff whose gate is ONLY expressed as free-text gate_dependency
    (non-roadmap kind, or roadmap kind with empty blocked_by) is
    indeterminate by construction — never parsed with keyword heuristics."""

    def test_prose_only_non_roadmap_handoff_is_indeterminate(self) -> None:
        handoff = {
            "id": "hnd-tri-000008",
            "kind": "session-handoff",
            "deployment_state": "awaiting_gate",
            "gate_dependency": "widget-engine-migration",
        }

        result = evaluate_gate_triage(handoff, [handoff])

        assert result["status"] == "indeterminate"
        assert result["has_prose_gate"] is True


class TestTriageEmptyBlockedByWithProseIndeterminate:
    def test_empty_blocked_by_with_prose_gate_is_indeterminate_not_freed(self) -> None:
        handoff = _roadmap_handoff("hnd-tri-000009", blocked_by=[])
        handoff["gate_dependency"] = "some subsystem condition"

        result = evaluate_gate_triage(handoff, [handoff])

        assert result["status"] == "indeterminate"
        assert result["has_prose_gate"] is True


class TestTriageBlockingNotesNoLongerDominatesSatisfiedStructuredSet:
    """Triage-side twin of `TestBlockingNotesNoLongerDominatesSatisfiedStructuredSet`
    (AC4.6): a non-empty `blocking_notes` (no `gate_dependency`) no longer
    overrides a SATISFIED structured `blocked_by` graph — status is `freed`,
    matching `evaluate_gate`'s `clear`. This flips the prior expectation;
    that flip IS the C4 fix."""

    def test_blocking_notes_present_with_all_structured_shipped_is_freed(self) -> None:
        dependent = _roadmap_handoff("hnd-tri-bn-000001", blocked_by=["hnd-b1-bn-000001"])
        dependent["blocking_notes"] = "Windows machine required for AC7 verification"
        b1 = _blocker("hnd-b1-bn-000001", "shipped", shipped_in="e" * 40, blocks=["hnd-tri-bn-000001"])

        result = evaluate_gate_triage(dependent, [dependent, b1])

        assert result["status"] == "freed"
        assert result["shipped_ids"] == ["hnd-b1-bn-000001"]


class TestTriageBlockingNotesDominatesVacuousEmptyBlockedBy:
    """Triage-side twin of `TestBlockingNotesDominatesVacuousEmptyBlockedBy`
    — the Windows-box regression shape: `blocked_by: []`, non-empty
    `blocking_notes` -> `indeterminate`, never vacuously `freed`."""

    def test_empty_blocked_by_with_blocking_notes_is_indeterminate_not_freed(self) -> None:
        handoff = _roadmap_handoff("hnd-tri-bn-000002", blocked_by=[])
        handoff["blocking_notes"] = (
            "Windows machine required for AC7 verification — no baton, advisory only"
        )

        result = evaluate_gate_triage(handoff, [handoff])

        assert result["status"] == "indeterminate"


class TestTriageEmptyBlockedByEmptyBlockingNotesStillVacuouslyFreed:
    def test_empty_blocked_by_absent_blocking_notes_is_freed(self) -> None:
        handoff = _roadmap_handoff("hnd-tri-bn-000003", blocked_by=[])

        result = evaluate_gate_triage(handoff, [handoff])

        assert result["status"] == "freed"

    def test_empty_blocked_by_whitespace_only_blocking_notes_is_freed(self) -> None:
        handoff = _roadmap_handoff("hnd-tri-bn-000004", blocked_by=[])
        handoff["blocking_notes"] = "   "

        result = evaluate_gate_triage(handoff, [handoff])

        assert result["status"] == "freed"


# ---------------------------------------------------------------------------
# Completion-log resolution source (corpus-gap close): a blocker unresolved
# against the handoff-only index may still have durable shipped-evidence
# under archive/completed/ (workstream-complete completion records).
# ---------------------------------------------------------------------------

def _completion_entry(path: str, chain, chain_terminal: bool, status: str = "pending-release", commits=None) -> dict:
    return {
        "path": path,
        "frontmatter": {
            "chain": chain,
            "chain_terminal": chain_terminal,
            "status": status,
            "commits": commits if commits is not None else [],
        },
    }


class TestTriageCompletionLogExactMatchResolvesShipped:
    """Exact chain==blocker_id match, chain_terminal:true, non-empty commits ->
    resolves as shipped-equivalent and can produce `freed`."""

    def test_exact_chain_terminal_with_commits_frees(self) -> None:
        dependent = _roadmap_handoff("hnd-tri-comp-000001", blocked_by=["strang-02"])
        completions = [
            _completion_entry(
                "archive/completed/2026-07/2026-07-05-strang-02-dedab5.md",
                chain="strang-02", chain_terminal=True, commits=["45ca22e"],
            )
        ]

        result = evaluate_gate_triage(dependent, [dependent], completion_entries=completions)

        assert result["status"] == "freed"
        assert result["shipped_ids"] == ["strang-02"]
        assert result["unresolved_ids"] == []


class TestTriageCompletionLogFuzzyMatchStaysIndeterminate:
    """A blocker_id embedded as a contiguous token-run inside a LONGER,
    differently-shaped chain slug (the real strang-01 shape) is a heuristic
    match — never auto-resolved to shipped, stays indeterminate with the
    candidate evidence surfaced."""

    def test_fuzzy_chain_match_does_not_free(self) -> None:
        dependent = _roadmap_handoff("hnd-tri-comp-000002", blocked_by=["strang-01"])
        completions = [
            _completion_entry(
                "archive/completed/2026-07/2026-07-05-2026-07-04-strang-01-tc3-emission-port-facade-respin-8a0ced.md",
                chain="2026-07-04-strang-01-tc3-emission-port-facade-respin",
                chain_terminal=True,
                commits=["bcd0419"],
            )
        ]

        result = evaluate_gate_triage(dependent, [dependent], completion_entries=completions)

        assert result["status"] == "indeterminate"
        assert result["unresolved_ids"] == ["strang-01"]
        assert any("completion-log candidate" in e for e in result["evidence"])


class TestTriageCompletionLogNonTerminalEntryDoesNotResolve:
    """A completion entry that is NOT chain_terminal must not clear a blocker
    on its own — an in-progress workstream session entry is not proof the
    whole chain landed."""

    def test_non_chain_terminal_entry_does_not_free(self) -> None:
        dependent = _roadmap_handoff("hnd-tri-comp-000003", blocked_by=["strang-03"])
        completions = [
            _completion_entry(
                "archive/completed/2026-07/2026-07-05-2026-07-05-strang-03-cross-repo-memo-send-strangle-c26869.md",
                chain="2026-07-05-strang-03-cross-repo-memo-send-strangle",
                chain_terminal=False,
                commits=[],
            )
        ]

        result = evaluate_gate_triage(dependent, [dependent], completion_entries=completions)

        assert result["status"] == "indeterminate"
        assert result["unresolved_ids"] == ["strang-03"]


class TestTriageCompletionLogPendingReleaseCountsAsShippedEquivalent:
    """Explicit derivation check: `status: pending-release` (the ONLY status
    value ever observed in this corpus) does NOT block the exact-chain-
    terminal-with-commits resolution — 'shipped' means landed-as-commits,
    not released-to-users, per the ratified lifecycle_constants precedent."""

    def test_pending_release_status_still_resolves_exact_match(self) -> None:
        dependent = _roadmap_handoff("hnd-tri-comp-000004", blocked_by=["qsub-02x"])
        completions = [
            _completion_entry(
                "archive/completed/2026-07/fake-qsub-02x.md",
                chain="qsub-02x", chain_terminal=True, status="pending-release",
                commits=["deadbee"],
            )
        ]

        result = evaluate_gate_triage(dependent, [dependent], completion_entries=completions)

        assert result["status"] == "freed"
        assert result["shipped_ids"] == ["qsub-02x"]


class TestTriageCompletionLogNoMatchStaysUnresolved:
    def test_no_completion_match_stays_unresolved_as_before(self) -> None:
        dependent = _roadmap_handoff("hnd-tri-comp-000005", blocked_by=["qsub-01"])
        completions = [
            _completion_entry(
                "archive/completed/2026-07/unrelated.md",
                chain="2026-07-01-unrelated-workstream", chain_terminal=True, commits=["cafe123"],
            )
        ]

        result = evaluate_gate_triage(dependent, [dependent], completion_entries=completions)

        assert result["status"] == "indeterminate"
        assert result["unresolved_ids"] == ["qsub-01"]
        assert any("dangling" in e for e in result["evidence"])


class TestTriageCompletionLogAmbiguousExactMatchesStayIndeterminate:
    """Two exact chain_terminal+commits-bearing matches for the SAME id ->
    ambiguous, never guess which one — mirrors the prose-path's own
    '>1 candidate -> surface' rule."""

    def test_multiple_exact_terminal_matches_stay_indeterminate(self) -> None:
        dependent = _roadmap_handoff("hnd-tri-comp-000006", blocked_by=["dup-01"])
        completions = [
            _completion_entry("archive/completed/2026-07/a.md", chain="dup-01", chain_terminal=True, commits=["a1"]),
            _completion_entry("archive/completed/2026-07/b.md", chain="dup-01", chain_terminal=True, commits=["b2"]),
        ]

        result = evaluate_gate_triage(dependent, [dependent], completion_entries=completions)

        assert result["status"] == "indeterminate"
        assert result["unresolved_ids"] == ["dup-01"]


def _io_leg(leg_id: str, kind: str, expected, observed, read_ok: bool = True, error=None) -> dict:
    """Build one caller-resolved `gate_evidence` I/O leg — the shape a real
    caller assembles by merging `sibling_fact.resolve_leg`'s
    `{read_ok, observed, error}` onto the leg's own authored
    `{leg_id, kind, expected}` declaration (module docstring "GATE_EVIDENCE
    PROJECTION")."""
    return {
        "leg_id": leg_id,
        "kind": kind,
        "expected": expected,
        "read_ok": read_ok,
        "observed": observed,
        "error": error,
    }


def _non_roadmap_handoff(handoff_id: str, **extra) -> dict:
    handoff = {
        "id": handoff_id,
        "kind": "session-handoff",
        "deployment_state": "awaiting_gate",
    }
    handoff.update(extra)
    return handoff


class TestTriageGateEvidencePrecedenceMatrix:
    """C3 D2 — the full precedence matrix: prose-only, evidence-only,
    both-with-covers_prose, both-without-covers_prose, neither. The
    both-with-covers_prose case is the load-bearing one (evidence wins,
    prose demoted to commentary)."""

    def test_prose_only_is_indeterminate(self) -> None:
        handoff = _non_roadmap_handoff(
            "hnd-tri-eve-000001", gate_dependency="widget engine migration"
        )

        result = evaluate_gate_triage(handoff, [handoff])

        assert result["status"] == "indeterminate"
        assert result["has_prose_gate"] is True
        assert result["has_gate_evidence"] is False

    def test_evidence_only_all_satisfied_frees(self) -> None:
        handoff = _non_roadmap_handoff("hnd-tri-eve-000002")
        gate_evidence = {
            "covers_prose": False,
            "legs": [
                _io_leg("leg-1", "file-exists", True, True),
                _io_leg("leg-2", "commit-ancestor", None, True),
            ],
        }

        result = evaluate_gate_triage(handoff, [handoff], gate_evidence=gate_evidence)

        assert result["status"] == "freed"
        assert result["has_prose_gate"] is False
        assert result["has_gate_evidence"] is True
        assert len(result["gate_evidence_legs"]) == 2

    def test_both_with_covers_prose_evidence_wins_and_frees(self) -> None:
        handoff = _non_roadmap_handoff(
            "hnd-tri-eve-000003", gate_dependency="widget engine migration"
        )
        gate_evidence = {
            "covers_prose": True,
            "legs": [_io_leg("leg-1", "file-exists", True, True)],
        }

        result = evaluate_gate_triage(handoff, [handoff], gate_evidence=gate_evidence)

        assert result["status"] == "freed"
        assert any("demoted to commentary" in e for e in result["evidence"])
        assert "gate_evidence covers_prose:True" in result["reason"]

    def test_blocking_notes_dominates_even_when_covers_prose_satisfied(self) -> None:
        """Review: code-reviewer — Finding 1 (P1) regression test, updated for
        C4's blocking_notes demotion (docs/plans/2026-08-03-gate-dependency-
        template-emission-spec.md § C4): `blocked_by` is empty on this fixture
        (`_non_roadmap_handoff` sets no `blocked_by`), so `blocking_notes`
        dominance still applies in its narrowed, empty-`blocked_by`-only form
        — `gate_dependency` present + `gate_evidence.covers_prose: True` (the
        same satisfied-evidence shape that frees in
        `test_both_with_covers_prose_evidence_wins_and_frees` above) + a
        non-empty `blocking_notes` must stay `indeterminate`, never `freed`,
        and must be checked ahead of D2, not after."""
        handoff = _non_roadmap_handoff(
            "hnd-tri-eve-000099", gate_dependency="widget engine migration"
        )
        handoff["blocking_notes"] = "Windows machine required for AC7 verification"
        gate_evidence = {
            "covers_prose": True,
            "legs": [_io_leg("leg-1", "file-exists", True, True)],
        }

        result = evaluate_gate_triage(handoff, [handoff], gate_evidence=gate_evidence)

        assert result["status"] == "indeterminate"
        assert any("blocking_notes" in e for e in result["evidence"])

    def test_both_without_covers_prose_stays_indeterminate_even_when_evidence_satisfied(
        self,
    ) -> None:
        """The D2a guard: a partial gate_evidence backfill (legs resolve, but
        covers_prose is not asserted True) must NOT silently free a prose
        gate — this is exactly the inversion the covers_prose gate exists to
        prevent."""
        handoff = _non_roadmap_handoff(
            "hnd-tri-eve-000004", gate_dependency="widget engine migration"
        )
        gate_evidence = {
            "covers_prose": False,
            "legs": [_io_leg("leg-1", "file-exists", True, True)],
        }

        result = evaluate_gate_triage(handoff, [handoff], gate_evidence=gate_evidence)

        assert result["status"] == "indeterminate"
        assert result["has_gate_evidence"] is True
        assert result["has_prose_gate"] is True

    def test_neither_falls_back_to_unchanged_prior_behavior(self) -> None:
        handoff = _non_roadmap_handoff("hnd-tri-eve-000005")

        result = evaluate_gate_triage(handoff, [handoff])

        assert result["status"] == "freed"
        assert result["has_prose_gate"] is False
        assert result["has_gate_evidence"] is False


class TestGateEvidenceAndReduceAnyIndeterminateLegWins:
    def test_one_unreadable_leg_among_satisfied_legs_is_indeterminate(self) -> None:
        handoff = _non_roadmap_handoff("hnd-tri-eve-000006")
        gate_evidence = {
            "covers_prose": False,
            "legs": [
                _io_leg("leg-1", "file-exists", True, True),
                _io_leg("leg-2", "file-exists", True, None, read_ok=False, error="repo unregistered"),
            ],
        }

        result = evaluate_gate_triage(handoff, [handoff], gate_evidence=gate_evidence)

        assert result["status"] == "indeterminate"
        assert any("repo unregistered" in e for e in result["evidence"])


class TestGateEvidenceHumanLegPermanentIndeterminate:
    def test_human_leg_is_always_indeterminate(self) -> None:
        handoff = _non_roadmap_handoff("hnd-tri-eve-000007")
        gate_evidence = {
            "covers_prose": False,
            "legs": [
                _io_leg("leg-1", "file-exists", True, True),
                {"leg_id": "leg-2", "kind": "human", "reason": "confirm the memo landed"},
            ],
        }

        result = evaluate_gate_triage(handoff, [handoff], gate_evidence=gate_evidence)

        assert result["status"] == "indeterminate"
        assert any("confirm the memo landed" in e for e in result["evidence"])


class TestGateEvidenceDeadlineReviewDueExcludedFromAndReduce:
    def test_elapsed_deadline_with_all_else_satisfied_is_review_due_not_freed(self) -> None:
        handoff = _non_roadmap_handoff("hnd-tri-eve-000008")
        gate_evidence = {
            "covers_prose": False,
            "legs": [
                _io_leg("leg-1", "file-exists", True, True),
                {"leg_id": "leg-2", "kind": "deadline", "elapsed": True},
            ],
        }

        result = evaluate_gate_triage(handoff, [handoff], gate_evidence=gate_evidence)

        assert result["status"] == "review-due"

    def test_not_yet_elapsed_deadline_with_all_else_satisfied_is_still_blocked(self) -> None:
        handoff = _non_roadmap_handoff("hnd-tri-eve-000009")
        gate_evidence = {
            "covers_prose": False,
            "legs": [
                _io_leg("leg-1", "file-exists", True, True),
                {"leg_id": "leg-2", "kind": "deadline", "elapsed": False},
            ],
        }

        result = evaluate_gate_triage(handoff, [handoff], gate_evidence=gate_evidence)

        assert result["status"] == "still-blocked"

    def test_single_elapsed_deadline_leg_never_frees_alone(self) -> None:
        handoff = _non_roadmap_handoff("hnd-tri-eve-000010")
        gate_evidence = {
            "covers_prose": False,
            "legs": [{"leg_id": "leg-1", "kind": "deadline", "elapsed": True}],
        }

        result = evaluate_gate_triage(handoff, [handoff], gate_evidence=gate_evidence)

        assert result["status"] == "review-due"
        assert result["status"] != "freed"


class TestGateEvidenceEmptyLegsMalformedIndeterminate:
    def test_empty_legs_list_is_indeterminate_never_vacuously_freed(self) -> None:
        handoff = _non_roadmap_handoff("hnd-tri-eve-000011")
        gate_evidence = {"covers_prose": False, "legs": []}

        result = evaluate_gate_triage(handoff, [handoff], gate_evidence=gate_evidence)

        assert result["status"] == "indeterminate"


# ---------------------------------------------------------------------------
# C9 — lineage-is-not-gating (PM ruling 2026-07-26): predecessor/origin_* must
# never be read to infer, narrow, or clear a gate. RED by design — the very
# next chunk rewrites `evaluate_gate`'s routing to fix this; this test only
# pins the spec and proves today's code trips on it.
# ---------------------------------------------------------------------------


def _real_spinoff_with_lineage_and_no_structured_gate() -> dict:
    """Modeled on the real, on-disk baton `coordinator-claude/state/handoffs/
    2026-07-08_160001_roadmap-oaxis-01.md` (verified 2026-07-27) — one of the
    30-of-33 live `awaiting_gate` batons carrying a non-`none` `predecessor`
    and/or a populated `origin_*` field, per the live-corpus sweep cited in
    the module docstring's "LINEAGE IS NOT GATING" section. Field names,
    value shapes (`predecessor: "none"`, `origin_session` a session UUID,
    `origin_handoff` a `state/handoffs/...` path, `handoff_id`/`stub_id`/
    `roadmap_id` conventions) are copied verbatim from that real baton.

    One deliberate adaptation from the literal file: the real oaxis-01 baton
    ALSO carries a non-empty `gate_dependency` prose one-liner ("rag
    acl-principal axis co-design reply reconciled...") naming a genuinely
    still-open, unrelated cross-repo gate — that specific instance is not
    actually ready to fire today, for reasons that have nothing to do with
    lineage. This fixture omits that field so the scenario isolates exactly
    the claim under test — an EMPTY `blocked_by` plus lineage fields
    (`predecessor`/`origin_*`) alone, no other gate of any kind — from the
    orthogonal, already-covered prose-gate-dominance behavior (see
    TestTriageProseGateDominatesEvenWhenStructuredClear /
    TestEmptyBlockedByFallsBackToProseSurface elsewhere in this file). Every
    other field is the real baton's own shape.
    """
    return {
        "id": "2026-07-08_160001_roadmap-oaxis-01",
        "handoff_id": "hnd-oaxis-01-cross-repo-vocabulary-7e36b5",
        "kind": "spinoff-roadmap",
        "roadmap_id": "owner-axis-rollout",
        "stub_id": "oaxis-01",
        "predecessor": "none",
        "origin_session": "3028c7d2-4eaf-4ba3-86fa-8058f709eecf",
        "origin_handoff": "state/handoffs/2026-07-08_152608_owner-field-human-axis-ratification.md",
        "origin_plan_id": None,
        "origin_goal_id": None,
        "deployment_state": "awaiting_gate",
        "blocks": ["oaxis-03"],
        "blocked_by": [],
    }


class TestLineageFieldsNeverGateEmptyBlockedBy:
    """AC9 (C9 dispatch): a baton with a NON-TERMINAL `predecessor` and/or
    populated `origin_*` fields, and an EMPTY `blocked_by`, MUST resolve to
    `ready_to_fire` — i.e. `evaluate_gate` must return verdict `clear`
    (per `handoff-reconcile-producer-contract.md`, a `clear` verdict is what
    drives C4's full flip to `deployment_state: ready_to_fire`). A resolver
    that "helpfully" walked `predecessor`/`origin_*` to infer an implied
    dependency would instead gate this spinoff on its own origin baton —
    exactly backwards (see module docstring "THE WRONG TURN..."). This
    assertion is intentionally RED today: `evaluate_gate` currently routes
    ANY `kind == spinoff-roadmap` handoff with a falsy `blocked_by` to the
    PROSE fallback path (`_evaluate_prose_gate`), which returns `surface`
    when no witness_candidates are supplied — regardless of lineage fields,
    and regardless of there being no gate of any kind left to clear. The
    very next chunk rewrites that routing; this test does not.
    """

    def test_lineage_fields_do_not_block_ready_to_fire(self) -> None:
        dependent = _real_spinoff_with_lineage_and_no_structured_gate()

        result = evaluate_gate(dependent, [dependent])

        assert result["verdict"] == "clear", (
            "evaluate_gate must resolve an empty blocked_by + lineage-only "
            f"baton to clear/ready_to_fire; got {result['verdict']!r} — "
            "lineage (predecessor/origin_*) must never gate"
        )
        assert result["remaining_blockers"] == []

    def test_triage_projection_already_reports_freed_for_the_same_shape(self) -> None:
        """Sanity companion (not RED): `evaluate_gate_triage` already treats
        an empty `blocked_by` with no prose gate as vacuously `freed`,
        confirming the triage projection does not read lineage fields
        either — the defect is isolated to `evaluate_gate`'s routing, not
        to lineage-field leakage in the shared classification primitives
        (`_classify_blocked_by`/`_has_asymmetry`/`_index_by_id`)."""
        dependent = _real_spinoff_with_lineage_and_no_structured_gate()

        result = evaluate_gate_triage(dependent, [dependent])

        assert result["status"] == "freed"
        assert result["blocked_by"] == []


class TestComputeIndexAgreesWithActTimeResolver:
    """C2c acceptance criterion AC2b: the gate-eval compute index and
    `handoff_transition._resolve_blocker_deployment_state` (the act-time mutating
    resolver it must agree with) resolve the SAME `blocked_by` id to the SAME
    handoff/deployment_state. Before C2c, `_index_by_id` keyed only on `id or
    stub_id` — a real handoff's synthesized `id` (path stem) never carries the
    durable `handoff_id` shape the mutating resolver matches against, so every
    non-roadmap-stub baton read as permanently dangling at compute time while
    resolving fine at act time. This test builds one real on-disk handoff (as
    `handoff_transition.py` reads it) and asserts both resolvers land on it.
    """

    def test_same_handoff_id_resolves_to_same_deployment_state_both_resolvers(
        self, tmp_path
    ) -> None:
        from coordinator_core.ops.handoff_transition import (
            _resolve_blocker_deployment_state,
        )
        from coordinator_core.reconcile.gate_eval import _index_by_id

        blocker_handoff_id = "hnd-c2c-agreement-check-abc123"
        worktree = tmp_path
        handoffs_dir = worktree / "state" / "handoffs"
        handoffs_dir.mkdir(parents=True)
        (handoffs_dir / "2026-07-27-c2c-agreement-check.md").write_text(
            "---\n"
            f'handoff_id: "{blocker_handoff_id}"\n'
            "status: consumed\n"
            "deployment_state: shipped\n"
            'shipped_in: "' + ("a" * 40) + '"\n'
            "---\n"
            "body\n",
            encoding="utf-8",
        )

        # Act-time path: handoff_transition.py re-scans disk fresh.
        act_time_state = _resolve_blocker_deployment_state(blocker_handoff_id, worktree)

        # Compute-time path: gate_eval's index over caller-collected dicts, as
        # `_collect_open_handoffs`/the archive-scan collector would hand them in
        # — `id` synthesized from the path stem (never `hnd-...` shaped), the
        # frontmatter's own `handoff_id` field carried through separately.
        collected = [
            {
                "id": "2026-07-27-c2c-agreement-check",
                "handoff_id": blocker_handoff_id,
                "kind": "spinoff-roadmap",
                "deployment_state": "shipped",
                "shipped_in": "a" * 40,
                "blocks": [],
            }
        ]
        compute_time_index = _index_by_id(collected)
        compute_time_blocker = compute_time_index.get(blocker_handoff_id)

        assert act_time_state.deployment_state == "shipped"
        assert compute_time_blocker is not None
        assert compute_time_blocker["deployment_state"] == act_time_state.deployment_state
        assert compute_time_blocker["handoff_id"] == blocker_handoff_id


# ---------------------------------------------------------------------------
# C5 — continued_into terminus-chase (real lifecycle-vocab corpus shape)
# ---------------------------------------------------------------------------

_LVV05_STUB_ID = "lvv-05"
_LVV06_STUB_ID = "lvv-06"
_TERMINUS_HANDOFF_ID = "hnd-dual-read-claimed-by-consumed--7e3c06"
_TERMINUS_SHA = "cdb5878808f95e1837a9d1e47a1a094b44436f91"


def _lvv05_continued(continued_into: str, blocks=None) -> dict:
    """The real lvv-05 shape: `stub_id` only (no `handoff_id` — matches the
    actual on-disk frontmatter), `deployment_state: continued`,
    `continued_into` pointing at its dr084 successor."""
    return {
        "stub_id": _LVV05_STUB_ID,
        "kind": "spinoff-roadmap",
        "deployment_state": "continued",
        "continued_into": continued_into,
        "blocked_by": ["lvv-03"],
        "blocks": blocks if blocks is not None else ["lvv-06", "lvv-07", "lvv-08"],
    }


def _lvv06_dependent(blocked_by=None) -> dict:
    """The real lvv-06 shape: gated on lvv-05 (+ lvv-03 in the real corpus)."""
    return {
        "stub_id": _LVV06_STUB_ID,
        "handoff_id": "hnd-c5-abandoned-closed-reason-ren-1f686c",
        "id": "hnd-c5-abandoned-closed-reason-ren-1f686c",
        "kind": "spinoff-roadmap",
        "deployment_state": "awaiting_gate",
        "blocked_by": blocked_by if blocked_by is not None else ["lvv-05"],
        "blocks": ["lvv-07", "lvv-08"],
    }


def _terminus(deployment_state: str, shipped_in=None, blocks=None, path=None) -> dict:
    """The real dr084-skill-layer-dual-read successor shape: a plain `spinoff`
    (NOT `spinoff-roadmap`), identified only by its durable `handoff_id` — no
    `blocks:[...]` back-reference to lvv-06 at all (a terminus never authors
    one; only a roadmap-kind blocker is held to that convention)."""
    d = {
        "handoff_id": _TERMINUS_HANDOFF_ID,
        "kind": "spinoff",
        "deployment_state": deployment_state,
    }
    if shipped_in:
        d["shipped_in"] = shipped_in
    if blocks is not None:
        d["blocks"] = blocks
    if path is not None:
        d["_path"] = path
    return d


class TestC5ContinuedIntoStillOpenSurfaces:
    """The named lvv-05/lvv-06 case: continuation still open at drain time ->
    must NOT clear."""

    def test_open_terminus_narrows_and_surfaces_never_clears(self) -> None:
        lvv05 = _lvv05_continued(continued_into=_TERMINUS_HANDOFF_ID)
        lvv06 = _lvv06_dependent(blocked_by=["lvv-05", "lvv-03"])
        lvv03 = _blocker("lvv-03", "shipped", shipped_in="b" * 40, blocks=["lvv-06", "lvv-07", "lvv-08"])
        terminus = _terminus("ready_to_fire")

        result = evaluate_gate(lvv06, [lvv06, lvv05, lvv03, terminus])

        assert result["verdict"] != "clear"
        assert result["verdict"] == "narrow"
        assert result["also_surface"] is True
        assert "lvv-05" in result["remaining_blockers"]
        assert any("chased to terminus" in e and "not shipped" in e for e in result["evidence"])

    def test_single_blocker_open_terminus_surfaces_outright(self) -> None:
        lvv05 = _lvv05_continued(continued_into=_TERMINUS_HANDOFF_ID)
        lvv06 = _lvv06_dependent(blocked_by=["lvv-05"])
        terminus = _terminus("awaiting_gate")

        result = evaluate_gate(lvv06, [lvv06, lvv05, terminus])

        assert result["verdict"] == "surface"
        assert result["cleared_by_shas"] == []


class TestC5ContinuedIntoShippedTerminusClears:
    def test_shipped_terminus_clears_naming_both_hops_in_evidence(self) -> None:
        lvv05 = _lvv05_continued(continued_into=_TERMINUS_HANDOFF_ID)
        lvv06 = _lvv06_dependent(blocked_by=["lvv-05"])
        terminus = _terminus("shipped", shipped_in=_TERMINUS_SHA)

        result = evaluate_gate(lvv06, [lvv06, lvv05, terminus])

        assert result["verdict"] == "clear"
        # cleared_by_shas/cleared_blocker_ids stay 1:1-paired against the
        # ORIGINAL blocked_by id (lvv-05), never the terminus id — the
        # gate-cascade-clear verb requires this pairing.
        assert result["cleared_blocker_ids"] == ["lvv-05"]
        assert result["cleared_by_shas"] == [_TERMINUS_SHA]
        both_hops = [
            e for e in result["evidence"]
            if "lvv-05" in e and _TERMINUS_HANDOFF_ID in e and "shipped" in e
        ]
        assert both_hops, result["evidence"]

    def test_asymmetry_check_not_tripped_by_terminus_missing_back_reference(self) -> None:
        """The terminus carries no `blocks:[...]` naming lvv-06 back at all —
        this must NOT false-fire asymmetry; only lvv-05's own `blocks:` (which
        correctly lists lvv-06) is consulted."""
        lvv05 = _lvv05_continued(continued_into=_TERMINUS_HANDOFF_ID, blocks=["lvv-06"])
        lvv06 = _lvv06_dependent(blocked_by=["lvv-05"])
        terminus = _terminus("shipped", shipped_in=_TERMINUS_SHA)  # no `blocks` at all

        result = evaluate_gate(lvv06, [lvv06, lvv05, terminus])

        assert result["verdict"] == "clear"


class TestC5ContinuedIntoDepthCap:
    def test_chain_exceeding_depth_cap_surfaces(self) -> None:
        # Build a straight-line chain of `continued` hops one longer than the
        # cap, terminating in a `shipped` handoff that is never reached.
        from coordinator_core.reconcile.gate_eval import _MAX_CONTINUATION_CHASE_DEPTH

        hop_count = _MAX_CONTINUATION_CHASE_DEPTH + 2
        hops = []
        for i in range(hop_count):
            this_id = f"hnd-chain-hop-{i:02d}-abcdef"
            next_id = f"hnd-chain-hop-{i + 1:02d}-abcdef"
            hops.append({
                "handoff_id": this_id,
                "kind": "spinoff",
                "deployment_state": "continued",
                "continued_into": next_id,
            })
        final_id = f"hnd-chain-hop-{hop_count:02d}-abcdef"
        hops.append({
            "handoff_id": final_id,
            "kind": "spinoff",
            "deployment_state": "shipped",
            "shipped_in": "c" * 40,
        })

        lvv06 = _lvv06_dependent(blocked_by=[hops[0]["handoff_id"]])

        result = evaluate_gate(lvv06, [lvv06] + hops)

        assert result["verdict"] == "surface"
        assert any("depth cap" in e for e in result["evidence"])


class TestC5ContinuedIntoCycle:
    def test_chain_cycle_surfaces_never_loops(self) -> None:
        a_id = "hnd-cycle-hop-a-000001"
        b_id = "hnd-cycle-hop-b-000002"
        hop_a = {
            "handoff_id": a_id,
            "kind": "spinoff",
            "deployment_state": "continued",
            "continued_into": b_id,
        }
        hop_b = {
            "handoff_id": b_id,
            "kind": "spinoff",
            "deployment_state": "continued",
            "continued_into": a_id,
        }
        lvv06 = _lvv06_dependent(blocked_by=[a_id])

        result = evaluate_gate(lvv06, [lvv06, hop_a, hop_b])

        assert result["verdict"] == "surface"
        assert any("cycle" in e for e in result["evidence"])


class TestC5ContinuedIntoPathFallback:
    """Pre-existing successors carry no handoff_id — only a `state/handoffs/
    ...` path. Resolved via basename match against whatever path-shaped field
    the caller's collector attached; never re-read off disk."""

    def test_path_valued_continued_into_resolves_via_basename_and_clears(self) -> None:
        relative_path = "state/handoffs/2026-07-22_152437_dr084-skill-layer-dual-read.md"
        lvv05 = _lvv05_continued(continued_into=relative_path)
        lvv06 = _lvv06_dependent(blocked_by=["lvv-05"])
        terminus = _terminus(
            "shipped",
            shipped_in=_TERMINUS_SHA,
            path="/Users/alice/X/coordinator-claude/archive/handoffs/2026-07/"
            "2026-07-22_152437_dr084-skill-layer-dual-read.md",
        )

        result = evaluate_gate(lvv06, [lvv06, lvv05, terminus])

        assert result["verdict"] == "clear"
        assert result["cleared_by_shas"] == [_TERMINUS_SHA]

    def test_path_valued_continued_into_unmatched_surfaces(self) -> None:
        lvv05 = _lvv05_continued(continued_into="state/handoffs/does-not-exist.md")
        lvv06 = _lvv06_dependent(blocked_by=["lvv-05"])

        result = evaluate_gate(lvv06, [lvv06, lvv05])

        assert result["verdict"] == "surface"
        assert any("does not resolve" in e for e in result["evidence"])


class TestC5TriageProjectionParity:
    """`evaluate_gate_triage` reuses the SAME `_classify_blocked_by` chase —
    verify the freed/indeterminate projection matches `evaluate_gate`'s
    clear/surface outcome on the same corpus shape."""

    def test_open_terminus_is_indeterminate_not_freed(self) -> None:
        lvv05 = _lvv05_continued(continued_into=_TERMINUS_HANDOFF_ID)
        lvv06 = _lvv06_dependent(blocked_by=["lvv-05"])
        terminus = _terminus("ready_to_fire")

        result = evaluate_gate_triage(lvv06, [lvv06, lvv05, terminus])

        assert result["status"] == "indeterminate"

    def test_shipped_terminus_is_freed(self) -> None:
        lvv05 = _lvv05_continued(continued_into=_TERMINUS_HANDOFF_ID)
        lvv06 = _lvv06_dependent(blocked_by=["lvv-05"])
        terminus = _terminus("shipped", shipped_in=_TERMINUS_SHA)

        result = evaluate_gate_triage(lvv06, [lvv06, lvv05, terminus])

        assert result["status"] == "freed"


def _oaxis_handoff(**extra) -> dict:
    """The C6 motivating record: `blocked_by: []` with a prose gate naming a
    sibling-REPO fact (example-retrieval-repo is a repo, not a baton — can never be a
    `blocked_by` slug)."""
    handoff = {
        "id": "oaxis-01",
        "kind": "session-handoff",
        "deployment_state": "awaiting_gate",
        "blocked_by": [],
        "gate_dependency": "rag acl-principal axis co-design reply reconciled",
    }
    handoff.update(extra)
    return handoff


class TestC6ExternalGateWitnessedClearsMutatingPath:
    """AC7 (oaxis-01 shape): `evaluate_gate` — the MUTATING evaluator, not
    only `evaluate_gate_triage` — now clears a prose-only external gate once
    a `sibling-commitment-ref` `gate_evidence` leg resolves True under
    `covers_prose: True`. Before C6 this handoff had no path to `clear` at
    all: empty `blocked_by` + non-empty prose falls to the legacy
    `witness_candidates` fallback, which surfaces on zero candidates forever
    — a sibling repo can never BE a witness_candidates handoff dict."""

    def test_unwitnessed_surfaces(self) -> None:
        handoff = _oaxis_handoff()
        gate_evidence = {
            "covers_prose": True,
            "legs": [
                _io_leg(
                    "leg-1",
                    "sibling-commitment-ref",
                    expected=None,
                    observed=False,
                    read_ok=True,
                )
            ],
        }

        result = evaluate_gate(handoff, [handoff], gate_evidence=gate_evidence)

        assert result["verdict"] == "surface"
        assert result["remaining_blockers"] == []
        assert result["cleared_blocker_ids"] == []
        assert any("demoted to commentary" in e for e in result["evidence"])

    def test_witnessed_clears(self) -> None:
        handoff = _oaxis_handoff()
        gate_evidence = {
            "covers_prose": True,
            "legs": [
                _io_leg(
                    "leg-1",
                    "sibling-commitment-ref",
                    expected=None,
                    observed=True,
                    read_ok=True,
                )
            ],
        }

        result = evaluate_gate(handoff, [handoff], gate_evidence=gate_evidence)

        assert result["verdict"] == "clear"
        assert result["cleared_blocker_ids"] == []
        assert result["remaining_blockers"] == []

    def test_unreadable_leg_is_indeterminate_surfaces(self) -> None:
        handoff = _oaxis_handoff()
        gate_evidence = {
            "covers_prose": True,
            "legs": [
                _io_leg(
                    "leg-1",
                    "sibling-commitment-ref",
                    expected=None,
                    observed=None,
                    read_ok=False,
                    error="commitment record not found",
                )
            ],
        }

        result = evaluate_gate(handoff, [handoff], gate_evidence=gate_evidence)

        assert result["verdict"] == "surface"
        assert any("commitment record not found" in e for e in result["evidence"])

    def test_covers_prose_false_falls_back_to_legacy_witness_candidates(self) -> None:
        """D2a guard, applied to the mutating path too: a partial
        gate_evidence backfill (leg satisfied) without an explicit
        `covers_prose: True` must NOT silently free the gate — falls through
        to the pre-C6 witness_candidates fallback unchanged, which surfaces
        on zero candidates."""
        handoff = _oaxis_handoff()
        gate_evidence = {
            "covers_prose": False,
            "legs": [
                _io_leg(
                    "leg-1",
                    "sibling-commitment-ref",
                    expected=None,
                    observed=True,
                    read_ok=True,
                )
            ],
        }

        result = evaluate_gate(
            handoff, [handoff], witness_candidates=[], gate_evidence=gate_evidence
        )

        assert result["verdict"] == "surface"

    def test_no_gate_evidence_reproduces_pre_c6_behavior_byte_for_byte(self) -> None:
        handoff = _oaxis_handoff()

        result = evaluate_gate(handoff, [handoff], witness_candidates=[])

        assert result["verdict"] == "surface"
        assert result["remaining_blockers"] == []

    def test_blocking_notes_dominates_even_when_covers_prose_witnessed(self) -> None:
        """Review: code-reviewer — Finding 1 (P1) regression test, updated for
        C4's blocking_notes demotion (docs/plans/2026-08-03-gate-dependency-
        template-emission-spec.md § C4): `_oaxis_handoff` carries `blocked_by:
        []`, so `blocking_notes` dominance still applies in its narrowed,
        empty-`blocked_by`-only form — `gate_dependency` present +
        `gate_evidence.covers_prose: True` (a satisfied witnessed leg, i.e.
        the exact combination that clears in `test_witnessed_clears` above) +
        a non-empty `blocking_notes` must NOT resolve to `clear`, and must be
        checked ahead of the covers_prose Rule 0 branch, not after it."""
        handoff = _oaxis_handoff()
        handoff["blocking_notes"] = "Windows machine required for AC7 verification"
        gate_evidence = {
            "covers_prose": True,
            "legs": [
                _io_leg(
                    "leg-1",
                    "sibling-commitment-ref",
                    expected=None,
                    observed=True,
                    read_ok=True,
                )
            ],
        }

        result = evaluate_gate(handoff, [handoff], gate_evidence=gate_evidence)

        assert result["verdict"] == "surface"
        assert any("blocking_notes" in e for e in result["evidence"])


class TestC6ExternalGateCoversProseOverridesStructuredBlockedByToo:
    """Rule 0 fires before structured routing (mirroring evaluate_gate_triage's
    own D2 check order) — a `covers_prose: True` evidence block also
    overrides a NON-empty `blocked_by`, not merely the empty-blocked_by
    prose-only case."""

    def test_non_empty_blocked_by_with_covering_evidence_clears_whole_gate(self) -> None:
        dependent = _roadmap_handoff("hnd-oax-000001", blocked_by=["hnd-tc9-000001"])
        dependent["gate_dependency"] = "rag acl-principal axis co-design reply reconciled"
        still_open_blocker = _blocker("hnd-tc9-000001", "awaiting_gate")
        gate_evidence = {
            "covers_prose": True,
            "legs": [
                _io_leg(
                    "leg-1",
                    "sibling-commitment-ref",
                    expected=None,
                    observed=True,
                    read_ok=True,
                )
            ],
        }

        result = evaluate_gate(
            dependent, [dependent, still_open_blocker], gate_evidence=gate_evidence
        )

        assert result["verdict"] == "clear"
        assert result["cleared_blocker_ids"] == ["hnd-tc9-000001"]
        assert result["remaining_blockers"] == []


class TestC6BooleanObservedKindsRecognizedInTriageToo:
    """The four C6 kinds are recognized by the shared leg predicate
    (`_evaluate_gate_evidence_leg`/`_EVIDENCE_IO_KINDS`), so
    `evaluate_gate_triage` picks them up for free — one predicate, not a
    parallel one for the mutating path."""

    def test_test_node_id_leg_satisfied_frees_triage(self) -> None:
        handoff = _non_roadmap_handoff("hnd-c6-tri-000001")
        gate_evidence = {
            "covers_prose": False,
            "legs": [
                _io_leg("leg-1", "test-node-id", expected=None, observed=True, read_ok=True)
            ],
        }

        result = evaluate_gate_triage(handoff, [handoff], gate_evidence=gate_evidence)

        assert result["status"] == "freed"

    def test_probe_op_key_leg_unsatisfied_still_blocked(self) -> None:
        handoff = _non_roadmap_handoff("hnd-c6-tri-000002")
        gate_evidence = {
            "covers_prose": False,
            "legs": [
                _io_leg("leg-1", "probe-op-key", expected=None, observed=False, read_ok=True)
            ],
        }

        result = evaluate_gate_triage(handoff, [handoff], gate_evidence=gate_evidence)

        assert result["status"] == "still-blocked"

    def test_commit_sha_leg_unreadable_is_indeterminate(self) -> None:
        handoff = _non_roadmap_handoff("hnd-c6-tri-000003")
        gate_evidence = {
            "covers_prose": False,
            "legs": [
                _io_leg(
                    "leg-1",
                    "commit-sha",
                    expected=None,
                    observed=None,
                    read_ok=False,
                    error="commit unreachable",
                )
            ],
        }

        result = evaluate_gate_triage(handoff, [handoff], gate_evidence=gate_evidence)

        assert result["status"] == "indeterminate"
        assert any("commit unreachable" in e for e in result["evidence"])


class TestC7DisposedDanglingRefDoesNotClearButQuietsEvidence:
    """C7 AC8 (docs/plans/2026-07-13-claude-klabauter-auto-reconcile-open-handoffs.md
    § C7): a `blocked_by` id unresolvable in the live+archived index, whose
    gated handoff carries a `resolved_without_baton` disposition for that id,
    must stop producing the loud "dangling blocked_by ref(s)" evidence line
    and must never, by itself, clear the gate — the disposition explains why
    the ref will never resolve, it does not assert the blocked-on work
    shipped."""

    def test_sole_disposed_blocker_does_not_clear_stays_not_cleared(self) -> None:
        dependent = _roadmap_handoff("hnd-dep-c7-000001", blocked_by=["pcli-01"])
        dependent["blocked_by_dispositions"] = {
            "pcli-01": {
                "disposition": "resolved_without_baton",
                "reason": "shipped then pruned by keep-10 archive window (git log confirms)",
            }
        }

        result = evaluate_gate(dependent, [dependent])

        assert result["verdict"] == "not-cleared"
        assert result["remaining_blockers"] == ["pcli-01"]
        assert not any("dangling" in e for e in result["evidence"])
        assert any("resolved_without_baton" in e for e in result["evidence"])

    def test_disposed_blocker_alongside_shipped_narrows_quietly_not_also_surface(self) -> None:
        dependent = _roadmap_handoff(
            "hnd-dep-c7-000002", blocked_by=["hnd-tc1-c7-000002", "pcli-03"]
        )
        dependent["blocked_by_dispositions"] = {
            "pcli-03": {
                "disposition": "resolved_without_baton",
                "reason": "shipped then pruned",
            }
        }
        tc1 = _blocker(
            "hnd-tc1-c7-000002", "shipped", shipped_in="9" * 40, blocks=["hnd-dep-c7-000002"]
        )

        result = evaluate_gate(dependent, [dependent, tc1])

        assert result["verdict"] == "narrow"
        assert result["remaining_blockers"] == ["pcli-03"]
        assert result["cleared_by_shas"] == ["9" * 40]
        # Quiet: unlike an undispositioned dangling ref (also_surface=True,
        # TestNarrowWithUnresolvedIdAlsoSurfaces), a disposed id must not
        # re-trigger the surface signal on every pass.
        assert result["also_surface"] is False

    def test_undisposed_dangling_ref_still_fails_loud_alongside_a_disposed_one(self) -> None:
        dependent = _roadmap_handoff(
            "hnd-dep-c7-000003", blocked_by=["pcli-01", "hnd-ghost-c7-000003"]
        )
        dependent["blocked_by_dispositions"] = {
            "pcli-01": {"disposition": "resolved_without_baton", "reason": "shipped then pruned"}
        }

        result = evaluate_gate(dependent, [dependent])

        assert result["verdict"] == "surface"
        assert set(result["remaining_blockers"]) == {"pcli-01", "hnd-ghost-c7-000003"}
        assert any("dangling" in e for e in result["evidence"])

    def test_wrong_disposition_value_does_not_quiet_stays_loud(self) -> None:
        dependent = _roadmap_handoff("hnd-dep-c7-000004", blocked_by=["pcli-01"])
        dependent["blocked_by_dispositions"] = {
            "pcli-01": {"disposition": "some-other-value", "reason": "not honored"}
        }

        result = evaluate_gate(dependent, [dependent])

        assert result["verdict"] == "surface"
        assert any("dangling" in e for e in result["evidence"])


class TestC7DisposedDanglingRefTriageProjectionParity:
    """Same disposition mechanism, `evaluate_gate_triage`'s three-way
    projection: a disposed-only remainder reports `still-blocked` (never
    `freed`), and a genuinely dangling+undisposed id still reports
    `indeterminate` (loud) exactly as before."""

    def test_sole_disposed_blocker_is_still_blocked_never_freed(self) -> None:
        handoff = _roadmap_handoff("hnd-dep-c7-tri-000001", blocked_by=["mcollab-01"])
        handoff["blocked_by_dispositions"] = {
            "mcollab-01": {
                "disposition": "resolved_without_baton",
                "reason": "shipped then pruned by keep-10 archive window",
            }
        }

        result = evaluate_gate_triage(handoff, [handoff])

        assert result["status"] == "still-blocked"
        assert result["disposed_ids"] == ["mcollab-01"]
        assert not any("dangling" in e for e in result["evidence"])

    def test_disposed_blocker_alongside_shipped_is_still_blocked_not_freed(self) -> None:
        handoff = _roadmap_handoff(
            "hnd-dep-c7-tri-000002", blocked_by=["hnd-tc1-c7-tri-000002", "lvv-01"]
        )
        handoff["blocked_by_dispositions"] = {
            "lvv-01": {"disposition": "resolved_without_baton", "reason": "shipped then pruned"}
        }
        tc1 = _blocker(
            "hnd-tc1-c7-tri-000002",
            "shipped",
            shipped_in="9" * 40,
            blocks=["hnd-dep-c7-tri-000002"],
        )

        result = evaluate_gate_triage(handoff, [handoff, tc1])

        assert result["status"] == "still-blocked"
        assert result["shipped_ids"] == ["hnd-tc1-c7-tri-000002"]
        assert result["disposed_ids"] == ["lvv-01"]

    def test_undisposed_dangling_ref_still_indeterminate(self) -> None:
        handoff = _roadmap_handoff("hnd-dep-c7-tri-000003", blocked_by=["hnd-ghost-c7-tri-000003"])

        result = evaluate_gate_triage(handoff, [handoff])

        assert result["status"] == "indeterminate"
        assert result["unresolved_ids"] == ["hnd-ghost-c7-tri-000003"]
        assert result["disposed_ids"] == []


# ---------------------------------------------------------------------------
# C6d — downstream-consequence pin: WHY the roadmap-baton-supersession-hazard
# plan's C2/C3 (blocked_by_dependents refusal + d6 judgment-point) are
# load-bearing, not cosmetic. This class pins CURRENT gate_eval.py behaviour
# and is expected to PASS against HEAD — it is not a red test. It exists to
# make explicit what only lived implicitly in gate_eval.py's own docstring
# (rule 2 / module docstring "CLEAR predicate"): a `blocked_by` member
# stamped `deployment_state: continued` never mechanically clears its
# dependent's gate on its own — `evaluate_gate` requires SHIPPED
# specifically, and a `continued` member is only ever rescued by
# `_chase_continuation` resolving its `continued_into` terminus as
# genuinely `shipped` (see gate_eval.py's rule 2 exception). Neither
# `_chase_continuation` nor `_resolve_continuation_target` ever reads
# `kind` — the discriminator is `deployment_state` alone. When the chased
# terminus's `deployment_state` is not `shipped`, the chase yields
# outcome="open" and the dependent surfaces — permanently when that state
# is itself terminal, across repeat re-evaluation, since
# nothing in this module writes back a "seen and decided" marker. This is
# the hazard docs/plans/2026-08-02-roadmap-baton-supersession-hazard.md
# exists to guard against at supersession time: a candidate roadmap baton
# force-superseded (flipped to `continued`) while a LIVE dependent still
# lists it in `blocked_by` leaves that dependent SURFACE-locked forever,
# never auto-clearing, unless an operator (or C2's refusal / C3's
# judgment-point) intervenes before the supersession happens.
# ---------------------------------------------------------------------------


def _session_handoff_terminus(deployment_state: str) -> dict:
    """A session handoff (not a roadmap stub) as a `continued_into` terminus
    — resolved by durable `handoff_id`, `kind != "spinoff-roadmap"`, and
    deliberately left at a non-`shipped` deployment_state (the chase reaches
    it but finds it still open)."""
    return {
        "handoff_id": _TERMINUS_HANDOFF_ID,
        "kind": "session-handoff",
        "deployment_state": deployment_state,
    }


class TestC6dContinuedBlockerNeverMechanicallyClearsDependent:
    """Pins the permanent-hazard shape: a `continued` blocker whose chased
    terminus is a still-open session handoff surfaces the dependent, and
    stays surfaced identically across repeat re-evaluation passes — there is
    no mechanism anywhere in this module that ever flips this to `clear`
    short of the terminus itself shipping."""

    def test_continued_blocker_with_open_session_terminus_surfaces_and_stays_surfaced(
        self,
    ) -> None:
        lvv05 = _lvv05_continued(continued_into=_TERMINUS_HANDOFF_ID)
        lvv06 = _lvv06_dependent(blocked_by=["lvv-05"])
        terminus = _session_handoff_terminus("awaiting_gate")
        corpus = [lvv06, lvv05, terminus]

        first = evaluate_gate(lvv06, corpus)
        second = evaluate_gate(lvv06, corpus)

        assert first["verdict"] == "surface"
        assert first["verdict"] == second["verdict"]
        assert first["cleared_by_shas"] == second["cleared_by_shas"] == []
        assert first["cleared_blocker_ids"] == second["cleared_blocker_ids"] == []
        assert "lvv-05" in first["remaining_blockers"]
        assert any(
            "chased to terminus" in e and "not shipped" in e for e in first["evidence"]
        )
        # The chase never trusts kind — a session-handoff terminus (not a
        # roadmap stub) is treated identically to any other non-shipped
        # terminus: still open -> surface, never a clear.
        assert terminus["kind"] != "spinoff-roadmap"


# ---------------------------------------------------------------------------
# C2 — scaffold sentinel (docs/plans/2026-08-03-gate-dependency-template-
# emission-spec.md § C2): an unfilled coordinator-doc-new `PLACEHOLDER`
# default is not authored prose and must never clear/free.
# ---------------------------------------------------------------------------


class TestC2ScaffoldSentinelGateDependencySurfacesNeverClears:
    """AC2.1: `gate_dependency: PLACEHOLDER` + `blocked_by: []` -> `surface`,
    never `clear`, with a distinct evidence line naming the unfilled
    placeholder — not the generic prose-dominance/no-witness text."""

    def test_bare_placeholder_gate_dependency_empty_blocked_by_surfaces(self) -> None:
        handoff = {
            "id": "hnd-c2-gd-000001",
            "handoff_id": "hnd-c2-gd-000001",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
            "gate_dependency": "PLACEHOLDER",
        }

        result = evaluate_gate(handoff, [handoff])

        assert result["verdict"] == "surface"
        assert result["verdict"] != "clear"
        assert any("unfilled" in e and "scaffold placeholder" in e for e in result["evidence"])
        assert not any("no concrete checkable witness" in e for e in result["evidence"])
        assert not any("dominates" in e for e in result["evidence"])


class TestC2ScaffoldSentinelBlockingNotesSurfacesNeverClears:
    """AC2.2: same as AC2.1 but for `blocking_notes` carrying the C1
    scaffold's authored placeholder continuation."""

    def test_blocking_notes_placeholder_continuation_surfaces(self) -> None:
        handoff = {
            "id": "hnd-c2-bn-000001",
            "handoff_id": "hnd-c2-bn-000001",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
            "blocking_notes": (
                "PLACEHOLDER — name the condition gating this baton, or delete "
                "this line once blocked_by names it"
            ),
        }

        result = evaluate_gate(handoff, [handoff])

        assert result["verdict"] == "surface"
        assert any("unfilled" in e and "scaffold placeholder" in e for e in result["evidence"])
        assert not any("dominates" in e for e in result["evidence"])


class TestC2ScaffoldSentinelWithSatisfiedStructuredSetStillSurfaces:
    """The sentinel must never clear even when `blocked_by` is populated and
    every member has shipped — this is the exact "obvious but wrong"
    implementation the spec warns against (treating the sentinel as "no
    gate" would let this fall through to the vacuous-clear branch)."""

    def test_placeholder_gate_dependency_with_all_shipped_blocked_by_still_surfaces(
        self,
    ) -> None:
        dependent = _roadmap_handoff(
            "hnd-c2-satisfied-000001", blocked_by=["hnd-c2-tc1-000001"]
        )
        dependent["gate_dependency"] = "PLACEHOLDER"
        tc1 = _blocker(
            "hnd-c2-tc1-000001", "shipped", shipped_in="5" * 40,
            blocks=["hnd-c2-satisfied-000001"],
        )

        result = evaluate_gate(dependent, [dependent, tc1])

        assert result["verdict"] == "surface"
        assert result["cleared_by_shas"] == []


class TestC2ScaffoldSentinelBlockingNotesWithSatisfiedStructuredSetStillSurfaces:
    """AC4.5 (C4, docs/plans/2026-08-03-gate-dependency-template-emission-
    spec.md § C4): the C2 scaffold sentinel still precedes everything and
    still never clears, INCLUDING when the sentinel lands in `blocking_
    notes` rather than `gate_dependency` and `blocked_by` is non-empty and
    fully satisfied — the C4 blocking_notes demotion must not weaken the C2
    sentinel guard, which is now load-bearing for correctness in exactly
    this shape (see module docstring "C2 SCAFFOLD SENTINEL")."""

    def test_placeholder_blocking_notes_with_all_shipped_blocked_by_still_surfaces(
        self,
    ) -> None:
        dependent = _roadmap_handoff(
            "hnd-c2-bn-satisfied-000001", blocked_by=["hnd-c2-bn-tc1-000001"]
        )
        dependent["blocking_notes"] = (
            "PLACEHOLDER — name the condition gating this baton, or delete "
            "this line once blocked_by names it"
        )
        tc1 = _blocker(
            "hnd-c2-bn-tc1-000001", "shipped", shipped_in="4" * 40,
            blocks=["hnd-c2-bn-satisfied-000001"],
        )

        result = evaluate_gate(dependent, [dependent, tc1])

        assert result["verdict"] == "surface"
        assert result["cleared_by_shas"] == []
        assert any("unfilled" in e and "scaffold placeholder" in e for e in result["evidence"])


class TestC2ScaffoldSentinelPrefixTestNotSubstring:
    """AC2.3: a `gate_dependency` that merely CONTAINS the word "placeholder"
    in a real authored sentence is ordinary prose and keeps ordinary
    dominance (surfaces via the generic prose-dominance line, not the
    sentinel's distinct one) — pinning that the sentinel check is a PREFIX
    test, never a substring search."""

    def test_placeholder_word_in_real_sentence_keeps_ordinary_dominance(self) -> None:
        handoff = {
            "id": "hnd-c2-substring-000001",
            "handoff_id": "hnd-c2-substring-000001",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
            "gate_dependency": "blocked on the placeholder registry landing",
        }

        result = evaluate_gate(handoff, [handoff])

        # Still surfaces (no witness given) — but via the ORDINARY
        # `_evaluate_prose_gate` no-witness line, never the sentinel's.
        assert result["verdict"] == "surface"
        assert not any(
            "unfilled" in e and "scaffold placeholder" in e for e in result["evidence"]
        )
        assert any("no concrete checkable witness" in e for e in result["evidence"])


class TestC2WhitespaceIsEmptyBehaviourUnchanged:
    """AC2.4: a whitespace-only `gate_dependency`/`blocking_notes` is still
    treated as no gate at all (vacuously clears alongside an empty
    `blocked_by`) — the sentinel addition must not touch this pre-existing
    discipline."""

    def test_whitespace_only_gate_dependency_still_vacuously_clears(self) -> None:
        handoff = {
            "id": "hnd-c2-ws-gd-000001",
            "handoff_id": "hnd-c2-ws-gd-000001",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
            "gate_dependency": "   ",
        }

        result = evaluate_gate(handoff, [handoff])

        assert result["verdict"] == "clear"

    def test_whitespace_only_blocking_notes_still_vacuously_clears(self) -> None:
        handoff = {
            "id": "hnd-c2-ws-bn-000001",
            "handoff_id": "hnd-c2-ws-bn-000001",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
            "blocking_notes": "   ",
        }

        result = evaluate_gate(handoff, [handoff])

        assert result["verdict"] == "clear"


class TestC2TriageProjectionParity:
    """The sentinel treatment mirrors into `evaluate_gate_triage` too (the
    module's own "exactly one gate evaluator" discipline — see
    `TestBlockingNotesDominatesSatisfiedStructuredSet`'s triage sibling
    elsewhere in this file for the established pattern): `indeterminate`,
    never `freed`, with the same distinct evidence line."""

    def test_placeholder_gate_dependency_empty_blocked_by_is_indeterminate(self) -> None:
        handoff = {
            "id": "hnd-c2-tri-000001",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
            "gate_dependency": "PLACEHOLDER",
        }

        result = evaluate_gate_triage(handoff, [handoff])

        assert result["status"] == "indeterminate"
        assert result["status"] != "freed"
        assert any("unfilled" in e and "scaffold placeholder" in e for e in result["evidence"])


# ---------------------------------------------------------------------------
# C3 — staleness evidence on dominance (docs/plans/2026-08-03-gate-
# dependency-template-emission-spec.md § C3): dominance's verdict never
# changes, but the evidence names it when every structured co-blocker has
# already shipped out from under the prose.
# ---------------------------------------------------------------------------


class TestC3DominanceStaleEvidenceAllShipped:
    """AC3.1: dominance fires (rule 1, prose `gate_dependency`) + non-empty
    `blocked_by` + every member shipped -> verdict stays `surface`, evidence
    gains an addendum naming each blocker id and its shipping sha."""

    def test_prose_dominance_with_all_blocked_by_shipped_names_staleness(self) -> None:
        dependent = _roadmap_handoff(
            "hnd-c3-stale-000001", blocked_by=["hnd-c3-tc1-000001"]
        )
        dependent["gate_dependency"] = (
            "no acceptance oracle exists until success criteria ship"
        )
        tc1 = _blocker(
            "hnd-c3-tc1-000001", "shipped", shipped_in="b7d08bde" + "0" * 32,
            blocks=["hnd-c3-stale-000001"],
        )

        result = evaluate_gate(dependent, [dependent, tc1])

        assert result["verdict"] == "surface"
        assert any(
            "every structured blocked_by member has since shipped" in e
            for e in result["evidence"]
        )
        assert any("hnd-c3-tc1-000001" in e and "shipped" in e for e in result["evidence"])
        # C1: the same all-shipped-under-dominance shape also names itself
        # machine-legibly via `contradiction`, not only in evidence prose.
        assert result["contradiction"] == {
            "kind": "prose-gate-outlived-structured-blockers",
            "discharge_verb": "handoff.transition gate-recheck --cleared",
            "shipped_blocker_ids": ["hnd-c3-tc1-000001"],
        }


class TestC3StalenessEvidenceNormalizationAgreesAcrossEvaluators:
    """Review: code-reviewer (Finding 1, P1) — `evaluate_gate` and
    `evaluate_gate_triage` must key `_all_blocked_by_shipped_evidence` on the
    SAME (str-normalized) `blocked_by` precondition, or a non-`str` member
    (e.g. `None`) lets the two evaluators disagree about whether the
    contradiction fired. Constructed per the reviewer's own drift scenario:
    `blocked_by: ["<shipped-id>", None]`. The assertion is that the two
    evaluators AGREE — not which verdict they land on."""

    def test_non_str_blocked_by_member_does_not_drift_staleness_verdict(self) -> None:
        dependent = _roadmap_handoff(
            "hnd-c3-drift-000001", blocked_by=["hnd-c3-drift-tc1-000001", None]
        )
        dependent["gate_dependency"] = "stale prose, structurally outlived"
        tc1 = _blocker(
            "hnd-c3-drift-tc1-000001", "shipped", shipped_in="7" * 40,
            blocks=["hnd-c3-drift-000001"],
        )

        gate_result = evaluate_gate(dependent, [dependent, tc1])
        triage_result = evaluate_gate_triage(dependent, [dependent, tc1])

        gate_has_contradiction = "contradiction" in gate_result
        triage_is_review_due = triage_result["status"] == "review-due"
        assert gate_has_contradiction == triage_is_review_due, (
            f"evaluate_gate contradiction={gate_has_contradiction!r} vs "
            f"evaluate_gate_triage status={triage_result['status']!r} — "
            "the two evaluators disagreed about whether the structured "
            "graph is stale under this same precondition"
        )


class TestC4BlockingNotesStructuredAllShippedNoLongerNeedsStalenessEvidence:
    """C4 (docs/plans/2026-08-03-gate-dependency-template-emission-spec.md
    § C4, AC4.7): the C3 staleness-evidence addendum on rule 1a (`blocking_
    notes`) is now UNREACHABLE for a non-empty `blocked_by` — rule 1a only
    ever fires when `blocked_by` is empty (see module docstring "C3
    STALENESS EVIDENCE ON DOMINANCE" and "BLOCKING_NOTES DOMINANCE"), where
    `_all_blocked_by_shipped_evidence` always returns `None` (AC3.3). A
    handoff shaped exactly like the old `TestC3DominanceStaleEvidence
    BlockingNotesVariant` fixture (non-empty `blocked_by`, all shipped,
    non-empty `blocking_notes`) now simply CLEARS via the structured walk
    (AC4.1) — there is no staleness addendum to name because the verdict is
    no longer `surface`. The addendum remains reachable only via rule 1
    (`gate_dependency`) — see `TestC3DominanceStaleEvidenceAllShipped`
    elsewhere in this file, unaffected by this chunk."""

    def test_blocking_notes_with_all_blocked_by_shipped_clears_no_staleness_evidence(
        self,
    ) -> None:
        dependent = _roadmap_handoff(
            "hnd-c3-bn-stale-000001", blocked_by=["hnd-c3-bn-tc1-000001"]
        )
        dependent["blocking_notes"] = "Windows machine required for AC7 verification"
        tc1 = _blocker(
            "hnd-c3-bn-tc1-000001", "shipped", shipped_in="6" * 40,
            blocks=["hnd-c3-bn-stale-000001"],
        )

        result = evaluate_gate(dependent, [dependent, tc1])

        assert result["verdict"] == "clear"
        assert result["cleared_by_shas"] == ["6" * 40]
        assert not any(
            "every structured blocked_by member has since shipped" in e
            for e in result["evidence"]
        )


class TestC3DominanceNoStaleClaimWhenOneMemberUnshipped:
    """AC3.2: at least one `blocked_by` member unshipped -> evidence carries
    NO staleness claim — never assert staleness we cannot substantiate."""

    def test_prose_dominance_with_one_unshipped_member_names_no_staleness(self) -> None:
        dependent = _roadmap_handoff(
            "hnd-c3-partial-000001",
            blocked_by=["hnd-c3-shipped-000001", "hnd-c3-open-000001"],
        )
        dependent["gate_dependency"] = "claude-klabauter action layer live; cutover landed"
        shipped = _blocker(
            "hnd-c3-shipped-000001", "shipped", shipped_in="7" * 40,
            blocks=["hnd-c3-partial-000001"],
        )
        still_open = _blocker(
            "hnd-c3-open-000001", "awaiting_gate", blocks=["hnd-c3-partial-000001"]
        )

        result = evaluate_gate(dependent, [dependent, shipped, still_open])

        assert result["verdict"] == "surface"
        assert not any(
            "every structured blocked_by member has since shipped" in e
            for e in result["evidence"]
        )
        # C1: no staleness evidence -> no `contradiction` key at all — absent,
        # never present-and-None (a `.get(...) is None` check would pass
        # against a broken implementation that stamped `None` in).
        assert "contradiction" not in result

    def test_blocking_notes_dominance_with_unresolved_member_names_no_staleness(self) -> None:
        dependent = _roadmap_handoff(
            "hnd-c3-dangling-000001", blocked_by=["hnd-c3-nowhere-000001"]
        )
        dependent["blocking_notes"] = "Windows machine required for AC7 verification"

        result = evaluate_gate(dependent, [dependent])

        assert result["verdict"] == "surface"
        assert not any(
            "every structured blocked_by member has since shipped" in e
            for e in result["evidence"]
        )
        assert "contradiction" not in result


class TestC3DominanceNoStaleClaimWhenBlockedByEmpty:
    """AC3.3: dominance + empty `blocked_by` -> unchanged from today, no new
    evidence line at all."""

    def test_prose_dominance_with_empty_blocked_by_gains_no_staleness_line(self) -> None:
        handoff = {
            "id": "hnd-c3-empty-000001",
            "handoff_id": "hnd-c3-empty-000001",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
            "gate_dependency": "some vague subsystem condition",
        }

        result = evaluate_gate(handoff, [handoff])

        assert result["verdict"] == "surface"
        assert len(result["evidence"]) == 1
        assert not any(
            "every structured blocked_by member has since shipped" in e
            for e in result["evidence"]
        )
        assert "contradiction" not in result

    def test_blocking_notes_dominance_with_empty_blocked_by_gains_no_staleness_line(
        self,
    ) -> None:
        handoff = {
            "id": "hnd-c3-bn-empty-000001",
            "handoff_id": "hnd-c3-bn-empty-000001",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
            "blocking_notes": "Windows machine required for AC7 verification",
        }

        result = evaluate_gate(handoff, [handoff])

        assert result["verdict"] == "surface"
        assert len(result["evidence"]) == 1
        assert not any(
            "every structured blocked_by member has since shipped" in e
            for e in result["evidence"]
        )
        assert "contradiction" not in result


class TestTriageReviewDueRerouteNotFiredWhenOneMemberUnshipped:
    """Triage-side twin of `TestC3DominanceNoStaleClaimWhenOneMemberUnshipped`
    (AC3.2): staleness cannot be substantiated when at least one `blocked_by`
    member is unshipped, so `evaluate_gate_triage` must NOT re-route onto
    `review-due` — it stays the pre-existing `indeterminate` status, same as
    before C2's re-route existed."""

    def test_prose_gate_with_one_unshipped_member_stays_indeterminate(self) -> None:
        dependent = _roadmap_handoff(
            "hnd-tri-c3-partial-000001",
            blocked_by=["hnd-tri-c3-shipped-000001", "hnd-tri-c3-open-000001"],
        )
        dependent["gate_dependency"] = "claude-klabauter action layer live; cutover landed"
        shipped = _blocker(
            "hnd-tri-c3-shipped-000001", "shipped", shipped_in="8" * 40,
            blocks=["hnd-tri-c3-partial-000001"],
        )
        still_open = _blocker(
            "hnd-tri-c3-open-000001", "awaiting_gate", blocks=["hnd-tri-c3-partial-000001"]
        )

        result = evaluate_gate_triage(dependent, [dependent, shipped, still_open])

        assert result["status"] == "indeterminate"


class TestTriageReviewDueRerouteNotFiredWhenBlockedByEmpty:
    """Triage-side twin of `TestC3DominanceNoStaleClaimWhenBlockedByEmpty`
    (AC3.3): an empty `blocked_by` has nothing to have shipped, so
    `evaluate_gate_triage` must NOT re-route onto `review-due` — it stays
    the pre-existing `indeterminate` status."""

    def test_prose_gate_with_empty_blocked_by_stays_indeterminate(self) -> None:
        handoff = {
            "id": "hnd-tri-c3-empty-000001",
            "handoff_id": "hnd-tri-c3-empty-000001",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
            "gate_dependency": "some vague subsystem condition",
        }

        result = evaluate_gate_triage(handoff, [handoff])

        assert result["status"] == "indeterminate"


# ---------------------------------------------------------------------------
# consumes_gate_evidence — the single-source-of-truth predicate mirroring
# evaluate_gate's own SC / demoted-1a / rule-0 precedence, exported so
# handoff_reconcile.py never re-derives it locally (the exact bug class
# this module's own docstring already records once — see "C4
# RECONCILIATION" — recurring because a mirror drifted when the precedence
# order itself moved underneath it, DR-259 + the C2 scaffold sentinel).
# ---------------------------------------------------------------------------


def _io_leg_covers_prose_true(observed: bool = True) -> dict:
    return {
        "covers_prose": True,
        "legs": [
            {
                "leg_id": "leg-1",
                "kind": "commit-ancestor",
                "expected": None,
                "read_ok": True,
                "observed": observed,
                "error": None,
            }
        ],
    }


class TestConsumesGateEvidencePostC4UnderReport:
    """AC5.1: a satisfied, non-empty `blocked_by` + non-empty `blocking_notes`
    + prose `gate_dependency` + `gate_evidence.covers_prose: True` now DOES
    reach rule 0 in `evaluate_gate` and its evidence IS consumed (the demoted
    `blocking_notes` rule 1a only applies when `blocked_by` is empty) — but
    the OLD locally-reimplemented predicate
    (`_has_prose_gate(h) and not _has_blocking_notes(h) and ...`) reports
    `False` here, an under-report. `consumes_gate_evidence` must report
    `True`, and must agree with `evaluate_gate`'s actual verdict (which
    clears via rule 0's evidence, not via the structured walk)."""

    def _fixture(self):
        dependent = _roadmap_handoff(
            "hnd-ac51-dep-000001", blocked_by=["hnd-ac51-tc1-000001"]
        )
        dependent["gate_dependency"] = "sibling repo ships the widgetforge feature"
        dependent["blocking_notes"] = "still waiting on a human call"
        blocker = _blocker(
            "hnd-ac51-tc1-000001", "shipped", shipped_in="a" * 40,
            blocks=["hnd-ac51-dep-000001"],
        )
        gate_evidence = _io_leg_covers_prose_true(observed=True)
        return dependent, blocker, gate_evidence

    def test_pre_fix_predicate_would_under_report(self) -> None:
        """Pins the defect: the old inline expression this module replaces
        reports False on exactly this shape."""
        dependent, _blocker_h, gate_evidence = self._fixture()
        has_prose = bool(str(dependent.get("gate_dependency") or "").strip())
        has_blocking_notes = bool(str(dependent.get("blocking_notes") or "").strip())
        pre_fix_evidence_consumed = bool(
            has_prose and not has_blocking_notes and gate_evidence
            and gate_evidence.get("covers_prose")
        )
        assert pre_fix_evidence_consumed is False

    def test_consumes_gate_evidence_reports_true(self) -> None:
        dependent, blocker, gate_evidence = self._fixture()

        assert consumes_gate_evidence(dependent, gate_evidence) is True

    def test_evaluate_gate_actually_consults_the_evidence(self) -> None:
        dependent, blocker, gate_evidence = self._fixture()

        result = evaluate_gate(
            dependent, [dependent, blocker], gate_evidence=gate_evidence
        )

        assert result["verdict"] == "clear"
        assert any("demoted to commentary" in e for e in result["evidence"])
        assert consumes_gate_evidence(dependent, gate_evidence) is True


class TestConsumesGateEvidenceSentinelOverReport:
    """AC5.2: an unfilled C1 scaffold sentinel in `gate_dependency` makes
    `_has_prose_gate` True, so the OLD predicate reports `evidence_consumed=
    True` — but `evaluate_gate` short-circuits at rule SC and never consults
    `gate_evidence` at all. `consumes_gate_evidence` must report `False`."""

    def _fixture(self):
        handoff = {
            "id": "hnd-ac52-000001",
            "handoff_id": "hnd-ac52-000001",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
            "gate_dependency": "PLACEHOLDER",
        }
        gate_evidence = _io_leg_covers_prose_true(observed=True)
        return handoff, gate_evidence

    def test_pre_fix_predicate_would_over_report(self) -> None:
        handoff, gate_evidence = self._fixture()
        has_prose = bool(str(handoff.get("gate_dependency") or "").strip())
        has_blocking_notes = bool(str(handoff.get("blocking_notes") or "").strip())
        pre_fix_evidence_consumed = bool(
            has_prose and not has_blocking_notes and gate_evidence
            and gate_evidence.get("covers_prose")
        )
        assert pre_fix_evidence_consumed is True

    def test_consumes_gate_evidence_reports_false(self) -> None:
        handoff, gate_evidence = self._fixture()

        assert consumes_gate_evidence(handoff, gate_evidence) is False

    def test_evaluate_gate_never_reaches_rule_0(self) -> None:
        handoff, gate_evidence = self._fixture()

        result = evaluate_gate(handoff, [handoff], gate_evidence=gate_evidence)

        assert result["verdict"] == "surface"
        assert not any("demoted to commentary" in e for e in result["evidence"])
        assert any(
            "unfilled" in e and "scaffold placeholder" in e for e in result["evidence"]
        )
        assert consumes_gate_evidence(handoff, gate_evidence) is False


class TestConsumesGateEvidenceVacuousBlockingNotesUnchanged:
    """AC5.3: empty `blocked_by` + non-empty `blocking_notes` + prose +
    `covers_prose: True` -> False (notes still intercept ahead of rule 0 in
    the vacuous case; unchanged pre-existing behaviour)."""

    def test_empty_blocked_by_blocking_notes_intercepts_before_rule_0(self) -> None:
        handoff = {
            "id": "hnd-ac53-000001",
            "handoff_id": "hnd-ac53-000001",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
            "gate_dependency": "sibling repo ships the widgetforge feature",
            "blocking_notes": "still waiting on a human call",
        }
        gate_evidence = _io_leg_covers_prose_true(observed=True)

        assert consumes_gate_evidence(handoff, gate_evidence) is False

        result = evaluate_gate(handoff, [handoff], gate_evidence=gate_evidence)
        assert result["verdict"] == "surface"
        assert not any("demoted to commentary" in e for e in result["evidence"])


class TestConsumesGateEvidenceNoEvidenceOrNotCoveringProse:
    """AC5.4: no `gate_evidence`, or `covers_prose` absent/False -> False."""

    def _dependent(self):
        return {
            "id": "hnd-ac54-000001",
            "handoff_id": "hnd-ac54-000001",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocked_by": [],
            "gate_dependency": "sibling repo ships the widgetforge feature",
        }

    def test_no_gate_evidence(self) -> None:
        assert consumes_gate_evidence(self._dependent(), None) is False

    def test_covers_prose_absent(self) -> None:
        gate_evidence = {"legs": []}
        assert consumes_gate_evidence(self._dependent(), gate_evidence) is False

    def test_covers_prose_false(self) -> None:
        gate_evidence = _io_leg_covers_prose_true(observed=True)
        gate_evidence["covers_prose"] = False
        assert consumes_gate_evidence(self._dependent(), gate_evidence) is False


class TestConsumesGateEvidenceAgreesWithEvaluateGateActualBehaviour:
    """AC5.5 (the anti-drift AC): `consumes_gate_evidence` is pinned against
    `evaluate_gate`'s ACTUAL behaviour, not a restatement of the predicate —
    for every field combination below, `consumes_gate_evidence`'s verdict
    must agree with whether `evaluate_gate` actually reached rule 0 (pinned
    by the literal "demoted to commentary" marker rule 0 alone emits). A
    future precedence-order edit to `evaluate_gate` that updates only one of
    the two functions fails THIS test loudly, not merely the fixed-shape
    ACs above."""

    def _cases(self):
        blocker = _blocker(
            "hnd-ac55-tc1-000001", "shipped", shipped_in="b" * 40,
            blocks=["hnd-ac55-dep-000001"],
        )
        base_dependent = {
            "id": "hnd-ac55-dep-000001",
            "handoff_id": "hnd-ac55-dep-000001",
            "kind": "spinoff-roadmap",
            "deployment_state": "awaiting_gate",
            "blocks": [],
        }
        covering = _io_leg_covers_prose_true(observed=True)
        non_covering = _io_leg_covers_prose_true(observed=True)
        non_covering["covers_prose"] = False

        def _h(**overrides):
            h = dict(base_dependent)
            h.update(overrides)
            return h

        return [
            # AC5.1 shape: satisfied blocked_by + blocking_notes + prose + covers_prose.
            (
                _h(
                    blocked_by=["hnd-ac55-tc1-000001"],
                    gate_dependency="sibling repo ships the widgetforge feature",
                    blocking_notes="still waiting on a human call",
                ),
                [blocker],
                covering,
            ),
            # AC5.2 shape: scaffold sentinel + covers_prose.
            (
                _h(blocked_by=[], gate_dependency="PLACEHOLDER"),
                [],
                covering,
            ),
            # AC5.3 shape: empty blocked_by + blocking_notes + prose + covers_prose.
            (
                _h(
                    blocked_by=[],
                    gate_dependency="sibling repo ships the widgetforge feature",
                    blocking_notes="still waiting on a human call",
                ),
                [],
                covering,
            ),
            # AC5.4 shape: no gate_evidence.
            (
                _h(blocked_by=[], gate_dependency="sibling repo ships the widgetforge feature"),
                [],
                None,
            ),
            # AC5.4 shape: covers_prose False.
            (
                _h(blocked_by=[], gate_dependency="sibling repo ships the widgetforge feature"),
                [],
                non_covering,
            ),
            # Plain prose-only rule-0 clear (the oaxis-01 shape).
            (
                _h(blocked_by=[], gate_dependency="sibling repo ships the widgetforge feature"),
                [],
                covering,
            ),
            # Non-empty blocked_by + prose (no blocking_notes) + covers_prose.
            (
                _h(
                    blocked_by=["hnd-ac55-tc1-000001"],
                    gate_dependency="sibling repo ships the widgetforge feature",
                ),
                [blocker],
                covering,
            ),
        ]

    def test_predicate_agrees_with_evaluate_gate_across_the_matrix(self) -> None:
        for dependent, blockers, gate_evidence in self._cases():
            all_handoffs = [dependent] + blockers
            result = evaluate_gate(dependent, all_handoffs, gate_evidence=gate_evidence)
            rule_0_actually_fired = any(
                "demoted to commentary" in e for e in result["evidence"]
            )

            predicate_says = consumes_gate_evidence(dependent, gate_evidence)

            assert predicate_says == rule_0_actually_fired, (
                dependent, gate_evidence, result["evidence"],
            )


# ---------------------------------------------------------------------------
# C6 — a shipped blocker with no shipped_in must not enter the cleared set
# (docs/plans/2026-08-05-c2-supersede-gate-chaseable-terminus.md § C6):
# `_classify_blocked_by`'s plain-`shipped` and chased-`shipped` branches both
# appended unconditionally to `shipped_ids` but only conditionally to
# `shipped_shas`, producing unequal `cleared_blocker_ids`/`cleared_by_shas`
# arrays. The fix routes a shipped-but-unstamped terminus into a NEW
# seventh `unstamped_shipped_ids` bucket instead — it surfaces, never
# clears the paired arrays.
# ---------------------------------------------------------------------------


class TestC6PlainShippedNoShaSurfacesNeverClears:
    """AC8a: a plain-`shipped` blocker with no `shipped_in` is absent from
    `cleared_blocker_ids`, present in `remaining_blockers`, and drives
    verdict `surface` (sole blocker) or `narrow`+`also_surface=True` (a
    co-blocker is shipped-with-sha) — evidence names both the diagnosis
    (no shipped_in) and the repair (kind: no-commit)."""

    def test_sole_unstamped_shipped_blocker_surfaces(self) -> None:
        dependent = _roadmap_handoff("hnd-c6-plain-000001", blocked_by=["hnd-c6-b1-000001"])
        b1 = _blocker("hnd-c6-b1-000001", "shipped", blocks=["hnd-c6-plain-000001"])

        result = evaluate_gate(dependent, [dependent, b1])

        assert result["verdict"] == "surface"
        assert result["verdict"] != "clear"
        assert "hnd-c6-b1-000001" not in result["cleared_blocker_ids"]
        assert "hnd-c6-b1-000001" in result["remaining_blockers"]
        assert len(result["cleared_blocker_ids"]) == len(result["cleared_by_shas"])
        assert any(
            "carries no shipped_in" in e and "kind: no-commit" in e
            for e in result["evidence"]
        )

    def test_unstamped_shipped_co_blocker_narrows_and_also_surfaces(self) -> None:
        dependent = _roadmap_handoff(
            "hnd-c6-plain-000002",
            blocked_by=["hnd-c6-b1-000002", "hnd-c6-b2-000002"],
        )
        shipped_with_sha = _blocker(
            "hnd-c6-b1-000002", "shipped", shipped_in="c" * 40,
            blocks=["hnd-c6-plain-000002"],
        )
        shipped_no_sha = _blocker(
            "hnd-c6-b2-000002", "shipped", blocks=["hnd-c6-plain-000002"]
        )

        result = evaluate_gate(dependent, [dependent, shipped_with_sha, shipped_no_sha])

        assert result["verdict"] == "narrow"
        assert result["also_surface"] is True
        assert result["cleared_blocker_ids"] == ["hnd-c6-b1-000002"]
        assert result["cleared_by_shas"] == ["c" * 40]
        assert "hnd-c6-b2-000002" in result["remaining_blockers"]
        assert len(result["cleared_blocker_ids"]) == len(result["cleared_by_shas"])
        assert any(
            "carries no shipped_in" in e and "kind: no-commit" in e
            for e in result["evidence"]
        )


class TestC6ChasedShippedNoShaSurfacesNeverClears:
    """AC8b: the same holds for a `continued` blocker whose CHASE terminus
    reads `shipped` with no `shipped_in` — absent from `cleared_blocker_ids`,
    present in `remaining_blockers`, verdict `surface`/`narrow`+
    `also_surface=True` per the same disjunction, evidence containing the
    same two substrings."""

    def test_sole_chased_unstamped_shipped_terminus_surfaces(self) -> None:
        lvv05 = _lvv05_continued(continued_into=_TERMINUS_HANDOFF_ID)
        lvv06 = _lvv06_dependent(blocked_by=["lvv-05"])
        terminus = _terminus("shipped")  # no shipped_in

        result = evaluate_gate(lvv06, [lvv06, lvv05, terminus])

        assert result["verdict"] == "surface"
        assert result["verdict"] != "clear"
        assert "lvv-05" not in result["cleared_blocker_ids"]
        assert "lvv-05" in result["remaining_blockers"]
        assert len(result["cleared_blocker_ids"]) == len(result["cleared_by_shas"])
        assert any(
            "carries no shipped_in" in e and "kind: no-commit" in e
            for e in result["evidence"]
        )

    def test_chased_unstamped_shipped_co_blocker_narrows_and_also_surfaces(self) -> None:
        lvv05 = _lvv05_continued(continued_into=_TERMINUS_HANDOFF_ID)
        lvv06 = _lvv06_dependent(blocked_by=["lvv-05", "lvv-03"])
        lvv03 = _blocker(
            "lvv-03", "shipped", shipped_in="d" * 40, blocks=["lvv-06", "lvv-07", "lvv-08"]
        )
        terminus = _terminus("shipped")  # no shipped_in

        result = evaluate_gate(lvv06, [lvv06, lvv05, lvv03, terminus])

        assert result["verdict"] == "narrow"
        assert result["also_surface"] is True
        assert result["cleared_blocker_ids"] == ["lvv-03"]
        assert result["cleared_by_shas"] == ["d" * 40]
        assert "lvv-05" in result["remaining_blockers"]
        assert len(result["cleared_blocker_ids"]) == len(result["cleared_by_shas"])
        assert any(
            "carries no shipped_in" in e and "kind: no-commit" in e
            for e in result["evidence"]
        )


class TestC6TriageUnstampedShippedNeverThroughDeadIds:
    """`evaluate_gate_triage` must NOT flip `status` to `indeterminate` via
    `dead_ids`'s "never shipped" reason text for a shipped-no-sha blocker —
    it folds into `still-blocked` via its own reason text instead."""

    def test_sole_unstamped_shipped_blocker_is_still_blocked_not_indeterminate(
        self,
    ) -> None:
        dependent = _roadmap_handoff("hnd-c6-tri-000001", blocked_by=["hnd-c6-tri-b1-000001"])
        b1 = _blocker("hnd-c6-tri-b1-000001", "shipped", blocks=["hnd-c6-tri-000001"])

        result = evaluate_gate_triage(dependent, [dependent, b1])

        assert result["status"] == "still-blocked"
        assert result["status"] != "indeterminate"
        assert result["dead_ids"] == []
        assert result["unstamped_shipped_ids"] == ["hnd-c6-tri-b1-000001"]
        assert "never shipped" not in result["reason"]

    def test_sole_chased_unstamped_shipped_terminus_is_still_blocked_not_indeterminate(
        self,
    ) -> None:
        lvv05 = _lvv05_continued(continued_into=_TERMINUS_HANDOFF_ID)
        lvv06 = _lvv06_dependent(blocked_by=["lvv-05"])
        terminus = _terminus("shipped")  # no shipped_in

        result = evaluate_gate_triage(lvv06, [lvv06, lvv05, terminus])

        assert result["status"] == "still-blocked"
        assert result["status"] != "indeterminate"
        assert result["dead_ids"] == []
        assert result["unstamped_shipped_ids"] == ["lvv-05"]
        assert "never shipped" not in result["reason"]


class TestC6StalenessEvidenceNoneForUnstampedShipped:
    """`_all_blocked_by_shipped_evidence` returns `None` (not staleness
    evidence) when the sole `blocked_by` member is shipped-no-sha —
    exercised via `evaluate_gate`'s prose-dominance path, the sole caller of
    that helper."""

    def test_prose_dominance_with_unstamped_shipped_blocker_asserts_no_staleness(
        self,
    ) -> None:
        dependent = _roadmap_handoff(
            "hnd-c6-stale-000001", blocked_by=["hnd-c6-stale-b1-000001"]
        )
        dependent["gate_dependency"] = (
            "no acceptance oracle exists until success criteria ship"
        )
        b1 = _blocker(
            "hnd-c6-stale-b1-000001", "shipped", blocks=["hnd-c6-stale-000001"]
        )

        result = evaluate_gate(dependent, [dependent, b1])

        assert result["verdict"] == "surface"
        assert not any(
            "every structured blocked_by member has since shipped" in e
            for e in result["evidence"]
        )


class TestC6PairedArrayInvariantMatrix:
    """AC9: `len(cleared_blocker_ids) == len(cleared_by_shas)` holds across
    a parameterised matrix over every `HANDOFF_TERMINAL_DEPLOYMENT` member
    plus the live states, crossed with `shipped_in ∈ {valid sha, the
    no-commit token, None, "", non-str}`, on every cell — for both the
    plain and chased branches — not by two hand-written examples."""

    _ALL_STATES = sorted(
        HANDOFF_TERMINAL_DEPLOYMENT | {"awaiting_gate", "ready_to_fire", "in_flight"}
    )
    _SHIPPED_IN_VALUES = (
        "a" * 40,
        "substantively-shipped-no-commit:2026-08-05",
        None,
        "",
        42,
    )

    @pytest.mark.parametrize("state", _ALL_STATES)
    @pytest.mark.parametrize("shipped_in", _SHIPPED_IN_VALUES)
    def test_plain_branch_paired_arrays_every_cell(self, state, shipped_in) -> None:
        dependent = _roadmap_handoff("hnd-c6-mtx-000001", blocked_by=["hnd-c6-mtx-b1-000001"])
        blocker = {
            "id": "hnd-c6-mtx-b1-000001",
            "handoff_id": "hnd-c6-mtx-b1-000001",
            "kind": "spinoff-roadmap",
            "deployment_state": state,
            "blocks": ["hnd-c6-mtx-000001"],
        }
        if shipped_in is not None:
            blocker["shipped_in"] = shipped_in

        result = evaluate_gate(dependent, [dependent, blocker])

        assert len(result["cleared_blocker_ids"]) == len(result["cleared_by_shas"])

    @pytest.mark.parametrize("state", _ALL_STATES)
    @pytest.mark.parametrize("shipped_in", _SHIPPED_IN_VALUES)
    def test_chased_branch_paired_arrays_every_cell(self, state, shipped_in) -> None:
        lvv05 = _lvv05_continued(continued_into=_TERMINUS_HANDOFF_ID)
        lvv06 = _lvv06_dependent(blocked_by=["lvv-05"])
        terminus = _terminus(state)
        if shipped_in is not None:
            terminus["shipped_in"] = shipped_in

        result = evaluate_gate(lvv06, [lvv06, lvv05, terminus])

        assert len(result["cleared_blocker_ids"]) == len(result["cleared_by_shas"])
