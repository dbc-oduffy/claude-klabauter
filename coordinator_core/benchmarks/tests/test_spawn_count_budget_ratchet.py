"""Ratchet + completeness pin for every `spawn_count_budget` row in
`budget-manifest.json`'s `overrides`.

docs/wiki/cost-budgets-and-the-kill-disposition.md's budget rule: a bound is
derived from what the box can afford, then measured against -- never fitted
to what the code currently costs. Without a ratchet, a `spawn_count_budget`
entry is prose a reviewer might notice drift past, not a mechanism -- exactly
the failure the page names (`spawns_per_gap_date`, `per_batch`) for the two
rows already deleted/renamed on 2026-08-15/16. This module is the enforcement
half for the rows that survived that audit -- see `_SPAWN_COUNT_HIGH_WATER`
below for the current, authoritative row count rather than a number restated
here that would drift out from under it --
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

import json
import re
from pathlib import Path

from coordinator_core.benchmarks.budget import load_manifest

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
# `ceremony.scoped_git_commit`'s row was RETIRED on 2026-08-25 along with its
# fourteen ceilings, because the op it budgeted no longer exists: killed at
# K-045 / `c07062c99`, handler and registration removed. A budget for a
# subject that cannot run reads as governed coverage while governing nothing,
# which is the exact failure this module's docstring cites for the two rows
# deleted on 2026-08-15/16. Note the orphan pin below did NOT catch it: that
# check greps the test tree for the row's subject, and the dead op's name
# still appears in guard-message tests (`test_deny_message_accuracy.py`), so
# it stayed green on a false negative its own docstring predicts. Found by
# hand instead.
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
    "fleet.archive_and_commit": {
        "op_total_20_move_batch_sync_push_git_spawns": {
            "ceiling": 1,
            "reason": (
                "The one remaining git spawn on the archival path is "
                "disclosed and does not belong to archive_and_commit: `git "
                "restore --staged` (the shared-index resync, the unfinished "
                "half of our own hand-rolled commit -- see "
                "`_resync_main_index_for_moves`' docstring). Lowered 2 -> 1 "
                "on 2026-08-27: the second spawn this ceiling was sized "
                "against, `git status --porcelain` from session/scope.py's "
                "claim release, was retired at `e0d100640`, and the "
                "cadence-marked gates did not re-run against that cut. "
                "Spy-counted argv, never a job-object process count: that "
                "count includes non-deterministic conhost.exe pairing and "
                "reads 3.0/5.0/7.0 for identical code on one box."
            ),
        },
        "op_total_20_move_batch_sync_push_own_git_spawns": {
            "ceiling": 0,
            "reason": (
                "Floor of 0: a restage_src=False archival move is a rename, so "
                "the blob sha comes from read_tree_spine's HEAD entry and there "
                "is nothing to hash (C1, `cffa6e99f`). A restage_src=True batch "
                "spends exactly one `hash-object` over that subset alone -- "
                "pinned separately by "
                "test_archival_commit_ac1_zero_then_one_own_spawn, which "
                "carries its own two known-point control arms because a "
                "derived spawn_count cannot distinguish zero from one."
            ),
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
    # See module-level comment for the `ops.discover_working_repos` context:
    # `per_call` is a re-baseline (0 -> 1) against newly visible truth, the
    # `op_total_*` marks are the OP end-to-end (nothing stubbed) and are new
    # floors first measured 2026-08-19, and
    # `machine_local_cli_elimination_calls` legitimately keeps `_sort_unique`
    # stubbed to isolate `_merged_flat_registry`'s CLI elimination.
    # GRAVESTONE -- `ceremony.wsc_tail`'s high-water entry, retired 2026-08-27.
    # The op was killed 2026-08-23 (state/kill-ledger.md K-046); DR-358 rebuilt
    # its requirements as in-process calls and explicitly NOT as an op, so no
    # registered subject has carried this name since. Its pinned ceiling of 34
    # `op_total_normal_pass` spawns governed something that cannot run, and its
    # own reason text named the enforcer as
    # `ops/ceremony/tests/test_wsc_tail_spawn_budget.py` -- a file deleted with
    # the op.
    #
    # THIS IS A RETIREMENT, NEVER AN UNBANKED REDUCTION. The ratchet's rule is
    # that a measured reduction must be banked so the ceiling only tightens;
    # dropping a row would be ratchet evasion IF a live subject still spawned
    # under it. Nothing does. The recorded open question this row carried --
    # the unattributed -3 between C3's 37 and the fixture's 34 -- dies with the
    # subject rather than being resolved, and must not be inherited by any
    # successor row: it was measured against a handler that no longer exists.
    #
    # WHY IT SURVIVED THE KILL, which is the part worth keeping. The orphan
    # check below, `test_spawn_count_budget_rows_name_a_subject_that_still_
    # exists`, is a SUBSTRING SWEEP of the test tree, and `wsc_tail` still
    # occurs as prose in guard-message fixtures -- so the sweep found the word,
    # passed, and the row outlived its subject by four days. That is the SECOND
    # time this exact false negative has fired: this module's own docstring
    # already records `ceremony.scoped_git_commit` surviving K-045 the same way,
    # "found by hand instead." Found by hand again. The durable fix is
    # resolving a row's subject against the op registry rather than grepping
    # for its name; surfaced as a design question, deliberately not patched
    # here, because dropping this row treats the symptom and leaves the sweep
    # blind to the third occurrence.
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


# The substring sweep that used to live here -- `_op_leaf`,
# `_live_test_corpus_text`, `_ops_with_no_live_test_reference` -- was REMOVED
# 2026-08-27, not kept alongside its replacement. It asked whether a row's leaf
# name appears anywhere in the test corpus, which cannot separate a subject a
# test exercises from a dead string a test uses as data, so keeping it as a
# second opinion would only have re-supplied the false negative that let two
# killed ops keep their budgets. Its design rationale and the two alternatives
# it was chosen over are preserved in this module's own history and in
# `_rows_whose_named_enforcer_is_gone`'s docstring below, which records what it
# measured and why the signal was wrong rather than merely noisy.

#: Any `test_*.py` filename mentioned in a row's own text. Rows name their
#: enforcing test in `_rationale`/`reason` prose as an existing convention --
#: all 8 live rows did so unprompted when this check was written -- so the
#: pointer needs no new manifest field and no manifest-wide hand edit.
_TEST_FILE_IN_PROSE = re.compile(r"test_[\w.]*\.py")


def _named_enforcer_paths(row: dict) -> list:
    """Every `test_*.py` filename this row's own text names, deduped."""
    return sorted(set(_TEST_FILE_IN_PROSE.findall(json.dumps(row))))


def _rows_whose_named_enforcer_is_gone(overrides: dict) -> tuple:
    """Partition `spawn_count_budget` rows by whether the enforcing test they
    NAME still exists on disk.

    Returns `(orphaned, unpinnable)` -- `orphaned` rows name at least one test
    and NONE of them resolve; `unpinnable` rows name no test at all.

    WHY THIS REPLACED THE SUBSTRING SWEEP, removed outright as the gravestone
    above records. That sweep asked "does this row's leaf name appear anywhere
    in the test corpus", and that question cannot distinguish a subject a test
    EXERCISES from a dead string a test happens to USE AS DATA. Measured on `ceremony.wsc_tail`
    after its op was killed 2026-08-23: the leaf appears in 28 test files;
    stripping comments and docstrings (so pure prose cannot vouch for a
    subject) still leaves 8, and every one of those 8 is the dead name as
    sample data -- `ipc._timeout_for("ceremony.wsc_tail")` in a timeout-table
    test, `monkeypatch.setattr(cost_census, "HOT_PATH_OPS",
    ("ceremony.wsc_tail",))` as an arbitrary fixture value, a local variable
    named `wsc_tail`. No refinement of the search fixes that; the signal is
    wrong, not merely noisy.

    THIS IS THE THIRD ATTEMPT AND THE FIRST SOUND ONE. The sweep false
    -negatived twice, and both times the row was found by hand instead:
    `ceremony.scoped_git_commit` surviving K-045, then `ceremony.wsc_tail`
    surviving K-046 by four days with a ceiling of 34 and a `_rationale`
    naming `test_wsc_tail_spawn_budget.py`, a file deleted with the op. THIS
    CHECK WOULD HAVE CAUGHT THAT ONE ON THE DAY: the named file does not
    resolve, so the row is `orphaned` and the assertion is red.

    A NAMED-AND-RESOLVING TEST IS NOT PROOF THE SUBJECT IS EXERCISED -- state
    the residual plainly rather than overclaiming a third time. A row could
    name a test that exists but no longer touches it. That is a strictly
    smaller gap than the sweep's, and it is closable later by asserting the
    named node id rather than the file; it is not closed here.

    `unpinnable` rows are REPORTED, NEVER SILENTLY GREEN -- a row naming no
    enforcer is exactly the shape both escapees had in effect, and letting it
    pass unremarked rebuilds the blind spot one level up.
    """
    orphaned, unpinnable = [], []
    for op, row in overrides.items():
        named = _named_enforcer_paths(row)
        if not named:
            unpinnable.append(op)
            continue
        if not any(
            any(True for _ in _REPO_ROOT.rglob(name)) for name in named
        ):
            orphaned.append((op, named))
    return orphaned, unpinnable


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
    rows = {
        op: entry
        for op, entry in load_manifest().get("overrides", {}).items()
        if isinstance(entry, dict) and entry.get("spawn_count_budget")
    }
    orphaned, unpinnable = _rows_whose_named_enforcer_is_gone(rows)

    assert not orphaned, (
        f"spawn_count_budget row(s) naming an enforcing test that no longer "
        f"exists: {orphaned}. Each row budgets a subject that reads as "
        f"governed coverage to any reviewer while governing nothing -- its "
        f"own text names the test that was supposed to enforce it, and that "
        f"file is gone. Either restore the enforcer or retire the row "
        f"(manifest is EM-owned; report the exact row to the EM rather than "
        f"editing it here). If the subject itself was killed, retire the row "
        f"AND leave a gravestone in _SPAWN_COUNT_HIGH_WATER rather than "
        f"deleting its mark silently -- a retirement is not an unbanked "
        f"reduction, but only the gravestone says so."
    )

    assert not unpinnable, (
        f"spawn_count_budget row(s) naming no enforcing test at all: "
        f"{sorted(unpinnable)}. Not a style nit: a row with no named enforcer "
        f"is unfalsifiable by this check, which is the exact state both rows "
        f"that escaped it were effectively in. Name the enforcing test in the "
        f"row's own _rationale -- every row did so unprompted when this check "
        f"was written, so this asks for the existing convention, not a new "
        f"manifest field."
    )


def test_named_enforcer_check_catches_the_row_that_escaped_it_twice():
    """MUTATION PROBE, and the only evidence that matters for this check.

    A green orphan check has meant nothing here twice: the substring sweep it
    replaced passed while `ceremony.scoped_git_commit` (K-045) and then
    `ceremony.wsc_tail` (K-046) sat in the manifest budgeting dead subjects,
    and both were found by hand. So this reconstructs the EXACT row that
    escaped -- `ceremony.wsc_tail`'s real shape, ceiling 34, its `_rationale`
    naming `test_wsc_tail_spawn_budget.py`, the file deleted along with the op
    on 2026-08-23 -- and asserts the replacement classifies it `orphaned`.

    Without this probe the new check is only ASSERTED to be sound. With it,
    the third attempt is the first one with a demonstration attached.
    """
    escaped_row = {
        "target_ms": 300,
        "spawn_count_budget": {"op_total_normal_pass": 34},
        "_rationale": (
            "34, reproduced across two independent runs 2026-08-21 against "
            "test_wsc_tail_spawn_budget.py's own fixture -- the figure the "
            "shipped assertion actually measures."
        ),
    }
    orphaned, unpinnable = _rows_whose_named_enforcer_is_gone(
        {"ceremony.wsc_tail": escaped_row}
    )
    assert unpinnable == [], (
        "the row names a test, so it must not be classified unpinnable -- "
        f"got {unpinnable!r}"
    )
    assert [op for op, _ in orphaned] == ["ceremony.wsc_tail"], (
        "the replacement check failed to flag the row that escaped its "
        f"predecessor twice -- got {orphaned!r}. The named enforcer "
        "test_wsc_tail_spawn_budget.py was deleted with the op on 2026-08-23."
    )

    # AND THE CONVERSE, so this cannot pass by flagging everything: a row
    # naming an enforcer that DOES exist stays clean. Uses this module's own
    # file, which is guaranteed present while the test is running.
    live_row = {
        "spawn_count_budget": {"per_call": 1},
        "_rationale": "enforced by test_spawn_count_budget_ratchet.py",
    }
    orphaned, unpinnable = _rows_whose_named_enforcer_is_gone({"live.row": live_row})
    assert orphaned == [] and unpinnable == []


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
