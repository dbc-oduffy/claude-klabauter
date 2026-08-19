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

from pathlib import Path

from coordinator_core.benchmarks.budget import load_manifest

_THIS_FILE = Path(__file__).resolve()
_REPO_ROOT = Path(__file__).resolve().parents[3]

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
# `ceremony.scoped_git_commit`'s green_path/directory_pathspec_expansion marks
# were raised 12->17 and 13->18 on 2026-08-18 (opro-01). The argument this
# table exists to force: the +5 is the agree-branch CAS reading its
# index/HEAD blob pair twice by construction (a compare-and-swap needs both
# the capture and the re-read immediately before `git add`) plus one
# `interpret-trailers --in-place`. Five real spawns doing named,
# correctness-bearing work, landed after the 2026-08-11 figure without
# moving the budget, and unnoticed because the enforcing module carries
# `pytest.mark.cadence`. A raise here is still a raise: it is defensible
# only because nothing among the five is reducible, not because the count
# moved.
_SPAWN_COUNT_HIGH_WATER = {
    "ceremony.scoped_git_commit": {
        "green_path": 17,
        "refusal_path_unanswerable": 0,
        "directory_pathspec_expansion": 18,
        "push_raced_path": 20,
    },
    "changelog.cited_in_range_count": {
        "n_tokens": 1,
        "no_tokens": 0,
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
    # `per_call` was raised 0 -> 1 on 2026-08-19 (opro-03 C-04). The argument
    # this table exists to force: NO SPAWN WAS ADDED. The old 0 was asserted
    # only after the test monkeypatched out `_sort_unique` -- the single
    # function on this path that spawns -- so it was a true statement about a
    # stubbed op and a false one about this op. The 1 is `_tier_a5`'s tail
    # call to `_sort_unique`, which shells to `sort -u` once per call
    # regardless of key count. It is irreducible here because it is a
    # SANCTIONED carve-out, named in
    # `coordinator_core/tests/test_no_bash_dependency.py` for byte-parity with
    # the bash oracle -- retiring it is a carve-out decision, not a budget
    # one. A re-baseline against newly visible truth, not a raise fitted to an
    # unreduced spawn set. `machine_local_cli_elimination_calls` keeps the
    # original assertion at its original 0: it isolates `_merged_flat_registry`'s
    # elimination of the per-key `machine-local` CLI spawns, and legitimately
    # stubs `_sort_unique` to do so.
    # The `op_total_*` marks are the OP (main() end-to-end, nothing stubbed);
    # `per_call` and `machine_local_cli_elimination_calls` are SUB-PATHS and
    # are not the op total. First measured 2026-08-19, so these three are a
    # new floor rather than a raise -- there was no prior mark to exceed.
    # Three, not one, because `_sort_unique` shells to `sort -u` once per
    # NON-EMPTY call and `main()` reaches it three times (`_tier_a`'s tail,
    # `_tier_a5`'s tail, `main`'s own merge tail); the all-empty shape is 0
    # because `_sort_unique` early-returns on an empty list without spawning.
    # Irreducible for the same reason `per_call` is: the shell-out is a
    # sanctioned carve-out named in `test_no_bash_dependency.py` for
    # byte-parity with the bash oracle. Lowering any of these means removing
    # a real call, which is what this table wants and will not block.
    "ops.discover_working_repos": {
        "per_call": 1,
        "machine_local_cli_elimination_calls": 0,
        "op_total_tier_a_non_empty": 3,
        "op_total_tier_b_fallback": 3,
        "op_total_all_empty": 0,
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


def _op_leaf(op: str) -> str:
    """The subject-bearing tail of a manifest op key.

    `coverage.diagnose_open_review_loop_dag_mode` -> `diagnose_open_review_loop_dag_mode`;
    `bin.workday_complete_step2_5_dirty_tree.classify_main_pass` -> `classify_main_pass`.
    Deliberately the leaf, not the full dotted key -- several legitimately-live
    rows are never spelled out as their full manifest key anywhere outside this
    module. `changelog.cited_in_range_count`'s real function is
    `_cited_in_range_count`; the string `"changelog.cited_in_range_count"`
    appears nowhere but this file and the manifest, while `cited_in_range_count`
    (the leaf) appears throughout `changelog_ops.py` and its spawn-bound test.
    Substring containment (not exact match) is deliberate for the same reason:
    it catches both the bare function name and any leading-underscore private
    spelling of it.
    """
    return op.rsplit(".", 1)[-1]


def _live_test_corpus_text() -> str:
    """Concatenated source of every live (non-cache) `.py` file that is part
    of the test surface -- lives under a `tests/` directory or is named
    `test_*.py` -- excluding this module itself.

    Excluding this file matters: `_SPAWN_COUNT_HIGH_WATER` and the manifest
    op-key literals below name every op by construction, so leaving it in
    would make every row look "referenced" regardless of whether anything
    OUTSIDE this file still exercises it -- exactly the check this function
    exists to make meaningful.
    """
    chunks = []
    for path in _REPO_ROOT.rglob("*.py"):
        parts = path.parts
        if "__pycache__" in parts or ".git" in parts:
            continue
        if path.resolve() == _THIS_FILE:
            continue
        if "tests" not in parts and not path.name.startswith("test_"):
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(chunks)


def _ops_with_no_live_test_reference(live: dict) -> list:
    """Manifest `spawn_count_budget` rows whose leaf subject name (`_op_leaf`)
    appears in NO live test file outside this module.

    A sweep for a live *reader*, not an import/introspection check on the op
    itself -- deliberately, per the FIX-B brief: a manifest key like
    `bin.workday_complete_step2_5_dirty_tree.classify_main_pass` names a call
    shape, not an importable symbol, so "does this dotted path import" cannot
    be made to work uniformly across the table without faking it for that
    shape of key. See this test module's own docstring for the design options
    weighed and rejected in favor of this one.
    """
    corpus = _live_test_corpus_text()
    return [op for op in live if _op_leaf(op) not in corpus]


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


def test_spawn_count_budget_rows_name_a_subject_that_still_exists():
    """Every live `spawn_count_budget` row must name a subject some live test
    outside this module still reads -- the converse of the two ratchet checks
    above, which both key off `_SPAWN_COUNT_HIGH_WATER` and so can only ever
    catch a mark that outlived its row, never a row that outlived its subject.

    Not hypothetical: `coverage.diagnose_open_review_loop_dag_mode` has been
    in exactly that state since 2026-08-16 -- its function lost its last
    caller and its enforcing test was deleted in the same commit that removed
    the coverage-gate feature, and both the manifest row and this module's
    `_SPAWN_COUNT_HIGH_WATER` mark survived untouched. Nothing failed.
    Evidence: state/audits/2026-08-19-opro-03-c08-budgeted-op-spawn-trace.md,
    EM verification addendum.

    Design chosen over two rejected alternatives (see module docstring for
    the ratchet's own worked-example lineage):

    - Require each row to carry an explicit pointer field (its enforcing test
      path and/or subject symbol) and assert the pointer resolves. Rejected:
      correct in principle, but `budget-manifest.json` is EM-owned this pass
      (a concurrent session holds it too, per this fix's own dispatch brief)
      and every existing row would need a new field added by hand before this
      check could even run -- a one-file test fix has no business forcing a
      manifest-wide edit as its price of entry.
    - Attempt to import/resolve each key as a dotted symbol path. Rejected
      outright, not just costed: several keys (e.g.
      `bin.workday_complete_step2_5_dirty_tree.classify_main_pass`) name a
      call *shape* inside a function, not an importable symbol -- there is no
      uniform way to turn a manifest key into an import that does not silently
      degrade to false-negative-prone special-casing per key, which is the
      "faked into working" trap the brief calls out by name.

    What this check does NOT catch (state plainly, not left implicit): a
    leaf name colliding with unrelated live source that mentions the same
    word for a different reason would false-negative (report "referenced"
    when the real subject is still gone) -- a substring sweep confirms a
    word survives somewhere in the test tree, not that the row's own subject
    does. It also cannot tell a genuinely-reachable subject from a subject
    that is reachable but dead code no test actually exercises at runtime
    (a `test_*.py` file that merely imports the name without calling it would
    still count as "found"). Both are false-negative risks, not false-positive
    ones -- this check can miss a still-dead row, but coming back green never
    means a live row is wrong, only that nothing currently proves it.
    """
    live = _manifest_spawn_count_overrides()
    orphaned = _ops_with_no_live_test_reference(live)
    assert not orphaned, (
        f"spawn_count_budget row(s) with no live test referencing their "
        f"subject anywhere outside this module: {sorted(orphaned)}. Each row "
        f"budgets a subject that reads as governed coverage to any reviewer "
        f"while governing nothing once its last caller/test is gone -- "
        f"either restore a live caller+test or drop the row (manifest is "
        f"EM-owned; report the exact row to the EM rather than editing it "
        f"here)."
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
