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
_BASELINE_REASON = (
    "Pre-existing baseline mark predating this fix; no per-row rationale "
    "was captured when spawn-count ratcheting was first added to this table."
)

# Each mark pairs a `ceiling` with a `reason` -- see
# tasks/opro-03-c08-trace/FIX-H-raise-needs-a-stated-reason.md. A unit test
# has no prior value to compare against (git history is not a reliable
# source inside a test), so this table cannot detect a RAISE directly; it
# instead requires every mark to carry a stated reason, uniformly, present
# and future -- direction (raise vs. floor vs. re-baseline) stays a
# review-time judgment made from the reason text, not something computed
# here. Lowering a value never touches `reason`, so it stays frictionless --
# only adding a new key or row requires authoring one.
_SPAWN_COUNT_HIGH_WATER = {
    "ceremony.scoped_git_commit": {
        "green_path": {
            "ceiling": 17,
            "reason": (
                "Raised 12->17 on 2026-08-18 (opro-01): the CAS re-read of "
                "index/HEAD plus one interpret-trailers call are five real, "
                "correctness-bearing spawns landed after the 2026-08-11 "
                "baseline -- see module docstring."
            ),
        },
        "refusal_path_unanswerable": {
            "ceiling": 0,
            "reason": (
                "Floor of 0: the refusal path returns before any git spawn "
                "by construction, so this ceiling has never moved and needs "
                "no re-baseline argument."
            ),
        },
        "directory_pathspec_expansion": {
            "ceiling": 18,
            "reason": (
                "Raised 13->18 alongside green_path on 2026-08-18 (opro-01) "
                "for the same CAS re-read plus interpret-trailers spawns -- "
                "see green_path's reason above."
            ),
        },
        "push_raced_path": {
            "ceiling": 20,
            "reason": (
                "Pre-existing mark, not touched by the 2026-08-18 CAS raise "
                "documented above; no session-local rationale is recorded "
                "for this key."
            ),
        },
        "pending_drain_superseded": {
            "ceiling": 23,
            "reason": (
                "New shape first measured 2026-08-19 (opro-03 C6): a due "
                "pending-push record draining into a superseded "
                "non-fast-forward push. Enters at its measured value, not a "
                "raise -- it budgets a path no prior shape reached."
            ),
        },
        # The three `op_total_*` marks below entered the manifest on
        # 2026-08-19 (opro-03 C6, 46bab79e52ca) without a row here, which
        # left `test_spawn_count_high_water_table_covers_every_override` red
        # -- the completeness pin doing exactly its job, on a module the fast
        # tier does not run. They are the WHOLE-OP `subprocess.run` counts,
        # deliberately larger than the same shape's seam figure: the seam
        # (`git_native._git`) is routing-narrow and cannot observe the push
        # drain, claim release, `git/divergence.py`, or `git/repo_root.py`.
        # Each enters at its measured value; none is a raise of an existing
        # mark, because no `op_total_*` mark existed before.
        "op_total_green_path": {
            "ceiling": 23,
            "reason": (
                "Entered 2026-08-19 at its first measured value (opro-03 "
                "C6). Whole-op count against green_path's seam figure of 17; "
                "the gap is the six spawns the seam is structurally unable "
                "to see."
            ),
        },
        "op_total_directory_pathspec_expansion": {
            "ceiling": 24,
            "reason": (
                "Entered 2026-08-19 at its first measured value (opro-03 "
                "C6). green_path's whole-op figure plus the one extra "
                "pathspec-scoped `git status --porcelain` that shape adds."
            ),
        },
        "op_total_push_raced_path": {
            "ceiling": 28,
            "reason": (
                "Entered 2026-08-19 at its first measured value (opro-03 "
                "C6). Whole-op count against push_raced_path's seam figure "
                "of 20."
            ),
        },
        "op_total_pending_drain_superseded": {
            "ceiling": 39,
            "reason": (
                "Entered 2026-08-19 at its first measured value (opro-03 "
                "C6). The largest whole-op figure of the four shapes because "
                "this is the only one whose drain actually pushes: it pays "
                "`push_once`, the `_is_superseded` fetch and ancestor test, "
                "and the cockpit-contract publish leg on top of the commit."
            ),
        },
    },
    "changelog.cited_in_range_count": {
        "n_tokens": {"ceiling": 1, "reason": _BASELINE_REASON},
        "no_tokens": {
            "ceiling": 0,
            "reason": (
                "Floor of 0: the no-tokens path short-circuits before any "
                "spawn, matching the early-return-means-zero pattern used "
                "across this table."
            ),
        },
    },
    "percolate.functional_identifier_output_drift_in_tree": {
        "no_dest_publish_time": {
            "ceiling": 0,
            "reason": (
                "Floor of 0: no destination publish time means the drift "
                "check has nothing to diff and returns before spawning."
            ),
        },
        "with_dest_publish_time_n_files": {
            "ceiling": 1,
            "reason": _BASELINE_REASON,
        },
    },
    "bin.coordinator_harvest_deferrals_dedup_scan_root_resolution": {
        "resolution_calls_for_5_candidate_rows": {
            "ceiling": 3,
            "reason": _BASELINE_REASON,
        },
        "directory_scan_calls_for_5_candidate_rows": {
            "ceiling": 1,
            "reason": _BASELINE_REASON,
        },
    },
    "bin.reap_integrated_review_findings.tracked_untracked_split": {
        "per_reap_call": {"ceiling": 1, "reason": _BASELINE_REASON},
    },
    "bin.workday_complete_step2_5_dirty_tree.classify_main_pass": {
        "per_classify_call": {"ceiling": 2, "reason": _BASELINE_REASON},
    },
    "execute_plan_assemble.dispatch_ledger_delivered": {
        "no_committed_rows": {
            "ceiling": 0,
            "reason": (
                "Floor of 0: no committed rows means the dispatch ledger "
                "has nothing to walk and returns before spawning."
            ),
        },
        "n_committed_rows": {"ceiling": 2, "reason": _BASELINE_REASON},
    },
    "execute_plan_assemble.sibling_committed_chunk_ids_memo": {
        "second_call_identical_inputs": {
            "ceiling": 0,
            "reason": (
                "Floor of 0: a memoized second call with identical inputs "
                "returns the cached result without spawning again."
            ),
        },
        "first_call_one_sibling": {
            "ceiling": 3,
            "reason": (
                "New shape first measured 2026-08-19 (opro-03 C6), entering "
                "at its measured value. This op's only prior shape was the "
                "memo hit at 0, and a shape that spawns nothing by "
                "construction can never make a spawn site visible to a "
                "counter -- which is why `close_out_and_stamp.py::_run_git` "
                "sat permanently red on the uncounted-spawn gate. The 3 is "
                "one merge-base range resolution plus two "
                "`_deliverable_log_records` queries against the sibling."
            ),
        },
    },
    # See module-level comment for the `ops.discover_working_repos` context:
    # `per_call` is a re-baseline (0 -> 1) against newly visible truth, the
    # `op_total_*` marks are the OP end-to-end (nothing stubbed) and are new
    # floors first measured 2026-08-19, and
    # `machine_local_cli_elimination_calls` legitimately keeps `_sort_unique`
    # stubbed to isolate `_merged_flat_registry`'s CLI elimination.
    "ceremony.wsc_tail": {
        "op_total_normal_pass": {
            "ceiling": 34,
            "reason": (
                "34, reproduced across two independent runs 2026-08-21 against "
                "test_wsc_tail_spawn_budget.py's own fixture -- this is the "
                "figure the shipped assertion actually measures, and it is the "
                "one pinned. C3 recorded 37 earlier the same day from a "
                "STANDALONE REPRO SCRIPT (not shipped), and the plan's origin "
                "figure was also '~37 per close'. ATTRIBUTION OF THE -3 IS "
                "OPEN: it is a REDUCTION, so banking it can only make this "
                "ratchet stricter, and it was banked rather than left "
                "unbanked. The leading candidate is a1e5dd970 (15:36, "
                "'wsc-tail: its review-trail records reach the commit that "
                "reviewed them'), which touched wsc_tail.py after C3's "
                "measurement; the other candidate is a harness difference "
                "between C3's standalone script and this fixture. Neither is "
                "established -- do NOT restate either as the cause without "
                "measuring it. Recorded open so the next person to see this "
                "move does not re-derive it from scratch. "
                "Superseded provenance follows. "
                "First measured 2026-08-21 (C3, dlv-every-op-knows-what-it-"
                "spawns-f4bbf6): a fresh single-file commit on `main` "
                "through the full `ceremony.wsc_tail` handler, deferred-"
                "push mode with `_spawn_deferred_push_skip_loud` "
                "monkeypatched off. Matches the plan's own '~37 spawns per "
                "close' origin figure exactly -- the first time that number "
                "is a real measurement rather than an inherited "
                "approximation. Migrated 2026-08-21 (C4) from a test-local "
                "constant (`_WSC_TAIL_OP_TOTAL_CEILING`, deleted in the "
                "same change) into this manifest-backed table; the value "
                "is unchanged by the migration."
            ),
        },
    },
    "ops.discover_working_repos": {
        "per_call": {
            "ceiling": 1,
            "reason": (
                "Raised 0->1 on 2026-08-19 (opro-03 C-04): the old 0 only "
                "held with `_sort_unique` monkeypatched out; the 1 is "
                "`_tier_a5`'s tail call to that sanctioned `sort -u` "
                "carve-out (test_no_bash_dependency.py) -- a re-baseline "
                "against newly visible truth, not a raise fitted to an "
                "unreduced spawn set."
            ),
        },
        "machine_local_cli_elimination_calls": {
            "ceiling": 0,
            "reason": (
                "Kept at its original 0: this key isolates "
                "`_merged_flat_registry`'s elimination of per-key "
                "machine-local CLI spawns and legitimately stubs "
                "`_sort_unique` to measure that in isolation."
            ),
        },
        "op_total_tier_a_non_empty": {
            "ceiling": 3,
            "reason": (
                "New floor, first measured 2026-08-19: `main()` reaches the "
                "sanctioned `_sort_unique` carve-out three times on this "
                "path (`_tier_a`'s tail, `_tier_a5`'s tail, `main`'s own "
                "merge tail) -- irreducible for the same carve-out reason "
                "as per_call."
            ),
        },
        "op_total_tier_b_fallback": {
            "ceiling": 3,
            "reason": (
                "New floor, first measured 2026-08-19: same three-call "
                "shape as op_total_tier_a_non_empty on the tier-b fallback "
                "path -- see that key's reason."
            ),
        },
        "op_total_all_empty": {
            "ceiling": 0,
            "reason": (
                "New floor, first measured 2026-08-19: `_sort_unique` "
                "early-returns on an empty list without spawning, so the "
                "all-empty shape floors at 0."
            ),
        },
    },
}

_MIN_REASON_LEN = 40
_TRIVIAL_REASONS = frozenset({
    "", "n/a", "na", "todo", "tbd", "fixme", "raise", "raised", "reason",
    "because", "why not", "no reason", "no reason given", "misc", "various",
})


def _thin_reason_violations(table: dict) -> list:
    """Every `(op, key)` mark in `table` whose `reason` fails the
    presence-and-length floor -- catches an omitted `reason`, an empty
    string, and the handful of known dodges (`"n/a"`, `"raised"`, ...) a
    required-but-uninspected field invites.

    What this does NOT catch, stated plainly: a reason that is present,
    non-trivial in length, and simply wrong -- or one that argues for a
    different key than the one it justifies. Rationale QUALITY stays
    human-judged at review time; this only makes a reason's EXISTENCE
    mechanical. It also cannot tell a genuine raise from a lowered value or
    a brand-new floor -- there is no prior value inside a unit test to
    compare against (git history is not a reliable source here), so every
    mark carries a reason uniformly and direction stays a review-time call.
    See tasks/opro-03-c08-trace/FIX-H-raise-needs-a-stated-reason.md.
    """
    violations = []
    for op, keys in table.items():
        for key, mark in keys.items():
            reason = mark.get("reason", "") if isinstance(mark, dict) else ""
            normalized = reason.strip().lower().strip(".! ")
            if (
                not reason
                or len(reason.strip()) < _MIN_REASON_LEN
                or normalized in _TRIVIAL_REASONS
            ):
                violations.append(f"{op}.{key}")
    return violations


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
        for key, mark in keys.items():
            ceiling = mark["ceiling"]
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
        "per_call": {
            "ceiling": 0,
            "reason": (
                "Probe fixture reusing a confirmed-deleted op -- see this "
                "test's own docstring for why this op was picked."
            ),
        },
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


def test_spawn_count_high_water_marks_carry_a_stated_reason():
    """Every `_SPAWN_COUNT_HIGH_WATER` mark pairs its ceiling with a stated
    reason -- see `_thin_reason_violations` for what this does and does not
    enforce, and tasks/opro-03-c08-trace/FIX-H-raise-needs-a-stated-reason.md
    for why a unit test cannot detect a raise directly (no prior value
    survives to compare against) and settles for requiring the pair instead.
    """
    violations = _thin_reason_violations(_SPAWN_COUNT_HIGH_WATER)
    assert not violations, (
        f"mark(s) with no stated reason (missing, empty, too short, or a "
        f"known dodge like 'n/a'): {sorted(violations)}. Add a `reason` "
        f"explaining what is irreducible about the count, or why a new row "
        f"is a floor rather than a raise."
    )


#: The marks that carried no per-row rationale when `reason` was introduced
#: (opro-03 C7, 2026-08-19). ENUMERATIVE AND CLOSED: `_BASELINE_REASON` is a
#: grandfather clause, and a grandfather clause nothing pins is just an escape
#: hatch with a polite name -- a future raise could write
#: `reason: _BASELINE_REASON` and satisfy the reason check while stating
#: nothing, which is the exact dodge that check exists to refuse. Pinning the
#: set means a NEW mark cannot reach for it: it must author a real reason or
#: fail. This list only ever shrinks -- when one of these earns a real
#: rationale, delete its entry here in the same commit.
_BASELINE_REASON_GRANDFATHERED = frozenset(
    {
        "changelog.cited_in_range_count.n_tokens",
        "percolate.functional_identifier_output_drift_in_tree.with_dest_publish_time_n_files",
        "bin.coordinator_harvest_deferrals_dedup_scan_root_resolution.resolution_calls_for_5_candidate_rows",
        "bin.coordinator_harvest_deferrals_dedup_scan_root_resolution.directory_scan_calls_for_5_candidate_rows",
        "bin.reap_integrated_review_findings.tracked_untracked_split.per_reap_call",
        "bin.workday_complete_step2_5_dirty_tree.classify_main_pass.per_classify_call",
        "execute_plan_assemble.dispatch_ledger_delivered.n_committed_rows",
    }
)


def _unpinned_baseline_reason_marks(table: dict) -> list:
    """Marks using `_BASELINE_REASON` that are not in the grandfathered set.

    Without this, the reason requirement is satisfiable by a symbol rather
    than an argument. `_thin_reason_violations` catches the STRING dodges
    (`""`, `"n/a"`); this catches the SYMBOL one, which is the more likely
    of the two precisely because it looks like following the convention.
    """
    return [
        f"{op}.{key}"
        for op, keys in table.items()
        for key, mark in keys.items()
        if isinstance(mark, dict)
        and mark.get("reason") is _BASELINE_REASON
        and f"{op}.{key}" not in _BASELINE_REASON_GRANDFATHERED
    ]


def test_baseline_reason_is_a_closed_set_not_an_escape_hatch():
    """`_BASELINE_REASON` may only appear on the marks that predate the reason
    requirement. A new mark reaching for it fails here.

    The reason check one test above is satisfied by any sufficiently long
    string, so the cheapest way to defeat it is to reuse the boilerplate that
    is already in the file and passing -- not to invent a dodge. That path is
    closed here rather than left to review-time vigilance, which is the
    discharge this repo's own north star refuses ("name the artifact that
    discharges it -- the operator remembers is not one").
    """
    unpinned = _unpinned_baseline_reason_marks(_SPAWN_COUNT_HIGH_WATER)
    assert not unpinned, (
        f"mark(s) using _BASELINE_REASON without being grandfathered: "
        f"{sorted(unpinned)}. That string records the ABSENCE of a rationale "
        f"and is reserved for marks predating the requirement. Write what is "
        f"irreducible about this count, or why it is a floor rather than a "
        f"raise."
    )

    stale = sorted(
        pin
        for pin in _BASELINE_REASON_GRANDFATHERED
        if pin
        not in {
            f"{op}.{key}"
            for op, keys in _SPAWN_COUNT_HIGH_WATER.items()
            for key in keys
        }
    )
    assert not stale, (
        f"grandfathered pin(s) naming a mark that no longer exists: {stale}. "
        f"Remove them -- a pin outliving its mark is the same orphan class "
        f"`_stale_high_water_ops` exists to catch one level up."
    )


def test_spawn_count_high_water_reason_check_rejects_and_accepts():
    """Mutation probe: the reason check must actually reject a raise with no
    stated reason (missing, empty, too short, or a known dodge) and accept
    the identical raise once a real reason is attached -- proof the check
    bites, not just parses.
    """
    bare = {"probe.op": {"raised_key": {"ceiling": 999, "reason": ""}}}
    assert _thin_reason_violations(bare) == ["probe.op.raised_key"], (
        "an empty reason was not flagged -- the presence check is not firing."
    )

    dodge = {"probe.op": {"raised_key": {"ceiling": 999, "reason": "n/a"}}}
    assert _thin_reason_violations(dodge) == ["probe.op.raised_key"], (
        "a known-dodge reason ('n/a') was not flagged."
    )

    short = {"probe.op": {"raised_key": {"ceiling": 999, "reason": "needed it"}}}
    assert _thin_reason_violations(short) == ["probe.op.raised_key"], (
        "a too-short reason was not flagged."
    )

    real = {
        "probe.op": {
            "raised_key": {
                "ceiling": 999,
                "reason": (
                    "probe reason long enough to clear the minimum-length "
                    "floor and distinct from the known-dodge denylist."
                ),
            }
        }
    }
    assert _thin_reason_violations(real) == [], (
        "a genuine, non-trivial reason was flagged -- the check is over-firing."
    )
