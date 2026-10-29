"""Regression guard for archive/specs/2026-09/2026-09-02-one-owner-for-where-a-work-
record-lives.md: a hand-built `state/<declared-kind>` path join re-entering
production code under `coordinator_core/` fails this test, where `<declared-kind>`
is one of the work-record kinds `session/record_homes.HOMES` declares.

SCOPE -- production modules, by two rules (unlike its sibling
`test_no_hand_built_machinery_joins.py`, which polices only the sites its own
chunk repointed, this guard walks the whole `coordinator_core/` tree):

    1. Test scaffolding is out of scope. The predicate is the generator
       census's own, `assemble._is_test_scaffolding` (a `/tests/` segment, a
       `test_` basename, `conftest.py`, `coordinator_core/benchmarks/`,
       `coordinator_core/testing/`). A test literal is the independent witness
       of where a record must land; computing it from the accessor under test
       would remove the witness.
    2. A column-0 op declaration is not a join. A literal inside a plain or
       annotated module-body assignment to one of
       `declarations.DECLARATION_NAMES` (`MUTATES = ["state/handoffs/*.md"]`) is
       not counted: the census reads those statically and a function call there
       folds to MALFORMED, so they cannot go through an accessor.

THE RATCHET, both directions. `_KNOWN_LITERAL_SITES` is the seeded frozenset of
the production files that carry a counted `state/<kind>` literal. It only
shrinks. A literal in a file NOT in the set fails -- a new hand-built join
landing anywhere is what this guard exists to catch. A file IN the set that no
longer carries a counted literal also fails, so a file repointed onto
`record_homes.py`'s accessors cannot keep its exemption by staying listed. A
scaffolding path added to the set fails the same way: the scan never visits it.

`record_homes.py` itself is exempt -- the one module allowed to spell the
`"state"` + `"<kind>"` segments together, the same way
`test_no_hand_built_machinery_joins.py` exempts `machinery_paths.py`. This file
is scaffolding by rule 1, so its inline fixture strings are never scanned.

Negative-spec:
    - Static scan only -- this test never imports or executes any scanned
      module. It reads each file's source text, regex-matches it, and parses
      (never imports) a file only when it both matches and names a declaration.
    - Does NOT sweep the seeded sites onto `record_homes.py`'s accessors; this
      guard ratchets the count and does not reduce it.
    - Does NOT police a `"state/<segment>"` literal for a segment that is NOT a
      declared `record_homes.HOMES` kind (e.g. a machinery bucket like
      `"state/subagent-share"`, covered by `test_no_hand_built_machinery_joins.py`).
    - Does NOT run at cadence -- no `@pytest.mark.cadence` or `spawns_process`
      marker; it fires on every fast-tier run.
"""

from __future__ import annotations

import ast
import os
import re
from functools import lru_cache

from coordinator_core.ops.generator_census.assemble import _is_test_scaffolding
from coordinator_core.ops.generator_census.declarations import DECLARATION_NAMES
from coordinator_core.session import record_homes

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_SCAN_ROOT = os.path.join(_REPO_ROOT, "coordinator_core")

_EXCLUDED_FILES = {
    os.path.join(_SCAN_ROOT, "session", "record_homes.py"),
}

_KIND_ALTERNATION = "|".join(
    re.escape(kind) for kind in sorted(record_homes.HOMES, key=len, reverse=True)
)
_JOIN_PATTERN = re.compile(
    r"(?:"
    r'["\']state/(?:' + _KIND_ALTERNATION + r')(?:["\'/]|$)'
    r'|["\']state["\']\s*[,/]\s*["\'](?:' + _KIND_ALTERNATION + r')["\']'
    r")"
)

_KNOWN_LITERAL_SITES = frozenset({
    "hooks/sizing_scout_deny.py",
    "ops/dispatch_emit/extant_work.py",
    "ops/dispatch_emit/run_truth_contract.py",
    "ops/review_mint/execute_review.py",
    "backlog_grind_assemble/grind_rows.py",
    "backlog_grind_assemble/readers_blitz.py",
    "backlog_grind_assemble/readers_mise.py",
    "backlog_grind_assemble/readers_sweep.py",
    "bash_guards/_alternative_liveness.py",
    "bash_guards/_write_bump_message.py",
    "bash_guards/_write_bump_stand_down.py",
    "bash_guards/_dc_blanket_add.py",
    "bash_guards/_dc_commit.py",
    "baton_assemble/__init__.py",
    "ceremony_common/_phantom_sweep_providers.py",
    "claude_md_budget.py",
    "commit_ledger/resolve_owner.py",
    "commit_ledger/store.py",
    "contract/cockpit_schema/entities/roadmap_summary.py",
    "contract/emit_memo_schema.py",
    "coverage.py",
    "dag.py",
    "distill/_common.py",
    "distill/curation_status.py",
    "distill/delete_guard.py",
    "environment_story/guard_enforcement_join.py",
    "fact_contract_gate/dangling_consumer.py",
    "fact_contract_gate/engine_gap_ratchet.py",
    "fact_contract_gate/prose_rederivation.py",
    "frontmatter/author_dependence.py",
    "frontmatter/schema_validate.py",
    "goals/__init__.py",
    "goals/reassess_krs.py",
    "hooks/claude_md_ledger.py",
    "hooks/guard_handoff_summary_cap_on_write.py",
    "hooks/nudge_initiative_goals_ladder.py",
    "hooks/nudge_unauthorized_handoff.py",
    "hooks/pickup_autofire.py",
    "hooks/support/stop_family_runner_contract.py",
    "housekeeping/cycle.py",
    "install/scaffold_structure.py",
    "install/uninstall_legs.py",
    "learn_lessons_pipeline/__init__.py",
    "memo_corpus.py",
    "op_budget_suspension.py",
    "ops/backfill_deliverable_spine.py",
    "ops/backfill_reference_edges.py",
    "ops/baton_drift_sweep.py",
    "ops/cascade_backstop_sweep.py",
    "ops/cascade_divergence_report.py",
    "ops/cascade_retract.py",
    "ops/ceremony/renderers.py",
    "ops/ceremony/tail_ops.py",
    "ops/changelog_ops.py",
    "ops/check_competitor_positioning_nudge.py",
    "ops/commit_anchors.py",
    "ops/completion_nature.py",
    "ops/completion_ops.py",
    "ops/crossrepo_closure_status.py",
    "ops/cutover_gate.py",
    "ops/deferral_detect_orphan_memo.py",
    "ops/deliverable_cascade.py",
    "ops/deliverable_rollup.py",
    "ops/detect_onboarding_offer.py",
    "ops/dispatch_emit/grind_compose.py",
    "ops/dispatch_emit/queue_select.py",
    "ops/draft_plan_aging.py",
    "ops/emit/sections/initiatives.py",
    "ops/emit/sections/roadmaps.py",
    "ops/emit/sections/routine_signals.py",
    "ops/extract_cited_sidecars.py",
    "ops/fleet/_common.py",
    "ops/fleet/_memo_anchor.py",
    "ops/fleet/_memo_compose.py",
    "ops/fleet/archive_queue_entry.py",
    "ops/fleet/archive_release_accumulator.py",
    "ops/fleet/archive_sizings.py",
    "ops/fleet/archive_terminal_handoffs.py",
    "ops/fleet/capability_index.py",
    "ops/fleet/consumer_corpus_preflight.py",
    "ops/fleet/migrate_handoff_vocabulary.py",
    "ops/fleet/prune_bugs.py",
    "ops/fleet_machinery_sweep.py",
    "ops/goals_match.py",
    "ops/handoff_append_session_ledger.py",
    "ops/handoff_archive_transition.py",
    "ops/handoff_author_fork.py",
    "ops/handoff_children.py",
    "ops/handoff_close_origin_stub.py",
    "ops/handoff_correct_body.py",
    "ops/handoff_discharge_criteria.py",
    "ops/handoff_lineage_ancestry.py",
    "ops/handoff_match.py",
    "ops/handoff_normalize.py",
    "ops/handoff_phase_stamp.py",
    "ops/handoff_repoint_origin.py",
    "ops/handoff_stamp.py",
    "ops/handoff_transition.py",
    "ops/initiatives_serve.py",
    "ops/normalize_claimed_frontmatter.py",
    "ops/plan_status_transition.py",
    "ops/promote_shipped_in_flight_stubs.py",
    "ops/propagate_body.py",
    "ops/queue_scaffold_baton.py",
    "ops/read_sizing_object_fields.py",
    "ops/reap_in_progress_memos.py",
    "ops/reap_orphaned_agent_dirs.py",
    "ops/records_query.py",
    "ops/refresh_roadmap_callout.py",
    "ops/review_brightline_gate.py",
    "ops/roadmap_dag.py",
    "ops/roadmap_link_stubs.py",
    "ops/session/fix_concrete_path_citations.py",
    "ops/session/guard_concrete_path_citations.py",
    "ops/session/record_pickup.py",
    "ops/session/resolve_chain_terminal_disposition.py",
    "ops/sizing_accept_exit_criterion.py",
    "ops/sizing_decline.py",
    "ops/sizing_discharge_surfaced.py",
    "ops/sizing_ship.py",
    "ops/sizing_spike_verdict.py",
    "ops/strategic/draft_writer.py",
    "ops/strategic/emit_writer.py",
    "ops/strategic/version_highlights.py",
    "ops/workday_complete_step2_5_dirty_tree.py",
    "ops/workday_stitch_sidecar_summary.py",
    "ops/workweek_trail_scope.py",
    "orient_brief/_health.py",
    "orient_brief/_work.py",
    "pickup_assemble/__init__.py",
    "pickup_assemble/apply.py",
    "pickup_brief.py",
    "plugin_health/__init__.py",
    "quick_wrap_assemble/__init__.py",
    "reconcile/ac27_differential_oracle.py",
    "reconcile/commitments_recheck.py",
    "reconcile/handoff_corpus.py",
    "roadmap/blitz_land.py",
    "roadmap/number_stubs.py",
    "roadmap/plan_gate.py",
    "roadmap/spine.py",
    "roadmap_planning_assemble/__init__.py",
    "session/claim_neighbours.py",
    "session/claims.py",
    "session/session_facts.py",
    "session_ledger/aggregate_chain_loe.py",
    "sizing_assemble/__init__.py",
    "updatedocs/plan_prune.py",
    "workstream_complete/__init__.py",
    "workstream_complete/directives_commit_tail.py",
    "workstream_complete/directives_completion.py",
    "workstream_complete/directives_memo_lifecycle.py",
    "write_guards/block_home_dir_memo_delivery.py",
    "write_guards/block_oss_mirror_memo_delivery.py",
    "write_guards/nudge_baton_body_bar.py",
    "write_guards/nudge_session_display_name_as_identifier.py",
    "write_guards/nudge_terminal_artifact_edit.py",
    "write_guards/nudge_unattributed_process_time_figure.py",
    "write_guards/validate_frontmatter_schema_advisory.py",
    "write_guards/validate_frontmatter_schema_deny.py",
})


def _iter_py_files():
    for dirpath, _dirnames, filenames in os.walk(_SCAN_ROOT):
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fn)
            if fpath in _EXCLUDED_FILES:
                continue
            rel = os.path.relpath(fpath, _REPO_ROOT).replace(os.sep, "/")
            if _is_test_scaffolding(rel):
                continue
            yield fpath


def _declaration_lines(text: str) -> set[int]:
    """0-based line indexes of every column-0 declaration statement; empty if `text` does not parse."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    lines: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id in DECLARATION_NAMES for t in targets):
            lines.update(range(node.lineno - 1, node.end_lineno))
    return lines


def _holds_counted_literal(text: str) -> bool:
    """True when `text` has a record-home literal outside a column-0 op declaration."""
    if not _JOIN_PATTERN.search(text):
        return False
    if not any(name in text for name in DECLARATION_NAMES):
        return True
    blank = _declaration_lines(text)
    if not blank:
        return True
    kept = "\n".join(
        "" if i in blank else line for i, line in enumerate(text.split("\n"))
    )
    return bool(_JOIN_PATTERN.search(kept))


@lru_cache(maxsize=1)
def _sites_with_literal():
    hits = set()
    for fpath in _iter_py_files():
        try:
            with open(fpath, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        if _holds_counted_literal(text):
            rel = os.path.relpath(fpath, _SCAN_ROOT).replace(os.sep, "/")
            hits.add(rel)
    return frozenset(hits)


def test_no_new_hand_built_record_home_joins():
    """A literal at a file NOT in `_KNOWN_LITERAL_SITES` fails -- a new
    hand-built `state/<kind>` join landing anywhere in production code under
    `coordinator_core/` (outside `record_homes.py`) is exactly what this guard
    exists to catch.
    """
    hits = _sites_with_literal()
    unseeded = hits - _KNOWN_LITERAL_SITES
    assert not unseeded, (
        "hand-built 'state/<kind>' path join(s) found outside the seeded "
        f"ratchet set (route through session.record_homes instead): {sorted(unseeded)}"
    )


def test_seeded_sites_still_carry_a_literal():
    hits = _sites_with_literal()
    stale = _KNOWN_LITERAL_SITES - hits
    assert not stale, (
        "seeded site(s) no longer carry a hand-built 'state/<kind>' "
        f"literal -- remove from _KNOWN_LITERAL_SITES: {sorted(stale)}"
    )


def test_declaration_rule_counts_only_joins_outside_column_0_declarations():
    single = 'MUTATES = ["state/handoffs/*.md"]\n'
    spanning = 'GENERATES = [\n    "x",\n    "state/sizings/*.yaml",\n]\n'
    annotated = 'MUTATES: list = ["state/goals/*.yaml"]\n'
    for text in (single, spanning, annotated):
        assert not _holds_counted_literal(text), text

    join = 'p = root / "state" / "handoffs"\n'
    for text in (single + join, spanning + join, annotated + join):
        assert _holds_counted_literal(text), text

    nested = 'if True:\n    MUTATES = ["state/handoffs/*.md"]\n'
    assert _holds_counted_literal(nested)
    in_function = 'def f():\n    GENERATES = ["state/handoffs/*.md"]\n'
    assert _holds_counted_literal(in_function)

    assert _holds_counted_literal('MUTATES = [\n  "state/handoffs/*.md"\n')
