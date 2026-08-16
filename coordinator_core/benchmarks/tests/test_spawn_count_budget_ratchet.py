"""Ratchet + completeness pin for every `spawn_count_budget` row in
`budget-manifest.json`'s `overrides`.

docs/wiki/cost-budgets-and-the-kill-disposition.md's budget rule: a bound is
derived from what the box can afford, then measured against -- never fitted
to what the code currently costs. Without a ratchet, a `spawn_count_budget`
entry is prose a reviewer might notice drift past, not a mechanism -- exactly
the failure the page names (`spawns_per_gap_date`, `per_batch`) for the two
rows already deleted/renamed on 2026-08-15/16. This module is the enforcement
half for the rows that survived that audit, plus the pre-existing
`ceremony.scoped_git_commit` row (the original worked example the manifest's
own `_rationale` cites, governed here too so the completeness pin below sees
the whole `spawn_count_budget` class, not just the audited set) -- see
`_SPAWN_COUNT_HIGH_WATER` below for the current, authoritative row count
rather than a number restated here that would drift out from under it --
mirroring the worked example at
`coordinator_core/tests/test_ipc_per_request_state.py::test_op_timeout_overrides_never_ratchet_upward`
/ `test_timeout_high_water_table_covers_every_override`, generalized from a
scalar-per-op high-water mark to a dict-of-keys-per-op one (a
`spawn_count_budget` row carries multiple named path/key spawn counts per op,
not one).

Spec backlink: docs/wiki/cost-budgets-and-the-kill-disposition.md
               state/handoffs/2026-08-15-kill-it-if-it-cannot-pay-for-itself.md
"""

from __future__ import annotations

from coordinator_core.benchmarks.budget import load_manifest

# Each op's high-water mark, one entry per named key inside its
# `spawn_count_budget` dict. A value may be LOWERED freely -- that is the
# direction this repo wants and no test should stand in its way; raising one
# requires editing this table, turning a one-character tune into a visible
# argument in a diff rather than a silent constant bump in the manifest.
#
# `bin.freeze_review_diff.paths_contributing_nothing` and
# `bin.workweek_complete_drift_guards.shellcheck_sweep` were cut entirely by a
# concurrent 2026-08-16 pass (advisory-prose removal, confirmed absent from
# both `budget-manifest.json` and their own dedicated test files at the time
# this module was written) -- both rows are correspondingly absent here, not
# left dangling with no manifest entry to ratchet against.
_SPAWN_COUNT_HIGH_WATER = {
    "ceremony.scoped_git_commit": {
        "green_path": 12,
        "refusal_path_unanswerable": 0,
        "directory_pathspec_expansion": 13,
    },
    "changelog.cited_in_range_count": {
        "n_tokens": 1,
        "no_tokens": 0,
    },
    "coverage.diagnose_open_review_loop_dag_mode": {
        "many_pending_records": 1,
    },
    "percolate.functional_identifier_output_drift_in_tree": {
        "no_dest_publish_time": 0,
        "with_dest_publish_time_n_files": 1,
    },
    "bin.coordinator_harvest_deferrals_dedup_scan_root_resolution": {
        "resolution_calls_for_5_candidate_rows": 3,
        "directory_scan_calls_for_5_candidate_rows": 1,
    },
    "bin.reap_integrated_review_findings.tracked_untracked_split": {
        "per_reap_call": 1,
    },
    "bin.workday_complete_step2_5_dirty_tree.classify_main_pass": {
        "per_classify_call": 2,
    },
    "execute_plan_assemble.dispatch_ledger_delivered": {
        "no_committed_rows": 0,
        "n_committed_rows": 2,
    },
    "execute_plan_assemble.sibling_committed_chunk_ids_memo": {
        "second_call_identical_inputs": 0,
    },
    "ops.discover_working_repos": {
        "per_call": 0,
    },
}


def _manifest_spawn_count_overrides() -> dict:
    """Every `overrides` row in the live manifest that carries a `spawn_count_budget`.

    Review: code-reviewer (P5 F3) -- a row may carry the `spawn_count_budget`
    key with a JSON `null` value (the key present, no budget set), distinct
    from the key being absent entirely. Such a row is simply ungoverned by
    the ratchet, not a malformed one, so it is excluded here the same as a
    row lacking the key outright -- rather than surfacing as an unhandled
    `TypeError` later when a caller indexes into it.
    """
    manifest = load_manifest()
    overrides = manifest.get("overrides", {})
    return {
        op: entry["spawn_count_budget"]
        for op, entry in overrides.items()
        if entry.get("spawn_count_budget") is not None
    }


def _stale_high_water_ops(live: dict) -> list:
    """Ops named in `_SPAWN_COUNT_HIGH_WATER` with no `spawn_count_budget` row
    left in the live manifest at all -- an orphaned mark governing nothing."""
    return [op for op in _SPAWN_COUNT_HIGH_WATER if op not in live]


def test_spawn_count_budget_never_ratchets_upward():
    """No manifest `spawn_count_budget` value may exceed its recorded high-water mark.

    Over budget is a kill candidate, not a budget raise -- see
    docs/wiki/cost-budgets-and-the-kill-disposition.md. A regression that
    reintroduces a per-item spawn (e.g. `ops.discover_working_repos` falling
    back to its old per-key CLI shellout) fails this test at the first
    exceeded key, not silently as an unenforced number in JSON.
    """
    live = _manifest_spawn_count_overrides()
    for op, keys in _SPAWN_COUNT_HIGH_WATER.items():
        if op not in live:
            # A concurrent kill (freeze_review_diff / shellcheck_sweep, see
            # module docstring) may remove the whole op row -- that is a
            # DROP from this table, flagged as a stale orphan by the
            # completeness pin below, not a ratchet violation here.
            continue
        for key, ceiling in keys.items():
            assert key in live[op], (
                f"{op}.spawn_count_budget dropped key {key!r} while its "
                f"high-water mark remains here -- drop the key from "
                f"_SPAWN_COUNT_HIGH_WATER too."
            )
            assert live[op][key] <= ceiling, (
                f"{op}.spawn_count_budget[{key!r}] raised to {live[op][key]} "
                f"over its {ceiling} high-water mark. Over budget is a kill "
                f"candidate, not a budget raise -- see "
                f"docs/wiki/cost-budgets-and-the-kill-disposition.md."
            )


def test_spawn_count_high_water_table_covers_every_override():
    """Every manifest `spawn_count_budget` key carries a high-water mark, so a
    new op/key cannot enter the manifest above the ratchet's reach and become
    the next ungoverned constant -- and, in the other direction, a row
    deleted from the manifest cannot leave a stale high-water entry behind
    that silently governs nothing.

    Review: code-reviewer (P5 F1) -- this test previously only checked the
    forward direction (new/live keys missing a high-water mark). The reverse
    (`_stale_high_water_ops`) closes the gap the ratchet test's own comment
    already claimed was covered here.
    """
    live = _manifest_spawn_count_overrides()
    missing = []
    for op, keys in live.items():
        tracked = _SPAWN_COUNT_HIGH_WATER.get(op, {})
        for key in keys:
            if key not in tracked:
                missing.append(f"{op}.{key}")
    assert not missing, (
        f"new spawn_count_budget rows with no high-water mark: {sorted(missing)}. "
        f"Add each to _SPAWN_COUNT_HIGH_WATER at the value it enters with."
    )

    stale = _stale_high_water_ops(live)
    assert not stale, (
        f"_SPAWN_COUNT_HIGH_WATER carries op(s) with no spawn_count_budget row "
        f"left in the live manifest: {sorted(stale)}. Delete each entry from "
        f"_SPAWN_COUNT_HIGH_WATER -- an orphaned mark governs nothing."
    )


def test_spawn_count_high_water_table_flags_stale_orphan_via_mutation():
    """Mutation probe for the F1 fix: an op in `_SPAWN_COUNT_HIGH_WATER` with
    no matching row in the live manifest must be caught, not silently
    ignored.

    Uses `bin.freeze_review_diff.paths_contributing_nothing`, one of the two
    rows this session actually deleted from the manifest (K-101/K-102) --
    confirmed absent from the live `_manifest_spawn_count_overrides()`
    result, so injecting it back into a copy of `_SPAWN_COUNT_HIGH_WATER`
    reproduces the exact live stale-orphan shape rather than a synthetic one.
    """
    live = _manifest_spawn_count_overrides()
    assert "bin.freeze_review_diff.paths_contributing_nothing" not in live, (
        "fixture assumption broken: this op is back in the live manifest -- "
        "pick a different confirmed-deleted op for this probe."
    )

    mutated = dict(_SPAWN_COUNT_HIGH_WATER)
    mutated["bin.freeze_review_diff.paths_contributing_nothing"] = {
        "per_call": 0,
    }
    stale = [op for op in mutated if op not in live]
    assert stale == ["bin.freeze_review_diff.paths_contributing_nothing"], (
        "the stale-orphan check did not fire on a known-deleted op -- "
        "the completeness pin's reverse direction is broken."
    )


def test_manifest_spawn_count_overrides_excludes_explicit_null():
    """Mutation probe for the F3 fix: a `spawn_count_budget` key present with
    a JSON `null` value must be excluded (ungoverned), not crash a caller
    that indexes into it."""
    manifest = load_manifest()
    manifest = dict(manifest)
    overrides = dict(manifest.get("overrides", {}))
    overrides["_probe_null_budget_op"] = {
        "target_ms": 1,
        "tolerance": {"kind": "relative", "value": 0.2},
        "spawn_count_budget": None,
    }
    manifest["overrides"] = overrides

    result = {
        op: entry["spawn_count_budget"]
        for op, entry in overrides.items()
        if entry.get("spawn_count_budget") is not None
    }
    assert "_probe_null_budget_op" not in result, (
        "a spawn_count_budget of JSON null should be treated as ungoverned, "
        "not surfaced as a real row."
    )
