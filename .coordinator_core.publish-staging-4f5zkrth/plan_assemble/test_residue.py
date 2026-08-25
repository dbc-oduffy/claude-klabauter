"""
coordinator_core.plan_assemble.test_residue — co-located pytest for
coordinator_core.plan_assemble.residue.brief.

Per-AC conformance test, mirroring
`coordinator_core.review_assemble.test_residue`'s shape: build segment
corpora as fixture directories under `tmp_path`, monkeypatch content-root
resolution — never read the live DoE-claude tree, which does not carry
`skills/plan/residue` at all (see this chunk's plan, substrate finding).

Run: python -m pytest coordinator_core/plan_assemble/test_residue.py -q

Spec backlink: pln-plan-assemble-brief-route-the-2d016a, chunk C1
Spec backlink: pln-plan-assemble-admits-instead-o-e441e3, chunk C1
Spec backlink: pln-plan-assemble-admits-instead-o-e441e3, chunk C2
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coordinator_core.contract.decision_object.envelope import ENVELOPE_KEYS
from coordinator_core.resolve_coordinator_clone import ResolveCoordinatorCloneError
from coordinator_core.plan_assemble import residue as residue_mod
from coordinator_core.plan_assemble.residue import (
    DEFAULT_ROUTE,
    ResidueAssembleError,
    RouteUsageError,
    brief,
)

# Declares a real external-process spawn (spawn ratchet Rule 2). Tiering onto the
# cadence suite is the separate threshold ruling, not this declaration.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _segment_text(segment_id: str, route: str, cls: str, order: int, body: str) -> str:
    return (
        "---\n"
        f"segment_id: {segment_id}\n"
        f"route: {route}\n"
        f"class: {cls}\n"
        f"order: {order}\n"
        "---\n"
        f"{body}\n"
    )


def _make_residue_dir(
    tmp_path: Path,
    *,
    include_plan: bool = True,
    include_spec_dispatch: bool = True,
    include_shared: bool = True,
) -> Path:
    content_root = tmp_path / "content-root"
    residue_dir = content_root / "skills" / "plan" / "residue"
    residue_dir.mkdir(parents=True)
    if include_shared:
        (residue_dir / "010-shared.md").write_text(
            _segment_text("shared-reminder", "shared", "protected", 0, "Shared body."),
            encoding="utf-8",
        )
    if include_plan:
        (residue_dir / "020-plan.md").write_text(
            _segment_text("plan-reminder", "plan", "droppable", 1, "Plan body."),
            encoding="utf-8",
        )
    if include_spec_dispatch:
        (residue_dir / "030-spec-dispatch.md").write_text(
            _segment_text(
                "spec-dispatch-reminder", "spec-dispatch", "droppable", 2,
                "Spec-dispatch body.",
            ),
            encoding="utf-8",
        )
    return content_root


def _patch_content_root(monkeypatch: pytest.MonkeyPatch, content_root: Path) -> None:
    monkeypatch.setattr(residue_mod, "resolve_content_root", lambda: str(content_root))


# ---------------------------------------------------------------------------
# AC-1 — explicit --route plan / --route spec-dispatch each return an
# envelope, exit 0 (checked at the compute layer: no exception, correct
# key-set).
# ---------------------------------------------------------------------------


def test_envelope_key_set_is_exactly_the_eight_canonical_keys_plus_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    result = brief(explicit_route="plan")

    envelope_only = {k: v for k, v in result.items() if k != "segments"}
    assert set(envelope_only.keys()) == set(ENVELOPE_KEYS)
    assert set(result.keys()) == set(ENVELOPE_KEYS) | {"segments"}


def test_explicit_route_spec_dispatch_selects_spec_dispatch_and_shared_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    result = brief(explicit_route="spec-dispatch")

    segment_ids = {s["segment_id"] for s in result["segments"]}
    assert segment_ids == {"shared-reminder", "spec-dispatch-reminder"}
    assert result["artifact"]["route"] == "spec-dispatch"


def test_explicit_route_plan_selects_plan_and_shared_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    result = brief(explicit_route="plan")

    segment_ids = {s["segment_id"] for s in result["segments"]}
    assert segment_ids == {"shared-reminder", "plan-reminder"}
    assert result["artifact"]["route"] == "plan"


# ---------------------------------------------------------------------------
# AC-2 — absent --route resolves to `plan`, not an error, not a judgment
# point, not an inference.
# ---------------------------------------------------------------------------


def test_absent_route_resolves_to_default_route_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    assert DEFAULT_ROUTE == "plan"

    result_absent = brief()
    result_explicit = brief(explicit_route="plan")

    assert result_absent["artifact"]["route"] == "plan"
    assert result_absent["judgment_points"] == []
    assert {s["segment_id"] for s in result_absent["segments"]} == {
        s["segment_id"] for s in result_explicit["segments"]
    }


# ---------------------------------------------------------------------------
# AC-3 — illegal --route values raise RouteUsageError before any disk
# access, never a silent fallthrough or inference.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "illegal_route", ["dispatch", "shape", "roadmap", "pm-decision", "bogus"]
)
def test_illegal_route_raises_usage_error_before_disk_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, illegal_route: str
) -> None:
    # Deliberately do NOT create a content root or patch resolve_content_root
    # — a disk-touching resolution attempt would raise something other than
    # RouteUsageError (e.g. ResolveCoordinatorCloneError), proving the usage
    # check happens first.
    with pytest.raises(RouteUsageError) as excinfo:
        brief(explicit_route=illegal_route)

    assert illegal_route in str(excinfo.value)


# ---------------------------------------------------------------------------
# AC-4 — a `route: shared` segment appears in both lanes; a `route: plan`
# segment appears only in the `plan` lane.
# ---------------------------------------------------------------------------


def test_shared_segment_appears_in_both_lanes_route_specific_segment_in_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    plan_result = brief(explicit_route="plan")
    spec_dispatch_result = brief(explicit_route="spec-dispatch")

    plan_ids = {s["segment_id"] for s in plan_result["segments"]}
    spec_dispatch_ids = {s["segment_id"] for s in spec_dispatch_result["segments"]}

    assert "shared-reminder" in plan_ids
    assert "shared-reminder" in spec_dispatch_ids
    assert "plan-reminder" in plan_ids
    assert "plan-reminder" not in spec_dispatch_ids
    assert "spec-dispatch-reminder" in spec_dispatch_ids
    assert "spec-dispatch-reminder" not in plan_ids


def test_segments_are_ordered_ascending_by_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    result = brief(explicit_route="plan")

    orders = [s["order"] for s in result["segments"]]
    assert orders == sorted(orders)


# ---------------------------------------------------------------------------
# AC-5 — zero applicable segments after filtering is fail-loud; a missing
# segment directory propagates the loader's own SegmentLoadError.
# ---------------------------------------------------------------------------


def test_resolved_route_with_zero_applicable_segments_is_fail_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_root = _make_residue_dir(
        tmp_path, include_plan=False, include_spec_dispatch=True, include_shared=False
    )
    _patch_content_root(monkeypatch, content_root)

    with pytest.raises(ResidueAssembleError) as excinfo:
        brief(explicit_route="plan")

    assert "zero applicable segments" in str(excinfo.value)


def test_empty_residue_directory_raises_fail_loud_not_empty_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_root = tmp_path / "content-root"
    residue_dir = content_root / "skills" / "plan" / "residue"
    residue_dir.mkdir(parents=True)  # exists, but carries zero segment files
    _patch_content_root(monkeypatch, content_root)

    with pytest.raises(ResidueAssembleError) as excinfo:
        brief(explicit_route="plan")

    assert "no segment files" in str(excinfo.value)


def test_absent_residue_directory_raises_segment_load_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_root = tmp_path / "content-root"  # never created at all
    _patch_content_root(monkeypatch, content_root)

    with pytest.raises(ResidueAssembleError) as excinfo:
        brief(explicit_route="plan")

    assert "residue directory not found" in str(excinfo.value)


# ---------------------------------------------------------------------------
# AC-6 — the segment directory resolves in exactly ONE step: no fallback
# probing of alternate locations.
# ---------------------------------------------------------------------------


def test_residue_dir_is_content_root_skills_plan_residue_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    result = brief(explicit_route="plan")

    assert result["decisions"]["residue_dir"] == "skills/plan/residue"


# ---------------------------------------------------------------------------
# AC-8 — an unresolvable content root exits transport (propagated
# ResolveCoordinatorCloneError, distinct from AC-5's business failure).
# ---------------------------------------------------------------------------


def test_unresolvable_content_root_propagates_resolve_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise() -> str:
        raise ResolveCoordinatorCloneError(
            "resolve-coordinator-clone --for-content: no readable content root found.\n"
            "  Run: coordinator:install"
        )

    monkeypatch.setattr(residue_mod, "resolve_content_root", _raise)

    with pytest.raises(ResolveCoordinatorCloneError) as excinfo:
        brief(explicit_route="plan")

    assert "coordinator:install" in str(excinfo.value)


# ---------------------------------------------------------------------------
# AC-9 — segment frontmatter is segment_id/route/class/order; `surface`
# appears nowhere in this module's own vocabulary.
# ---------------------------------------------------------------------------


def test_selected_segments_carry_route_key_not_surface_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    result = brief(explicit_route="plan")

    for segment in result["segments"]:
        assert "route" in segment
        assert "surface" not in segment
        assert set(segment.keys()) == {
            "segment_id", "route", "class", "order", "content", "source_path",
        }


def test_segment_source_path_is_relative_not_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    result = brief(explicit_route="plan")

    content_root_str = str(content_root)
    for segment in result["segments"]:
        source_path = segment["source_path"]
        assert not Path(source_path).is_absolute(), source_path
        assert content_root_str not in source_path, source_path
        assert source_path.startswith("skills/plan/residue/"), source_path


# ---------------------------------------------------------------------------
# Chunk C13 — `gates` assembly coverage. Every wave-2 predicate producer's
# contract row must resolve to either a populated field or the
# `undetermined` sentinel, at the `gates.<namespace>.*` path each producer
# module's own docstring documents (this is AC2's oracle: it is what stops
# a row going silently missing from the assembled envelope). `plan_path`/
# `sizing_object_path` are both left absent here deliberately — every row
# then resolves through its own `undetermined(...)` branch, which is
# itself a legal, populated `gates.*` leaf per this package's one sentinel
# contract; a row a caller DOES supply inputs for is exercised by each
# producer module's own co-located test suite, not re-tested here.
#
# Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C13
# ---------------------------------------------------------------------------

_MISSING = object()


def _navigate(node: Any, path: tuple[str, ...]) -> Any:
    """Walk *node* through *path*, one dict key at a time.

    An `undetermined` sentinel encountered before *path* is exhausted
    propagates as the terminal value (per `predicates.composed._field`'s
    own convention — an undetermined row never gets indexed into further).
    Returns the sentinel object `_MISSING` if a key is absent at any point
    (a silently-missing row — the failure mode this test exists to catch).
    """
    current = node
    for key in path:
        if isinstance(current, dict) and current.get("undetermined") is True:
            return current
        if not isinstance(current, dict) or key not in current:
            return _MISSING
        current = current[key]
    return current


#: Every wave-2 contract row this package's producers compute, paired with
#: the `gates.<namespace>.*` path `residue.brief`'s `_assemble_gates`
#: assembles it to. `("triage", ...)` navigates `gates["triage"][...]`,
#: and so on. Grouped by the plan's own Layer partition (Branch A / Branch
#: B / Branch C / Exit / Layer 1 / Layer 2) for cross-reference against
#: `## Layer partition — the row ledger` in the wave-2 plan.
_ROW_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # --- Layer 0, Branch A (triage.py) ---------------------------------
    (":30 sizing_object.present", ("triage", "sizing_object", "present")),
    (":30 sizing_object.path", ("triage", "sizing_object", "path")),
    (":32a sizing_object.arrival", ("triage", "sizing_object", "arrival")),
    (":32b sizing_object.intent", ("triage", "sizing_object", "intent")),
    (":32b sizing_object.estimate", ("triage", "sizing_object", "estimate")),
    (":32b sizing_object.appetite", ("triage", "sizing_object", "appetite")),
    (":33 route", ("triage", "route", "route")),
    (":34 roadmap_precondition.disqualified (clean arm)", ("triage", "roadmap_precondition", "disqualified")),
    (":37/:39 sizing_wall.fires", ("triage", "sizing_wall", "fires")),
    (":38 sizing_wall.disposition", ("triage", "sizing_wall", "disposition")),
    (":40 sizing_wall.via_memo", ("triage", "sizing_wall", "via_memo")),
    (":40 sizing_wall.source_memo", ("triage", "sizing_wall", "source_memo")),
    (":42 sizing_wall.carveout", ("triage", "sizing_wall", "carveout")),
    (":50 handoff_prescribes_plan", ("triage", "handoff_prescribes_plan", "handoff_prescribes_plan")),
    # --- Layer 0, Branch B (substrate_seven_dim.py / substrate_scans.py /
    #     citation_staleness.py / concurrent_preflight.py) ---------------
    (":72 problem_set.present", ("substrate", "problem_set", "present")),
    (":72 problem_set.path", ("substrate", "problem_set", "path")),
    (":73 scope_mode.value", ("substrate", "scope_mode", "value")),
    (":83 concurrent_preflight.today_dated_plan", ("substrate", "concurrent_preflight", "today_dated_plan")),
    (":83 concurrent_preflight.source_memo_collision", ("substrate", "concurrent_preflight", "source_memo_collision")),
    (":85-87 staleness.scope_paths_stale", ("substrate", "staleness", "scope_paths_stale")),
    (":85-87 staleness.stale_paths", ("substrate", "staleness", "stale_paths")),
    (":85-87 staleness.cited_lines_stale", ("substrate", "staleness", "cited_lines_stale")),
    (":85-87 staleness.stale_citations", ("substrate", "staleness", "stale_citations")),
    (":89 peer_sha_lint.fires", ("substrate", "peer_sha_lint", "fires")),
    (":90(1) seven_dim.no_duplicate", ("substrate", "seven_dim", "no_duplicate")),
    (":90(2) seven_dim.no_fabrication.no_fabrication", ("substrate", "seven_dim", "no_fabrication", "no_fabrication")),
    (":90(4) seven_dim.official_docs_read", ("substrate", "seven_dim", "official_docs_read")),
    (":90(5) seven_dim.reference_impl_seen", ("substrate", "seven_dim", "reference_impl_seen")),
    (":94 premise_gate.m_band_uncovered", ("substrate", "premise_gate", "m_band_uncovered")),
    (":94 premise_gate.tshirt", ("substrate", "premise_gate", "tshirt")),
    (":96 trampoline.verdict_cited", ("substrate", "trampoline", "verdict_cited")),
    (":96 trampoline.verdict_path", ("substrate", "trampoline", "verdict_path")),
    (":100 trampoline.dec4_signal", ("substrate", "trampoline", "dec4_signal")),
    (":106 collapse.invoked", ("substrate", "collapse", "invoked")),
    (":107 collapse.route_reachability_violation", ("substrate", "collapse", "route_reachability_violation")),
    (":108 collapse.fired_this_pass", ("substrate", "collapse", "fired_this_pass")),
    (":111 fix_locus.citation_present", ("substrate", "fix_locus", "citation_present")),
    (":112 fix_locus.registry_has_gate_type", ("substrate", "fix_locus", "registry_has_gate_type")),
    (":114 citations_verified", ("substrate", "citations_verified")),
    (":116 symbol_liveness.live", ("substrate", "symbol_liveness", "live")),
    (":118 reverses_teardown.candidate", ("substrate", "reverses_teardown", "candidate")),
    (":120 native_code_plan", ("substrate", "native_code_plan")),
    (":123 registered_dispatch_added", ("substrate", "registered_dispatch_added")),
    (":125 port_seam.exists", ("substrate", "port_seam", "exists")),
    (":159 mutates_shared_symbol.mutates_shared_symbol", ("substrate", "mutates_shared_symbol", "mutates_shared_symbol")),
    (":159 mutates_shared_symbol.consumer_count", ("substrate", "mutates_shared_symbol", "consumer_count")),
    (":166 scaffold_checklist.items_1_5", ("substrate", "scaffold_checklist", "items_1_5")),
    (":166 scaffold_checklist.item_6_grep_present", ("substrate", "scaffold_checklist", "item_6_grep_present")),
    # --- Layer 1 (shared_booleans.py, no DoE row) -----------------------
    (":105(3a) collapse.scope_file_count_le_2", ("substrate", "collapse", "scope_file_count_le_2")),
    (":105(3a) collapse.scope_file_count", ("substrate", "collapse", "scope_file_count")),
    (":105(3d) collapse.no_cross_repo_contract", ("substrate", "collapse", "no_cross_repo_contract")),
    (":105(3d) collapse.crossing_paths", ("substrate", "collapse", "crossing_paths")),
    # --- Layer 0, Branch C (composition_lints.py / composition_graph.py /
    #     supersedes_index.py) --------------------------------------------
    (":136 spine_row_shape.valid", ("composition", "spine_row_shape", "valid")),
    (":137 ac_reject_list.hits", ("composition", "ac_reject_list", "hits")),
    (":143 deferral_case_against.entries", ("composition", "deferral_case_against", "entries")),
    (":150 hard_constraints_block.present", ("composition", "hard_constraints_block", "present")),
    (":151 chunk_overlap.pairs", ("composition", "chunk_overlap", "pairs")),
    (":152 stub_spawns_subagents", ("composition", "stub_spawns_subagents")),
    (":153 concurrency_shared_state.candidate", ("composition", "concurrency_shared_state", "candidate")),
    (":153 concurrency_shared_state.matched_paths", ("composition", "concurrency_shared_state", "matched_paths")),
    (":156 path_rename_or_move.fires", ("composition", "path_rename_or_move", "fires")),
    (":156 path_rename_or_move.paths", ("composition", "path_rename_or_move", "paths")),
    (":160 cross_plan_conflict.hits", ("composition", "cross_plan_conflict", "hits")),
    (":162 amends_assumption.candidate", ("composition", "amends_assumption", "candidate")),
    (":162 amends_assumption.matched_plan", ("composition", "amends_assumption", "matched_plan")),
    (":164 supersedes_plan.present", ("composition", "supersedes_plan", "present")),
    (":164 supersedes_plan.target", ("composition", "supersedes_plan", "target")),
    (":172 chunk_index_sidecar.exists", ("composition", "chunk_index_sidecar", "exists")),
    (":172 chunk_index_sidecar.path", ("composition", "chunk_index_sidecar", "path")),
    # --- Exit (exit_gates.py) -------------------------------------------
    (":189 sizing_object_flag.passed", ("exit", "sizing_object_flag", "passed")),
    # --- Layer 2 (composed.py) — pure composition over the above --------
    (":44 trivial_conjunction", ("triage", "trivial_conjunction")),
    (":57 nontrivial_disjunction", ("triage", "nontrivial_disjunction")),
    (":90(7) seven_dim.fix_locus", ("substrate", "seven_dim", "fix_locus")),
    (":91 seven_dim.all_green", ("substrate", "seven_dim", "all_green")),
    (":105(1) collapse.seven_dim_green", ("substrate", "collapse", "seven_dim_green")),
    (":105(2) collapse.premise_gate_green", ("substrate", "collapse", "premise_gate_green")),
    (":134 scope_mode_declared", ("substrate", "scope_mode_declared")),
    (":139 route_triggers_review", ("triage", "route_triggers_review")),
    (":195-198 terminal_table.result", ("exit", "terminal_table", "result")),
)


def _is_undetermined_value(value: Any) -> bool:
    return isinstance(value, dict) and value.get("undetermined") is True


def test_every_contract_row_resolves_to_populated_field_or_undetermined_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2's oracle: every row in `_ROW_PATHS` — the full wave-2 predicate
    producer set — is reachable in the assembled `gates` dict (never a
    `KeyError`/missing path), and its terminal value is either the
    `undetermined` sentinel or a populated (non-sentinel) field. `--plan`/
    `--sizing-object` are both absent, so every row's own `undetermined`
    branch is what this test actually exercises — that branch resolving
    cleanly, at the documented path, is itself the coverage guarantee."""
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    result = brief(explicit_route="plan")
    gates = result["gates"]

    missing_rows: list[str] = []
    for row_id, path in _ROW_PATHS:
        value = _navigate(gates, path)
        if value is _MISSING:
            missing_rows.append(row_id)

    assert not missing_rows, f"rows silently missing from gates: {missing_rows}"

    # Not every row goes `undetermined` without a plan, and asserting so
    # would be false: a row's disposition here follows its SOURCE, not the
    # absent flag. Rows sourced from the already-resolved route (`:33`,
    # `:34`, `:139`, `:195-198`), from a static table (`:38`), or from the
    # repo tree rather than the plan (`:37`/`:39`, `:83`) all resolve
    # legitimately. What must hold is the converse: a row whose ONLY source
    # is the plan body or its frontmatter cannot invent an answer without
    # one, and must reach for the sentinel rather than a guessed `False`.
    # That is the branch this test exercises, and it is the assertion worth
    # making.
    plan_sourced_primaries = {
        ":85-87 staleness.scope_paths_stale",
        ":85-87 staleness.cited_lines_stale",
        ":89 peer_sha_lint.fires",
        ":90(2) seven_dim.no_fabrication.no_fabrication",
        ":111 fix_locus.citation_present",
        ":116 symbol_liveness.live",
        ":118 reverses_teardown.candidate",
        ":120 native_code_plan",
        ":123 registered_dispatch_added",
        ":125 port_seam.exists",
        ":136 spine_row_shape.valid",
        ":137 ac_reject_list.hits",
        ":150 hard_constraints_block.present",
        ":151 chunk_overlap.pairs",
        ":152 stub_spawns_subagents",
        ":153 concurrency_shared_state.candidate",
        ":164 supersedes_plan.present",
        ":172 chunk_index_sidecar.exists",
        ":105(3a) collapse.scope_file_count_le_2",
        ":105(3d) collapse.no_cross_repo_contract",
    }
    guessed: list[tuple[str, Any]] = []
    for row_id, path in _ROW_PATHS:
        if row_id not in plan_sourced_primaries:
            continue
        value = _navigate(gates, path)
        if not _is_undetermined_value(value):
            guessed.append((row_id, value))

    assert not guessed, (
        "plan-sourced rows answered without a plan instead of reaching for "
        f"the undetermined sentinel: {guessed}"
    )


def test_judgment_points_carries_architectural_tier_candidate_evidence_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`:59` -> exactly one `judgment_points[]` entry, three named
    candidate criteria (never a `multi-stakeholder` entry, per AC4), and a
    `None` disposition — this engine presents evidence, it never resolves
    the `U`-classified verdict itself.

    Requires `--plan`: the entry is deliberately suppressed without one, so
    that a wave-1 caller's `judgment_points` stays `[]` and the EM is never
    handed a judgment point whose every candidate reads "could not look."
    """
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    plan_file = tmp_path / "plan.md"
    plan_file.write_text(
        "---\ntitle: fixture\nscope_mode: spec-dispatch\n---\n\n# fixture\n",
        encoding="utf-8",
    )

    result = brief(explicit_route="plan", plan_path=plan_file)

    assert len(result["judgment_points"]) == 1
    entry = result["judgment_points"][0]
    criteria_names = {c["criterion"] for c in entry["candidate_criteria"]}
    assert criteria_names == {
        "cross-system-irreversible",
        "security-privacy-boundary",
        "naming-collision-with-product-policy",
    }
    assert "multi-stakeholder" not in criteria_names

    # The bare `disposition: None` key this used to assert is gone: the entry
    # is now built through the shared constructor, so "the engine presents
    # evidence, the EM names the criterion" is expressed as a real
    # `recommendation=None` on an addressable point rather than a key no
    # consumer could act on. `candidate_criteria` above is unchanged.
    assert "disposition" not in entry
    assert entry["recommendation"] is None
    assert entry["id"]
    assert {d["value"] for d in entry["dispositions"]} >= criteria_names


def test_withdrawn_and_vacuous_rows_emit_no_field_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`:43` (vacuous by construction), `:34`'s XL-exit arm (withdrawn),
    and `:121` (withdrawn) are asserted ABSENT — no field, no
    `undetermined` entry, nothing to accidentally resurrect. Also covers
    AC7's `xl_exit` namespace ban at the assembled-envelope level."""
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    result = brief(explicit_route="plan")
    gates = result["gates"]

    # :34's XL-exit arm — no `workstream_count`/`goal_fk_present` field,
    # and no `xl_exit` namespace anywhere in the assembled gates dict.
    roadmap_precondition = gates["triage"]["roadmap_precondition"]
    assert "workstream_count" not in roadmap_precondition
    assert "goal_fk_present" not in roadmap_precondition
    assert "xl_exit" not in gates["triage"]

    def _walk_keys(node: Any) -> Any:
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from _walk_keys(value)
        elif isinstance(node, list):
            for item in node:
                yield from _walk_keys(item)

    all_keys = set(_walk_keys(gates))
    assert "xl_exit" not in all_keys

    # :43 — the express-lane carve-out is vacuous by construction; no
    # field name exists anywhere in this package for it.
    assert "express_lane" not in all_keys
    assert "expedite" not in all_keys

    # :121 — API-rekey, withdrawn in full; no narrower variant either.
    assert "api_rekey" not in all_keys


# ---------------------------------------------------------------------------
# Chunk C13 defect fix — the residue/predicate SPLIT: a bare wave-1 call
# still fail-louds on zero/missing residue exactly as before; a
# predicates-requested call does NOT abort and still returns a well-formed
# envelope with `gates` populated.
# ---------------------------------------------------------------------------


def test_bare_call_still_fail_louds_on_missing_residue_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wave-1 shape unchanged: no `plan_path`, no `sizing_object_path` — a
    missing residue directory still raises `ResidueAssembleError`, same
    type, same message, exactly as before this chunk's split."""
    content_root = tmp_path / "content-root"  # never created at all
    _patch_content_root(monkeypatch, content_root)

    with pytest.raises(ResidueAssembleError) as excinfo:
        brief(explicit_route="spec-dispatch")

    assert "residue directory not found" in str(excinfo.value)


def test_predicates_requested_with_missing_residue_directory_returns_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Predicates requested (`plan_path` supplied) + residue directory
    absent: `brief()` does NOT raise. It returns a well-formed envelope —
    `segments` is `[]`, the absence is reported in-band via `narration`/
    `decisions["residue_unavailable"]`, and `gates` still carries all four
    namespaces (this caller's actual ask, which does not depend on the
    residue corpus at all)."""
    content_root = tmp_path / "content-root"  # never created at all
    _patch_content_root(monkeypatch, content_root)

    plan_file = tmp_path / "plan.md"
    plan_file.write_text(
        "---\ntitle: fixture\nscope_mode: spec-dispatch\n---\n\n# fixture\n",
        encoding="utf-8",
    )

    result = brief(explicit_route="spec-dispatch", plan_path=plan_file)

    assert result["segments"] == []
    assert "unavailable" in result["narration"] or "residue" in result["narration"]
    assert "residue_unavailable" in result["decisions"]
    gates = result["gates"]
    assert set(gates.keys()) == {"triage", "substrate", "composition", "exit"}
    for namespace in ("triage", "substrate", "composition", "exit"):
        assert gates[namespace], f"gates.{namespace} unexpectedly empty"


def test_predicates_requested_with_zero_applicable_segments_returns_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Predicates requested (`sizing_object_path` supplied) + residue
    directory present but zero segments applicable to the resolved route:
    `brief()` does NOT raise, same in-band-reporting contract as the
    missing-directory case above."""
    content_root = _make_residue_dir(
        tmp_path, include_plan=False, include_spec_dispatch=True, include_shared=False
    )
    _patch_content_root(monkeypatch, content_root)

    sizing_file = tmp_path / "sizing.yaml"
    sizing_file.write_text("intent: fixture\n", encoding="utf-8")

    result = brief(explicit_route="plan", sizing_object_path=sizing_file)

    assert result["segments"] == []
    assert "residue_unavailable" in result["decisions"]
    assert result["gates"]["triage"]
    assert result["gates"]["substrate"]
    assert result["gates"]["composition"]
    assert result["gates"]["exit"]


# ---------------------------------------------------------------------------
# pln-plan-assemble-admits-instead-o-e441e3 chunks C1/C2 — `gates.triage.
# admission` (the SIZING-axis FK resolution) and the `next_move` it drives.
# ---------------------------------------------------------------------------







