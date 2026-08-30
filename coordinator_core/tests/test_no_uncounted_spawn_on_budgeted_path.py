"""C-13 (`docs/plans/2026-08-19-every-budgeted-op-counts-its-own-spawns.md`): the mechanism
decision opro-03 shipped for "every budgeted op counts the spawns it actually makes."

THE PROPERTY. *A spawn reachable from a budgeted op's entrypoint must be visible to that op's
counter.* NOT "a budgeted op may not call `subprocess`" -- `discover_working_repos::_sort_unique`
shells to `sort -u` as a *sanctioned* carve-out (`test_no_bash_dependency.py`), and a literal
prohibition would either delete a ruled-on carve-out or accrete an exemption list to survive it.
A legitimate spawn is fine; an UNCOUNTED one is not.

THIS GATE'S OPERATIONAL DEFINITION OF "COUNTED" (revised 2026-08-19; see "WHAT CHANGED AND WHY"
below for the superseded text and the measurement that retired it). A site is counted when it
carries a `_LEGITIMIZED_SITES` entry proving all THREE of:

  1. MECHANISM -- the counter observes the way this site actually reaches the process boundary.
     For a `_SEAM` counter (function-OBJECT substitution) this holds by construction. For a
     `_GLOBAL_SUBPROCESS_RUN` counter it holds only while the site spawns via `subprocess.run`,
     and that precondition is ENFORCED, not assumed: `test_legitimized_site_mechanism_pins_hold`.
  2. ASSERTION -- an existing test checks the resulting figure by EXACT EQUALITY against a
     manifest budget. A bound, a range, or a `<=` does not qualify: a new spawn must FAIL
     something, not fit under something.
  3. EXECUTION -- the asserting test's counter was MEASURED to actually run this site. Static
     reachability does not establish this and neither do (1) and (2).

NOTHING is pre-populated, and tuning `_LEGITIMIZED_SITES` to buy a green without earning all three
legs is exactly the failure this plan's anti-scope names.

Leg 3 is the one that does the real work, and it cuts BOTH ways. `ceremony.scoped_git_commit`'s
`green_path: 17` is not a miscount -- its fixture configures no remote, so the whole
`_drain_pending_push_after_sync` -> `auto_push` leg never runs. That path could grow without bound
and the budget would never move. Conversely, of the nine sites that were reachable-and-under-an-
exact-equality-global-counter, SIX were measured to execute under it (legitimized below) and THREE
were measured not to (`_UNCOUNTED_MEASURED_UNREACHED`, still red). "Reachable and under a counter"
reads as counted right up until the measurement is taken; taking it is leg 3.

WHAT CHANGED AND WHY (the C6 contract decision, opro-03). This gate previously refused a global
`subprocess.run` counter outright, on the grounds that such visibility is "a test-time accident of
how that one companion test happens to patch, not a structural guarantee a future refactor
preserves." The REASONING was right and the CONCLUSION did not follow. The two counter shapes have
complementary holes, not a hierarchy (see `_SEAM` / `_GLOBAL_SUBPROCESS_RUN`): a function-object
seam is mechanism-robust and routing-narrow, a global patch is routing-robust and mechanism-narrow.
Ranking them by patch width picks the wrong axis -- and it is the SEAM shape's hole, not the global
one's, that produced this gate's seven open `ceremony.scoped_git_commit` bypasses. The right move
was therefore to ENFORCE the global shape's precondition rather than to keep refusing the shape.
Note what that enforcement had to close: `spawn_policy.site_key` is
`(path, enclosing, argv0, ordinal)` with no mechanism component, so a `subprocess.run` -> `Popen`
edit at a legitimized site keeps the key byte-identical and the exemption would have silently
outlived the counter. The old text named that fragility and then left it unmeasured and unfixed;
`_MECHANISM_PIN` fixes it. Measurement backing all of this: 2026-08-19, every flagged site's spawn
callee derived from the AST (13 of 13 are `subprocess.run`), and every companion test re-run under
a stack-recording `subprocess.run` wrapper to see which sites actually execute.

OPRO-03 FOLLOW-UP (2026-08-21): the site left standing after C6 drained `_LEGITIMIZED_SITES` to
empty -- `ceremony.scoped_git_commit`'s reachable set is NOT static, and `_replay_post_commit_
auto_push` being wired into `commit_scoped`'s private-index (diverged-path) branch put a NEW site
(`auto_push._detach_and_run`) on it, reachable only under the op's default `push_mode="deferred"`
shape, which none of the four then-existing `test_commit_e2e_spawn_budget.py` fixtures exercised.
That site spawns via `subprocess.Popen`, not `run` -- the FIRST site this gate has ever flagged
that neither `_GLOBAL_SUBPROCESS_RUN` NOR the seam counter could see regardless of reachability,
since it is the first genuinely `Popen`-only spawn on any budgeted op's reached set. Closed by
route 3 (a counter that can see it) plus route 1 (a fixture that takes the branch): a new
`_GLOBAL_SUBPROCESS_SPAWN` counter constant widens `_count_op_spawns_both_ways` to also watch
`subprocess.Popen`, scoped to the one recognised auto-push-respawn argv shape so it never
intercepts `subprocess.run`'s own internal `Popen` calls; `test_deferred_diverged_commit_reaches_
detached_push_spawn_count_matches_budget` plants the diverged-path-under-`deferred` precondition
and measures the site executing directly. `_GLOBAL_SUBPROCESS_RUN`'s own pin stays unwidened --
see `_MECHANISM_PINS`.

REUSE FROM `spawn_policy`, UNMODIFIED (pinned API, `tasks/shell-spawn-regrowth-gate/
PINNED-API.md`): `sites_in_source`, `is_test_tree_site`, `DEFAULT_EXCLUDE`, `SpawnParseError`,
`discover_source_files`. Not extending `SpawnSite`.

REUSE FROM `coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`, UNMODIFIED, its
repo-wide cross-module name index: `_FuncIndex`, `_build_func_index`, `_FileRecord`,
`_load_file_records`, `_discover_scope_files`, `_REPO_ROOT`, `_GATE_SCOPE_ROOTS`. That module's
own docstring names its resolution as ONE-HOP ONLY (routes b/c/d/e/f/g each resolve a callee at
most one indirection deep) and explicitly excludes the transitive deep tail, measured there at
32% true-positive repo-wide with no static discriminator separating true from false at any depth.
This gate's whole reason to exist is that C-13 needs the transitive closure that module declined
to build -- so it is reopened here, deliberately, over a MUCH smaller surface (see "SCOPE
RESTRICTION AND ITS MEASURED EFFECT" below).

THE ONE NEW PREDICATE: is a spawn site's enclosing function TRANSITIVELY reachable, through any
number of hops, from one of the eight live budgeted entrypoints (`_BUDGETED_ENTRYPOINTS`)? This
gate builds that reachable-function set itself (`_reachable_functions`, a plain worklist BFS over
a call graph, fixed-point over a finite domain the same way `_FuncIndex.spawn_bearing_params`
terminates) -- reusing the OTHER module's per-file `func_defs` (every top-level function's own AST
node) as the domain, and adding its OWN edge resolution rather than that module's
`imported_names_by_file` + repo-wide `funcs_by_name` pairing (see `_import_function_aliases`'s own
docstring for why: that pairing is safe for a ONE-HOP route that independently re-verifies each
resolved callee's body, and measured UNUSABLE for a transitive BFS that does not -- a single
common-name collision, e.g. two unrelated modules each defining a `_git` helper, opened the entire
unrelated subtree behind whichever one the repo-wide index happened to list first, inflating one
op's reachable set from 0 to roughly 40 on the first run against the live tree). This gate resolves
three edge kinds instead, all PRECISE (pinned to the specific module a name was imported FROM, not
a repo-wide bare-name search): same-module direct calls; `from X import func` resolved to the
exact `X` (`_import_function_aliases`); and a bare `module.attr(...)` attribute call through a
tracked `import X` / `from pkg import X` / `from . import X` module alias (`_import_module_aliases`),
plus the one further hop the C-08 audit's own trace hit by hand (op 5, hop 4:
`_claude_klabauter_root = cli_shared.claude_klabauter_root` -- a module-level local name bound to an imported module's
attribute, then called bare -- `_local_module_attr_aliases`). Deeper relative-import levels
(`from ...pkg import X`, level >= 2) and any alias chain longer than the hops above are accepted
false-negative gaps, not traced.

FUNCTION GRANULARITY, NOT LITERAL MODULE GRANULARITY. The plan phrases the predicate as "is this
MODULE reachable" -- this gate answers a tighter question, "is this specific ENCLOSING FUNCTION
reachable," and treats a spawn site as on-path only when its own top-level enclosing function (not
merely some other function in the same file) is in the reached set. A literal module-level
predicate was tried by hand first and rejected: `ceremony.scoped_git_commit` imports
`git_native` for its one counted seam (`git_native._git`), which would make the WHOLE of
`git_native.py` "reachable" under a module-level reading -- including
`cat_file_batch_objects`/`_hash_object_stdin_bytes`, two sites the C-08 audit traced BY HAND as
unreachable from this op (they belong to a different commit form entirely). Function-granularity
does not report either of those two, because neither function is in the transitively-reached set;
a literal module-level predicate would have reported both as false positives on the very first op
it was tried against. This is a deliberate tightening past the plan's literal wording, not a
loophole -- see the measurement section below for what it does and does not fix.

SCOPE RESTRICTION AND ITS MEASURED EFFECT (per the plan's own instruction: "whether that holds is
itself a measurement, not an assumption"). Restricted to the nine live budgeted entrypoints'
reachable sets, this predicate flags 18 sites on the live tree at HEAD (not 10 -- see below for
why that is a finding, not a bug). HAND-TRACED classification of every one of the 18 (full per-site
call chains in this file's own run-report sidecar):

  - 9 sites match the C-08 audit's confirmed-reachable table exactly, op-for-op, site-for-site:
    `changelog.cited_in_range_count` (1), `percolate.functional_identifier_output_drift_in_tree`
    (1), `bin.coordinator_harvest_deferrals_dedup_scan_root_resolution` (1, its `repo_root.py` hop),
    `bin.reap_integrated_review_findings.tracked_untracked_split` (1),
    `bin.workday_complete_step2_5_dirty_tree.classify_main_pass` (1),
    `execute_plan_assemble.dispatch_ledger_delivered` (2), `ops.discover_working_repos` (1), plus
    one of `execute_plan_assemble.sibling_committed_chunk_ids_memo`'s two (its `_run_git` site) --
    that op (and its whole underlying mechanism, `_sibling_committed_chunk_ids` and friends) was
    later deleted outright (2026-08-21 close-ceremony C3), taking both its `_BUDGETED_ENTRYPOINTS`
    row and this `_LEGITIMIZED_SITES` entry with it; the `_run_git` site itself survives, still
    legitimized, but now only under `dispatch_ledger_delivered`, which reaches the same physical
    site independently.
  - 1 site (`sibling_committed_chunk_ids_memo`'s second hit, `git/repo_root.py::_spawn_rev_parse`)
    was NOT in the audit's own op-9 section but is a real hop the audit's own limits section
    predicted ("a different session re-running this same method could plausibly find 1-3 more
    sites") -- `_sibling_committed_chunk_ids` -> `_committed_chunk_ids` -> `_committed_chunk_shas`
    reaches it 3-4 hops deep, past where the by-hand trace stopped. That whole call chain is gone
    along with the op (see above); this site is no longer part of any budgeted op's reachable set.
  - 8 sites belonged to `ceremony.scoped_git_commit`, which the C-08 audit called CLEAN (0 sites) --
    verified at the time by hand-tracing every one back to `_handler`: `_handler` called
    `run_commit_pipeline` (`commit_pipeline.py`, imported), which directly imported and called
    `git_native._git` (the op's own declared, intentionally-counted seam -- reachable and correct,
    simply not yet `_LEGITIMIZED_SITES`-entered) and `diverging_paths` (`git/divergence.py`,
    confirmed: its body calls `_run_git` directly), and separately -- past where `run_commit_pipeline`
    itself handed off to a best-effort post-sync helper -- `auto_push.drain_pending_push` (a real,
    confirmed one-hop `module.attr(...)` call at `commit_pipeline.py`'s own local `from
    coordinator_core.hooks import auto_push`), whose body calls `run_push_with_retry`,
    `_branch_resolves_locally`, and `_drain_dead_ref_record`, which in turn reach `push_once`,
    `_run_git`, and `_is_ancestor` (all confirmed by reading each function's own body); plus
    `_handler` ALSO calls `session_scope.release_committed_claims` /
    `release_phantom_claims` directly (both confirmed imported, both confirmed to call git-shaped
    helpers in `session/scope.py` reaching `_git_run`). Every one of these 8 was traced to an
    actual `ast.Call` node, not inferred -- ZERO of them are collision artifacts of the precise
    (per-import-pinned) resolution this gate uses.

PAST-TENSE PROVENANCE, NOT A LIVE CLASSIFICATION (2026-08-29). Every `run_commit_pipeline`
mention above and below is a record of what the C-08 measurement traced when it ran, and none of it
is load-bearing today: `ceremony.scoped_git_commit` was killed under K-045, `ceremony.commit` with
it, and `coordinator_core/ops/ceremony/commit_pipeline.py` was deleted outright at 12b6a009aa (C4 of
docs/plans/2026-08-29-the-push-subsystem-leaves-and-then-the-pipeline-can-go.md). The 8-site trace is
kept because it is the evidence behind this gate's design finding -- that transitive analysis reached
further than the manual one-hop audit -- and that finding outlives the function it was measured on.
No entry in any disposition dict is justified by a `run_commit_pipeline` call chain any more; the
surviving committer is `ceremony.commit_v2` / `git/commit.py :: commit_paths`, and the push
subsystem the trace followed into `auto_push.py` now lives at `ops/ceremony/push.py`.

MEASURED FALSE-POSITIVE RATE: 0 of 18 (every flagged site traced to a real call chain). This IS a
genuine result, not a tuned one, and it is a NARROWER claim than "the technique has no false
positives": it is a measurement of THIS run, over nine entrypoints' traced reachable sets, using
the precise (not repo-wide-by-name) resolution this file ships. The finding worth carrying forward
is not the zero -- it is that transitive analysis found MORE real reachability than the manual
one-hop-per-op audit did, on the op the audit was most confident about (`ceremony.scoped_git_commit`,
called "CLEAN" there). That is the mechanism working as designed, not a defect in the C-08
baseline -- a by-hand DFS that stopped at "no direct site in `scoped_git_commit.py` itself, and
`git_native.py`'s two OTHER sites are unreachable" never followed `run_commit_pipeline`'s own
fan-out into `auto_push.py`. Restated per the plan's own instruction: scope restriction was not
assumed to fix the false-positive problem; it was traced, hop by hop, and on this run every flagged
site held up.

WHAT THIS GATE DELIBERATELY DOES NOT CATCH (negative spec):
  - A callee reached only through an alias chain longer than two hops (module import ->
    module-level local-name-to-attribute binding), a `lambda`, a `getattr`/dynamic dispatch, or a
    relative import at level >= 2. Matches the reused module's own bare-`Name`-only, false-
    negative-biased restriction.
  - A spawn inside a function that is itself reachable, but where the spawn call is gated behind a
    literal condition this gate does not evaluate (e.g. discriminator 5's verb-gated chokepoints in
    the sibling module) -- this gate reports every spawn site whose ENCLOSING function is reached,
    with no attempt to determine whether the specific branch containing the spawn is live on any
    particular call. A verb-gated chokepoint that never spawns for the verbs a budgeted op actually
    uses will still be reported; narrowing that is left to a future pass, matching this module's
    stated false-negative-over-false-positive preference in the other direction (it would rather
    over-report a site than silently drop one whose gating it cannot prove).
  - Leg 3 (EXECUTION) of its own definition of counted. This gate does not run any companion test,
    so it cannot see whether that test's counter reaches a legitimized site; the `executed` field
    records a measurement taken OUT of band and cited, not one re-derived on each run. A fixture
    that stops exercising a legitimized site therefore leaves a stale-but-passing exemption behind,
    which is the same defect class as `green_path: 17` and is NOT closed here. Legs 1 and 2 are
    checked (`test_legitimized_site_mechanism_pins_hold` enforces the mechanism pin; the exact-
    equality requirement is carried by the asserting tests themselves). Closing leg 3 mechanically
    means re-running each companion test under a stack-recording `subprocess.run` wrapper and
    asserting each legitimized site appears -- a `cadence`/`spawns_process` job, deliberately left
    to its own chunk rather than smuggled into this file's fast-tier surface.
  - The `coverage.diagnose_open_review_loop_dag_mode` manifest row. Its subject
    (`coverage._diagnose_open_review_loop`) is unreachable dead code (EM-verified addendum,
    `state/audits/2026-08-19-opro-03-c08-budgeted-op-spawn-trace.md`) with no registered op and no
    live caller -- it is not one of the eight LIVE entrypoints this gate enforces, and removing that
    row/function is a different chunk's (C1/C2/C4) job. `_BUDGETED_ENTRYPOINTS` omits it on
    purpose, not by oversight.

ENTRYPOINT REGISTRY ROT GUARD. `_BUDGETED_ENTRYPOINTS` is a hardcoded (relpath, function-name)
mapping -- explicit by necessity, since a manifest key names a call SHAPE
(`bin.workday_complete_step2_5_dirty_tree.classify_main_pass`), not always an importable symbol.
"Do not hardcode a list of entrypoints that silently rots" (the brief's own words) is answered by
`test_budgeted_entrypoints_resolve_to_live_functions`: every entry must resolve to an actual
top-level function definition in the reused index's `func_defs`, checked fresh on every run, never
trusted from a comment. A row whose entrypoint no longer exists fails LOUDLY, the same defect class
C2 closes for the manifest's own orphan-row gap (a different file, not touched here).

EXEMPTION MODEL. `_LEGITIMIZED_SITES`, keyed on `(relpath, enclosing, argv0, ordinal)` -- the
SAME four-tuple `spawn_policy.site_key` uses for site identity, deliberately excluding `lineno`
(a `file:line` pin rots on the next edit above it, exactly the defect class
`test_no_unbatched_per_item_git_spawn.py`'s own exemption register calls out by name). No pragma
comment escape hatch, matching that module's own stance: a structural exemption a call site could
opt out of by comment would let this class regrow behind a comment, the inverse of what this gate
exists to end.

Each VALUE is a `_Legitimation` naming the three legs above -- `counter`, `counted_by`,
`executed`. That shape is load-bearing, not documentation: because `site_key` carries no mechanism
component, an exemption recorded as a bare key cannot express WHAT it depends on and therefore
cannot be invalidated when that thing changes. Recording the counter shape is what lets
`test_legitimized_site_mechanism_pins_hold` fail the moment a `_GLOBAL_SUBPROCESS_RUN` site stops
spawning via `subprocess.run`.

RE-ENTRANCY. This file's own path (`coordinator_core/tests/test_no_uncounted_spawn_on_budgeted_
path.py`) is excluded from every real scan by the REUSED `is_test_tree_site` (a `test_*` basename,
same rule that excludes every other gate module in this tree) -- inherited, not re-derived; this
module does not repeat `test_no_unbatched_per_item_git_spawn.py`'s own loud self-scan sentinel
because it does not re-implement `_discover_scope_files`, it imports the one copy that already
carries that guard.

ASSERTION-MESSAGE RULE (overengineering-review, 2026-08-30, Finding 2): a cluster-total assertion
message states how to RE-DERIVE THE CURRENT number, and only the current number. It does not carry
a prior chunk's superseded derivation alongside it -- git already holds every earlier value and the
arithmetic that produced it. When a total moves again, replace the derivation; do not append a
second stratum under it.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import typing

import coordinator_core.hooks as _hooks
from coordinator_core.op_census import spawn_bearing_ops
from coordinator_core.spawn_policy.detect import site_key
from coordinator_core.tests.test_no_unbatched_per_item_git_spawn import (
    _GATE_SCOPE_ROOTS,
    _REPO_ROOT,
    _FileRecord,
    _FuncIndex,
    _build_func_index,
    _discover_scope_files,
    _load_file_records,
)

#: The eight LIVE `spawn_count_budget` rows in `coordinator_core/benchmarks/budget-manifest.json`,
#: manifest key -> (relpath, tuple-of-top-level-entrypoint-function-names). Resolved and verified
#: against `state/audits/2026-08-19-opro-03-c08-budgeted-op-spawn-trace.md`'s per-op sections and
#: cross-checked against each row's own companion test (which function it actually exercises, not
#: the manifest key's prose shape) -- see this file's own run-report sidecar for the per-row
#: verification. The ninth manifest row, `coverage.diagnose_open_review_loop_dag_mode`, is
#: deliberately omitted -- see module docstring's negative spec.
#:
#: `ops.discover_working_repos` carries TWO entrypoints (`main()` calls both) -- the audit traced
#: both as independently reachable-with-a-spawn.
#:
#: WIDENED past these nine hand-verified rows by C2a (2026-08-23): 175 further rows (plus 2 more,
#: C7 addendum), each a live registry op MEASURED to have an EMPTY function-granular reachable
#: spawn set -- see the block comment directly above `"app_session.census"` below for the
#: measurement this widening rests on, and `test_registry_divergence_and_residual_stay_accounted`
#: for the completeness guard that keeps it honest against the live registry. The nine rows above
#: are the only ones that were ever hand-verified against the C-08 audit; every row below them was
#: derived mechanically from a zero measurement, not audited op-by-op the way these nine were.
#:
#: C8 RE-DERIVATION (2026-08-23): `_reachable_functions` did not resolve `asyncio.to_thread(fn,
#: ...)` / `loop.run_in_executor(None, fn, ...)` as a call edge, so a function reached only
#: through a thread hop was invisible to this widening's own empty-set measurement -- a
#: false-negative on the ENROLMENT direction, not merely on the reachable-site count for an
#: already-nonempty op (see `_direct_call_targets`'s thread-hop branch and
#: `_UNRESOLVED_THREAD_HOP_CALLEES` for the fix and its own accepted-gap reporting). Re-running
#: the C2a measurement under the widened walker moved 40 of the 177 C2a-family rows OUT of this
#: dict and back to the residual: their function-granular reachable spawn set is no longer empty
#: once a thread-hop callee resolves. None of the 40 carried a `_LEGITIMIZED_SITES` entry (an
#: empty-measured row never needed one), so removing them drops no legitimization. Their spawn
#: sites are all already members of `_FROZEN_UNENROLLED_SPAWN_SITES` at module granularity
#: (verified: 0 newly-undeclared site keys), so `test_unenrolled_spawn_bearing_ops_are_declared_
#: in_the_frozen_inventory` needed no new entries for them. 185 -> 145 enrolled rows.
_BUDGETED_ENTRYPOINTS: dict[str, tuple[str, tuple[str, ...]]] = {
    # Enrolled 2026-08-30: each of these four resolves to a function-granular
    # reachable spawn set that is EMPTY. An op that reaches no spawn site needs
    # no legitimization and no static pin -- it is enrolled directly, per this
    # file's own EM-adjudication step 2, rather than left to the residual where
    # it reads as unaccounted-for. If any of them later grows a spawn site, that
    # is a real regression and this enrolment is exactly what will surface it.
    "delegation.check": (
        "coordinator_core/ops/delegation_check.py",
        ("_delegation_check",),
    ),
    "fleet.archive_sweep_status": (
        "coordinator_core/ops/fleet/sweep_status.py",
        ("_handler",),
    ),
    "fleet.mode_set": (
        "coordinator_core/ops/fleet/mode_control.py",
        ("_fleet_mode_set",),
    ),
    "fleet.mode_show": (
        "coordinator_core/ops/fleet/mode_control.py",
        ("_fleet_mode_show",),
    ),
    "changelog.cited_in_range_count": (
        "coordinator_core/ops/changelog_ops.py",
        ("_cited_in_range_count",),
    ),
    "percolate.functional_identifier_output_drift_in_tree": (
        "coordinator_core/percolate/store.py",
        ("find_functional_identifier_output_drift_in_tree",),
    ),
    "bin.coordinator_harvest_deferrals_dedup_scan_root_resolution": (
        "coordinator/bin/coordinator-harvest-deferrals.py",
        ("_candidate_search_dirs",),
    ),
    "bin.reap_integrated_review_findings.tracked_untracked_split": (
        "coordinator/bin/reap-integrated-review-findings.py",
        ("_reap_integrated_legacy",),
    ),
    "bin.workday_complete_step2_5_dirty_tree.classify_main_pass": (
        "coordinator_core/ops/workday_complete_step2_5_dirty_tree.py",
        ("_classify_main_pass",),
    ),
    "execute_plan_assemble.dispatch_ledger_delivered": (
        "coordinator_core/execute_plan_assemble/close_out_and_stamp.py",
        ("_dispatch_ledger_delivered",),
    ),
    "ops.discover_working_repos": (
        "coordinator_core/ops/discover_working_repos.py",
        ("_tier_a5", "_publish_mirror_keys"),
    ),
    #
    # -- C2a widening (2026-08-23, EM adjudication over the structural BLOCKED on this chunk) --
    #
    # 175 rows below, each a live registry op whose function-granular reachable spawn set
    # (`spawn_bearing_ops.ops_with_spawn_evidence(..., function_granular=True)`) was MEASURED
    # empty -- not assumed, not a constant chosen ahead of the probe. An op that reaches no
    # spawn site has nothing to legitimize: zero `_LEGITIMIZED_SITES` entries, zero invented
    # per-op budget tests, and enrolling it converts "not measured" into "measured, and zero",
    # the ratchet AC5 asks for. Ops whose reachable set is NON-EMPTY stay OUT of this dict --
    # they are `test_registry_divergence_and_residual_stay_accounted`'s residual, C2b's
    # partition to disposition, not enrolled or legitimized here.
    #
    # Re-measured from inside the pytest import order (the 142/104 figures from a prior run hit
    # the `coordinator_core.ops`/`op_census_report` circular-import degradation outside that
    # order and were correctly flagged unverified) -- this run: `live_registry_op_names()` 280,
    # `registry_divergence()` agrees (empty both ways), 280/280 resolve to a top-level function
    # in the reused corpus's `func_defs`, 105 carry non-empty function-granular evidence
    # (residual), 175 measured empty (enrolled below). 175 + 105 == 280.
    #
    # C7 ADDENDUM (2026-08-23): the completeness guard above was itself blind to every `hooks.*`
    # op -- peer commit `117d960ec` made `coordinator_core.hooks` stop registering at package
    # import, so a bare `import coordinator_core.ops` never populated them and
    # `registry_divergence()` read 20 `hooks.*` ops as `only_in_fast_path` (map rot that was not
    # map rot). Fixed by calling `coordinator_core.hooks._eager_import_all()` before the registry
    # read in both `test_registry_divergence_and_residual_stay_accounted` and
    # `test_registry_fast_path_matches_live_registry` -- do not revert that call. Doing so also
    # unmasked two newly-registered ops the residual had not caught up to
    # (`decision_record.mint_id`, `decision_record.release_id`, both measured empty
    # function-granular reachable spawn sets) -- enrolled below per this test's own "mechanical,
    # not a judgment call" rule.
    "app_session.census": (
        "coordinator_core/ops/app_session.py",
        ("_census",),
    ),
    "app_session.teardown": (
        "coordinator_core/ops/app_session.py",
        ("_teardown",),
    ),
    "cartography.count_references": (
        "coordinator_core/ops/cartography_edges.py",
        ("_cartography_count_references",),
    ),
    "cartography.edges": (
        "coordinator_core/ops/cartography_edges.py",
        ("_cartography_edges",),
    ),
    "cartography.op_edges": (
        "coordinator_core/ops/cartography_op_edges.py",
        ("_cartography_op_edges",),
    ),
    "cartography.stack": (
        "coordinator_core/ops/cartography_stack.py",
        ("_cartography_stack",),
    ),
    "cartography.symbols": (
        "coordinator_core/ops/cartography_symbols.py",
        ("_cartography_symbols",),
    ),
    "changelog.append_day": (
        "coordinator_core/ops/changelog_ops.py",
        ("_append_day_handler",),
    ),
    "cli.parse_date_flags": (
        "coordinator_core/ops/parse_cli_args.py",
        ("_handler_parse_date_flags",),
    ),
    "cli.parse_flag": (
        "coordinator_core/ops/parse_cli_args.py",
        ("_handler_parse_flag",),
    ),
    "compute_layer.scaffold": (
        "coordinator_core/ops/compute_layer_scaffold/op.py",
        ("_compute_layer_scaffold",),
    ),
    "cutover.advance": (
        "coordinator_core/ops/cutover_advance.py",
        ("_cutover_advance",),
    ),
    "decision_record.mint_id": (
        "coordinator_core/ops/decision_record_mint.py",
        ("_mint_handler",),
    ),
    "decision_record.release_id": (
        "coordinator_core/ops/decision_record_mint.py",
        ("_release_handler",),
    ),
    "deferral.detect_orphan_memo": (
        "coordinator_core/ops/deferral_detect_orphan_memo.py",
        ("_handler",),
    ),
    "deferral.detect_partial_strangle": (
        "coordinator_core/ops/deferral_detect_partial_strangle.py",
        ("_handler",),
    ),
    "deliverable.fork_detect": (
        "coordinator_core/ops/deliverable_fork_detect.py",
        ("_handler",),
    ),
    "detect.plugin_layout": (
        "coordinator_core/ops/detect_plugin_layout.py",
        ("_handler",),
    ),
    "detect.primary_languages": (
        "coordinator_core/ops/detect_primary_languages.py",
        ("_detect_primary_languages",),
    ),
    "diagnostics.always_refuses": (
        "coordinator_core/ops/diagnostics_probes.py",
        ("_always_refuses",),
    ),
    "diagnostics.always_structural_pin": (
        "coordinator_core/ops/diagnostics_probes.py",
        ("_always_structural_pin",),
    ),
    "diagnostics.always_succeeds": (
        "coordinator_core/ops/diagnostics_probes.py",
        ("_always_succeeds",),
    ),
    "dispatch.emit": (
        "coordinator_core/ops/dispatch_emit/op.py",
        ("_dispatch_emit",),
    ),
    "distill.curate_clusters": (
        "coordinator_core/ops/distill_curate_clusters.py",
        ("_handler",),
    ),
    "distill.stamp_disposal": (
        "coordinator_core/ops/distill_stamp_disposal.py",
        ("_handler",),
    ),
    "distill.workflow_input": (
        "coordinator_core/ops/distill_workflow_input.py",
        ("_handler",),
    ),
    "doctrine.assert_cross_reference_counts": (
        "coordinator_core/ops/assert_doctrine_cross_reference_counts.py",
        ("_handler",),
    ),
    "fanout.poll_scratch_dir": (
        "coordinator_core/ops/poll_scratch_dir.py",
        ("_poll_scratch_dir",),
    ),
    "findings.self_persist_fallback": (
        "coordinator_core/ops/self_persist_findings.py",
        ("_handler",),
    ),
    "fleet.aggregate_capability_index": (
        "coordinator_core/ops/fleet/capability_index.py",
        ("_fleet_aggregate_capability_index",),
    ),
    "fleet.backfill_dispositionless_memos": (
        "coordinator_core/ops/fleet/backfill_memo_disposition.py",
        ("_handler",),
    ),
    "fleet.backfill_reference_edges": (
        "coordinator_core/ops/backfill_reference_edges.py",
        ("_handler",),
    ),
    "fleet.handoffs_for_plan": (
        "coordinator_core/ops/fleet/plan_handoffs.py",
        ("_handoffs_for_plan",),
    ),
    "fleet.work_state": (
        "coordinator_core/ops/fleet/work_state.py",
        ("_fleet_work_state",),
    ),
    "gate.validate_invocable": (
        "coordinator_core/ops/gate_validate_invocable.py",
        ("_gate_validate_invocable",),
    ),
    "gate_liveness.reconcile": (
        "coordinator_core/ops/gate_liveness/reconcile.py",
        ("_handler",),
    ),
    "gate_liveness.resolve": (
        "coordinator_core/ops/gate_liveness/resolve.py",
        ("_handler",),
    ),
    "goal.match_candidates": (
        "coordinator_core/ops/goals_match.py",
        ("_handler",),
    ),
    "goal.set_kr_status": (
        "coordinator_core/ops/goal_kr_status.py",
        ("_goal_set_kr_status",),
    ),
    "handoff.append_session_ledger": (
        "coordinator_core/ops/handoff_append_session_ledger.py",
        ("_handler",),
    ),
    "handoff.author_lint": (
        "coordinator_core/ops/handoff_author_lint.py",
        ("_handler",),
    ),
    "handoff.blocked_by_dependents": (
        "coordinator_core/ops/handoff_children.py",
        ("_handoff_blocked_by_dependents",),
    ),
    "handoff.correct_body": (
        "coordinator_core/ops/handoff_correct_body.py",
        ("_handler",),
    ),
    "handoff.discharge_criteria": (
        "coordinator_core/ops/handoff_discharge_criteria.py",
        ("_handler",),
    ),
    "handoff.match_candidates": (
        "coordinator_core/ops/handoff_match.py",
        ("_handler",),
    ),
    "handoff.normalize": (
        "coordinator_core/ops/handoff_normalize.py",
        ("_handler",),
    ),
    "handoff.stamp": (
        "coordinator_core/ops/handoff_stamp.py",
        ("_handler",),
    ),
    "handoff.stamp_phase": (
        "coordinator_core/ops/handoff_phase_stamp.py",
        ("_handler",),
    ),
    "hooks.agent_completion_log": (
        "coordinator_core/hooks/agent_completion_log.py",
        ("_handler",),
    ),
    "hooks.nudge_em_code_dispatch": (
        "coordinator_core/hooks/nudge_em_code_dispatch.py",
        ("_handler",),
    ),
    "hooks.nudge_foreground_agent_dispatch": (
        "coordinator_core/hooks/nudge_foreground_agent_dispatch.py",
        ("_handler",),
    ),
    "hooks.nudge_named_agent_report_delivery": (
        "coordinator_core/hooks/nudge_named_agent_report_delivery.py",
        ("_handler",),
    ),
    "hooks.nudge_unauthorized_handoff": (
        "coordinator_core/hooks/nudge_unauthorized_handoff.py",
        ("_handler",),
    ),
    "hooks.postuse_advisory_dispatch": (
        "coordinator_core/hooks/postuse_advisory_dispatch.py",
        ("_handler",),
    ),
    "hooks.receiver_state_sensor": (
        "coordinator_core/hooks/receiver_state_sensor.py",
        ("_handler",),
    ),
    "hooks.subagent_arrival_check": (
        "coordinator_core/hooks/subagent_arrival_check.py",
        ("_handler",),
    ),
    "hooks.subagent_sidecar_fill_check": (
        "coordinator_core/hooks/subagent_sidecar_fill_check.py",
        ("_handler",),
    ),
    "hooks.subagent_zero_tool_use": (
        "coordinator_core/hooks/subagent_zero_tool_use.py",
        ("_handler",),
    ),
    "hooks.subagent_zero_tool_use_resolve": (
        "coordinator_core/hooks/subagent_zero_tool_use_resolve.py",
        ("_handler",),
    ),
    "hooks.subagent_zero_tool_use_surface": (
        "coordinator_core/hooks/subagent_zero_tool_use_surface.py",
        ("_handler",),
    ),
    "hooks.track_dispatched_agents": (
        "coordinator_core/hooks/track_dispatched_agents.py",
        ("_handler",),
    ),
    "initiative.serve_set": (
        "coordinator_core/ops/initiatives_serve.py",
        ("_handler",),
    ),
    "install.detect_cmd_autorun_coverage": (
        "coordinator_core/ops/cmd_autorun_guard.py",
        ("_detect_handler",),
    ),
    "install.strip_cmd_autorun_guard": (
        "coordinator_core/ops/cmd_autorun_guard.py",
        ("_strip_handler",),
    ),
    "install.wrapper_onto_path": (
        "coordinator_core/install/wrapper_onto_path.py",
        ("_install_wrapper_onto_path",),
    ),
    "install.write_cmd_autorun_guard": (
        "coordinator_core/ops/cmd_autorun_guard.py",
        ("_write_handler",),
    ),
    "install.write_identity_file": (
        "coordinator_core/ops/write_identity_file.py",
        ("_handler",),
    ),
    "install.write_shell_rc_guard_block": (
        "coordinator_core/install/shell_rc_guard.py",
        ("_handler",),
    ),
    "lessons.filter_undated_universal": (
        "coordinator_core/ops/lessons_filter.py",
        ("_lessons_filter_undated_universal",),
    ),
    "lessons.reject_orphan_strip_entries": (
        "coordinator_core/ops/lessons_filter.py",
        ("_lessons_reject_orphan_strip_entries",),
    ),
    "mcp.resolve_server_cli_path": (
        "coordinator_core/ops/resolve_mcp_server_cli_path.py",
        ("_handler",),
    ),
    "memo.blitz_buckets": (
        "coordinator_core/ops/fleet/memo_blitz_buckets.py",
        ("_memo_blitz_buckets",),
    ),
    "memo.check_addressee": (
        "coordinator_core/ops/fleet/memo_check_addressee.py",
        ("_memo_check_addressee",),
    ),
    "memo.compose": (
        "coordinator_core/ops/fleet/memo_compose.py",
        ("_memo_compose",),
    ),
    "memo.draft": (
        "coordinator_core/ops/fleet/memo_draft.py",
        ("_memo_draft",),
    ),
    "memo.fate_partition": (
        "coordinator_core/ops/memo_fate_partition.py",
        ("_handler",),
    ),
    "memo.list": (
        "coordinator_core/ops/fleet/memo_list.py",
        ("_memo_list",),
    ),
    "memo.list_outbox": (
        "coordinator_core/ops/fleet/memo_list_outbox.py",
        ("_memo_list_outbox",),
    ),
    "memo.triage": (
        "coordinator_core/ops/memo_triage.py",
        ("_handler",),
    ),
    "op_census.breaches": (
        "coordinator_core/ops/op_budget_breaches.py",
        ("_op_budget_breaches",),
    ),
    "peer_notice.check": (
        "coordinator_core/ops/peer_notice_check.py",
        ("_peer_notice_check",),
    ),
    "peer_notice.send": (
        "coordinator_core/ops/peer_notice_send.py",
        ("_peer_notice_send",),
    ),
    "percolate.list_files_newer_than_marker": (
        "coordinator_core/ops/list_files_newer_than_marker.py",
        ("_list_files_newer_than_marker",),
    ),
    "percolate.run": (
        "coordinator_core/ops/percolate_run.py",
        ("_percolate_run",),
    ),
    "percolate.scan_content_leakage_tiers": (
        "coordinator_core/ops/scan_content_leakage.py",
        ("_scan_content_leakage_tiers",),
    ),
    "percolate.validate_store": (
        "coordinator_core/ops/percolate_validate.py",
        ("_percolate_validate_store",),
    ),
    "ping": (
        "coordinator_core/ops/ping.py",
        ("_ping",),
    ),
    "plan.append_session": (
        "coordinator_core/ops/completion_ops.py",
        ("_append_session_handler",),
    ),
    "plan.list_orphaned": (
        "coordinator_core/ops/draft_plan_aging.py",
        ("_plan_list_orphaned",),
    ),
    "plan.match_candidates": (
        "coordinator_core/ops/plan_match.py",
        ("_handler",),
    ),
    "plan.tasks.grouping_digest": (
        "coordinator_core/ops/plan_tasks_grouping_digest.py",
        ("_handler",),
    ),
    "plan.tasks.mutate": (
        "coordinator_core/ops/plan_tasks_mutate.py",
        ("_handler",),
    ),
    "plan.tasks.spine_drift_check": (
        "coordinator_core/ops/plan_tasks_spine_drift_check.py",
        ("_handler",),
    ),
    "plugin_health.scan": (
        "coordinator_core/plugin_health/scan.py",
        ("_plugin_health_scan",),
    ),
    "queue.cluster": (
        "coordinator_core/ops/queue_cluster.py",
        ("_handler",),
    ),
    "records.query": (
        "coordinator_core/ops/records_query.py",
        ("_handler",),
    ),
    "research.verify_scout_inventory_completeness": (
        "coordinator_core/ops/verify_scout_inventory_completeness.py",
        ("_handler",),
    ),
    "review.mint_workflow": (
        "coordinator_core/ops/review_mint/op.py",
        ("_review_mint_workflow",),
    ),
    "roadmap.link_stubs": (
        "coordinator_core/ops/roadmap_link_stubs.py",
        ("_handler",),
    ),
    "schema.describe": (
        "coordinator_core/frontmatter/schema_cli.py",
        ("_op_schema_describe",),
    ),
    "schema.validate": (
        "coordinator_core/frontmatter/schema_cli.py",
        ("_op_schema_validate",),
    ),
    "session.artifact_owner": (
        "coordinator_core/ops/session_artifact_owner.py",
        ("_session_artifact_owner",),
    ),
    "session.peer_roster": (
        "coordinator_core/ops/session_peer_roster.py",
        ("_session_peer_roster",),
    ),
    "session.reap": (
        "coordinator_core/ops/session/reap.py",
        ("_handler",),
    ),
    "session.record_pickup": (
        "coordinator_core/ops/session/record_pickup.py",
        ("_handler",),
    ),
    "session.resolve_address": (
        "coordinator_core/ops/session_resolve_address.py",
        ("_session_resolve_address",),
    ),
    "session.rotate_orphan_sweep_log": (
        "coordinator_core/ops/session/rotate_orphan_sweep_log.py",
        ("_handler",),
    ),
    "session.scope_report": (
        "coordinator_core/ops/session/scope_report.py",
        ("_handler",),
    ),
    "session.work_state": (
        "coordinator_core/ops/session_work_state.py",
        ("_session_work_state",),
    ),
    "session_baton.mint": (
        "coordinator_core/ops/session_baton_mint.py",
        ("_handler",),
    ),
    "session_hierarchy.derive": (
        "coordinator_core/ops/session_hierarchy_derive.py",
        ("_handler",),
    ),
    "sizing.decline": (
        "coordinator_core/ops/sizing_decline.py",
        ("_handler",),
    ),
    "sizing.read_object_fields": (
        "coordinator_core/ops/read_sizing_object_fields.py",
        ("_handler",),
    ),
    "sizing.record_spike_verdict": (
        "coordinator_core/ops/sizing_spike_verdict.py",
        ("_handler",),
    ),
    "sizing.ship": (
        "coordinator_core/ops/sizing_ship.py",
        ("_handler",),
    ),
    "spec_backlink.resolve": (
        "coordinator_core/ops/spec_backlink_resolve.py",
        ("_resolve_handler",),
    ),
    "spec_backlink.rewrite": (
        "coordinator_core/ops/spec_backlink_resolve.py",
        ("_rewrite_handler",),
    ),
    "strategic.emit": (
        "coordinator_core/ops/strategic_emit.py",
        ("_strategic_emit",),
    ),
    "tracker.advance_status": (
        "coordinator_core/ops/tracker/advance_status.py",
        ("_handler",),
    ),
    "tracker.assert_code_complete": (
        "coordinator_core/ops/tracker/completion_policy.py",
        ("_handler",),
    ),
    "tracker.assign": (
        "coordinator_core/ops/tracker/assign.py",
        ("_handler",),
    ),
    "tracker.fold_observed_set": (
        "coordinator_core/ops/tracker/fold_observed_set.py",
        ("_handler",),
    ),
    "tracker.fold_ownership": (
        "coordinator_core/ops/tracker/fold_ownership.py",
        ("_handler",),
    ),
    "tracker.render_status": (
        "coordinator_core/ops/tracker/render_status.py",
        ("_handler",),
    ),
    "update_docs.probe_fresh_repo_noop": (
        "coordinator_core/ops/probe_fresh_repo_noop.py",
        ("_probe_fresh_repo_noop",),
    ),
    "updatedocs.gates": (
        "coordinator_core/ops/updatedocs_gates.py",
        ("_updatedocs_gates",),
    ),
    "workday.stitch_sidecar_into_summary": (
        "coordinator_core/ops/workday_stitch_sidecar_summary.py",
        ("_handler",),
    ),
    "workday.surface_auto_push_failure_stats": (
        "coordinator_core/ops/workday_surface_auto_push_failure_stats.py",
        ("_surface_auto_push_failure_stats",),
    ),
    "workflow.scaffold": (
        "coordinator_core/ops/workflow_scaffold.py",
        ("_workflow_scaffold",),
    ),
    "workflow.validate": (
        "coordinator_core/ops/workflow_validate.py",
        ("_workflow_validate",),
    ),
    #
    # -- 2026-08-25 widening: 3 more live registry ops MEASURED to have an EMPTY
    # function-granular reachable spawn set (`test_registry_divergence_and_residual_stay_
    # accounted`'s completeness guard), same EM adjudication step 2 as the C2a widening
    # above -- an op reaching no spawn site needs zero legitimization. `handoff.
    # ship_and_archive` and `hooks.session_heartbeat` were previously carried as
    # `_STATIC_SPAWN_COUNT_PINS` residual rows and moved here once their reachable set
    # went empty (their pins are removed in the same change); `memo.reconcile_outbox` is
    # new to both routes.
    #
    "handoff.ship_and_archive": (
        "coordinator_core/ops/handoff_ship_archive.py",
        ("_handler",),
    ),
    "hooks.session_heartbeat": (
        "coordinator_core/hooks/session_heartbeat.py",
        ("_handler",),
    ),
    "memo.reconcile_outbox": (
        "coordinator_core/ops/fleet/memo_reconcile_outbox.py",
        ("_memo_reconcile_outbox",),
    ),
    #
    # D6/D11 (2026-08-22-the-composition-gate-counts-processes-across-the-op-graph, this
    # chunk): the registry divergence check flagged four live ops with an EMPTY
    # function-granular reachable spawn set. D11 adjudicated each by hand (not just by the
    # mechanical empty measurement, EM adjudication step 2, this file's own dispatch brief).
    # Three are genuinely spawn-free and enrolled below:
    #   - `hooks.agent_postuse_dispatch`: `_handler` runs its two legs
    #     (`agent_completion_log.run`, `track_dispatched_agents.run`) through `asyncio.gather`
    #     -- an indirect call the walker cannot trace as an edge -- but both leg modules were
    #     read directly and carry no `subprocess`/`Popen` call anywhere in either file, and
    #     their shared `git_common_dir` call (`coordinator_core/lifecycle.py`) is documented
    #     and verified WALK-ONLY (no spawn fallback). Zero spawns on the real reachable set,
    #     not merely on what the walker can see.
    #   - `percolate.build_token_index`: `_percolate_build_token_index` hands off to
    #     `build_token_index_slice` (`asyncio.to_thread`) -> `_derive_slices`/
    #     `_count_total_files` and `coordinator_core.percolate.token_index` -- read directly,
    #     no `subprocess`/`Popen` anywhere in either module.
    #   - `session.audit_unreapable`: `_handler_audit_unreapable` reaches `check_repo_root`
    #     (`coordinator_core/ops/fleet/_common.py`) -> `git_common_dir` (walk-only, verified
    #     above) and `_collect_unreapable` (`coordinator_core/ops/session/reap.py`, an
    #     `iterdir`/`stat` loop only) -- neither its own module's REAPER neighbours nor any
    #     other hub-mutating function is on this handler's own call path. The module's own
    #     docstring for this handler calling `git_common_dir` "a subprocess" is stale prose
    #     from before that seam's spawn fallback was retired (`lifecycle.py`'s own docstring);
    #     not corrected here since it is out of this chunk's `writes:` scope, but the emptiness
    #     claim below rests on the CURRENT `git_common_dir` behaviour, hand-verified, not on
    #     that stale sentence.
    #
    # The fourth, `merge_assemble.brief`, is NOT enrolled -- it has real, non-empty evidence
    # (the closed resolver-gap paragraph below) and is a pinned, un-legitimized residual instead,
    # next to `merge_assemble.apply`'s own disposition.
    "hooks.agent_postuse_dispatch": (
        "coordinator_core/hooks/agent_postuse_dispatch.py",
        ("_handler",),
    ),
    "percolate.build_token_index": (
        "coordinator_core/ops/percolate_build_token_index.py",
        ("_percolate_build_token_index",),
    ),
    "session.audit_unreapable": (
        "coordinator_core/ops/session/reap.py",
        ("_handler_audit_unreapable",),
    ),
    #
    # `merge_assemble.apply` measured an empty function-granular reachable spawn set before
    # D8 (this plan): `_merge_assemble_apply` calls `merge_assemble.apply.apply()`, which
    # dispatches through its own closed `_CLI_DISPATCH` table (`coordinator_core/
    # merge_assemble/apply.py`) -- a dict of function VALUES passed BY REFERENCE into
    # `apply_base.execute_directives`. D8 built the resolver edge for exactly this shape (a
    # dict-of-callables handed to another module's function), and `merge_assemble.apply` now
    # measures a real, NON-EMPTY reachable spawn set (`_run_py_script`/
    # `_dispatch_node_ceremony_gate`, both `subprocess.run`-backed). It stays OUT of
    # `_BUDGETED_ENTRYPOINTS` -- a live, MUTATING, subprocess-reaching op is C2b's partition to
    # disposition (legitimize or leave as an accounted residual), not a mechanical empty-set
    # enrolment -- and is picked up as an accounted residual by
    # `test_registry_divergence_and_residual_stay_accounted`, which no longer flags it since
    # its evidence is non-empty.
    #
    # `merge_assemble.brief` MEASURED EMPTY here through D11 (2026-08-27, HEAD), which
    # hand-traced it to a genuine RESOLVER GAP, not spawn-freedom: `_merge_assemble_brief` calls
    # `merge_assemble.brief()` (`coordinator_core/merge_assemble/__init__.py`), which calls
    # `compute_branch_state`/`compute_version_bump_proposal` (same file), each of which calls
    # `_run_git` (same file) -> `subprocess.run` directly.
    #
    # CAUSE CORRECTED BY THE EM, 2026-08-27, same session that landed D11. D11's own note
    # called this "a same-file, 3-hop, plain direct-call chain the walker's current call-depth
    # does not follow". That was NOT the gap, and the distinction decided what closed it.
    # `_reachable_functions` is a transitive BFS with no depth limit and `_direct_call_targets`
    # resolves same-file direct calls, so hops 2-4 (`brief` -> `compute_branch_state` ->
    # `_run_git` -> `subprocess.run`, all inside `__init__.py`) were followed fine. The chain
    # died at HOP 1, and it was CROSS-module: `ops.py` reaches the handler's callee via
    # `from coordinator_core.merge_assemble import brief as _brief`, a `from <PACKAGE> import
    # <fn>` whose function is defined in that package's `__init__.py`. `_import_function_aliases`
    # pins the alias to the module NAMED in the import statement, and
    # `coordinator_core.merge_assemble` resolved to a package directory rather than to
    # `coordinator_core/merge_assemble/__init__.py`, so the alias never landed in `func_defs` and
    # the seed set was empty from the first hop. This was a class of gap, not one op: every
    # `from pkg import fn` where `fn` lives in `pkg/__init__.py` measured empty the same way.
    # Distinct from `merge_assemble.apply`'s by-reference dispatch table, so D8's fix did not
    # cover it.
    #
    # CLOSED 2026-08-27 (`state/audits/2026-08-27-package-init-resolver-gap-population.md`
    # measured the population before this fix landed): `_module_index` now also registers each
    # package's bare dotted name (`pkg/__init__.py` -> `"pkg"`, additive-only, never overwriting
    # a real module's own key) alongside its `"pkg.__init__"` entry, so `from pkg import fn`
    # resolves into `pkg/__init__.py`'s `func_defs` like any other cross-module import. The
    # census measured 62 static aliases (61 distinct triples) across 12 packages relying on this
    # missing key, and re-ran the op-level walk over all 279 live ops with function-granular
    # entrypoints: exactly 2 ops moved -- `merge_assemble.brief` (0 -> non-empty, the case this
    # comment names) and `merge_assemble.apply` (already non-empty via D8's edge, gained the same
    # newly-reached `_run_git` site through a second, independent route). No
    # `_BUDGETED_ENTRYPOINTS` row was affected by either. `merge_assemble.brief` now measures a
    # real, non-empty reachable spawn set (the same `_run_git` site) and is no longer a resolver
    # gap: it is a pinned, un-legitimized residual (`_STATIC_SPAWN_COUNT_PINS`) like
    # `merge_assemble.apply` above, not a mechanical enrolment (an empty spawn tuple would
    # certify a live, git-spawning op as spawn-free, which was never true) and no longer a member
    # of `_KNOWN_RESOLVER_GAP_OPS` (retired to an empty frozenset just below).
}

#: Live ops whose function-granular reachable-spawn measurement is a known RESOLVER GAP, not
#: genuine spawn-freedom -- each hand-traced (per-op comment above `_BUDGETED_ENTRYPOINTS`'s
#: closing brace) to a REAL spawn site the walker's current resolution does not follow.
#: Excluded from `test_registry_divergence_and_residual_stay_accounted`'s emptiness-implies-
#: enrol check so that check does not assert a false "should have been enrolled" for an op
#: this file has already hand-verified as non-empty. NOT enrolled in `_BUDGETED_ENTRYPOINTS`
#: (that would certify the opposite falsehood): stays a reported, un-legitimized residual.
#: EMPTIED 2026-08-27: `merge_assemble.brief` was the sole member, contained here solely
#: because `_module_index` had no bare-package-name key for `pkg/__init__.py` (see
#: `_module_index`'s own docstring and the `_BUDGETED_ENTRYPOINTS` prose block above). Now that
#: the resolver carries that key, `merge_assemble.brief`'s function-granular reachable spawn set
#: measures genuinely non-empty (one `_run_git` site) and it is a live, unenrolled residual with
#: real evidence like any other -- no longer a gap case, so it needs no entry here. Left as a
#: `frozenset()` rather than deleted so a future gap of this same shape has a named place to go
#: rather than reintroducing the mechanism from scratch.
_KNOWN_RESOLVER_GAP_OPS: frozenset[str] = frozenset()

#: C4 (pln-reconcile-open-comes-back-under-the-bar, 2026-08-26) MEASUREMENT NOTE:
#: `handoff.reconcile_open` was rebuilt from first principles after DR-344's kill bar deleted
#: the prior implementation (C2b), which had seven git-spawn sites. The rebuild's own
#: orchestration (`_handler` itself, `gate_eval`, `handoff_corpus`, `policy_loader`) is
#: COMPUTE_ONLY plus one JSON file write -- but `_handler` also imports
#: `handoff_transition._read_gate_evidence_resolved` (R12, DR-320's prose-vs-evidence guard),
#: whose I/O-kind `gate_evidence` leg resolution (`sibling_fact.resolve_leg`) reaches
#: `coordinator_core/git/run.py:426 run_git` -- MEASURED here via
#: `test_no_uncounted_spawn_reachable_from_a_budgeted_entrypoint`: enrolling this op above
#: with an empty-set claim failed that test with exactly one uncounted site,
#: `coordinator_core/git/run.py:426 enclosing='run_git' argv0='git'`. That claim was wrong
#: and is corrected here rather than left green on a false premise: this is a genuinely
#: NON-EMPTY reachable spawn set, so `handoff.reconcile_open` stays OUT of
#: `_BUDGETED_ENTRYPOINTS` (the "measured empty, zero legitimization needed" bucket above)
#: and is left as an accounted RESIDUAL -- the same disposition this file's own comments give
#: `merge_assemble.apply` immediately above: a live op with a non-empty reachable spawn set,
#: not enrolled, not legitimized, reported rather than silenced. A per-site
#: `_LEGITIMIZED_SITES` entry (mechanism pin + a companion fixture proving the git call is
#: actually exercised/bounded) is future work for whoever picks up this op's own spawn
#: budget -- not built in this chunk.

#: The two counter shapes a legitimation may rest on. Each names what it guarantees AND the hole
#: it leaves; neither dominates the other, which is why the exemption model records which one an
#: entry depends on instead of privileging one (module docstring, "THIS GATE'S OPERATIONAL
#: DEFINITION").
#:
#: `SEAM` -- the companion test substitutes a FUNCTION OBJECT (`git_native._git = _wrapper`).
#: Mechanism-agnostic: whatever the seam's body spawns with, the call routes through the
#: substituted name first, so a `subprocess.run` -> `Popen` refactor inside it stays counted.
#: Routing-narrow: a caller that reaches the process boundary WITHOUT going through the seam is
#: invisible. That hole is not hypothetical -- it is precisely this gate's seven open
#: `ceremony.scoped_git_commit` bypasses.
#:
#: `GLOBAL_SUBPROCESS_RUN` -- the companion test substitutes the module attribute
#: (`subprocess.run = _wrapper`). The mirror image: routing-agnostic (a new spawn ANYWHERE in the
#: op's reached set is counted, however it was reached), mechanism-narrow (a `Popen`,
#: `os.posix_spawn`, or `asyncio.create_subprocess_exec` call is invisible). The mechanism hole is
#: what `_MECHANISM_PIN` closes below; the routing strength is what makes this shape admissible at
#: all, and the reason the pre-2026-08-19 text's blanket refusal of it was wrong.
#:
#: `GLOBAL_SUBPROCESS_SPAWN` (opro-03 follow-up, 2026-08-21) -- the same routing-agnostic shape,
#: WIDENED to also substitute the `subprocess.Popen` module attribute: `auto_push._detach_and_run`'s
#: Windows respawn leg spawns via `Popen`, never `run`, so the run-only counter could never see it
#: regardless of reachability. A NEW counter constant rather than widening `_GLOBAL_SUBPROCESS_RUN`
#: in place, so sites already legitimized under the narrower run-only counter keep their narrower
#: guarantee -- a future `run` -> `Popen` edit at one of THOSE sites still fails the pin, because
#: their own mechanism-pin entry (`_MECHANISM_PINS` below) never grew.
_SEAM = "seam"
_GLOBAL_SUBPROCESS_RUN = "global-subprocess-run"
_GLOBAL_SUBPROCESS_SPAWN = "global-subprocess-spawn"

#: For a `_GLOBAL_SUBPROCESS_RUN` legitimation, the dotted spawn callee the counter patches. Every
#: spawn call in the legitimized site's enclosing function must be exactly this
#: (`test_legitimized_site_mechanism_pins_hold`) -- that assertion is what converts "a global patch
#: happens to see this call" into an ENFORCED precondition, and it is the whole reason this gate
#: can now admit the shape it previously refused. `spawn_policy.site_key` is
#: `(path, enclosing, argv0, ordinal)` and carries no mechanism component, so a
#: `subprocess.run` -> `subprocess.Popen` edit at a legitimized site keeps the identical key: the
#: exemption would silently outlive the counter that justified it. The pin is checked against the
#: enclosing function's FULL callee set rather than one ordinal-matched call, so both an in-place
#: mechanism swap and an added second spawn of a different mechanism fail it.
_MECHANISM_PIN = "subprocess.run"

#: Per-counter mechanism pin, keyed on the counter constant a `_Legitimation.counter` names.
#: `_GLOBAL_SUBPROCESS_RUN` stays pinned to `subprocess.run` ALONE -- unwidened, per the note
#: above. `_GLOBAL_SUBPROCESS_SPAWN` admits both `subprocess.run` and `subprocess.Popen` because
#: that is what its own companion counter actually watches (`test_commit_e2e_spawn_budget.py`'s
#: `_count_op_spawns_both_ways`, widened 2026-08-21) -- not a loosening, an honest description of
#: a counter that watches two mechanisms. `test_legitimized_site_mechanism_pins_hold` checks
#: SUBSET membership against the mapped pin, not equality against a single string, which is a
#: pure generalization of the prior exact-match check for every singleton pin already in this
#: dict (`found <= pin and found` is `found == pin` whenever `pin` has exactly one member).
_MECHANISM_PINS: dict[str, frozenset[str]] = {
    _GLOBAL_SUBPROCESS_RUN: frozenset({_MECHANISM_PIN}),
    _GLOBAL_SUBPROCESS_SPAWN: frozenset({"subprocess.run", "subprocess.Popen"}),
}


class _Legitimation(typing.NamedTuple):
    """Why one site is counted, in the three parts a legitimation must prove. `counter` is the
    mechanism (above). `counted_by` is the asserting test -- required to make an exact-equality
    assertion against a manifest figure, never a bound or a range. `executed` records that the
    asserting test's counter was MEASURED to actually run this site, which is the leg neither the
    mechanism nor the assertion shape establishes and the one this gate cannot check statically
    (see "WHAT THIS GATE DELIBERATELY DOES NOT CATCH")."""

    counter: str
    counted_by: str
    executed: str


#: Sites PROVEN legitimately counted -- see module docstring's "EXEMPTION MODEL". C6's worklist
#: (opro-03, `docs/plans/2026-08-19-every-budgeted-op-counts-its-own-spawns.md`) drains this
#: mapping one entry at a time. An entry earns its place by satisfying all three legs of
#: `_Legitimation`, NOT by a companion test's incidental visibility: the distinction between the
#: two is the `executed` leg, and it is a measurement, never an inference from static
#: reachability. Nine of the sites on this gate's live-tree list were statically reachable AND
#: under an exact-equality global counter; six of them were measured to actually execute under
#: that counter and are legitimized here, and three were measured NOT to (see
#: `_UNCOUNTED_MEASURED_UNREACHED`) and stay red. `green_path: 17` is the same defect in the other
#: direction -- reachable, budgeted, and never run by its own fixture.
#: Keyed on `(op_key, *site_key)`, NOT on `site_key` alone. Counting is a property of an (op,
#: site) PAIR, because the counter belongs to an op: one physical call site can sit on two
#: budgeted ops' reachable sets and be genuinely counted under one while invisible under the
#: other. `close_out_and_stamp.py::_run_git` was exactly that while `sibling_committed_chunk_ids_
#: memo` still existed (2026-08-19 through its 2026-08-21 C3 deletion): measured executing under
#: `dispatch_ledger_delivered`'s exact-equality counter, and never reached under that op's own
#: memo-hit fixture. A site-keyed register cannot express that kind of per-op difference and must
#: resolve it in one direction or the other: refusing both leaves a
#: correctly-counted site permanently undischargeable, and admitting both silently exempts a site
#: nothing counts. Op-keying is what lets each pair carry its own verdict.
_LEGITIMIZED_SITES: dict[tuple[str, str, str, str, int], _Legitimation] = {
    (
        "bin.reap_integrated_review_findings.tracked_untracked_split",
        "coordinator/bin/reap-integrated-review-findings.py",
        "_git",
        "git",
        0,
    ): _Legitimation(
        # `bin.reap_integrated_review_findings.tracked_untracked_split`'s sole spawn seam. The
        # companion substitutes the module's own FUNCTION OBJECT (`mod._git = _counting_git`).
        # `spawn_policy.sites_in_source` finds exactly one spawn site in this whole file (`_git`),
        # and every git operation the module makes (`ls-files`, `rm`, `commit`) routes through it,
        # so wrapping the name wraps the site. Budget shape: that key's `per_reap_call`.
        counter=_SEAM,
        counted_by="coordinator/tests/test_reap_integrated_review_findings_spawn_budget.py",
        executed="Seam substitution; the file's only spawn site, exercised by `per_reap_call=1`.",
    ),
    (
        "changelog.cited_in_range_count",
        "coordinator_core/ops/changelog_ops.py",
        "_batch_resolve_commits",
        "git",
        0,
    ): _Legitimation(
        counter=_GLOBAL_SUBPROCESS_RUN,
        counted_by="coordinator_core/ops/test_changelog_cited_in_range_spawn_bound.py",
        executed="Measured 2026-08-19: origin-recorded at changelog_ops.py:1984 under the "
        "counter of `test_cited_in_range_count_spawns_once_regardless_of_token_count` "
        "(`assert n == 1`).",
    ),
    (
        "percolate.functional_identifier_output_drift_in_tree",
        "coordinator_core/percolate/store.py",
        "_git_commit_epoch_times_batch",
        "<dynamic>",
        0,
    ): _Legitimation(
        counter=_GLOBAL_SUBPROCESS_RUN,
        counted_by="coordinator_core/percolate/tests/"
        "test_functional_identifier_output_drift_spawn_budget.py",
        executed="Measured 2026-08-19: origin-recorded at store.py:2021 under the counter of "
        "`test_dest_publish_time_batches_to_one_spawn_regardless_of_file_count` "
        "(`assert n == budgeted`, budgeted read from the manifest).",
    ),
    (
        "bin.workday_complete_step2_5_dirty_tree.classify_main_pass",
        "coordinator_core/ops/workday_complete_step2_5_dirty_tree.py",
        "_run_git",
        "git",
        0,
    ): _Legitimation(
        counter=_GLOBAL_SUBPROCESS_RUN,
        counted_by="coordinator_core/ops/test_workday_complete_step2_5_dirty_tree_spawn_budget.py",
        executed="Measured 2026-08-19: origin-recorded at "
        "workday_complete_step2_5_dirty_tree.py:267 under the counter of "
        "`test_classify_main_pass_spawns_exactly_two_git_calls_for_several_dirty_paths` "
        "(`assert calls[\"n\"] == budgeted`).",
    ),
    (
        "execute_plan_assemble.dispatch_ledger_delivered",
        "coordinator_core/git/run.py",
        "run_git",
        "git",
        0,
    ): _Legitimation(
        # Until 2026-08-21 C3, the SAME physical site was deliberately NOT legitimized under
        # `execute_plan_assemble.sibling_committed_chunk_ids_memo` -- that op's fixture never
        # reached it. That op (and its whole call chain) was deleted in C3; this is now the only
        # op that reaches this site. Relpath updated 2026-08-23 (C10, pre-existing drift fixed
        # in-scope): a peer commit moved `_run_git`'s own definition from
        # `close_out_and_stamp.py` into `row_spans.py` (`close_out_and_stamp.py` now imports it),
        # which changed `site_key`'s `path` component and left this entry stale -- the same
        # function, same behaviour, same companion test, just relocated.
        #
        # Relocated a second time 2026-08-25 (G7, shared-git-runner migration): `row_spans.py::
        # _run_git` and `close_out_and_stamp.py::_batch_git_cat_file_check` both stopped calling
        # `subprocess.run` directly and now delegate to `coordinator_core.git.run.run_git`, which
        # is where the two-entry collapse below the module docstring's own "site relocates, stays
        # legitimized" precedent applies a second time: the two former call-path-distinct sites
        # are now literally the same physical `subprocess.run` call inside `run_git`'s own body,
        # so the pair of entries this dict used to carry collapses to this one. The companion
        # test's patch target moved with it (`test_dispatch_ledger_delivered_spawn_budget.py ::
        # _count_git_calls` now patches the real `subprocess` module's `run` attribute directly,
        # not `close_out_and_stamp.subprocess.run`, which no longer exists once that module
        # dropped its own `import subprocess`) -- still the same global module attribute either
        # way, so leg 1 (mechanism) still holds and legs 2/3 (assertion, execution) are unchanged.
        counter=_GLOBAL_SUBPROCESS_RUN,
        counted_by="coordinator_core/execute_plan_assemble/tests/"
        "test_dispatch_ledger_delivered_spawn_budget.py",
        executed="Measured 2026-08-19: origin-recorded at close_out_and_stamp.py:611 (now "
        "git/run.py::run_git, reached from both row_spans.py::_run_git and close_out_and_stamp.py"
        "::_batch_git_cat_file_check) under the counter of "
        "`test_multiple_committed_rows_spawn_exactly_two_git_calls` (`assert spawns == budgeted`).",
    ),
    (
        "ops.discover_working_repos",
        "coordinator_core/ops/discover_working_repos.py",
        "_sort_unique",
        "sort",
        0,
    ): _Legitimation(
        # The `sort -u` shell-out is a SANCTIONED carve-out (`test_no_bash_dependency.py`), kept
        # for byte-parity with the bash oracle. This gate's property is that it be COUNTED, not
        # that it be removed -- see the module docstring's opening paragraph, and the anti-scope
        # note on `_sort_unique` in the opro-03 plan.
        counter=_GLOBAL_SUBPROCESS_RUN,
        counted_by="coordinator_core/ops/test_discover_working_repos_whole_op_spawn_budget.py",
        executed="Measured 2026-08-19: origin-recorded at discover_working_repos.py:138 under all "
        "three whole-op counters, which assert exact equality against `call_count` AND "
        "cross-check the manifest's own `op_total_*` value (3/3/0, nothing stubbed).",
    ),
}

#: Sites this gate keeps RED that a static reading would wrongly clear. EMPTY as of 2026-08-19,
#: and that is the end state, not a reset: every entry it ever held was discharged, and the last
#: four went by DELETING the spawn rather than by counting it. `show_toplevel`'s spawn fallback
#: had no case in which it returned a right answer the walk had not already produced;
#: `git_dir`/`git_common_dir`'s existed solely for the bare repo, which `_looks_like_git_dir` now
#: answers from the filesystem markers git itself uses; and `absolute_git_dir` always spawned on
#: a docstring claim about a sibling function that measurement did not support.
#:
#: Kept as a named, empty mapping rather than removed: it is where the NEXT measured-unreached
#: site goes, and its emptiness is the evidence that "reachable and under a counter" was checked
#: against a measurement in every case rather than assumed. Not consumed by any assertion -- this
#: is the negative half of the drain evidence.
_UNCOUNTED_MEASURED_UNREACHED: dict[tuple[str, str], str] = {}


#: D2 (2026-08-23, this chunk's own dispatch brief -- `state/dispatch-briefs/2026-08-22-
#: the-composition-gate-counts-processes-across-the-op-graph/D2.md`): the OPEN half of the
#: auto_push / detached-spawn / detached-render-commit cluster. C6 (`ceremony.scoped_git_commit`)
#: and C11 (`memo.send`) already legitimized all 5-to-8 of this cluster's sites for THOSE two
#: ops, above in `_LEGITIMIZED_SITES`. Live re-measurement this chunk (`_reachable_functions`
#: seeded at every OTHER live op's own entrypoint, filtered to the three cluster files) originally
#: found 13 further live ops / 59 (op, site) pairs, matching the brief's own EM-measured slice
#: exactly (13 ops x their own site count: 8+5*8+1+1+4 == 59). `queue.close` (5 pairs) and
#: `ceremony.wsc_tail` (8 pairs) were killed and deleted 2026-08-23 (PM ruling, code gone); their
#: rows are removed rather than left pointing at dead entrypoints, and `workday.drain_pending_
#: push` lost its `_invoke_cockpit_publish` site on re-derivation (-1), leaving 10 ops / 41 pairs
#: (enumerated in `_CLUSTER_D2_OPEN_DISPOSITION` below).
#:
#: DISPOSITION, NAMED HONESTLY PER AC19C (not silence, not a fabricated legitimation, not "a
#: sibling op reaches it"): NONE of these 11 ops is added to `_BUDGETED_ENTRYPOINTS`, and NONE of
#: their cluster sites is added to `_LEGITIMIZED_SITES`. A `_Legitimation` requires an EXISTING
#: companion test that asserts the op's OWN spawn count by exact equality (leg 2) AND was
#: measured to actually execute the site (leg 3) -- checked for all 11, by reading each op's own
#: test module (`test_post_commit_tail*.py`, `test_deliverable_cascade*.py`,
#: `test_migrate_handoff_vocabulary.py`, `test_handoff_archive_transition_holder_live.py`,
#: `test_handoff_reconcile_close_terminal_defects.py`, `test_handoff_reconcile_*.py`,
#: `test_handoff_transition_*.py`, `test_invoke_from_argv.py`,
#: `test_memo_transition_*.py`, `test_warm_start*.py`): none of them contains a spawn-count
#: assertion at all. (`workday.drain_pending_push`'s own `test_workday_drain_pending_push.py`
#: is gone -- the op itself is gravestoned 2026-08-30, C2 -- so it no longer belongs in this
#: list either; see the `total_pairs` narrative below for the citation.)
#:
#: Building the missing companion fixture(s) for any of the 11 means writing to test files this
#: chunk's `writes:` scope does NOT include (`coordinator_core/tests/test_no_uncounted_spawn_on_
#: budgeted_path.py` is the only path in scope). Enrolling these 11 ops into
#: `_BUDGETED_ENTRYPOINTS` without that legitimation would turn this currently-green gate red for
#: a completeness gap this chunk cannot itself discharge -- worse than a named, ratcheted,
#: machine-checked gap. They stay OUT of
#: `_BUDGETED_ENTRYPOINTS`.
#:
#: Kept here as a mechanically-verified inventory (`test_cluster_d2_open_disposition_matches_
#: live_measurement` below) so it cannot rot silently in either direction -- a pair vanishing
#: unnoted, or a NEW pair appearing undeclared -- until a future chunk closes each entry by one
#: of the two routes AC19 itself names: a real per-op `_LEGITIMIZED_SITES` entry (once a
#: companion fixture exists), or moving it to a permanent non-reach disposition if further
#: tracing shows a site is not actually live on that op's path.
#:
#: SUPERSEDED IN PART BY D7 (2026-08-23): every op keying this dict now also carries a
#: `_STATIC_SPAWN_COUNT_PINS` entry (AC20) -- a per-op reachable-spawn-COUNT ratchet, not a
#: per-site legitimation. That pin is what makes "the op's own sites are not execution-counted"
#: (this dict's reason for existing) into "and cannot silently grow more of them either." It
#: does not close any (op, site) pair recorded here -- the reasoning below stays live and true.
_CLUSTER_D2_OPEN_DISPOSITION: dict[str, tuple[tuple[str, str, str, int], ...]] = {
    "invoke.from_argv": (
        ("coordinator_core/ops/ceremony/detached_spawn.py", "spawn_detached", "<dynamic>", 0),
    ),
}

#: Entrypoints for the 11 D2-open ops, resolved the same (relpath, func_name) shape
#: `_BUDGETED_ENTRYPOINTS` uses -- NOT merged into that dict (see the disposition text above for
#: why), kept separate so the verifying test below can seed `_reachable_functions` per op without
#: depending on `spawn_bearing_ops.resolve_op_entrypoints`'s own live-registry import-order
#: sensitivity (this file's own registry tests already carry that dependency; this one does not
#: need to).
_CLUSTER_D2_OPEN_ENTRYPOINTS: dict[str, tuple[str, str]] = {
    "invoke.from_argv": ("coordinator_core/ops/invoke_from_argv.py", "_invoke_from_argv"),
}

#: The three cluster files this D2 disposition is scoped to -- matches the chunk's own `writes:`
#: subject files (`coordinator_core/hooks/auto_push.py`,
#: `coordinator_core/ops/ceremony/detached_spawn.py`,
#: `coordinator_core/ops/ceremony/detached_render_commit.py`).
_CLUSTER_D2_TARGET_FILES = frozenset(
    {
        "coordinator_core/hooks/auto_push.py",
        "coordinator_core/ops/ceremony/detached_spawn.py",
        "coordinator_core/ops/ceremony/detached_render_commit.py",
    }
)


def test_cluster_d2_open_disposition_matches_live_measurement():
    """Ratchet for `_CLUSTER_D2_OPEN_DISPOSITION`: re-derives, from the live tree, exactly which
    cluster sites (the three D2 files above) each of the 11 D2-open ops' own function-granular
    reachable set contains, and asserts it against the frozen disposition -- byte for byte, per
    op. A site the live tree adds or drops without this dict being updated in the SAME change
    fails here, which is what keeps this disposition from silently going stale the way
    `ceremony.wsc_tail`'s own spawn reach did before C3's probe found it (module docstring's own
    cited precedent). Also asserts the total pair count (46) and the entrypoint-resolves-to-a-
    real-function precondition -- mirroring `test_budgeted_entrypoints_resolve_to_live_functions`'s
    own rot guard for `_BUDGETED_ENTRYPOINTS`, applied here to this dict's separate registry."""
    for op_key, (relpath, func_name) in _CLUSTER_D2_OPEN_ENTRYPOINTS.items():
        assert op_key in _CLUSTER_D2_OPEN_DISPOSITION, f"{op_key} has an entrypoint but no disposition entry"
        tree = ast.parse((_REPO_ROOT / relpath).read_text(encoding="utf-8"))
        names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert func_name in names, (
            f"{op_key}'s declared entrypoint {relpath}::{func_name} is not a live top-level "
            "function -- this dict rotted, or the op moved."
        )

    (
        index,
        spawn_sites_by_file,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
    ) = _build_corpus()

    total_pairs = 0
    mismatches: list[str] = []
    for op_key, (relpath, func_name) in _CLUSTER_D2_OPEN_ENTRYPOINTS.items():
        reached = _reachable_functions(
            {(relpath, func_name)},
            index,
            import_aliases_by_file,
            func_aliases_by_file,
            local_aliases_by_file,
        )
        sites = _on_path_spawn_sites(reached, spawn_sites_by_file, exempt=set())
        live_keys = frozenset(
            (site.path, site.enclosing, site.argv0, site.ordinal)
            for site in sites
            if site.path in _CLUSTER_D2_TARGET_FILES
        )
        declared_keys = frozenset(_CLUSTER_D2_OPEN_DISPOSITION[op_key])
        total_pairs += len(declared_keys)
        if live_keys != declared_keys:
            mismatches.append(
                f"  {op_key}: live={sorted(live_keys)} declared={sorted(declared_keys)}"
            )

    assert not mismatches, (
        "_CLUSTER_D2_OPEN_DISPOSITION has drifted from the live tree's own cluster reachability "
        "(re-derive and update the dict, do not silently widen or narrow it):\n" + "\n".join(mismatches)
    )
    assert total_pairs == 1, (
        f"_CLUSTER_D2_OPEN_DISPOSITION now totals {total_pairs} (op, site) pairs, not the "
        "1 left after 2026-08-30-who-pushes-and-when.md C2 gravestoned "
        "workday.drain_pending_push outright (op, module, and test all deleted -- nothing "
        "rides on it), removing its 4 pairs from this dict wholesale, leaving only "
        "invoke.from_argv's single pair. Prior to that, 5 was left after 5ae46cc1b9 (a peer's "
        "own Kira pass) removed the auto_push reach entirely: deliverable.cascade_terminal, "
        "fleet.migrate_handoff_vocabulary, handoff.archive_transition, handoff.transition and "
        "memo.transition each measured live=[] and left this dict outright. Update this "
        "constant deliberately if the shift is real and understood, never to silence a drift "
        "you have not traced."
    )

    for op_key in _CLUSTER_D2_OPEN_DISPOSITION:
        assert op_key not in _BUDGETED_ENTRYPOINTS, (
            f"{op_key} is now enrolled in _BUDGETED_ENTRYPOINTS -- its cluster sites need real "
            "_LEGITIMIZED_SITES entries (route a) instead of living in this open-disposition "
            "registry; remove it from _CLUSTER_D2_OPEN_DISPOSITION/_CLUSTER_D2_OPEN_ENTRYPOINTS "
            "in the same change that enrolls it."
        )


#: D3 (2026-08-23, this chunk's own dispatch brief -- `state/dispatch-briefs/2026-08-22-
#: the-composition-gate-counts-processes-across-the-op-graph/D3.md`): the OPEN half of the
#: session-and-git-core cluster (`session/scope.py`, `session/core.py`,
#: `ops/ceremony/git_native.py`, `git/run.py`, `git/ls_files_bytes.py`, `git/divergence.py`,
#: `git/repo_root.py`, `git_scope.py`, `dag.py`). Two of this cluster's sites are already
#: legitimized above in `_LEGITIMIZED_SITES` for the two ops that ARE enrolled in
#: `_BUDGETED_ENTRYPOINTS` and reach them (`ceremony.scoped_git_commit`: `git_native.py`,
#: `session/scope.py`, `git/run.py`, `git/divergence.py`; `memo.send`: `git_native.py` (both
#: sites), `session/scope.py`, `git/run.py`) -- those pairs are NOT repeated here.
#:
#: Live re-measurement this chunk (`spawn_bearing_ops.resolve_op_entrypoints` seeded per live
#: registry op, `_reachable_functions` from each op's own single resolved handler, filtered to
#: this cluster's nine files) originally found 53 further live ops / 110 (op, site) pairs, none
#: enrolled in `_BUDGETED_ENTRYPOINTS`, matching the brief's own EM-measured slice ("~12 distinct
#: sites carrying ~108 (op, site) pairs") within the brief's own stated tolerance (a scope
#: statement, not an oracle). `queue.close` (4 pairs), `ceremony.wsc_tail` (6 pairs),
#: `fleet.archive_actioned_memos` (1 pair), `fleet.archive_completed_plans` (1 pair), and
#: `session.sweep_consumed_handoffs` (4 pairs) were killed and deleted 2026-08-23 (PM ruling, code
#: gone); their rows are removed rather than left pointing at dead entrypoints, leaving 48 ops / 94
#: pairs (enumerated in `_CLUSTER_D3_OPEN_DISPOSITION` below).
#:
#: DISPOSITION, NAMED HONESTLY PER AC19C (not silence, not a fabricated legitimation, not "a
#: sibling op reaches it"): NONE of these 48 ops is added to `_BUDGETED_ENTRYPOINTS`, and NONE of
#: their cluster sites is added to `_LEGITIMIZED_SITES`. A `_Legitimation` requires an EXISTING
#: companion test that asserts the op's OWN spawn count by exact equality (leg 2) AND was
#: measured to actually execute the site (leg 3). Checked for the one remaining op with a
#: same-named spawn-budget companion file on disk:
#:   - `hooks.cater_subagent_start` (`test_cater_subagent_start_spawn_budget.py`): asserts a
#:     ZERO-spawn guard on `compose_catering` alone, not on the op's own `_handler` -- it does not
#:     cover `_handler`'s other reachable branches, which is exactly what puts `session/core.py`'s
#:     `init` and `session/scope.py`'s `_git_run` on this op's reachable set in the first place.
#:     Leg 2 (an exact-equality assertion against the OP's own total) is not discharged.
#: Every remaining op among the 48 has no `*_spawn_budget.py`/spawn-count-asserting companion at
#: all (checked: no file under `coordinator_core/**/tests/` matching this op's own handler module
#: makes an exact-equality spawn-count assertion) -- so leg 2 is undischarged for all 48, and the
#: question of leg 3 does not arise.
#:
#: Building the missing companion fixture(s) for any of the 48 means writing to test files this
#: chunk's `writes:` scope does NOT include (`coordinator_core/tests/test_no_uncounted_spawn_on_
#: budgeted_path.py` is the only path in scope). Enrolling these ops into `_BUDGETED_ENTRYPOINTS`
#: without that legitimation would turn this currently-green gate red for a completeness gap this
#: chunk cannot itself discharge -- worse than a named, ratcheted, machine-checked gap. They stay
#: OUT of `_BUDGETED_ENTRYPOINTS`.
#:
#: Kept here as a mechanically-verified inventory (`test_cluster_d3_open_disposition_matches_
#: live_measurement` below) so it cannot rot silently in either direction, matching D2's own
#: precedent above -- until a future chunk closes each entry by one of the two routes AC19 itself
#: names: a real per-op `_LEGITIMIZED_SITES` entry (once a companion fixture exists), or moving it
#: to a permanent non-reach disposition if further tracing shows a site is not actually live on
#: that op's path.
#:
#: SUPERSEDED IN PART BY D7 (2026-08-23): every op keying this dict -- including the 12
#: `ops/fleet/_common.py`-reaching ops D1's own structural BLOCKED named -- now also carries a
#: `_STATIC_SPAWN_COUNT_PINS` entry (AC20, AC20d). That pin is a per-op reachable-spawn-COUNT
#: ratchet, not a per-site legitimation, and it dissolves D1's blocker by covering the shared-helper
#: cluster the SAME way as every other op rather than widening the module-granular inventory to
#: follow import hops. It closes no (op, site) pair recorded here -- the reasoning below stays live.
_CLUSTER_D3_OPEN_DISPOSITION: dict[str, tuple[tuple[str, str, str, int], ...]] = {
    "baton.resolve_path_and_repo": (
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
    ),
    "ceremony.chunk_commits": (
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
    ),
    "commit.exec_bit_change": (
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
    ),
    "deliverable.cascade_backstop_sweep": (
        ("coordinator_core/dag.py", "_git_path_ever_tracked", "git", 0),
    ),
    "deliverable.cascade_terminal": (
        ("coordinator_core/dag.py", "_git_path_ever_tracked", "git", 0),
        ("coordinator_core/git/run.py", "run_git", "git", 0),
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
    ),
    "distill.apply_disposal": (
        ("coordinator_core/dag.py", "_git_path_ever_tracked", "git", 0),
        ("coordinator_core/session/scope.py", "_git_run", "git", 0),
    ),
    "engine.drift": (
        ("coordinator_core/git/run.py", "run_git", "git", 0),
    ),
    "fleet.archive_completed_handoffs": (
        ("coordinator_core/git/run.py", "run_git", "git", 0),
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
        ("coordinator_core/session/scope.py", "_git_run", "git", 0),
    ),
    "fleet.archive_paper_trail": (
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
        ("coordinator_core/session/scope.py", "_git_run", "git", 0),
    ),
    "fleet.archive_queue_entry": (
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
        ("coordinator_core/session/scope.py", "_git_run", "git", 0),
    ),
    "fleet.archive_release_accumulator": (
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
        ("coordinator_core/session/scope.py", "_git_run", "git", 0),
    ),
    "fleet.archive_terminal_sizings": (
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
        ("coordinator_core/session/scope.py", "_git_run", "git", 0),
    ),
    "fleet.migrate_handoff_vocabulary": (
        ("coordinator_core/dag.py", "_git_path_ever_tracked", "git", 0),
        ("coordinator_core/git/run.py", "run_git", "git", 0),
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
        ("coordinator_core/ops/ceremony/git_native.py", "_hash_object_stdin_bytes", "<dynamic>", 0),
        ("coordinator_core/session/scope.py", "_git_run", "git", 0),
    ),
    "fleet.prune_closed_bugs": (
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
        ("coordinator_core/session/scope.py", "_git_run", "git", 0),
    ),
    "fleet.reap_integrated_findings": (
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
        ("coordinator_core/session/scope.py", "_git_run", "git", 0),
    ),
    "fleet.reap_unintegrated_findings": (
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
        ("coordinator_core/session/scope.py", "_git_run", "git", 0),
    ),
    "git.push_failure_verdict": (
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
    ),
    "handoff.archive_transition": (
        ("coordinator_core/git/run.py", "run_git", "git", 0),
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
        ("coordinator_core/ops/ceremony/git_native.py", "_hash_object_stdin_bytes", "<dynamic>", 0),
        ("coordinator_core/session/scope.py", "_git_run", "git", 0),
    ),
    "handoff.close_origin_stub": (
        ("coordinator_core/dag.py", "_git_path_ever_tracked", "git", 0),
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
    ),
    "handoff.has_live_children": (
        ("coordinator_core/dag.py", "_git_path_ever_tracked", "git", 0),
    ),
    "handoff.lineage_ancestry": (
        ("coordinator_core/dag.py", "_git_path_ever_tracked", "git", 0),
    ),
    "handoff.repoint_origin": (
        ("coordinator_core/dag.py", "_git_path_ever_tracked", "git", 0),
    ),
    "handoff.transition": (
        ("coordinator_core/dag.py", "_git_path_ever_tracked", "git", 0),
        ("coordinator_core/git/run.py", "run_git", "git", 0),
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
        ("coordinator_core/ops/ceremony/git_native.py", "_hash_object_stdin_bytes", "<dynamic>", 0),
        ("coordinator_core/session/scope.py", "_git_run", "git", 0),
    ),
    "hooks.cater_subagent_start": (
        ("coordinator_core/session/scope.py", "_git_run", "git", 0),
    ),
    "memo.transition": (
        ("coordinator_core/git/run.py", "run_git", "git", 0),
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
        ("coordinator_core/ops/ceremony/git_native.py", "_hash_object_stdin_bytes", "<dynamic>", 0),
    ),
    "orientation.regenerate_cache": (
        ("coordinator_core/git/repo_root.py", "_spawn_rev_parse", "git", 0),
    ),
    "priority.drain": (
        ("coordinator_core/session/scope.py", "_git_run", "git", 0),
    ),
    "research.archive_workdir": (
        ("coordinator_core/session/scope.py", "_git_run", "git", 0),
    ),
    "research.restructure_for_repeat_topic": (
        ("coordinator_core/session/scope.py", "_git_run", "git", 0),
    ),
    "review.freeze_diff": (
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
    ),
    "review.snapshot_diff_and_head": (
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
    ),
    "schema.drift_gate": (
        ("coordinator_core/git_scope.py", "_probe_foreign_repo", "git", 0),
        ("coordinator_core/git_scope.py", "scoped_cat_file_batch", "git", 0),
    ),
    # `session.boot_sweep` rows removed 2026-08-27: the op is gravestoned
    # (K-059) and `ops/session/boot_backstop.py` is deleted. A frozen row
    # naming a path that no longer exists cannot fail loud -- it just
    # silently pins a site nothing reaches.
    "session.commits": (
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
    ),
    "session.reap_claims_for_repos": (
        ("coordinator_core/dag.py", "_git_path_ever_tracked", "git", 0),
    ),
    "session_ledger.aggregate_chain_loe": (
        ("coordinator_core/dag.py", "_git_path_ever_tracked", "git", 0),
    ),
    "tracker.push_suggestion": (
        ("coordinator_core/ops/ceremony/git_native.py", "_git._invoke", "<dynamic>", 0),
    ),
}

#: Entrypoints for the 48 D3-open ops, resolved via each op's own single registered handler
#: (`spawn_bearing_ops.resolve_op_entrypoints`), matching `_CLUSTER_D2_OPEN_ENTRYPOINTS`'s own
#: shape and kept separate for the same reason: the verifying test below seeds
#: `_reachable_functions` per op without depending on this file's own registry-divergence tests'
#: import-order sensitivity.
_CLUSTER_D3_OPEN_ENTRYPOINTS: dict[str, tuple[str, str]] = {
    "baton.resolve_path_and_repo": ("coordinator_core/ops/resolve_baton_path.py", "_resolve_baton_path_and_repo"),
    "ceremony.chunk_commits": ("coordinator_core/ops/ceremony/chunk_commits.py", "_handler"),
    "commit.exec_bit_change": ("coordinator_core/ops/ceremony/commit_exec_bit.py", "_handler"),
    "deliverable.cascade_backstop_sweep": ("coordinator_core/ops/cascade_backstop_sweep.py", "_handler"),
    "deliverable.cascade_terminal": ("coordinator_core/ops/deliverable_cascade.py", "_handler"),
    "distill.apply_disposal": ("coordinator_core/ops/distill_apply_disposal.py", "_handler"),
    "engine.drift": ("coordinator_core/ops/engine_drift.py", "_engine_drift"),
    "fleet.archive_completed_handoffs": ("coordinator_core/ops/fleet/archive_terminal_handoffs.py", "_handler"),
    "fleet.archive_paper_trail": ("coordinator_core/ops/fleet/archive_paper_trail.py", "_handler"),
    "fleet.archive_queue_entry": ("coordinator_core/ops/fleet/archive_queue_entry.py", "_handler"),
    "fleet.archive_release_accumulator": ("coordinator_core/ops/fleet/archive_release_accumulator.py", "_handler"),
    "fleet.archive_terminal_sizings": ("coordinator_core/ops/fleet/archive_sizings.py", "_archive_terminal_sizings"),
    "fleet.migrate_handoff_vocabulary": ("coordinator_core/ops/fleet/migrate_handoff_vocabulary.py", "_handler"),
    "fleet.prune_closed_bugs": ("coordinator_core/ops/fleet/prune_bugs.py", "_handler"),
    "fleet.reap_integrated_findings": ("coordinator_core/ops/fleet/reap_integrated_findings.py", "_handler"),
    "fleet.reap_unintegrated_findings": ("coordinator_core/ops/fleet/reap_unintegrated_findings.py", "_handler"),
    "git.push_failure_verdict": ("coordinator_core/ops/push_failure_verdict.py", "_handler"),
    "handoff.archive_transition": ("coordinator_core/ops/handoff_archive_transition.py", "_handler"),
    "handoff.close_origin_stub": ("coordinator_core/ops/handoff_close_origin_stub.py", "_handler"),
    "handoff.has_live_children": ("coordinator_core/ops/handoff_children.py", "_handoff_has_live_children"),
    "handoff.lineage_ancestry": ("coordinator_core/ops/handoff_lineage_ancestry.py", "_handler"),
    "handoff.repoint_origin": ("coordinator_core/ops/handoff_repoint_origin.py", "_handler"),
    "handoff.transition": ("coordinator_core/ops/handoff_transition.py", "_handler"),
    "hooks.cater_subagent_start": ("coordinator_core/hooks/cater_subagent_start.py", "_handler"),
    "memo.transition": ("coordinator_core/ops/memo_transition.py", "_handler"),
    "orientation.regenerate_cache": ("coordinator_core/orientation/regenerate_cache.py", "_orientation_regenerate_cache"),
    "priority.drain": ("coordinator_core/ops/priority_drain.py", "_priority_drain"),
    "research.archive_workdir": ("coordinator_core/ops/research_archive_workdir.py", "_handler"),
    "research.restructure_for_repeat_topic": ("coordinator_core/ops/research_dir_restructure.py", "_handler"),
    "review.freeze_diff": ("coordinator_core/ops/review_freeze_diff.py", "_handler"),
    "review.snapshot_diff_and_head": ("coordinator_core/ops/ceremony/snapshot_diff_and_head.py", "_handler"),
    "schema.drift_gate": ("coordinator_core/ops/schema_drift_gate.py", "_handler"),
    "session.commits": ("coordinator_core/ops/session_commits.py", "_handler"),
    "session.reap_claims_for_repos": ("coordinator_core/ops/session/reap.py", "_handler_reap_claims_for_repos"),
    "session_ledger.aggregate_chain_loe": (
        "coordinator_core/session_ledger/aggregate_chain_loe.py", "_session_ledger_aggregate_chain_loe",
    ),
    "tracker.push_suggestion": ("coordinator_core/ops/tracker/push_suggestion.py", "_handler"),
}

#: The nine cluster files this D3 disposition is scoped to -- matches the chunk's own dispatch
#: brief's file list (`session/scope.py`, `session/core.py`, `ops/ceremony/git_native.py`,
#: `git/run.py`, `git/ls_files_bytes.py`, `git/divergence.py`, `git/repo_root.py`,
#: `git_scope.py`, `dag.py`).
_CLUSTER_D3_TARGET_FILES = frozenset(
    {
        "coordinator_core/session/scope.py",
        "coordinator_core/session/core.py",
        "coordinator_core/ops/ceremony/git_native.py",
        "coordinator_core/git/run.py",
        "coordinator_core/git/ls_files_bytes.py",
        "coordinator_core/git/divergence.py",
        "coordinator_core/git/repo_root.py",
        "coordinator_core/git_scope.py",
        "coordinator_core/dag.py",
    }
)


def test_cluster_d3_open_disposition_matches_live_measurement():
    """Ratchet for `_CLUSTER_D3_OPEN_DISPOSITION`: re-derives, from the live tree, exactly which
    cluster sites (the nine D3 files above) each of the 48 D3-open ops' own function-granular
    reachable set contains -- seeded from each op's own live-registry-resolved entrypoint, not a
    hand-picked one -- and asserts it against the frozen disposition, byte for byte, per op.
    A site the live tree adds or drops without this dict being updated in the SAME change fails
    here, matching `test_cluster_d2_open_disposition_matches_live_measurement`'s own precedent.
    Also asserts the total pair count (110) and the entrypoint-resolves-to-a-real-function
    precondition."""
    for op_key, (relpath, func_name) in _CLUSTER_D3_OPEN_ENTRYPOINTS.items():
        assert op_key in _CLUSTER_D3_OPEN_DISPOSITION, f"{op_key} has an entrypoint but no disposition entry"
        tree = ast.parse((_REPO_ROOT / relpath).read_text(encoding="utf-8"))
        names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert func_name in names, (
            f"{op_key}'s declared entrypoint {relpath}::{func_name} is not a live top-level "
            "function -- this dict rotted, or the op moved."
        )

    (
        index,
        spawn_sites_by_file,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
    ) = _build_corpus()

    total_pairs = 0
    mismatches: list[str] = []
    for op_key, (relpath, func_name) in _CLUSTER_D3_OPEN_ENTRYPOINTS.items():
        reached = _reachable_functions(
            {(relpath, func_name)},
            index,
            import_aliases_by_file,
            func_aliases_by_file,
            local_aliases_by_file,
        )
        sites = _on_path_spawn_sites(reached, spawn_sites_by_file, exempt=set())
        live_keys = frozenset(
            (site.path, site.enclosing, site.argv0, site.ordinal)
            for site in sites
            if site.path in _CLUSTER_D3_TARGET_FILES
        )
        declared_keys = frozenset(_CLUSTER_D3_OPEN_DISPOSITION[op_key])
        total_pairs += len(declared_keys)
        if live_keys != declared_keys:
            mismatches.append(
                f"  {op_key}: live={sorted(live_keys)} declared={sorted(declared_keys)}"
            )

    assert not mismatches, (
        "_CLUSTER_D3_OPEN_DISPOSITION has drifted from the live tree's own cluster reachability "
        "(re-derive and update the dict, do not silently widen or narrow it):\n" + "\n".join(mismatches)
    )
    assert total_pairs == 63, (
        f"_CLUSTER_D3_OPEN_DISPOSITION now totals {total_pairs} (op, site) pairs, not the "
        "63: 5ae46cc1b9 also dropped session/scope.py::_git_run from deliverable.cascade_terminal and memo.transition (65 -> 63). Before that, 65 was left after the 2026-08-30 rot sweep. Two reductions, both traced pair-by-pair "
        "against the revision that set 79 (63cd18de01). FIRST, four pairs had already left "
        "without this constant moving, which is why it was red before the sweep: "
        "ceremony.session_instructions (-1), eol.census (-1), eol.repair (-1) and "
        "session.boot_sweep (-2), offset by fleet.archive_completed_handoffs gaining one "
        "(3 -> 4, the same growth that made its static pin of 5 slack and is now pinned at 4). "
        "79 - 4 = 75, the live total at HEAD before this sweep. SECOND, the sweep removed "
        "ceremony.post_commit_tail (-2) and handoff.reconcile_close_terminal (-5), neither of "
        "which resolves to a live op any more, plus review_trail.write (-1, ops/review_trail_write.py "
        "deleted outright; its writer was retired by DR-372/DR-374). THIRD, fleet.archive_completed_"
        "handoffs and handoff.archive_transition each stopped reaching dag.py::_git_path_ever_"
        "tracked (-2): the function still exists but now has no caller outside dag.py itself, "
        "the same narrowing that made the former op's static pin of 5 slack. 75 - 8 - 2 = 65. "
        "Update this constant deliberately if the shift is real and understood, never to "
        "silence a drift you have not traced."
    )

    for op_key in _CLUSTER_D3_OPEN_DISPOSITION:
        assert op_key not in _BUDGETED_ENTRYPOINTS, (
            f"{op_key} is now enrolled in _BUDGETED_ENTRYPOINTS -- its cluster sites need real "
            "_LEGITIMIZED_SITES entries (route a) instead of living in this open-disposition "
            "registry; remove it from _CLUSTER_D3_OPEN_DISPOSITION/_CLUSTER_D3_OPEN_ENTRYPOINTS "
            "in the same change that enrolls it."
        )


#: D4 (2026-08-23, this chunk's own dispatch brief -- `state/dispatch-briefs/2026-08-22-
#: the-composition-gate-counts-processes-across-the-op-graph/D4.md`): the plugin-health-and-
#: interpreter-resolution cluster (`plugin_health/release_currency.py`, `pyresolve.py`,
#: `resolve_coordinator_clone.py`, `engine_root.py`, `warm/skew.py`). None of this cluster's sites
#: is legitimized above in `_LEGITIMIZED_SITES` -- no `_BUDGETED_ENTRYPOINTS` row's own reachable
#: set includes any of these five files (verified below: every op in this cluster's live
#: measurement is absent from `_BUDGETED_ENTRYPOINTS`).
#:
#: Live re-measurement this chunk (`spawn_bearing_ops.resolve_op_entrypoints` seeded per live
#: registry op, `_reachable_functions` from each op's own single resolved handler, filtered to
#: this cluster's five files) originally found 16 live ops / 35 (op, site) pairs, none enrolled in
#: `_BUDGETED_ENTRYPOINTS`, matching the brief's own EM-measured slice ("~20 distinct sites
#: carrying ~31 (op, site) pairs") within the brief's own stated tolerance (a scope statement, not
#: an oracle). `completion.reconcile_commits` (1 pair) was killed and deleted 2026-08-23 (PM
#: ruling, code gone); its row is removed rather than left pointing at a dead entrypoint, leaving
#: 15 ops / 34 pairs (enumerated in `_CLUSTER_D4_OPEN_DISPOSITION` below). `release_currency.py`
#: alone accounts for 13 of the 20 distinct sites and all 13 of `plugin_health.sentinel`'s own
#: pairs on that file -- nearly one-to-one, matching the brief's own characterization of this file
#: as per-site work rather than shared machinery.
#:
#: DISPOSITION, NAMED HONESTLY PER AC19C (not silence, not a fabricated legitimation, not "a
#: sibling op reaches it"): NONE of these 15 ops is added to `_BUDGETED_ENTRYPOINTS`, and NONE of
#: their cluster sites is added to `_LEGITIMIZED_SITES`. A `_Legitimation` requires an EXISTING
#: companion test that asserts the op's OWN spawn count by exact equality (leg 2) AND was measured
#: to actually execute the site (leg 3). Checked for all 15: no file under
#: `coordinator_core/**/tests/*spawn_budget*.py` (the full, enumerated live list of every such file
#: in the repo) matches any of these 15 ops' own handler modules or op names -- `backlog.record`,
#: `ceremony.init_anchor_injection_state`, `goal.append`,
#: `goal.close_day`, `goal.close_day_apply`, `install.probe_skill_frontmatter_valid`,
#: `plugin_health.forwarder_drift`, `plugin_health.sentinel`, `priority.drain`, `priority.set`,
#: `queue.promote`, `repo_setup.copy_console_subprocess_tripwire`,
#: `session.guard_hooks_kill_switch_detail`, `session.guard_settings_integrity`, `workflow.fire`
#: have no exact-equality spawn-count-asserting companion at all -- leg 2 is undischarged for all
#: 15, and the question of leg 3 does not arise. Unlike D3's cluster, no partial-exception case
#: turned up here: none of these 15 ops has ANY same-named `*_spawn_budget.py` file on disk, not
#: even one that asserts a narrower or monkeypatched shape.
#:
#: Building the missing companion fixture(s) for any of the 15 means writing to test files this
#: chunk's `writes:` scope does NOT include (`coordinator_core/tests/test_no_uncounted_spawn_on_
#: budgeted_path.py` is the only path in scope). Enrolling these ops into `_BUDGETED_ENTRYPOINTS`
#: without that legitimation would turn this currently-green gate red for a completeness gap this
#: chunk cannot itself discharge -- worse than a named, ratcheted, machine-checked gap. They stay
#: OUT of `_BUDGETED_ENTRYPOINTS`.
#:
#: Kept here as a mechanically-verified inventory (`test_cluster_d4_open_disposition_matches_
#: live_measurement` below) so it cannot rot silently in either direction, matching D2/D3's own
#: precedent above -- until a future chunk closes each entry by one of the two routes AC19 itself
#: names: a real per-op `_LEGITIMIZED_SITES` entry (once a companion fixture exists), or moving it
#: to a permanent non-reach disposition if further tracing shows a site is not actually live on
#: that op's path.
_CLUSTER_D4_OPEN_ENTRYPOINTS: dict[str, tuple[str, str]] = {
    "backlog.record": ("coordinator_core/ops/emit/recorder.py", "_backlog_record"),
    "ceremony.init_anchor_injection_state": ("coordinator_core/ops/init_anchor_injection_state.py", "_handler"),
    "goal.append": ("coordinator_core/ops/goal_append.py", "_goal_append"),
    "goal.close_day": ("coordinator_core/ops/goal_close_day.py", "_goal_close_day"),
    "goal.close_day_apply": ("coordinator_core/ops/goal_close_day.py", "_goal_close_day_apply"),
    "install.probe_skill_frontmatter_valid": ("coordinator_core/install/prereq_probe.py", "_probe_skill_frontmatter_valid_op"),
    "plugin_health.forwarder_drift": ("coordinator_core/plugin_health/forwarder_drift.py", "_plugin_health_forwarder_drift"),
    "plugin_health.sentinel": ("coordinator_core/plugin_health/sentinel.py", "_plugin_health_sentinel"),
    "priority.drain": ("coordinator_core/ops/priority_drain.py", "_priority_drain"),
    "priority.set": ("coordinator_core/ops/priority_set.py", "_priority_set"),
    "queue.promote": ("coordinator_core/ops/queue_promote.py", "_queue_promote_handler"),
    "repo_setup.copy_console_subprocess_tripwire": ("coordinator_core/ops/copy_plugin_template.py", "_copy_console_subprocess_tripwire"),
    "session.guard_hooks_kill_switch_detail": ("coordinator_core/ops/session/guard_settings_integrity.py", "_handler_kill_switch_detail"),
    "session.guard_settings_integrity": ("coordinator_core/ops/session/guard_settings_integrity.py", "_handler"),
    "workflow.fire": ("coordinator_core/ops/workflow_fire/op.py", "_workflow_fire"),
}

#: The five cluster files this D4 disposition is scoped to -- matches the chunk's own subject
#: files (`coordinator_core/plugin_health/release_currency.py`, `coordinator_core/pyresolve.py`,
#: `coordinator_core/resolve_coordinator_clone.py`, `coordinator_core/engine_root.py`,
#: `coordinator_core/warm/skew.py`).
_CLUSTER_D4_TARGET_FILES = frozenset(
    {
        "coordinator_core/engine_root.py",
        "coordinator_core/plugin_health/release_currency.py",
        "coordinator_core/pyresolve.py",
        "coordinator_core/resolve_coordinator_clone.py",
        "coordinator_core/warm/skew.py",
    }
)

#: SUPERSEDED IN PART BY D7 (2026-08-23): every op keying this dict now also carries a
#: `_STATIC_SPAWN_COUNT_PINS` entry (AC20) -- a per-op reachable-spawn-COUNT ratchet, not a
#: per-site legitimation. It closes no (op, site) pair recorded here -- the reasoning below
#: stays live, matching D2/D3's own precedent above.
_CLUSTER_D4_OPEN_DISPOSITION: dict[str, tuple[tuple[str, str, str, int], ...]] = {
    "backlog.record": (
        ("coordinator_core/engine_root.py", "coordinator_engine_root", "machine-local", 0),
    ),
    "ceremony.init_anchor_injection_state": (
        ("coordinator_core/resolve_coordinator_clone.py", "_machine_local_get", "machine-local", 0),
    ),
    "goal.append": (
        ("coordinator_core/engine_root.py", "coordinator_engine_root", "machine-local", 0),
    ),
    "goal.close_day": (
        ("coordinator_core/engine_root.py", "coordinator_engine_root", "machine-local", 0),
    ),
    "goal.close_day_apply": (
        ("coordinator_core/engine_root.py", "coordinator_engine_root", "machine-local", 0),
    ),
    "install.probe_skill_frontmatter_valid": (
        ("coordinator_core/resolve_coordinator_clone.py", "_machine_local_get", "machine-local", 0),
    ),
    "plugin_health.forwarder_drift": (
        ("coordinator_core/engine_root.py", "coordinator_engine_root", "machine-local", 0),
        ("coordinator_core/resolve_coordinator_clone.py", "_machine_local_get", "machine-local", 0),
    ),
    "plugin_health.sentinel": (
        ("coordinator_core/plugin_health/release_currency.py", "_check_ancestry", "git", 0),
        ("coordinator_core/plugin_health/release_currency.py", "_fetch_latest_release_tag", "git", 0),
        ("coordinator_core/plugin_health/release_currency.py", "_git_clone_behind_count", "git", 0),
        ("coordinator_core/plugin_health/release_currency.py", "_git_clone_behind_count", "git", 1),
        ("coordinator_core/plugin_health/release_currency.py", "_git_clone_behind_count", "git", 2),
        ("coordinator_core/plugin_health/release_currency.py", "_git_clone_behind_count", "git", 3),
        ("coordinator_core/plugin_health/release_currency.py", "_git_clone_behind_count", "git", 4),
        ("coordinator_core/plugin_health/release_currency.py", "_local_describe_tag", "git", 0),
        ("coordinator_core/plugin_health/release_currency.py", "_local_describe_tag", "git", 1),
        ("coordinator_core/plugin_health/release_currency.py", "_resolve_tag_sha", "git", 0),
        ("coordinator_core/plugin_health/release_currency.py", "_resolve_tag_sha", "git", 1),
        ("coordinator_core/plugin_health/release_currency.py", "_run", "<dynamic>", 0),
        ("coordinator_core/plugin_health/release_currency.py", "release_currency_probe", "git", 0),
        ("coordinator_core/pyresolve.py", "_launcher_available", "<dynamic>", 0),
        ("coordinator_core/pyresolve.py", "_machine_local_get", "<dynamic>", 0),
        ("coordinator_core/pyresolve.py", "_validate_interpreter", "<dynamic>", 0),
        ("coordinator_core/resolve_coordinator_clone.py", "_machine_local_get", "machine-local", 0),
    ),
    "priority.drain": (
        ("coordinator_core/resolve_coordinator_clone.py", "_machine_local_get", "machine-local", 0),
    ),
    "priority.set": (
        ("coordinator_core/resolve_coordinator_clone.py", "_machine_local_get", "machine-local", 0),
    ),
    "queue.promote": (
        ("coordinator_core/resolve_coordinator_clone.py", "_machine_local_get", "machine-local", 0),
    ),
    "repo_setup.copy_console_subprocess_tripwire": (
        ("coordinator_core/resolve_coordinator_clone.py", "_machine_local_get", "machine-local", 0),
    ),
    "session.guard_hooks_kill_switch_detail": (
        ("coordinator_core/resolve_coordinator_clone.py", "_machine_local_get", "machine-local", 0),
    ),
    "session.guard_settings_integrity": (
        ("coordinator_core/resolve_coordinator_clone.py", "_machine_local_get", "machine-local", 0),
    ),
    "workflow.fire": (
        ("coordinator_core/resolve_coordinator_clone.py", "_machine_local_get", "machine-local", 0),
        ("coordinator_core/warm/skew.py", "publish_lag", "git", 0),
        ("coordinator_core/warm/skew.py", "publish_lag", "git", 1),
    ),
}


def test_cluster_d4_open_disposition_matches_live_measurement():
    """Ratchet for `_CLUSTER_D4_OPEN_DISPOSITION`: re-derives, from the live tree, exactly which
    cluster sites (the five D4 files above) each of the 15 D4-open ops' own function-granular
    reachable set contains, and asserts it against the frozen disposition -- byte for byte, per
    op. A site the live tree adds or drops without this dict being updated in the SAME change
    fails here, matching D2/D3's own drift guard. Also asserts the total pair count (34) and the
    entrypoint-resolves-to-a-real-function precondition -- mirroring `test_budgeted_entrypoints_
    resolve_to_live_functions`'s own rot guard for `_BUDGETED_ENTRYPOINTS`, applied here to this
    dict's separate registry."""
    for op_key, (relpath, func_name) in _CLUSTER_D4_OPEN_ENTRYPOINTS.items():
        assert op_key in _CLUSTER_D4_OPEN_DISPOSITION, f"{op_key} has an entrypoint but no disposition entry"
        tree = ast.parse((_REPO_ROOT / relpath).read_text(encoding="utf-8"))
        names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert func_name in names, (
            f"{op_key}'s declared entrypoint {relpath}::{func_name} is not a live top-level "
            "function -- this dict rotted, or the op moved."
        )

    (
        index,
        spawn_sites_by_file,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
    ) = _build_corpus()

    total_pairs = 0
    mismatches: list[str] = []
    for op_key, (relpath, func_name) in _CLUSTER_D4_OPEN_ENTRYPOINTS.items():
        reached = _reachable_functions(
            {(relpath, func_name)},
            index,
            import_aliases_by_file,
            func_aliases_by_file,
            local_aliases_by_file,
        )
        sites = _on_path_spawn_sites(reached, spawn_sites_by_file, exempt=set())
        live_keys = frozenset(
            (site.path, site.enclosing, site.argv0, site.ordinal)
            for site in sites
            if site.path in _CLUSTER_D4_TARGET_FILES
        )
        declared_keys = frozenset(_CLUSTER_D4_OPEN_DISPOSITION[op_key])
        total_pairs += len(declared_keys)
        if live_keys != declared_keys:
            mismatches.append(
                f"  {op_key}: live={sorted(live_keys)} declared={sorted(declared_keys)}"
            )

    assert not mismatches, (
        "_CLUSTER_D4_OPEN_DISPOSITION has drifted from the live tree's own cluster reachability "
        "(re-derive and update the dict, do not silently widen or narrow it):\n" + "\n".join(mismatches)
    )
    assert total_pairs == 34, (
        f"_CLUSTER_D4_OPEN_DISPOSITION now totals {total_pairs} (op, site) pairs, not the "
        "34 expected after completion.reconcile_commits's kill removed its 1 pair from the "
        "EM-measured 35 this chunk's own re-derivation found -- update this constant deliberately "
        "if the shift is real and understood, never to silence a drift you have not traced."
    )

    for op_key in _CLUSTER_D4_OPEN_DISPOSITION:
        assert op_key not in _BUDGETED_ENTRYPOINTS, (
            f"{op_key} is now enrolled in _BUDGETED_ENTRYPOINTS -- its cluster sites need real "
            "_LEGITIMIZED_SITES entries (route a) instead of living in this open-disposition "
            "registry; remove it from _CLUSTER_D4_OPEN_DISPOSITION/_CLUSTER_D4_OPEN_ENTRYPOINTS "
            "in the same change that enrolls it."
        )


#: D5 (2026-08-22/23, this chunk's own dispatch brief -- `state/dispatch-briefs/2026-08-22-
#: the-composition-gate-counts-processes-across-the-op-graph/D5.md`, "cover the long tail --
#: every remaining file, none excepted"): the 22 remaining files this plan names outright
#: (`distill/delete_guard.py`, `distill/_common.py`, `ops/emit/resolvers.py`,
#: `ops/emit/sections/handoff_columns.py`, `archive_stamp.py`, `ops/workflow_fire/fire.py`,
#: `cartography/tree.py`, `cartography/file_index.py`,
#: `ops/workday_complete_backfill_scan.py`, `reconcile/commit_reality.py`,
#: `person_resolver.py`, `ops/ceremony/resolver.py`, `ops/ceremony/branch_resolution.py`,
#: `ops/discover_working_repos.py`, `session_attribution.py`,
#: `ops/session/safe_commit_offer.py`, `execute_plan_assemble/row_spans.py`,
#: `ops/assert_no_dangling_plan_backlinks.py`, `subagent_sandbox/engine.py`,
#: `ops/bootstrap_repo.py`, `ops/strategic/version_highlights.py`, `testing/run.py`).
#:
#: Live re-measurement this chunk (`spawn_bearing_ops.resolve_op_entrypoints` seeded per live
#: registry op, `_reachable_functions` from each op's own single resolved handler, filtered to
#: this cluster's 22 files) originally found 37 live ops / 49 (op, site) pairs, NONE enrolled in
#: `_BUDGETED_ENTRYPOINTS`, matching the brief's own EM-measured slice ("~25 distinct sites
#: carrying ~57 (op, site) pairs") within the brief's own stated tolerance (a scope statement, not
#: an oracle). Three of those 37 -- `ceremony.wsc_tail`, `completion.reconcile_commits`,
#: `fleet.archive_completed_plans` -- were killed and deleted 2026-08-23 (PM ruling, code gone);
#: their rows are removed from this dict rather than left pointing at dead entrypoints, leaving 34
#: ops / 45 pairs. No op already enrolled in `_BUDGETED_ENTRYPOINTS` reaches any of these 22 files
#: at all (checked against every live op's own single resolved entrypoint, not just the nine
#: hand-audited rows).
#:
#: DISPOSITION, NAMED HONESTLY PER AC19C: NONE of these 34 ops is added to `_BUDGETED_ENTRYPOINTS`
#: and NONE of their cluster sites is added to `_LEGITIMIZED_SITES`, with one nuance worth
#: recording rather than silently flattening. One of the 34 -- `hooks.cater_subagent_start` --
#: DOES have an existing same-named companion
#: (`coordinator_core/hooks/tests/test_cater_subagent_start_spawn_budget.py`), unlike the other 33
#: (checked against the full, enumerated live list of every `*spawn_budget*.py` file in the repo
#: -- no other op name or handler module among these 34 matches any of them). For
#: `hooks.cater_subagent_start`: its one companion's own docstring states the reachable leg this
#: chunk's trace hit (`_resolve_git_root_uncached` under `_handler`'s `_provision` path) is
#: explicitly OUT OF SCOPE for that companion ("`_provision`'s own `resolve_git_root` call is OUT
#: OF SCOPE for this chunk") -- leg 2 (an assertion covering THIS site) does not hold for it at
#: all. For the remaining 33: no `*_spawn_budget.py` file matches their op name or handler module
#: -- leg 2 is undischarged outright, matching D4's own finding for its ops.
#:
#: Building the missing companion fixture(s) means writing to test files this chunk's `writes:`
#: scope does NOT include (`coordinator_core/tests/test_no_uncounted_spawn_on_budgeted_path.py`
#: is the only path in scope). Enrolling any of these 34 ops into `_BUDGETED_ENTRYPOINTS` without
#: that legitimation would turn this currently-green gate red for a completeness gap this chunk
#: cannot itself discharge -- worse than a named, ratcheted, machine-checked gap. They stay OUT of
#: `_BUDGETED_ENTRYPOINTS`.
#:
#: Kept here as a mechanically-verified inventory (`test_cluster_d5_open_disposition_matches_
#: live_measurement` below) so it cannot rot silently in either direction, matching D2/D3/D4's
#: own precedent above -- until a future chunk closes each entry by one of the two routes AC19
#: itself names: a real per-op `_LEGITIMIZED_SITES` entry (once a companion fixture exists
#: covering the WHOLE op), or moving it to a permanent non-reach disposition if further tracing
#: shows a site is not actually live on that op's path.
_CLUSTER_D5_OPEN_ENTRYPOINTS: dict[str, tuple[str, str]] = {
    "backlog.record": ("coordinator_core/ops/emit/recorder.py", "_backlog_record"),
    "cartography.chunk_table": ("coordinator_core/ops/cartography_chunk_table.py", "_cartography_chunk_table"),
    "cartography.file_index": ("coordinator_core/ops/cartography_file_index.py", "_cartography_file_index"),
    "cartography.tree": ("coordinator_core/ops/cartography_tree.py", "_cartography_tree"),
    "ceremony.update_docs_scan": ("coordinator_core/ops/ceremony/update_docs_scan.py", "_ceremony_update_docs_scan"),
    "changelog.compute_day_fields": ("coordinator_core/ops/changelog_ops.py", "_compute_day_fields_handler"),
    "changelog.inject_anchor": ("coordinator_core/ops/changelog_ops.py", "_inject_anchor_handler"),
    "changelog.upsert_reviewed": ("coordinator_core/ops/changelog_ops.py", "_upsert_reviewed_handler"),
    "completion.flip_to_released": ("coordinator_core/ops/completion_ops.py", "_flip_to_released_handler"),
    "crossrepo.closure_status": ("coordinator_core/ops/crossrepo_closure_status.py", "_handler"),
    "cruft_sweep.run": ("coordinator_core/ops/cruft_sweep.py", "_run_handler"),
    "deliverable.cascade_terminal": ("coordinator_core/ops/deliverable_cascade.py", "_handler"),
    "distill.apply_disposal": ("coordinator_core/ops/distill_apply_disposal.py", "_handler"),
    "distill.assemble_disposal_manifest": ("coordinator_core/ops/distill_disposal_manifest.py", "_handler"),
    "distill.curation_status": ("coordinator_core/ops/distill_curation_status.py", "_distill_curation_status"),
    "distill.scope": ("coordinator_core/ops/distill_scope.py", "_handler"),
    "goal.append": ("coordinator_core/ops/goal_append.py", "_goal_append"),
    "goal.close_day": ("coordinator_core/ops/goal_close_day.py", "_goal_close_day"),
    "goal.close_day_apply": ("coordinator_core/ops/goal_close_day.py", "_goal_close_day_apply"),
    "handoff.archive_transition": ("coordinator_core/ops/handoff_archive_transition.py", "_handler"),
    "handoff.author_fork": ("coordinator_core/ops/handoff_author_fork.py", "_handler"),
    "handoff.columns": ("coordinator_core/ops/handoff_columns_query.py", "_handler"),
    "handoff.scaffold_from_queue": ("coordinator_core/ops/queue_scaffold_baton.py", "_handler"),
    "hooks.cater_subagent_start": ("coordinator_core/hooks/cater_subagent_start.py", "_handler"),
    "memo.fate_backfill": ("coordinator_core/ops/memo_fate_backfill.py", "_handler"),
    "repo_setup.validate_target_root": ("coordinator_core/ops/bootstrap_repo.py", "_validate_target_root_op"),
    "scratchpad.sweep": ("coordinator_core/ops/scratchpad_sweep.py", "_handler"),
    "strategic.generate": ("coordinator_core/ops/strategic_generate.py", "_strategic_generate"),
    "tracker.mint_person": ("coordinator_core/ops/tracker/mint_person.py", "_handler"),
    "workflow.fire": ("coordinator_core/ops/workflow_fire/op.py", "_workflow_fire"),
    "workflow.fire_status": ("coordinator_core/ops/workflow_fire/op.py", "_workflow_fire_status"),
}

#: The 22 cluster files this D5 disposition is scoped to -- matches the chunk's own subject
#: files (this chunk's dispatch brief's `Your files:` list, verbatim).
_CLUSTER_D5_TARGET_FILES = frozenset(
    {
        "coordinator_core/distill/delete_guard.py",
        "coordinator_core/distill/_common.py",
        "coordinator_core/ops/emit/resolvers.py",
        "coordinator_core/ops/emit/sections/handoff_columns.py",
        "coordinator_core/archive_stamp.py",
        "coordinator_core/ops/workflow_fire/fire.py",
        "coordinator_core/cartography/tree.py",
        "coordinator_core/cartography/file_index.py",
        "coordinator_core/ops/workday_complete_backfill_scan.py",
        "coordinator_core/reconcile/commit_reality.py",
        "coordinator_core/person_resolver.py",
        "coordinator_core/ops/ceremony/resolver.py",
        "coordinator_core/ops/ceremony/branch_resolution.py",
        "coordinator_core/ops/discover_working_repos.py",
        "coordinator_core/session_attribution.py",
        "coordinator_core/ops/session/safe_commit_offer.py",
        "coordinator_core/execute_plan_assemble/row_spans.py",
        "coordinator_core/ops/assert_no_dangling_plan_backlinks.py",
        "coordinator_core/subagent_sandbox/engine.py",
        "coordinator_core/ops/bootstrap_repo.py",
        "coordinator_core/ops/strategic/version_highlights.py",
        "coordinator_core/testing/run.py",
    }
)

_CLUSTER_D5_OPEN_DISPOSITION: dict[str, tuple[tuple[str, str, str, int], ...]] = {
    "backlog.record": (
        ("coordinator_core/ops/emit/resolvers.py", "resolve_coordinator_root", "<dynamic>", 0),
    ),
    "cartography.chunk_table": (
        ("coordinator_core/cartography/tree.py", "list_tracked_files", "<dynamic>", 0),
    ),
    "cartography.file_index": (
        ("coordinator_core/cartography/file_index.py", "list_untracked_files", "<dynamic>", 0),
        ("coordinator_core/cartography/tree.py", "list_tracked_files", "<dynamic>", 0),
    ),
    "cartography.tree": (
        ("coordinator_core/cartography/tree.py", "list_tracked_files", "<dynamic>", 0),
    ),
    "ceremony.update_docs_scan": (
        ("coordinator_core/distill/_common.py", "active_reference_guard_many", "rg", 0),
    ),
    "changelog.compute_day_fields": (
        ("coordinator_core/ops/workday_complete_backfill_scan.py", "_run_git", "git", 0),
    ),
    "changelog.inject_anchor": (
        ("coordinator_core/ops/workday_complete_backfill_scan.py", "_run_git", "git", 0),
    ),
    "changelog.upsert_reviewed": (
        ("coordinator_core/ops/workday_complete_backfill_scan.py", "_run_git", "git", 0),
    ),
    "completion.flip_to_released": (
        ("coordinator_core/reconcile/commit_reality.py", "_git", "git", 0),
    ),
    "crossrepo.closure_status": (
        ("coordinator_core/distill/delete_guard.py", "_git_object_exists", "git", 0),
    ),
    "cruft_sweep.run": (
        ("coordinator_core/ops/discover_working_repos.py", "_sort_unique", "sort", 0),
    ),
    "deliverable.cascade_terminal": (
        ("coordinator_core/archive_stamp.py", "_run_git", "git", 0),
    ),
    "distill.apply_disposal": (
        ("coordinator_core/distill/_common.py", "active_reference_guard", "rg", 0),
        ("coordinator_core/distill/delete_guard.py", "_candidate_actioned_date", "git", 0),
        ("coordinator_core/distill/delete_guard.py", "_git_object_exists", "git", 0),
    ),
    "distill.assemble_disposal_manifest": (
        ("coordinator_core/distill/_common.py", "active_reference_guard", "rg", 0),
        ("coordinator_core/distill/delete_guard.py", "_candidate_actioned_date", "git", 0),
        ("coordinator_core/distill/delete_guard.py", "_git_object_exists", "git", 0),
    ),
    "distill.curation_status": (
        ("coordinator_core/distill/_common.py", "active_reference_guard_many", "rg", 0),
    ),
    "distill.scope": (
        ("coordinator_core/distill/_common.py", "active_reference_guard_many", "rg", 0),
    ),
    "goal.append": (
        ("coordinator_core/ops/emit/resolvers.py", "resolve_coordinator_root", "<dynamic>", 0),
    ),
    "goal.close_day": (
        ("coordinator_core/ops/emit/resolvers.py", "resolve_coordinator_root", "<dynamic>", 0),
    ),
    "goal.close_day_apply": (
        ("coordinator_core/ops/emit/resolvers.py", "resolve_coordinator_root", "<dynamic>", 0),
    ),
    "handoff.archive_transition": (
        ("coordinator_core/archive_stamp.py", "_run_git", "git", 0),
    ),
    "handoff.author_fork": (
        ("coordinator_core/person_resolver.py", "_git_config_uncached", "git", 0),
    ),
    "handoff.columns": (
        ("coordinator_core/ops/emit/sections/handoff_columns.py", "_resolve_shipped_in_dates", "git", 0),
    ),
    "handoff.scaffold_from_queue": (
        ("coordinator_core/person_resolver.py", "_git_config_uncached", "git", 0),
    ),
    "hooks.cater_subagent_start": (
        ("coordinator_core/subagent_sandbox/engine.py", "_resolve_git_root_uncached", "git", 0),
    ),
    "memo.fate_backfill": (
        ("coordinator_core/distill/delete_guard.py", "_git_object_exists", "git", 0),
    ),
    "repo_setup.validate_target_root": (
        ("coordinator_core/ops/bootstrap_repo.py", "_git._invoke", "<dynamic>", 0),
    ),
    "scratchpad.sweep": (
        ("coordinator_core/ops/discover_working_repos.py", "_sort_unique", "sort", 0),
    ),
    "strategic.generate": (
        ("coordinator_core/ops/strategic/version_highlights.py", "_run_git", "git", 0),
    ),
    "tracker.mint_person": (
        ("coordinator_core/person_resolver.py", "_git_config_uncached", "git", 0),
    ),
    "workflow.fire": (
        ("coordinator_core/ops/workflow_fire/fire.py", "_pid_alive", "tasklist", 0),
        ("coordinator_core/ops/workflow_fire/fire.py", "_shim_plugin_dir", "<dynamic>", 0),
        ("coordinator_core/ops/workflow_fire/fire.py", "fire_workflow", "<dynamic>", 0),
    ),
    "workflow.fire_status": (
        ("coordinator_core/ops/workflow_fire/fire.py", "_pid_alive", "tasklist", 0),
    ),
}


def test_cluster_d5_open_disposition_matches_live_measurement():
    """Ratchet for `_CLUSTER_D5_OPEN_DISPOSITION`: re-derives, from the live tree, exactly which
    cluster sites (the 22 D5 files above) each of the 34 D5-open ops' own function-granular
    reachable set contains, and asserts it against the frozen disposition -- byte for byte, per
    op. A site the live tree adds or drops without this dict being updated in the SAME change
    fails here, matching D2/D3/D4's own drift guard. Also asserts the total pair count (45) and
    the entrypoint-resolves-to-a-real-function precondition -- mirroring `test_budgeted_
    entrypoints_resolve_to_live_functions`'s own rot guard for `_BUDGETED_ENTRYPOINTS`, applied
    here to this dict's separate registry."""
    for op_key, (relpath, func_name) in _CLUSTER_D5_OPEN_ENTRYPOINTS.items():
        assert op_key in _CLUSTER_D5_OPEN_DISPOSITION, f"{op_key} has an entrypoint but no disposition entry"
        tree = ast.parse((_REPO_ROOT / relpath).read_text(encoding="utf-8"))
        names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert func_name in names, (
            f"{op_key}'s declared entrypoint {relpath}::{func_name} is not a live top-level "
            "function -- this dict rotted, or the op moved."
        )

    (
        index,
        spawn_sites_by_file,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
    ) = _build_corpus()

    total_pairs = 0
    mismatches: list[str] = []
    for op_key, (relpath, func_name) in _CLUSTER_D5_OPEN_ENTRYPOINTS.items():
        reached = _reachable_functions(
            {(relpath, func_name)},
            index,
            import_aliases_by_file,
            func_aliases_by_file,
            local_aliases_by_file,
        )
        sites = _on_path_spawn_sites(reached, spawn_sites_by_file, exempt=set())
        live_keys = frozenset(
            (site.path, site.enclosing, site.argv0, site.ordinal)
            for site in sites
            if site.path in _CLUSTER_D5_TARGET_FILES
        )
        declared_keys = frozenset(_CLUSTER_D5_OPEN_DISPOSITION[op_key])
        total_pairs += len(declared_keys)
        if live_keys != declared_keys:
            mismatches.append(
                f"  {op_key}: live={sorted(live_keys)} declared={sorted(declared_keys)}"
            )

    assert not mismatches, (
        "_CLUSTER_D5_OPEN_DISPOSITION has drifted from the live tree's own cluster reachability "
        "(re-derive and update the dict, do not silently widen or narrow it):\n" + "\n".join(mismatches)
    )
    assert total_pairs == 38, (
        f"_CLUSTER_D5_OPEN_DISPOSITION now totals {total_pairs} (op, site) pairs, not the "
        "38 left after the 2026-08-30 rot sweep dropped handoff.reconcile_close_terminal's "
        "single pair -- the op is deleted from the tree and absent from ops/_registry_map.py, "
        "so its row could only ever read a missing file. 39 - 1 = 38. Update this constant "
        "deliberately if the shift is real and understood, never to silence a drift you have "
        "not traced."
    )

    for op_key in _CLUSTER_D5_OPEN_DISPOSITION:
        assert op_key not in _BUDGETED_ENTRYPOINTS, (
            f"{op_key} is now enrolled in _BUDGETED_ENTRYPOINTS -- its cluster sites need real "
            "_LEGITIMIZED_SITES entries (route a) instead of living in this open-disposition "
            "registry; remove it from _CLUSTER_D5_OPEN_DISPOSITION/_CLUSTER_D5_OPEN_ENTRYPOINTS "
            "in the same change that enrolls it."
        )


def _module_dotted_name(relpath: str) -> str | None:
    """`coordinator_core/a/b.py` -> `"coordinator_core.a.b"`. `None` for anything outside
    `coordinator_core` (in particular every `coordinator/bin/*.py` script, which is never a real
    importable dotted module in this repo -- those files are loaded via `SourceFileLoader`, not
    `import`, so they can never be the TARGET of a resolved import alias below; they can still be
    a BFS start point, since `func_defs` indexes them regardless of importability)."""
    if not relpath.startswith("coordinator_core/") or not relpath.endswith(".py"):
        return None
    return relpath[:-3].replace("/", ".")


def _module_index(records: list[_FileRecord]) -> dict[str, str]:
    """dotted module name -> relpath, over every in-scope `coordinator_core` file.

    Also registers each package's own bare dotted name (`pkg/__init__.py` -> `"pkg.__init__"`
    AND `"pkg"`) pointing at the same `__init__.py` relpath, additive-only and never overwriting
    an existing key -- a real module legitimately occupying that bare name keeps its own entry.
    Without this second key, `from <pkg> import <fn>` where `<fn>` is defined at module scope in
    `pkg/__init__.py` resolves `module_index.get("<pkg>")` to nothing (see
    `_import_function_aliases`'s own module docstring), silently emptying the BFS seed set at hop
    1 even though the function genuinely exists and may spawn -- closed 2026-08-27, see
    `state/audits/2026-08-27-package-init-resolver-gap-population.md` (62 static aliases / 12
    packages measured; 2 of 279 live ops moved)."""
    out: dict[str, str] = {}
    for record in records:
        dotted = _module_dotted_name(record.relpath)
        if dotted:
            out[dotted] = record.relpath
    for record in records:
        if record.relpath.endswith("/__init__.py"):
            pkg = _package_dotted(record.relpath)
            if pkg and pkg not in out:
                out[pkg] = record.relpath
    return out


def _package_dotted(relpath: str) -> str | None:
    dotted = _module_dotted_name(relpath)
    if dotted is None:
        return None
    parts = dotted.split(".")
    return ".".join(parts[:-1])


def _import_module_aliases(record: _FileRecord, module_index: dict[str, str]) -> dict[str, str]:
    """alias name -> relpath of an in-scope module, for `import X`, `from pkg import X`, and
    `from . import X` (level-0 and level-1 relative only -- deeper relative levels are a named,
    accepted false-negative gap, see module docstring)."""
    out: dict[str, str] = {}
    pkg = _package_dotted(record.relpath)
    for node in ast.walk(record.tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            elif node.level == 1 and pkg is not None:
                base = pkg if not node.module else f"{pkg}.{node.module}"
            else:
                continue
            for alias in node.names:
                dotted = f"{base}.{alias.name}" if base else alias.name
                target = module_index.get(dotted)
                if target:
                    out[alias.asname or alias.name] = target
        elif isinstance(node, ast.Import):
            for alias in node.names:
                target = module_index.get(alias.name)
                if target:
                    out[alias.asname or alias.name.split(".")[0]] = target
    return out


def _import_function_aliases(
    record: _FileRecord,
    module_index: dict[str, str],
    func_defs: typing.Mapping[tuple[str, str], object],
) -> dict[str, tuple[str, str]]:
    """alias name -> `(relpath, func_name)`, for `from X import func` where `func` is a
    top-level FUNCTION defined in X (not a submodule -- that shape is `_import_module_aliases`'s
    job). PRECISE by construction: pinned to the specific module named in the import statement,
    never a repo-wide bare-name lookup.

    This is deliberately NOT the reused index's `imported_names_by_file` +
    repo-wide `funcs_by_name` combination -- that pairing answers "was this name imported from
    SOME module" and "does ANY same-named function anywhere directly spawn," which the one-hop
    routes can afford (each hop still independently re-verifies the resolved function's own body
    via `sites_in_source` before counting a route). This gate does NOT re-verify per hop -- it
    walks the resolved callee's own body for FURTHER calls -- so a same-named-function collision
    at one hop would silently open up that function's ENTIRE unrelated reachable subtree.
    Measured on the first run against the live tree: using the imprecise pairing here inflated
    `ceremony.scoped_git_commit` alone from 0 sites (matching the audit's own CLEAN finding) to
    roughly 40, by resolving common generic names (`_git`, `_run`) to whichever of the dozens of
    unrelated modules defining a same-named helper the repo-wide index happened to list first.
    Pinning resolution to the specific import statement's own named module removed that collision
    class entirely -- see module docstring's measured false-positive section."""
    out: dict[str, tuple[str, str]] = {}
    pkg = _package_dotted(record.relpath)
    for node in ast.walk(record.tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level == 0:
            base = node.module or ""
        elif node.level == 1 and pkg is not None:
            base = pkg if not node.module else f"{pkg}.{node.module}"
        else:
            continue
        target_relpath = module_index.get(base)
        if not target_relpath:
            continue
        for alias in node.names:
            if (target_relpath, alias.name) in func_defs:
                out[alias.asname or alias.name] = (target_relpath, alias.name)
    return out


def _local_module_attr_aliases(
    record: _FileRecord,
    import_aliases: dict[str, str],
    func_defs: typing.Mapping[tuple[str, str], object],
) -> dict[str, tuple[str, str]]:
    """Module-level `name = alias.attr` bindings (C-08 audit op 5, hop 4:
    `_claude_klabauter_root = cli_shared.claude_klabauter_root`), where `alias` resolves to an in-scope module and
    `attr` is one of that module's own top-level functions. One further hop past
    `_import_module_aliases` alone, matching what the audit's own hand trace needed."""
    out: dict[str, tuple[str, str]] = {}
    for node in record.tree.body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
        ):
            continue
        target_relpath = import_aliases.get(node.value.value.id)
        attr = node.value.attr
        if target_relpath and (target_relpath, attr) in func_defs:
            out[node.targets[0].id] = (target_relpath, attr)
    return out


#: `asyncio.to_thread(fn, ...)` / `loop.run_in_executor(None, fn, ...)` -- the attribute name
#: alone is the match, not a tracked `asyncio` import alias: both spellings are stdlib-fixed
#: (`to_thread` is a module-level `asyncio` function, `run_in_executor` an event-loop method with
#: no other plausible same-named attribute in this tree), so matching on attribute name carries
#: the same precision the module docstring asks for without needing an alias table for a name
#: that is never realistically imported under a different spelling.
_THREAD_HOP_ATTRS = frozenset({"to_thread", "run_in_executor"})

#: `(relpath, enclosing top-level function, lineno)` for every thread-hop call (`asyncio.to_thread`
#: / `loop.run_in_executor`) whose callee argument could NOT be statically resolved -- a bare
#: expression, a `getattr`/dynamic dispatch, a `self.`/module attribute the existing resolver does
#: not track, or a missing argument. AC15's own requirement: "a call whose callee argument is not
#: statically resolvable yields no edge and is NOT silently dropped -- it is counted and reported,
#: so the residual gap has a number rather than an assumption." Cleared at the start of every
#: `_build_corpus()` call so a caller always reads counts for the run it just built, never a stale
#: accumulation from a prior test in the same process.
_UNRESOLVED_THREAD_HOP_CALLEES: list[tuple[str, str, int]] = []


def _unresolved_thread_hop_report() -> tuple[tuple[str, str, int], ...]:
    """Sorted snapshot of `_UNRESOLVED_THREAD_HOP_CALLEES` as it stands after the most recent
    `_reachable_functions` walk over a freshly-`_build_corpus()`-built corpus -- the "counted and
    reported" half of AC15's requirement. A `(relpath, enclosing top-level function, lineno)` per
    thread-hop call whose callee argument this gate could not statically resolve; an empty tuple
    is a legitimate, checked answer (every thread-hop call on the walked path resolved), not an
    unasked question."""
    return tuple(sorted(_UNRESOLVED_THREAD_HOP_CALLEES))


def _thread_hop_callee_arg(node: ast.Call, attr: str) -> "ast.expr | None":
    """The AST node holding the callee for a recognised thread-hop call, or `None` if the call
    does not carry enough positional arguments to name one. `to_thread(fn, ...)` names it as the
    first positional argument; `run_in_executor(executor, fn, ...)` names it as the second --
    the executor (often a bare `None`) occupies the first slot."""
    if attr == "to_thread":
        return node.args[0] if node.args else None
    if attr == "run_in_executor":
        return node.args[1] if len(node.args) >= 2 else None
    return None


def _resolve_bare_or_attr_callee(
    callee: ast.expr,
    relpath: str,
    index: _FuncIndex,
    aliases_here: dict[str, str],
    func_aliases_here: dict[str, tuple[str, str]],
    local_here: dict[str, tuple[str, str]],
) -> "tuple[str, str] | None":
    """Resolve a bare `Name` or a `module.attr`-shaped `Attribute` callee expression to a
    `(relpath, func_name)` pair, via the SAME precise resolution `_direct_call_targets` already
    uses for an ordinary call -- shared here so a thread-hop's callee argument is pinned exactly
    as strictly as a direct call's callee, never a looser repo-wide guess. Returns `None` (not
    resolvable under this gate's accepted false-negative gaps -- e.g. `self.method`, a subscript,
    a call result) rather than raising."""
    if isinstance(callee, ast.Name):
        name = callee.id
        if (relpath, name) in index.func_defs:
            return (relpath, name)
        if name in local_here:
            return local_here[name]
        if name in func_aliases_here:
            return func_aliases_here[name]
        return None
    if isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name):
        target_relpath = aliases_here.get(callee.value.id)
        if target_relpath and (target_relpath, callee.attr) in index.func_defs:
            return (target_relpath, callee.attr)
        return None
    return None


def _module_callable_tables(
    record: "_FileRecord",
    index: _FuncIndex,
    aliases_here: dict[str, str],
    func_aliases_here: dict[str, tuple[str, str]],
    local_here: dict[str, tuple[str, str]],
) -> dict[str, frozenset[tuple[str, str]]]:
    """Module-level `NAME = {...}` / `NAME: T = {...}` (also `[...]`/`(...)`/`{...}`-set)
    literal bindings whose members resolve to top-level functions this corpus already knows --
    a BY-REFERENCE dispatch table (`coordinator_core/merge_assemble/apply.py::_CLI_DISPATCH` is
    the oracle: a closed `dict[str, Callable]` passed BY REFERENCE into `apply_base.
    execute_directives`, which invokes its values by key lookup, never by a literal `ast.Call`
    this walker's ordinary one-hop edge already sees).

    Each container element is resolved through `_resolve_bare_or_attr_callee` -- the SAME
    precise, same-file-or-tracked-alias-only resolution every other hop in this gate uses, never
    a looser repo-wide bare-name lookup (module docstring's measured false-positive section: that
    class of imprecision inflated one op's reachable set from 0 to ~40 on its first live run). A
    member that does not resolve is skipped -- an accepted false negative, named, never a
    fall-back to a looser lookup. A table with zero resolving members is omitted entirely."""
    out: dict[str, frozenset[tuple[str, str]]] = {}
    for node in record.tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target_name = node.targets[0].id
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            target_name = node.target.id
            value = node.value
        else:
            continue

        if isinstance(value, ast.Dict):
            elements = list(value.values)
        elif isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            elements = list(value.elts)
        else:
            continue

        members: set[tuple[str, str]] = set()
        for elt in elements:
            resolved = _resolve_bare_or_attr_callee(
                elt, record.relpath, index, aliases_here, func_aliases_here, local_here
            )
            if resolved is not None:
                members.add(resolved)
        if members:
            out[target_name] = frozenset(members)
    return out


def _module_callable_tables_by_file(
    records: list["_FileRecord"],
    index: _FuncIndex,
    import_aliases_by_file: dict[str, dict[str, str]],
    func_aliases_by_file: dict[str, dict[str, tuple[str, str]]],
    local_aliases_by_file: dict[str, dict[str, tuple[str, str]]],
) -> dict[str, dict[str, frozenset[tuple[str, str]]]]:
    """`_module_callable_tables` over every file in `records`, keyed by relpath. A file with no
    recognised table is simply absent from the result (never an empty-dict placeholder)."""
    out: dict[str, dict[str, frozenset[tuple[str, str]]]] = {}
    for record in records:
        tables = _module_callable_tables(
            record,
            index,
            import_aliases_by_file.get(record.relpath, {}),
            func_aliases_by_file.get(record.relpath, {}),
            local_aliases_by_file.get(record.relpath, {}),
        )
        if tables:
            out[record.relpath] = tables
    return out


def _import_table_aliases(
    record: "_FileRecord",
    module_index: dict[str, str],
    module_callable_tables_by_file: dict[str, dict[str, frozenset[tuple[str, str]]]],
) -> dict[str, tuple[str, str]]:
    """alias name -> `(relpath, table_name)`, for `from X import TABLE` where `TABLE` is a
    module-level callable-container table `_module_callable_tables` recognises in `X`. Mirrors
    `_import_function_aliases`'s precision contract exactly -- pinned to the specific module
    named in the import statement, never a repo-wide bare-name lookup -- so a table referenced
    across modules resolves the same strict way a cross-module function callee does."""
    out: dict[str, tuple[str, str]] = {}
    pkg = _package_dotted(record.relpath)
    for node in ast.walk(record.tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level == 0:
            base = node.module or ""
        elif node.level == 1 and pkg is not None:
            base = pkg if not node.module else f"{pkg}.{node.module}"
        else:
            continue
        target_relpath = module_index.get(base)
        if not target_relpath:
            continue
        tables_there = module_callable_tables_by_file.get(target_relpath, {})
        for alias in node.names:
            if alias.name in tables_there:
                out[alias.asname or alias.name] = (target_relpath, alias.name)
    return out


def _direct_call_targets(
    func_node,
    relpath: str,
    index: _FuncIndex,
    import_aliases_by_file: dict[str, dict[str, str]],
    func_aliases_by_file: dict[str, dict[str, tuple[str, str]]],
    local_aliases_by_file: dict[str, dict[str, tuple[str, str]]],
    module_callable_tables_by_file: "dict[str, dict[str, frozenset[tuple[str, str]]]] | None" = None,
    table_aliases_by_file: "dict[str, dict[str, tuple[str, str]]] | None" = None,
) -> set[tuple[str, str]]:
    """Every top-level `(relpath, func_name)` `func_node`'s body calls, one hop: same-module
    direct call, a PRECISE cross-module function import (`_import_function_aliases` -- pinned to
    the specific module named in the `from X import name` statement, never a repo-wide bare-name
    lookup; see that function's own docstring for why the imprecise version is unusable here),
    a module-level local-name-to-attribute alias (`_local_module_attr_aliases`), or a direct
    `module.attr(...)` attribute call through a tracked module-import alias
    (`_import_module_aliases`). ALSO resolves a thread-hop call (`asyncio.to_thread(fn, ...)`,
    `loop.run_in_executor(None, fn, ...)`) by taking its callee ARGUMENT through the same
    resolver (`_resolve_bare_or_attr_callee`) -- see `_UNRESOLVED_THREAD_HOP_CALLEES` for what
    happens when that argument is not statically resolvable.

    `module_callable_tables_by_file`/`table_aliases_by_file` add ONE more edge kind (this
    chunk): a BY-REFERENCE dispatch table LOADED as a plain `ast.Name` (not called) anywhere in
    `func_node`'s body reaches every function that table's own `_module_callable_tables` entry
    resolved -- same-file table first, then a precise cross-module import alias
    (`_import_table_aliases`), so a same-named local never collides with an imported table. Both
    default to `None` (treated as empty) so every EXISTING caller that does not pass them keeps
    today's byte-for-byte behaviour -- this is a strictly additive edge, never a replacement for
    the call-based edges above."""
    out: set[tuple[str, str]] = set()
    aliases_here = import_aliases_by_file.get(relpath, {})
    func_aliases_here = func_aliases_by_file.get(relpath, {})
    local_here = local_aliases_by_file.get(relpath, {})
    tables_here = (module_callable_tables_by_file or {}).get(relpath, {})
    table_aliases_here = (table_aliases_by_file or {}).get(relpath, {})
    for node in ast.walk(func_node):
        if node is func_node:
            continue
        if isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Name):
                name = callee.id
                if (relpath, name) in index.func_defs:
                    out.add((relpath, name))
                elif name in local_here:
                    out.add(local_here[name])
                elif name in func_aliases_here:
                    out.add(func_aliases_here[name])
            elif isinstance(callee, ast.Attribute):
                if isinstance(callee.value, ast.Name):
                    target_relpath = aliases_here.get(callee.value.id)
                    if target_relpath and (target_relpath, callee.attr) in index.func_defs:
                        out.add((target_relpath, callee.attr))
                if callee.attr in _THREAD_HOP_ATTRS:
                    arg = _thread_hop_callee_arg(node, callee.attr)
                    resolved = (
                        _resolve_bare_or_attr_callee(
                            arg, relpath, index, aliases_here, func_aliases_here, local_here
                        )
                        if arg is not None
                        else None
                    )
                    if resolved is not None:
                        out.add(resolved)
                    else:
                        _UNRESOLVED_THREAD_HOP_CALLEES.append(
                            (relpath, func_node.name, node.lineno)
                        )
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name = node.id
            if name in tables_here:
                out.update(tables_here[name])
            elif name in table_aliases_here:
                t_relpath, t_name = table_aliases_here[name]
                out.update(
                    (module_callable_tables_by_file or {}).get(t_relpath, {}).get(t_name, frozenset())
                )
    return out


def _reachable_functions(
    entry_funcs: set[tuple[str, str]],
    index: _FuncIndex,
    import_aliases_by_file: dict[str, dict[str, str]],
    func_aliases_by_file: dict[str, dict[str, tuple[str, str]]],
    local_aliases_by_file: dict[str, dict[str, tuple[str, str]]],
    module_callable_tables_by_file: "dict[str, dict[str, frozenset[tuple[str, str]]]] | None" = None,
    table_aliases_by_file: "dict[str, dict[str, tuple[str, str]]] | None" = None,
) -> set[tuple[str, str]]:
    """Transitive closure (plain worklist BFS) over `_direct_call_targets`'s one-hop edges,
    seeded at `entry_funcs`. Terminates: `seen` grows monotonically over the finite domain of
    `(relpath, func_name)` pairs `index.func_defs` defines, so a round that adds nothing halts
    the loop -- same termination argument the reused module's own route-g fixed point makes.

    `module_callable_tables_by_file`/`table_aliases_by_file` (this chunk) are threaded straight
    through to `_direct_call_targets` -- see that function's own docstring. Both default to
    `None`, so an EXISTING caller passing only the first five arguments walks exactly the edges
    it always has."""
    seen: set[tuple[str, str]] = set(entry_funcs)
    queue: list[tuple[str, str]] = list(entry_funcs)
    while queue:
        current = queue.pop()
        node = index.func_defs.get(current)
        if node is None:
            continue
        for target in _direct_call_targets(
            node,
            current[0],
            index,
            import_aliases_by_file,
            func_aliases_by_file,
            local_aliases_by_file,
            module_callable_tables_by_file,
            table_aliases_by_file,
        ):
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return seen


def _on_path_spawn_sites(
    reached_funcs: set[tuple[str, str]],
    spawn_sites_by_file: dict[str, list],
    exempt: typing.Container[tuple[str, str, str, int]],
) -> list:
    """Every `spawn_policy` site whose own top-level enclosing function is in `reached_funcs`,
    minus `_LEGITIMIZED_SITES`. Function granularity, not whole-file -- see module docstring's
    "FUNCTION GRANULARITY" section for why. `exempt` is membership-tested only, so the live
    `_LEGITIMIZED_SITES` mapping and the planted fixtures' bare key sets both satisfy it."""
    reached_names_by_file: dict[str, set[str]] = {}
    for relpath, name in reached_funcs:
        reached_names_by_file.setdefault(relpath, set()).add(name)
    out = []
    for relpath, names in reached_names_by_file.items():
        for site in spawn_sites_by_file.get(relpath, []):
            top_enclosing = site.enclosing.split(".")[0]
            if top_enclosing not in names:
                continue
            key = (relpath, site.enclosing, site.argv0, site.ordinal)
            if key in exempt:
                continue
            out.append(site)
    return out


def _scope_roots() -> tuple[pathlib.Path, ...]:
    return tuple(_REPO_ROOT / root for root in _GATE_SCOPE_ROOTS)


def _build_corpus_with_dispatch_tables(*, _with_tables: bool = True):
    """The full corpus build: everything `_build_corpus()` returns, PLUS the by-reference
    dispatch-table indexes this chunk adds (`module_callable_tables_by_file`,
    `table_aliases_by_file`). `_build_corpus()` is a stable slice of this same computation --
    refactored to share the scan rather than pay it twice -- so every EXISTING caller of
    `_build_corpus()` keeps its exact 5-tuple return shape and cost. Returns `(index,
    spawn_sites_by_file, import_aliases_by_file, func_aliases_by_file, local_aliases_by_file,
    module_callable_tables_by_file, table_aliases_by_file)`.

    Clears `_UNRESOLVED_THREAD_HOP_CALLEES` at the start of every build -- `_reachable_functions`
    calls made against this corpus append to that list as they walk, and a caller inspecting it
    (`_unresolved_thread_hop_report`) after its own BFS is reading counts for the run it just
    built, never a stale accumulation left over from an earlier corpus/test in the same process."""
    _UNRESOLVED_THREAD_HOP_CALLEES.clear()
    files = _discover_scope_files(_scope_roots())
    records = _load_file_records(files)
    index = _build_func_index(records)
    module_index = _module_index(records)

    import_aliases_by_file: dict[str, dict[str, str]] = {}
    for record in records:
        import_aliases_by_file[record.relpath] = _import_module_aliases(record, module_index)

    func_aliases_by_file: dict[str, dict[str, tuple[str, str]]] = {}
    for record in records:
        func_aliases_by_file[record.relpath] = _import_function_aliases(
            record, module_index, index.func_defs
        )

    local_aliases_by_file: dict[str, dict[str, tuple[str, str]]] = {}
    for record in records:
        local_aliases_by_file[record.relpath] = _local_module_attr_aliases(
            record, import_aliases_by_file[record.relpath], index.func_defs
        )

    # OPT-IN, not unconditional. Indexing every module-level callable table costs ~1.06s of
    # process time over this repo's ~1624 scope files, and `_build_corpus()`'s ~10 existing
    # callers do not consume the tables. Paying it for them would put a shared helper ~1s over
    # a path that DR-344 already holds to 500ms end-to-end and that this module's own docstring
    # records as having had a cold-scan problem. `_with_tables=False` returns the two table
    # indexes EMPTY rather than omitting them, so the 7-tuple shape is stable for every caller.
    module_callable_tables_by_file: dict[str, dict[str, frozenset[tuple[str, str]]]] = {}
    table_aliases_by_file: dict[str, dict[str, tuple[str, str]]] = {}
    if _with_tables:
        module_callable_tables_by_file = _module_callable_tables_by_file(
            records, index, import_aliases_by_file, func_aliases_by_file, local_aliases_by_file
        )
        for record in records:
            table_aliases_by_file[record.relpath] = _import_table_aliases(
                record, module_index, module_callable_tables_by_file
            )

    spawn_sites_by_file = {record.relpath: record.spawn_sites for record in records}
    return (
        index,
        spawn_sites_by_file,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
        module_callable_tables_by_file,
        table_aliases_by_file,
    )


def _build_corpus():
    """One shared corpus build: scope files, `_FileRecord`s, the reused repo-wide `_FuncIndex`,
    and this gate's own import/local-alias indexes, each computed exactly once. Returns
    `(index, spawn_sites_by_file, import_aliases_by_file, func_aliases_by_file,
    local_aliases_by_file)` -- a stable slice of `_build_corpus_with_dispatch_tables()`, which
    also computes the by-reference dispatch-table indexes this chunk adds. Kept as its own name
    (rather than inlining a `[:5]` at every call site) so every EXISTING caller's signature and
    cost stay unchanged.

    The cost half of that claim is what `_with_tables=False` buys. Sharing the scan was right;
    the first version of this shared it by making every caller pay the ~1.06s table-indexing pass
    whether or not it read the tables, which is the opposite of the promise this docstring makes.
    The tables are now opt-in, so a caller that takes the 5-tuple slice pays exactly what it paid
    before the dispatch-table edge existed."""
    return _build_corpus_with_dispatch_tables(_with_tables=False)[:5]


def _format_violation(op_key: str, site) -> str:
    return (
        f"  [{op_key}] {site.path}:{site.lineno} enclosing={site.enclosing!r} "
        f"argv0={site.argv0!r} ordinal={site.ordinal}"
    )


def test_budgeted_entrypoints_resolve_to_live_functions():
    """Registry-rot guard: every `_BUDGETED_ENTRYPOINTS` row must resolve to an actual top-level
    function definition, checked fresh against the live tree -- never trusted from a comment. A
    row whose subject no longer exists fails loudly here, the same defect class C2 closes for the
    manifest's own orphan-row gap on a different file."""
    index, _sites, _imp, _func_imp, _loc = _build_corpus()
    missing = []
    for op_key, (relpath, func_names) in _BUDGETED_ENTRYPOINTS.items():
        for func_name in func_names:
            if (relpath, func_name) not in index.func_defs:
                missing.append(f"{op_key}: {relpath}::{func_name}")
    assert not missing, (
        "budgeted entrypoint(s) no longer resolve to a live top-level function -- "
        "the manifest row's subject moved, was renamed, or was deleted:\n"
        + "\n".join(missing)
    )

    orphaned = sorted(
        f"  {key[0]} -- {key[1]}::{key[2]}"
        for key in _LEGITIMIZED_SITES
        if key[0] not in _BUDGETED_ENTRYPOINTS
    )
    assert not orphaned, (
        "legitimized site(s) name an op that is not a live budgeted entrypoint. An exemption "
        "whose op key no longer matches is dead weight that suppresses nothing today and would "
        "silently start suppressing if the key were ever reused:\n" + "\n".join(orphaned)
    )


def test_registry_divergence_and_residual_stay_accounted():
    """AC6 -- the completeness guard for C2a's widening, taken against the LIVE registry, never
    the static `_BUDGETED_ENTRYPOINTS` view alone: a residual computed from the static view would
    measure its own blind spot with its own blind spot, the same empty-key-reads-as-none defect
    a predecessor session had to defend, relocated one stage earlier (EM adjudication, this
    file's own dispatch brief).

    Two checks, in order:

    1. `spawn_bearing_ops.registry_divergence()`, REUSED not re-derived -- the loud comparison
       between the authoritative live registry and the hand-maintained fast-path map. Any
       disagreement here means neither set can be trusted for enrolment and must be reconciled
       before anything downstream of it means what it claims to.

    2. The RESIDUAL -- every live op NOT enrolled in `_BUDGETED_ENTRYPOINTS` -- must contain no
       op whose function-granular reachable spawn set is EMPTY. An empty-evidence op has nothing
       to legitimize (EM adjudication step 2: it needs zero `_LEGITIMIZED_SITES` entries and zero
       invented budget tests), so its enrolment is mechanical, not a judgment call, and finding
       one unenrolled here is the ratchet firing on a newly-registered op this file has not
       caught up to yet -- not a defect in the residual itself. Ops the residual DOES carry (a
       non-empty function-granular reachable spawn set) are C2b's partition to disposition, per
       the same adjudication, and are not asserted on further here.

    `coordinator_core.hooks._eager_import_all()` is called before the registry read below --
    peer commit `117d960ec` moved `coordinator_core.hooks` off package-import-time registration
    to an on-miss lazy fallback in `ipc`, so a bare `import coordinator_core.ops` now legitimately
    leaves every `hooks.*` op unregistered until something misses on one. Not map rot, not this
    plan's ops going stale -- the completeness guard's own read was blind to the peer's (correct)
    lazy-import change. Do not remove this call to "fix" a future green-without-it: the lazy
    behaviour is intentional and this eager call is what makes THIS gate's registry read complete
    despite it (this file's own dispatch brief, C7)."""
    _hooks._eager_import_all()
    div = spawn_bearing_ops.registry_divergence()
    assert div.agrees, (
        "the fast-path OP_MODULE_MAP and the live registry disagree -- only_in_live="
        f"{sorted(div.only_in_live)} only_in_fast_path={sorted(div.only_in_fast_path)}. "
        "Reconcile the fast path (coordinator_core/ops/_registry_map.py) before trusting either "
        "set for enrolment; a residual computed against either alone would be blind to this."
    )

    live = spawn_bearing_ops.live_registry_op_names()
    entrypoints = spawn_bearing_ops.resolve_op_entrypoints(live)
    evidence = spawn_bearing_ops.ops_with_spawn_evidence(entrypoints, function_granular=True)

    enrolled = frozenset(_BUDGETED_ENTRYPOINTS)
    residual = live - enrolled

    should_have_been_enrolled = sorted(
        name
        for name in residual
        if name not in evidence
        and name not in _KNOWN_RESOLVER_GAP_OPS
        and entrypoints[name].relpath is not None
        and entrypoints[name].function_name is not None
    )
    assert not should_have_been_enrolled, (
        f"{len(should_have_been_enrolled)} live op(s) resolve to a function-granular reachable "
        "spawn set that is EMPTY and are not yet enrolled in _BUDGETED_ENTRYPOINTS -- an op that "
        "reaches no spawn site needs zero legitimization and should be enrolled directly (EM "
        "adjudication step 2, this file's own dispatch brief), not left for the residual:\n"
        + "\n".join(f"  {name}" for name in should_have_been_enrolled)
    )


#: Dotted spawn callees `spawn_policy.detect` recognises, as they appear in a call's own AST. The
#: mechanism pin is checked against this set rather than against `subprocess.run` alone so that a
#: swap to any OTHER recognised mechanism is reported as a mechanism change (an unrecognised
#: callee is not a spawn site at all, so the site would leave the gate's input entirely and the
#: stale-entry half of `test_legitimized_site_mechanism_pins_hold` catches it instead).
_RECOGNISED_SPAWN_CALLEES = frozenset(
    {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.check_call",
        "subprocess.check_output",
        "os.system",
        "os.execv",
        "os.posix_spawn",
        "os.posix_spawnp",
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
    }
)


def _dotted_callee(node: ast.expr) -> str:
    """`ast.Attribute`/`ast.Name` chain -> its dotted source text; `""` for anything else (a call
    on a subscript, a call result, a lambda). Not a resolver -- it reads the call as written, which
    is exactly what a mechanism pin needs to compare against."""
    if isinstance(node, ast.Attribute):
        base = _dotted_callee(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _spawn_callees_in_function(relpath: str, top_enclosing: str) -> set[str]:
    """Every recognised spawn callee, as written, inside one top-level function."""
    tree = ast.parse((_REPO_ROOT / relpath).read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != top_enclosing:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                callee = _dotted_callee(sub.func)
                if callee in _RECOGNISED_SPAWN_CALLEES:
                    out.add(callee)
    return out


def test_legitimized_site_mechanism_pins_hold():
    """The assertion that makes a `_GLOBAL_SUBPROCESS_RUN` legitimation structural rather than
    incidental -- and the reason this gate can admit a counter shape its pre-2026-08-19 text
    refused outright.

    That counter sees a call only because it substitutes the `subprocess.run` module attribute.
    `spawn_policy.site_key` is `(path, enclosing, argv0, ordinal)` and carries NO mechanism
    component, so editing a legitimized site from `subprocess.run` to `subprocess.Popen` (or
    `os.posix_spawn`, or `asyncio.create_subprocess_exec` -- every one of them a site
    `spawn_policy.detect` still recognises, so the site does not disappear) keeps the key
    byte-identical: the exemption would silently outlive the counter that earned it, and the op's
    budget would stop counting a spawn while still claiming to. THAT, not the width of the patch,
    is the real defect in resting on a global counter, and pinning the mechanism is what closes it.

    Checked against the enclosing function's FULL callee set, not one ordinal-matched call, so an
    added second spawn of a different mechanism fails too. Also fails on a stale entry whose site
    no longer spawns at all -- an exemption for a site that has gone is an exemption that can
    silently start covering a future one that reuses the name.

    `_SEAM` entries are deliberately exempt from the pin: a function-OBJECT substitution wraps the
    name, so it keeps counting whatever the body spawns with. Their hole is routing, not mechanism,
    and no static pin addresses it -- the seven open `ceremony.scoped_git_commit` bypasses are that
    hole, and they are on the gate's red list rather than papered over here.

    Checked against `_MECHANISM_PINS`, keyed on `leg.counter`, not a single hardcoded string
    (opro-03 follow-up, 2026-08-21): `_GLOBAL_SUBPROCESS_SPAWN` widens the pin to admit BOTH
    `subprocess.run` and `subprocess.Popen`, because its own companion counter
    (`test_commit_e2e_spawn_budget.py::_count_op_spawns_both_ways`) watches both -- see that
    dict's own docstring. The comparison is SUBSET membership (`found <= pin`), which is a pure
    generalization of the old exact-equality check: for every singleton pin already in the dict
    (`_GLOBAL_SUBPROCESS_RUN`), `found <= pin and found` means exactly `found == pin`, so no
    existing `_GLOBAL_SUBPROCESS_RUN` entry's guarantee is loosened by this generalization."""
    drifted: list[str] = []
    for (_op_key, relpath, enclosing, _argv0, _ordinal), leg in _LEGITIMIZED_SITES.items():
        pin = _MECHANISM_PINS.get(leg.counter)
        if pin is None:
            continue
        found = _spawn_callees_in_function(relpath, enclosing.split(".")[0])
        if not found:
            drifted.append(
                f"  {relpath}::{enclosing} -- legitimized against a global "
                f"{sorted(pin)} counter, but no recognised spawn call remains in that "
                f"function. Stale exemption: remove it, or re-earn it for whatever replaced it."
            )
        elif not found <= pin:
            drifted.append(
                f"  {relpath}::{enclosing} -- legitimized against a global {sorted(pin)} "
                f"counter, but this function now spawns via {sorted(found)}. That counter does "
                f"not see those, so the site is no longer counted while its exemption says it is. "
                f"Route it back through {sorted(pin)}, or re-legitimize it against a counter "
                f"that actually observes the new mechanism ({leg.counted_by})."
            )
    assert not drifted, (
        f"{len(drifted)} legitimized site(s) drifted off the spawn mechanism their counter "
        "patches:\n" + "\n".join(drifted)
    )


def test_plant_mechanism_drift_is_detected(tmp_path, monkeypatch):
    """The planted counterpart to `test_legitimized_site_mechanism_pins_hold` -- that test scans
    the live tree and passes today, which on its own proves only that nothing has drifted YET, not
    that drift would be caught. Plants each way a legitimized site can slip out from under a global
    `subprocess.run` counter and asserts the helper reports it.

    Mirrors this module's existing planted RED/GREEN fixture: a gate whose red path is never
    exercised is a gate nobody knows is wired up."""
    fixture = tmp_path / "planted.py"
    monkeypatch.setattr(sys.modules[__name__], "_REPO_ROOT", tmp_path)

    fixture.write_text(
        "import subprocess\n"
        "def _sole_seam():\n"
        "    return subprocess.run(['git', 'status'])\n",
        encoding="utf-8",
    )
    assert _spawn_callees_in_function("planted.py", "_sole_seam") == {_MECHANISM_PIN}, (
        "baseline: an unmodified `subprocess.run` site must read as exactly the pinned mechanism, "
        "or every assertion below is vacuous"
    )

    fixture.write_text(
        "import subprocess\n"
        "def _sole_seam():\n"
        "    return subprocess.Popen(['git', 'status'])\n",
        encoding="utf-8",
    )
    swapped = _spawn_callees_in_function("planted.py", "_sole_seam")
    assert swapped == {"subprocess.Popen"}, swapped
    assert swapped != {_MECHANISM_PIN}, (
        "an in-place mechanism swap must be visible -- `spawn_policy.site_key` is unchanged by it, "
        "so this helper is the only thing standing between the swap and a silently-stale exemption"
    )

    fixture.write_text(
        "import subprocess\n"
        "def _sole_seam():\n"
        "    subprocess.run(['git', 'status'])\n"
        "    return subprocess.Popen(['git', 'log'])\n",
        encoding="utf-8",
    )
    added = _spawn_callees_in_function("planted.py", "_sole_seam")
    assert added == {_MECHANISM_PIN, "subprocess.Popen"} and added != {_MECHANISM_PIN}, added

    fixture.write_text(
        "def _sole_seam():\n    return None\n",
        encoding="utf-8",
    )
    assert _spawn_callees_in_function("planted.py", "_sole_seam") == set(), (
        "a site that stopped spawning must read empty, so the stale-exemption branch fires"
    )


def test_no_uncounted_spawn_reachable_from_a_budgeted_entrypoint():
    """The C-13 gate, now STANDING. For each live budgeted entrypoint in `_BUDGETED_ENTRYPOINTS`
    (nine hand-verified rows plus, since C2a, 175 rows enrolled on a measured-empty reachable
    set), every `spawn_policy`-detected spawn site whose enclosing function is transitively
    reachable from that entrypoint must carry a `_LEGITIMIZED_SITES` entry for THAT op. The 175
    C2a rows are expected to always pass this trivially -- their whole enrolment criterion IS an
    empty on-path spawn-site set (see `_BUDGETED_ENTRYPOINTS`'s own C2a widening comment); a
    non-trivial pass on one of them would mean the site count changed since enrolment and the row
    needs re-measuring, not that the gate is redundant for it.

    This carried `designed_red` from its landing until 2026-08-19, as a standing worklist rather
    than a tier-breaking failure. The marker came off when the list emptied, which its own text
    named as the definition of C6 being done -- and the condition it attached to that removal
    holds: `_LEGITIMIZED_SITES` was never populated wholesale to buy a green. It went 18 -> 0
    entry by entry, and the four routes out are worth keeping distinct, because only the first is
    the one a reader assumes:

      1. GIVE THE SITE'S BRANCH ITS PRECONDITION. The three `auto_push` conditional-path sites
         (`push_once`, `_is_ancestor`, `_invoke_cockpit_publish`) were reachable and would have
         been counted the moment they ran; no fixture took their branch. One shape
         (`pending_drain_superseded`) reaches all three.
      2. GIVE THE OP A SHAPE AT ALL. `sibling_committed_chunk_ids_memo`'s only budgeted shape was
         `second_call_identical_inputs: 0`, the memo hit. A shape that spawns nothing by
         construction can never make ANY site visible to a counter, so its `_run_git` was
         undischargeable for want of a shape, not a precondition. (That op, and the mechanism it
         budgeted, was deleted outright in 2026-08-21 C3 -- this route's example no longer has a
         live `_BUDGETED_ENTRYPOINTS` row, kept here as the historical record of how it was
         closed before it was cut.)
      3. GIVE THE OP A COUNTER THAT CAN SEE THE SITE. The first three `scoped_git_commit`
         bypasses left this way, via `op_total_*` keys counting `subprocess.run` globally rather
         than through the routing-narrow `git_native._git` seam.
      4. DELETE THE SPAWN. The last four `repo_root::_spawn_rev_parse` instances left this way,
         and this is the disposition to reach for FIRST. `show_toplevel`'s fallback had no case in
         which it returned a right answer the walk had not already produced -- with `GIT_DIR` set
         it reported the CWD as the toplevel, which is wrong, and hooks are where `GIT_DIR` is
         set. `git_dir`/`git_common_dir`'s existed only for the bare repo, now answered by
         `_looks_like_git_dir` from the same filesystem markers git's own `is_git_directory()`
         uses. `absolute_git_dir` always spawned on the claim that the walk-based pair "does not
         separately expose" the private gitdir, which `git_dir()` does; measured against real git
         in a plain repo, a subdirectory and a linked worktree, the two agree.

    A gate that can only be satisfied by accounting for a spawn teaches the wrong lesson. Three
    of the four routes above make a spawn visible; the fourth removes the reason it existed. When
    a site lands on this list, ask in that order and stop at the first that applies.

    Every `executed` leg cites a DIRECT measurement -- a call counter asserted at one
    or more, or a
    stack-recording `subprocess.run` wrapper attributing each spawn to its origin frame -- never
    an inference from a spawn total matching a budget. That is the leg neither the mechanism nor
    the assertion shape establishes, and the one this gate cannot check statically (see "WHAT
    THIS GATE DELIBERATELY DOES NOT CATCH").

    The five tests around this one gate normally too: the entrypoint registry must resolve, no
    exemption may name a dead op or drift off its counter's spawn mechanism, and the planted
    RED/GREEN fixtures must hold. Those are what prove this module still detects anything.
    """
    (
        index,
        spawn_sites_by_file,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
    ) = _build_corpus()

    all_violations: list[tuple[str, object]] = []
    for op_key, (relpath, func_names) in _BUDGETED_ENTRYPOINTS.items():
        entry_funcs = {(relpath, func_name) for func_name in func_names}
        reached = _reachable_functions(
            entry_funcs,
            index,
            import_aliases_by_file,
            func_aliases_by_file,
            local_aliases_by_file,
        )
        exempt_here = {
            key[1:] for key in _LEGITIMIZED_SITES if key[0] == op_key
        }
        for site in _on_path_spawn_sites(reached, spawn_sites_by_file, exempt_here):
            all_violations.append((op_key, site))

    assert not all_violations, (
        f"{len(all_violations)} spawn site(s) reachable from a budgeted entrypoint are not "
        "visible to that op's counter (no _LEGITIMIZED_SITES entry):\n"
        + "\n".join(_format_violation(op_key, site) for op_key, site in all_violations)
    )


# --------------------------------------------------------------------------
# Planted-fixture self-tests -- prove the transitive predicate on a synthetic
# corpus, independent of the live tree's own current pass/fail state.
# --------------------------------------------------------------------------


def test_plant_multi_hop_spawn_is_flagged_red_then_removed_is_green(tmp_path):
    """Proves the ONE thing this gate does that the reused one-hop index cannot: a spawn reached
    only through a THREE-hop chain (entry -> local helper -> imported cross-module function ->
    module-import-alias attribute call) is flagged. RED with the spawn planted, GREEN once
    removed -- the module docstring's own non-negotiable ("prove the gate bites")."""
    entry_mod = tmp_path / "entry.py"
    helper_mod = tmp_path / "helper.py"
    wrapper_mod = tmp_path / "wrapper.py"

    entry_mod.write_text(
        "import helper\n"
        "\n"
        "def _entry(rows):\n"
        "    return helper.stage(rows)\n",
        encoding="utf-8",
    )
    helper_mod.write_text(
        "import wrapper\n"
        "\n"
        "def stage(rows):\n"
        "    return _local_hop(rows)\n"
        "\n"
        "def _local_hop(rows):\n"
        "    return wrapper.run_git(rows)\n",
        encoding="utf-8",
    )
    # PLANT: a bare spawn three hops from `_entry` (entry -> stage -> _local_hop -> run_git).
    wrapper_mod.write_text(
        "import subprocess\n"
        "\n"
        "def run_git(rows):\n"
        "    return subprocess.run(['git', 'status'], cwd='/repo')\n",
        encoding="utf-8",
    )

    files = _discover_scope_files((tmp_path,))
    records = _load_file_records(files)
    index = _build_func_index(records)
    module_index = _module_index_for_test(records)
    import_aliases_by_file = {
        r.relpath: _import_module_aliases(r, module_index) for r in records
    }
    func_aliases_by_file = {
        r.relpath: _import_function_aliases(r, module_index, index.func_defs) for r in records
    }
    local_aliases_by_file = {
        r.relpath: _local_module_attr_aliases(r, import_aliases_by_file[r.relpath], index.func_defs)
        for r in records
    }
    spawn_sites_by_file = {r.relpath: r.spawn_sites for r in records}

    reached = _reachable_functions(
        {("entry.py", "_entry")},
        index,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
    )
    assert ("wrapper.py", "run_git") in reached, (
        "transitive BFS did not reach the 3-hop callee -- the predicate this gate exists to add "
        "is not working"
    )
    violations = _on_path_spawn_sites(reached, spawn_sites_by_file, set())
    assert len(violations) == 1
    assert violations[0].path == "wrapper.py"
    assert violations[0].enclosing == "run_git"

    # REMOVE: neutralize the spawn, prove the same predicate goes green.
    wrapper_mod.write_text(
        "def run_git(rows):\n"
        "    return len(rows)\n",
        encoding="utf-8",
    )
    files2 = _discover_scope_files((tmp_path,))
    records2 = _load_file_records(files2)
    index2 = _build_func_index(records2)
    module_index2 = _module_index_for_test(records2)
    import_aliases_by_file2 = {
        r.relpath: _import_module_aliases(r, module_index2) for r in records2
    }
    func_aliases_by_file2 = {
        r.relpath: _import_function_aliases(r, module_index2, index2.func_defs) for r in records2
    }
    local_aliases_by_file2 = {
        r.relpath: _local_module_attr_aliases(r, import_aliases_by_file2[r.relpath], index2.func_defs)
        for r in records2
    }
    spawn_sites_by_file2 = {r.relpath: r.spawn_sites for r in records2}
    reached2 = _reachable_functions(
        {("entry.py", "_entry")},
        index2,
        import_aliases_by_file2,
        func_aliases_by_file2,
        local_aliases_by_file2,
    )
    violations2 = _on_path_spawn_sites(reached2, spawn_sites_by_file2, set())
    assert violations2 == []


def _module_index_for_test(records: list[_FileRecord]) -> dict[str, str]:
    """`_module_index` is restricted to `coordinator_core/...` relpaths (module docstring's
    `_module_dotted_name`), which a bare `tmp_path` fixture (relpaths like `"entry.py"`,
    `"wrapper.py"`) never matches. This test-only variant maps every discovered file's own
    basename-minus-`.py` as its dotted name, matching the flat single-directory fixture shape the
    self-test above builds (`import helper` / `import wrapper` at top level, no package).
    Deliberately NOT given `_module_index`'s package-`__init__` bare-name key (2026-08-27): every
    fixture built against this variant is a flat single-directory tree with no `pkg/__init__.py`
    shape, so the gap that extra pass closes never arises here -- adding it would be an untested
    no-op, not parity."""
    out: dict[str, str] = {}
    for record in records:
        if record.relpath.endswith(".py"):
            out[record.relpath[:-3]] = record.relpath
    return out


def test_reachable_functions_follows_by_reference_dispatch_table(tmp_path):
    """D8 self-contained planted fixture, pinned independently of the live tree's own
    `merge_assemble.apply::_CLI_DISPATCH` shape: `owner.py` defines a spawn handler and names it
    (by reference, never called) inside a module-level dict literal; `entry.py` imports that
    dict and passes it BY REFERENCE into `runner.execute`, which is the ONLY place that ever
    calls the handler, via a runtime key lookup this static walker cannot follow. Before this
    chunk's edge, `_entrypoint`'s reachable set is EMPTY of the spawn site (the bare-Call walk
    never sees a container VALUE); after it, `_entrypoint` reaches `owner.py::_spawn_handler`
    because it LOADS the cross-module table by name."""
    owner_mod = tmp_path / "owner.py"
    owner_mod.write_text(
        "import subprocess\n"
        "\n"
        "def _spawn_handler(args):\n"
        "    return subprocess.run(['git', 'status'])\n"
        "\n"
        "_DISPATCH = {\n"
        "    'verb': _spawn_handler,\n"
        "}\n",
        encoding="utf-8",
    )
    runner_mod = tmp_path / "runner.py"
    runner_mod.write_text(
        "def execute(table, args):\n"
        "    return table[args[0]](args)\n",
        encoding="utf-8",
    )
    entry_mod = tmp_path / "entry3.py"
    entry_mod.write_text(
        "from owner import _DISPATCH\n"
        "import runner\n"
        "\n"
        "def _entrypoint(args):\n"
        "    return runner.execute(_DISPATCH, args)\n",
        encoding="utf-8",
    )

    files = _discover_scope_files((tmp_path,))
    records = _load_file_records(files)
    index = _build_func_index(records)
    module_index = _module_index_for_test(records)
    import_aliases_by_file = {r.relpath: _import_module_aliases(r, module_index) for r in records}
    func_aliases_by_file = {
        r.relpath: _import_function_aliases(r, module_index, index.func_defs) for r in records
    }
    local_aliases_by_file = {
        r.relpath: _local_module_attr_aliases(r, import_aliases_by_file[r.relpath], index.func_defs)
        for r in records
    }
    spawn_sites_by_file = {r.relpath: r.spawn_sites for r in records}

    # BEFORE the edge: the 5-argument call (no table indexes passed) is byte-for-byte the
    # walker's prior behaviour -- the spawn site stays invisible.
    reached_before = _reachable_functions(
        {("entry3.py", "_entrypoint")},
        index,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
    )
    assert ("owner.py", "_spawn_handler") not in reached_before
    assert _on_path_spawn_sites(reached_before, spawn_sites_by_file, set()) == []

    # AFTER the edge: threading the by-reference dispatch-table indexes through reaches it.
    module_callable_tables_by_file = _module_callable_tables_by_file(
        records, index, import_aliases_by_file, func_aliases_by_file, local_aliases_by_file
    )
    table_aliases_by_file = {
        r.relpath: _import_table_aliases(r, module_index, module_callable_tables_by_file)
        for r in records
    }
    reached_after = _reachable_functions(
        {("entry3.py", "_entrypoint")},
        index,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
        module_callable_tables_by_file,
        table_aliases_by_file,
    )
    assert ("owner.py", "_spawn_handler") in reached_after
    violations = _on_path_spawn_sites(reached_after, spawn_sites_by_file, set())
    assert len(violations) == 1
    assert violations[0].path == "owner.py"
    assert violations[0].enclosing == "_spawn_handler"


def test_legitimized_site_suppresses_a_reachable_violation(tmp_path):
    """`_LEGITIMIZED_SITES` (module docstring's "EXEMPTION MODEL") suppresses a specific,
    key-matched site without weakening the predicate for anything else."""
    entry_mod = tmp_path / "entry2.py"
    entry_mod.write_text(
        "import subprocess\n"
        "\n"
        "def _entry(rows):\n"
        "    return subprocess.run(['git', 'status'])\n",
        encoding="utf-8",
    )
    files = _discover_scope_files((tmp_path,))
    records = _load_file_records(files)
    index = _build_func_index(records)
    module_index = _module_index_for_test(records)
    import_aliases_by_file = {r.relpath: _import_module_aliases(r, module_index) for r in records}
    func_aliases_by_file = {
        r.relpath: _import_function_aliases(r, module_index, index.func_defs) for r in records
    }
    local_aliases_by_file = {
        r.relpath: _local_module_attr_aliases(r, import_aliases_by_file[r.relpath], index.func_defs)
        for r in records
    }
    spawn_sites_by_file = {r.relpath: r.spawn_sites for r in records}
    reached = _reachable_functions(
        {("entry2.py", "_entry")},
        index,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
    )
    unfiltered = _on_path_spawn_sites(reached, spawn_sites_by_file, set())
    assert len(unfiltered) == 1
    site = unfiltered[0]
    key = (site.path, site.enclosing, site.argv0, site.ordinal)
    filtered = _on_path_spawn_sites(reached, spawn_sites_by_file, {key})
    assert filtered == []


# --------------------------------------------------------------------------
# COMPLETENESS GUARD (C2, docs/plans/2026-08-21-the-census-that-cannot-miss-
# an-op.md). Everything above this line is the C-13 ROT guard: it proves an
# ENROLLED op's counted set stays counted. It has no opinion on an op that was
# NEVER enrolled -- that op is not under-measured, it is invisible, and that
# is the defect this section closes.
#
# `coordinator_core.op_census.spawn_bearing_ops` is the EVIDENCE layer (op
# registry -> owning module -> "does this module contain a spawn site,"
# module granularity, deliberately coarser than the function-level BFS
# above -- see that module's own docstring for why). The verdicts below —
# which unenrolled op's evidence is or is not accounted for — stay here, per
# hard constraint 9: op_census/ produces evidence, never verdicts.
# --------------------------------------------------------------------------


#: Real drift, found by this chunk's own divergence guard, not manufactured
#: for demonstration. `_registry_map.py::OP_MODULE_MAP` is a hand-maintained
#: PERFORMANCE OPTIMIZATION per its own docstring ("a stale/incomplete map
#: degrades to today's correctness, never to a broken dispatch") and is
#: outside this chunk's declared write scope. `designed_red`: this test's
#: failure output IS the worklist, never gated on by the fast tier, and it
#: is the demonstration that the divergence guard this chunk was required to
#: add actually catches something real.
#: GATING as of 2026-08-21. `designed_red` is gone and the divergence is
#: zero in both directions.
#:
#: This comment was WRONG TWICE, and the two errors share one move, so both
#: are recorded rather than replaced. (1) It first named the drift as
#: `_registry_map.py` missing `hooks.cater_subagent_start`; that entry landed
#: in the same diff, so the stated cause was already false when written
#: (staff-eng Finding 3). (2) It then named a "stale `sizing.read_object_fields`
#: entry the live registry no longer has." Also false, and in the more
#: dangerous direction: the op was never stale. `read_sizing_object_fields.py`
#: carried `@register_op`, sat in `OP_MODULE_MAP`, and had a test asserting
#: its own registry membership -- but its module was absent from
#: `_EAGER_OP_MODULES`, so `import coordinator_core.ops` never registered it
#: and `coordinator-invoke sizing.read_object_fields` could not resolve it.
#: PRESENT-BUT-DEAD, the exact defect this workstream shipped and fixed for
#: its own census op, live on someone else's. Its suite stayed green
#: throughout because that test file imports the module directly and the
#: decorator fires as an import side effect -- a test that imports what it
#: audits cannot see "declared but unreachable."
#:
#: The shared move is CHARACTERISING a divergence instead of DIAGNOSING it.
#: "Stale entry" was inferred from the direction of the set difference and
#: would have been discharged by deleting the map row, which would have
#: buried a break-class defect under a tidy-looking cleanup. Read the
#: mechanism; a set difference tells you THAT two tables disagree, never WHY.
#:
#: Both halves fixed at source rather than exempted: the eager-import entry
#: added, and `plan.tasks.spine_drift_check` added to `OP_MODULE_MAP`. The
#: guard now proves what PM Ruling 3-B asked of it -- and it earned its
#: keep, because it is what caught a dead op nothing else was looking at.
def test_registry_fast_path_matches_live_registry():
    """The divergence guard hard constraint 7 (amended) / PM Ruling 3-B
    require: `_registry_map.py::OP_MODULE_MAP` (the fast path) must agree
    with `ipc._REGISTRY` (the authoritative source, populated by importing
    `coordinator_core.ops`) on the full op-name set. Deriving a completeness
    gate from the fast path alone would relocate this plan's own
    invisibility hole one layer down -- this test is what keeps that from
    happening silently: the moment the two disagree, this fails loudly
    instead of the gap sitting unnoticed the way `hooks.cater_subagent_start`
    did until this chunk measured it.

    `coordinator_core.hooks._eager_import_all()` runs first for the same reason it does in
    `test_registry_divergence_and_residual_stay_accounted` above -- peer commit `117d960ec` made
    `hooks.*` registration lazy (on-miss, in `ipc`), so a bare `import coordinator_core.ops`
    leaves every `hooks.*` op out of the live registry by design. Do not remove this call."""
    _hooks._eager_import_all()
    divergence = spawn_bearing_ops.registry_divergence()
    assert divergence.agrees, (
        "the fast-path op-name map (_registry_map.py::OP_MODULE_MAP) and the "
        "live op registry (ipc._REGISTRY, via `import coordinator_core.ops`) "
        "disagree -- a stale/incomplete fast-path map degrades silently to "
        "today's dispatch correctness per that module's own docstring, but "
        "MUST NOT degrade this completeness gate's own fidelity:\n"
        f"  only in live registry (missing from the fast-path map): "
        f"{sorted(divergence.only_in_live)}\n"
        f"  only in fast-path map (registry no longer knows this op): "
        f"{sorted(divergence.only_in_fast_path)}"
    )


def _live_unenrolled_spawn_site_keys():
    """`(site_key -> {op_name, ...})` for every spawn site belonging to a
    live op that is NOT a key of `_BUDGETED_ENTRYPOINTS` -- the population
    `_FROZEN_UNENROLLED_SPAWN_SITES` below freezes. Module-granularity
    evidence (`spawn_bearing_ops.ops_with_spawn_evidence`), not the
    function-level BFS the rot guard above uses for its own eight entrypoints
    -- see that module's docstring for why that coarser predicate is the
    deliberate choice here."""
    live_ops = spawn_bearing_ops.live_registry_op_names()
    entrypoints = spawn_bearing_ops.resolve_op_entrypoints(live_ops)
    evidence = spawn_bearing_ops.ops_with_spawn_evidence(entrypoints)
    enrolled = set(_BUDGETED_ENTRYPOINTS.keys())

    keys_to_ops: dict[tuple[str, str, str, int], set[str]] = {}
    for op_name, sites in evidence.items():
        if op_name in enrolled:
            continue
        for site in sites:
            keys_to_ops.setdefault(site_key(site), set()).add(op_name)
    return keys_to_ops


#: The current unenrolled population, FROZEN as a declared inventory keyed
#: at spawn-site granularity (`spawn_policy.site_key` -- the same four-tuple
#: `_KNOWN_SITES`/`_LEGITIMIZED_SITES` already use), never per-op: keying
#: this per op would silence an inventoried op's FUTURE spawns forever (an
#: op admitted today stops tripping anything it grows tomorrow), which is
#: the exact hand-maintained-list-under-a-new-name failure this chunk exists
#: to avoid. Measured 2026-08-21 via `_live_unenrolled_spawn_site_keys()`
#: against the live tree: 76 of 274 live ops carry module-granularity spawn
#: evidence and are not a key of `_BUDGETED_ENTRYPOINTS`; their spawn sites
#: collapse to 149 distinct `site_key`s (module-granularity evidence
#: naturally produces MORE distinct sites than the eight-op rot guard's
#: function-level BFS does, per that predicate's own over-report bias).
#: Ratchets down from here, `_KNOWN_SITES`' 149 -> 94 -> 14 shape is the
#: precedent -- draining this list (by enrolling the op with a real
#: `op_total_*` pin, or by deleting a spawn that never needed to exist, per
#: the rot guard's own four-route disposition above) is future chunks' work,
#: never this one's job to do wholesale.
_FROZEN_UNENROLLED_SPAWN_SITES: frozenset = frozenset(
    {
        ("coordinator_core/goals/reassess_krs.py", "_gather_signal", "<dynamic>", 0),
        ("coordinator_core/hooks/context_pressure_precompact.py", "_run_git", "git", 0),
        ("coordinator_core/hooks/subagent_fabrication_check.py", "_git_porcelain_for_paths", "git", 0),
        ("coordinator_core/install/clone_sibling_repo.py", "clone_idempotent", "git", 0),
        ("coordinator_core/install/prereq_probe.py", "_check_windows_terminal_presence", "winget", 0),
        ("coordinator_core/install/prereq_probe.py", "_run", "<dynamic>", 0),
        ("coordinator_core/install/prereq_probe.py", "probe_clone_auth", "gh", 0),
        ("coordinator_core/install/prereq_probe.py", "probe_clone_auth", "git", 3),
        ("coordinator_core/install/prereq_probe.py", "probe_clone_auth", "git", 4),
        ("coordinator_core/install/prereq_probe.py", "probe_clone_auth", "git", 5),
        ("coordinator_core/install/prereq_probe.py", "probe_clone_auth", "glab", 1),
        ("coordinator_core/install/prereq_probe.py", "probe_clone_auth", "ssh", 2),
        ("coordinator_core/install/prereq_probe.py", "probe_gh", "gh", 0),
        ("coordinator_core/install/prereq_probe.py", "probe_gh", "gh", 1),
        ("coordinator_core/install/prereq_probe.py", "probe_gh", "gh", 2),
        ("coordinator_core/install/prereq_probe.py", "probe_git", "git", 0),
        ("coordinator_core/install/prereq_probe.py", "probe_git_lfs", "git", 0),
        ("coordinator_core/install/prereq_probe.py", "probe_git_lfs", "git", 1),
        ("coordinator_core/install/prereq_probe.py", "probe_git_lfs", "git", 2),
        ("coordinator_core/install/prereq_probe.py", "probe_longpaths", "git", 0),
        ("coordinator_core/install/prereq_probe.py", "probe_node", "node", 0),
        ("coordinator_core/install/prereq_probe.py", "probe_pwsh", "powershell", 1),
        ("coordinator_core/install/prereq_probe.py", "probe_pwsh", "pwsh", 0),
        ("coordinator_core/install/prereq_probe.py", "probe_python", "<dynamic>", 0),
        ("coordinator_core/install/prereq_probe.py", "probe_shell_login_env", "<dynamic>", 1),
        ("coordinator_core/install/prereq_probe.py", "probe_shell_login_env", "dscl", 0),
        ("coordinator_core/install/prereq_probe.py", "probe_uv", "uv", 0),
        ("coordinator_core/install/prereq_probe.py", "shell_login_env_reconstruction_source", "zsh", 0),
        ("coordinator_core/ops/app_session.py", "_launch", "<dynamic>", 0),
        ("coordinator_core/ops/bootstrap_repo.py", "_git._invoke", "<dynamic>", 0),
        ("coordinator_core/ops/bootstrap_repo.py", "_which_git", "git", 0),
        ("coordinator_core/ops/bootstrap_repo.py", "main", "<dynamic>", 0),
        ("coordinator_core/ops/bootstrap_repo.py", "main", "git", 1),
        ("coordinator_core/ops/cartography_churn.py", "_git_ls_files", "<dynamic>", 0),
        ("coordinator_core/ops/cartography_churn.py", "_git_name_only", "<dynamic>", 0),
        ("coordinator_core/ops/cascade_retract.py", "_run_git", "git", 0),
        ("coordinator_core/ops/ceremony/update_docs_scan.py", "_phase1_git_log_window", "git", 0),
        ("coordinator_core/ops/changelog_ops.py", "_batch_resolve_commits", "git", 0),
        ("coordinator_core/ops/changelog_ops.py", "_get_hostname", "<dynamic>", 0),
        ("coordinator_core/ops/changelog_ops.py", "_git_log_for_date", "git", 0),
        ("coordinator_core/ops/commit_anchors.py", "_read_meta_from_staged", "git", 0),
        ("coordinator_core/ops/commit_anchors.py", "_staged_files", "git", 0),
        ("coordinator_core/ops/completion_ops.py", "_canonicalize_stored_shas", "git", 0),
        ("coordinator_core/ops/copy_plugin_template.py", "_run_pytest", "<dynamic>", 0),
        ("coordinator_core/ops/create_github_remote.py", "_gh", "gh", 0),
        ("coordinator_core/ops/create_github_remote.py", "_git", "git", 0),
        ("coordinator_core/ops/create_github_remote.py", "_run", "<dynamic>", 0),
        ("coordinator_core/ops/cruft_sweep.py", "_batch_git_ignored_names", "git", 0),
        ("coordinator_core/ops/cruft_sweep.py", "_batch_is_untracked_dirs", "git", 0),
        ("coordinator_core/ops/cruft_sweep.py", "_delete_path", "rm", 0),
        ("coordinator_core/ops/cruft_sweep.py", "_delete_paths_batch", "rm", 0),
        ("coordinator_core/ops/cruft_sweep.py", "_is_git_ignored", "git", 0),
        ("coordinator_core/ops/cruft_sweep.py", "_is_inside_git_work_tree", "git", 0),
        ("coordinator_core/ops/cruft_sweep.py", "_is_untracked", "git", 0),
        ("coordinator_core/ops/cruft_sweep.py", "sweep_toolchain_caches", "<dynamic>", 0),
        ("coordinator_core/ops/cruft_sweep.py", "sweep_toolchain_caches", "<dynamic>", 1),
        ("coordinator_core/ops/cutover_gate.py", "_git_cat_file_batch_check", "git", 0),
        ("coordinator_core/ops/cutover_gate.py", "_run_pytest_batch", "<dynamic>", 0),
        ("coordinator_core/ops/cutover_gate.py", "resolve_cutover_schema", "git", 0),
        ("coordinator_core/ops/deliverable_rollup.py", "_machine_local_get", "<dynamic>", 0),
        ("coordinator_core/ops/detect_changed_dependency_manifests.py", "_run_git", "git", 0),
        ("coordinator_core/ops/distill_apply_disposal.py", "_run_git", "git", 0),
        ("coordinator_core/ops/draft_plan_aging.py", "_batch_git_commit_epochs", "git", 0),
        ("coordinator_core/ops/draft_plan_aging.py", "_git_commit_epoch", "git", 0),
        ("coordinator_core/ops/draft_plan_aging.py", "_has_recent_real_work_commit", "git", 0),
        ("coordinator_core/ops/ensure_python3_exe_shim.py", "_classify_python3", "python3", 0),
        ("coordinator_core/ops/hibernate_machine.py", "_run_binary", "<dynamic>", 0),
        ("coordinator_core/ops/hibernate_machine.py", "hibernate", "pmset", 0),
        ("coordinator_core/ops/hibernate_machine.py", "hibernate", "shutdown", 1),
        ("coordinator_core/ops/hibernate_machine.py", "hibernate", "systemctl", 2),
        ("coordinator_core/ops/merge_branch_into_workstream.py", "_git", "git", 0),
        ("coordinator_core/ops/merge_quiet_activity_gate.py", "_head_commit_epoch_seconds", "git", 0),
        ("coordinator_core/ops/orphan_branch_sweep.py", "_git", "git", 0),
        ("coordinator_core/ops/orphan_branch_sweep.py", "_run", "<dynamic>", 0),
        ("coordinator_core/ops/orphan_branch_sweep.py", "main", "gh", 1),
        ("coordinator_core/ops/orphan_branch_sweep.py", "main", "git", 0),
        ("coordinator_core/ops/percolate_check_inverse_drift.py", "_run_git", "git", 0),
        ("coordinator_core/ops/percolate_ci_smoke_check.py", "run_ci_smoke_check", "<dynamic>", 0),
        ("coordinator_core/ops/percolate_identity_check.py", "run_identity_check", "<dynamic>", 0),
        ("coordinator_core/ops/plan_capture_persist.py", "invoke_coordinator_doc_new", "<dynamic>", 0),
        ("coordinator_core/ops/plan_suggest_completion_steps.py", "_git", "<dynamic>", 0),
        ("coordinator_core/ops/plan_suggest_completion_steps.py", "_plan_touching_shas", "git", 0),
        ("coordinator_core/ops/plan_suggest_completion_steps.py", "_plan_touching_shas_batch", "git", 0),
        ("coordinator_core/ops/plan_suggest_completion_steps.py", "_resolve_range_shas", "git", 0),
        ("coordinator_core/ops/propagate_body.py", "_commit_delivery", "git", 0),
        ("coordinator_core/ops/propagate_body.py", "_run_git", "git", 0),
        ("coordinator_core/ops/queue_append.py", "_machine_local_get", "<dynamic>", 0),
        ("coordinator_core/ops/record_history.py", "_is_git_worktree", "git", 0),
        ("coordinator_core/ops/record_history.py", "_run_git_log_pass", "git", 0),
        ("coordinator_core/ops/release_tagging.py", "_gh", "gh", 0),
        ("coordinator_core/ops/release_tagging.py", "_git", "git", 0),
        ("coordinator_core/ops/release_tagging.py", "_run", "<dynamic>", 0),
        ("coordinator_core/ops/repo_bootstrap.py", "_machine_local_get", "<dynamic>", 0),
        ("coordinator_core/ops/repo_bootstrap.py", "_machine_local_set", "<dynamic>", 0),
        ("coordinator_core/ops/resolve_swept_baton.py", "_archiving_commit", "git", 0),
        ("coordinator_core/ops/review_trail_write.py", "_batch_resolve_ref_pair", "git", 0),
        ("coordinator_core/ops/review_trail_write.py", "_git_runner", "<dynamic>", 0),
        ("coordinator_core/ops/review_trail_write.py", "_resolve_ref_to_sha", "git", 0),
        ("coordinator_core/ops/run_pip_audit.py", "_run_pip_audit", "<dynamic>", 0),
        ("coordinator_core/ops/run_pre_ci_hooks.py", "_run_pre_ci_hooks", "<dynamic>", 0),
        ("coordinator_core/ops/run_semgrep_scan.py", "_diff_scoped_files", "git", 0),
        ("coordinator_core/ops/run_semgrep_scan.py", "_run_semgrep", "semgrep", 0),
        ("coordinator_core/ops/run_shellcheck_sweep.py", "_lint_one_file", "shellcheck", 0),
        ("coordinator_core/ops/run_shellcheck_sweep.py", "_run_git", "git", 0),
        ("coordinator_core/ops/session/guard_settings_integrity.py", "evaluate_settings_integrity", "git", 0),
        ("coordinator_core/ops/session/resolve_chain_terminal_disposition.py", "_run_git", "git", 0),
        ("coordinator_core/ops/session_baton_promote.py", "_scaffold_via_doc_new", "<dynamic>", 0),
        ("coordinator_core/ops/tracker/push_suggestion.py", "_commit_envelope", "git", 0),
        ("coordinator_core/ops/tracker/push_suggestion.py", "_commit_envelope._run", "git", 0),
        ("coordinator_core/ops/updatedocs_gates.py", "_run", "<dynamic>", 0),
        ("coordinator_core/ops/verify_fix_files_changed.py", "_changed_files", "git", 0),
        ("coordinator_core/orientation/regenerate_cache.py", "_find_uproject", "<dynamic>", 0),
        ("coordinator_core/orientation/regenerate_cache.py", "_git", "git", 0),
        ("coordinator_core/orientation/regenerate_cache.py", "_machine_local_get", "<dynamic>", 0),
        ("coordinator_core/plugin_health/drift.py", "_run_git", "git", 0),
        ("coordinator_core/plugin_health/sentinel.py", "_fetch_machine_json", "<dynamic>", 0),
        ("coordinator_core/plugin_health/sentinel.py", "_py_ident", "<dynamic>", 0),
        ("coordinator_core/plugin_health/sentinel.py", "_whoami_importable", "<dynamic>", 0),
        ("coordinator_core/plugin_health/sentinel.py", "probe_p10", "<dynamic>", 0),
        ("coordinator_core/plugin_health/sentinel.py", "probe_p2", "<dynamic>", 0),
        ("coordinator_core/plugin_health/sentinel.py", "probe_p20", "bash", 0),
        ("coordinator_core/plugin_health/sentinel.py", "probe_p3", "<dynamic>", 0),
        ("coordinator_core/plugin_health/sentinel.py", "probe_p4", "<dynamic>", 0),
        ("coordinator_core/plugin_health/sentinel.py", "probe_p6", "<dynamic>", 0),
        ("coordinator_core/plugin_health/sentinel.py", "probe_p6s", "<dynamic>", 0),
    }
)

#: The high-water ceiling `_FROZEN_UNENROLLED_SPAWN_SITES` itself may never
#: exceed -- pinned to the exact size measured at freeze time (2026-08-21).
#: Matches `_KNOWN_SITES`' own 149 -> 94 -> 14 ratchet shape: this number
#: only ever goes down, by draining an entry via one of the rot guard's own
#: four routes (enroll it, delete the spawn, etc.) and lowering this
#: constant to match. A hand-edit that adds an entry to the frozenset above
#: without also being unable to lower this constant is exactly the "silence
#: an op's future spawns forever" failure this section's own comment warns
#: against -- this assertion is what makes that a test failure instead of a
#: silent expansion.
#: 149 -> 148 (2026-08-22): `review_trail.write`'s spawn reduction drained both
#: `_reject_empty_sha_range` entries — its worktree probe became a filesystem
#: walk and its commit count now comes off the range walk the zero-credit
#: diagnostic already makes — and added one, `_batch_resolve_ref_pair`, which
#: replaces two per-endpoint `git rev-parse` spawns with one for the pair. Net
#: -1, so this comes down by exactly 1. It is deliberately NOT taken to the
#: frozenset's current size: that set is smaller still because of drains this
#: session did not make, and tightening the ratchet around another session's
#: in-flight accounting would fail their commit on this constant rather than on
#: their own work.
#: 148 -> 144 (2026-08-23, C6): drained the 4 dead named-argv0 sites the
#: delete-manifest (`state/spawn-deletions/2026-08-22-delete-manifest.md`)
#: recorded as already-gone-from-the-live-tree --
#: `hooks/track_touched_files.py::_ensure_session_dir` ordinals 0/1 (the
#: whole function was deleted by docs/plans/2026-08-22-track-touched-files-
#: pays-only-for-the-append.md § C1) and `fleet/memo_send.py::
#: _commit_delivered_memo` ordinals 2/3 (dead in the live AST's current
#: ordinal numbering; the function's still-live `git reset` (AC3
#: unstage-on-failure, nested in `_unstage_delivered_memo`), `git add`, and
#: `git commit` calls all keep their own separate, still-frozen entries --
#: hand-traced 2026-08-23 and confirmed load-bearing, NOT part of this
#: drain). Net -4.
#: 144 -> 139 (2026-08-23, C10): `memo.send` enrolled into
#: `_BUDGETED_ENTRYPOINTS` (AC17/AC17b) -- every one of its five remaining
#: named-argv0 sites (`_commit_delivered_memo` ordinals 0/1,
#: `_commit_delivered_memo._unstage_delivered_memo`, `_git_check_ignore`,
#: `_verify_scoped_to_sha_resolvable._rev_parse`) drains out of the frozen
#: inventory and into `_LEGITIMIZED_SITES`, counted by its own shipped
#: ratchet (`test_memo_send_spawn_budget.py`). Net -5.
#: 139 -> 137 (2026-08-23): `fleet.archive_completed_plans` killed and
#: deleted (PM ruling) -- `archive_plans.py` deleted whole, draining its two
#: named-argv0 sites (`_plan_worktree_dirty`, `_plan_worktree_dirty_batch`)
#: out of the frozen inventory entirely (the code no longer exists, so
#: there is nothing left to enroll or legitimize). Net -2.
_FROZEN_UNENROLLED_INVENTORY_HIGH_WATER = 137


def _op_keyed_uncovered_pairs():
    """D6 (2026-08-23): the STRONG completeness predicate AC19 asks for, replacing the site-keyed
    question `test_unenrolled_spawn_bearing_ops_are_declared_in_the_frozen_inventory` used to ask
    (is this site a member of `_FROZEN_UNENROLLED_SPAWN_SITES`) with the op-keyed one: for every
    live op, is every site in that op's own function-granular reachable set either legitimized
    under that op (execution-backed, `_LEGITIMIZED_SITES`) or covered by that op's own static
    reachable-spawn-count pin (`_STATIC_SPAWN_COUNT_PINS`, D7)? Membership in the frozen inventory
    used to be sufficient on its own -- a site could sit there forever, declared but never
    legitimized or counted, and the old gate stayed green. It is retired as the thing that passes;
    `_live_unenrolled_spawn_site_keys` (module-granularity, cheap) stays usable as a fast
    pre-filter but no longer settles the question by itself.

    An op enrolled in `_BUDGETED_ENTRYPOINTS` is out of this gate's scope -- the C-13 rot guard
    above already holds its own counted set to its own budget. Returns a list of
    `(op, site_key)` pairs, empty when the population is fully covered."""
    _hooks._eager_import_all()
    live = spawn_bearing_ops.live_registry_op_names()
    enrolled = frozenset(_BUDGETED_ENTRYPOINTS)
    entrypoints = spawn_bearing_ops.resolve_op_entrypoints(live)
    evidence = spawn_bearing_ops.ops_with_spawn_evidence(entrypoints, function_granular=True)

    # OP-KEYED, and the value tuple must match `site_keys` below element-for-element.
    # `_LEGITIMIZED_SITES` is `dict[tuple[str, str, str, str, int], _Legitimation]` --
    # (op_key, relpath, enclosing, argv0, ordinal), FIVE elements with the op FIRST. The prose in
    # this module's EXEMPTION MODEL paragraph says "keyed on (relpath, enclosing, argv0, ordinal)"
    # and omits the op; that prose is what misled two passes over this code on 2026-08-27.
    #
    # The shipped bug was the VALUE tuple, not the key: it built (key[1], key[2], key[3]) =
    # (relpath, enclosing, argv0) and compared it against site_keys' (enclosing, argv0, ordinal),
    # so the sets could never intersect and `legit_keys` was always empty. An intermediate fix the
    # same day rekeyed this to a FLAT site-key set, which cured the symptom by breaking AC19c --
    # a flat set closes a pair because SOME op legitimized that site, which is exactly the weaker
    # predicate this plan retired. Both are corrected here: keep the op key, fix the value tuple.
    # Masked either way, which is why the suite stayed green through both: every op currently
    # carries a D7 static pin and `if op in pinned: continue` returns before the subtraction.
    legitimized_ops: dict[str, set[tuple[str, str, int]]] = {}
    for key in _LEGITIMIZED_SITES:
        legitimized_ops.setdefault(key[0], set()).add((key[2], key[3], key[4]))

    (
        index,
        spawn_sites_by_file,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
    ) = _build_corpus()

    pinned = frozenset(_STATIC_SPAWN_COUNT_PINS)
    pairs: list[tuple[str, tuple[str, str, int]]] = []
    for op in sorted(live - enrolled):
        if op not in evidence:
            continue
        ep = entrypoints[op]
        entry_funcs = {(ep.relpath, ep.function_name)}
        reached = _reachable_functions(
            entry_funcs,
            index,
            import_aliases_by_file,
            func_aliases_by_file,
            local_aliases_by_file,
        )
        sites = _on_path_spawn_sites(reached, spawn_sites_by_file, exempt=frozenset())
        site_keys = {(site.enclosing, site.argv0, site.ordinal) for site in sites}
        if op in pinned:
            continue
        legit_keys = legitimized_ops.get(op, set())
        for sk in sorted(site_keys - legit_keys):
            pairs.append((op, sk))
    return pairs


def test_unenrolled_spawn_bearing_ops_are_declared_in_the_frozen_inventory():
    """AC19/AC19d: THE defect this chunk exists to close, made structural at op granularity --
    not the weaker site-keyed question this test used to ask (is this site a member of
    `_FROZEN_UNENROLLED_SPAWN_SITES`; see `_op_keyed_uncovered_pairs`'s own docstring for why
    that question is retired as the thing that passes). Every live op's own reachable spawn sites
    must be enrolled, execution-legitimized, or covered by a D7 static count pin -- a NEW
    unenrolled spawner escaping all three routes fails loudly here instead of being invisible the
    way `ceremony.wsc_tail` was until an EM hand-built a probe while chasing an unrelated flaky
    KPI (this plan's own worked example).

    Measured 2026-08-23 at the start of this drain: 347 uncovered (op, site) pairs across 95 ops,
    all site-keyed but not op-keyed accounted for. D7's static count pins close every one of
    them under the op-keyed predicate; this assertion is what keeps that closure from silently
    reopening."""
    pairs = _op_keyed_uncovered_pairs()
    ops = sorted({op for op, _ in pairs})
    assert not pairs, (
        f"{len(pairs)} (op, site) pair(s) across {len(ops)} op(s) escape op-keyed coverage -- "
        "neither enrolled in _BUDGETED_ENTRYPOINTS, nor execution-legitimized under their own "
        "op in _LEGITIMIZED_SITES, nor covered by a _STATIC_SPAWN_COUNT_PINS entry (D7). Enroll "
        "the op with a real op_total_* pin, legitimize the site, or add/extend its static "
        "reachable-spawn-count pin -- never silently:\n"
        + "\n".join(f"  {op}: {sk}" for op, sk in pairs)
    )


def test_frozen_unenrolled_inventory_is_monotonically_non_growing():
    """The frozen inventory ratchets DOWN, never up -- the real assertion
    behind the `_KNOWN_SITES`-precedent comment above the frozenset, not
    prose. Draining an entry (enrolling the op, or deleting the spawn) is
    always allowed and welcome; growing the frozenset without also lowering
    `_FROZEN_UNENROLLED_INVENTORY_HIGH_WATER` to match is not."""
    assert len(_FROZEN_UNENROLLED_SPAWN_SITES) <= _FROZEN_UNENROLLED_INVENTORY_HIGH_WATER, (
        f"_FROZEN_UNENROLLED_SPAWN_SITES grew to {len(_FROZEN_UNENROLLED_SPAWN_SITES)} entries, "
        f"past its high-water ceiling of {_FROZEN_UNENROLLED_INVENTORY_HIGH_WATER}. This "
        "inventory must ratchet down, matching _KNOWN_SITES' own 149 -> 94 -> 14 shape -- "
        "raising the ceiling to buy a green is exactly the regrowth this test exists to refuse."
    )


#: C2b-dyn (2026-08-23): dispositions for the 43 `<dynamic>`-argv0 sites in
#: `_FROZEN_UNENROLLED_SPAWN_SITES` (AC9c). Every one of the 43 lands at
#: exempt-with-dated-rationale -- none wants a live `_LEGITIMIZED_SITES` pin
#: (the enrol route), and none is a spawn that never needed to exist (the
#: delete-manifest this sub-chunk emits carries zero rows). Two disposition
#: SHAPES, not one, per the dispatch brief's own instruction to resolve what
#: is resolvable rather than reaching for the dynamic exemption by default:
#:
#:   - "resolves to '<program>' ..." -- the site's real argv0 IS resolvable
#:     on a one-hop read (a literal argv list built into a local variable
#:     before the `subprocess.run`/`Popen` call, or a documented single-
#:     -token contract every caller of a shared helper honours) even though
#:     the AST-based site_key detector reports "<dynamic>" for it -- the
#:     detector only recognises an inline literal, not one assigned to a
#:     variable first, so this is a detector false-negative on MECHANISM
#:     resolvability, not a live methodological limit.
#:   - "dynamic argv0, runtime evidence not gathered" -- the site's real
#:     argv0 genuinely varies: by platform (`_run_binary`'s pmset/shutdown/
#:     systemctl fan-out), by a caller-resolved interpreter candidate
#:     (`percolate_ci_smoke_check`/`percolate_identity_check`'s
#:     `resolve_python_interp()`, every `plugin_health/sentinel.py` probe's
#:     `py_bin`), by a per-tool resolution table (`cruft_sweep.sweep_
#:     toolchain_caches`), or by a caller-supplied argv/cmd parameter with
#:     no single fixed value across its call sites (`prereq_probe._run`,
#:     `create_github_remote._run`, `orphan_branch_sweep._run`,
#:     `release_tagging._run`,
#:     `updatedocs_gates._run`'s own platform+suffix branch,
#:     `repo_bootstrap._machine_local_get`/`_machine_local_set`'s caller-
#:     resolved `machine_local_bin`). This is the real methodological limit
#:     AC9c anticipates -- it is never "we stopped here".
#:
#: `plugin_health/sentinel.py::probe_p3` is the one entry that is neither
#: shape: its current body (read by hand, 2026-08-23) makes no
#: `subprocess.run`/`Popen` call at all -- its own docstring says exactly
#: that ("it no longer spawns `machine-local keys`"). The site key is
#: retained here rather than silently dropped (dropping it would shrink
#: `_FROZEN_UNENROLLED_SPAWN_SITES` without the matching high-water/ratchet
#: bookkeeping that drain requires, which is this sub-chunk's write scope to
#: do, not its job to do informally) -- its rationale below names the
#: anomaly instead of asserting a program that was never seen to run.
_DYNAMIC_ARGV0_DISPOSITIONS: dict[tuple[str, str, str, int], str] = {
    (
        "coordinator_core/goals/reassess_krs.py",
        "_gather_signal",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- argv0 is "
        "`resolve_launchable(...)`'s first element on Windows or `sys.executable` "
        "elsewhere, chosen by `launchable._is_windows()` at call time."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "_run",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- `_run` is a "
        "generic subprocess.run wrapper; `argv` is entirely caller-supplied and "
        "differs across this module's own probe_gh/probe_git/probe_git_lfs/"
        "probe_pwsh/probe_uv/... call sites."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_python",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- `python_bin` "
        "comes from `find_python()`, which tries python3/python/the py launcher "
        "and returns whichever resolves first."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_shell_login_env",
        "<dynamic>",
        1,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- `login_shell` "
        "is read from `dscl`'s own output (or the `SHELL` env var on a dscl miss) "
        "earlier in this same function, so its value is not known until runtime."
    ),
    (
        "coordinator_core/ops/app_session.py",
        "_launch",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- argv is "
        "resolved via `_resolve_argv(root, key, target_config)` off the "
        "consuming repo's own `coordinator.local.md` config target, which "
        "varies per `key`."
    ),
    (
        "coordinator_core/ops/bootstrap_repo.py",
        "_git._invoke",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 resolves to 'git' on a one-hop read -- `cmd = [\"git\"]` is "
        "built two lines above the `_invoke` closure's own `subprocess.run` call; "
        "the detector reports <dynamic> because the literal is assigned to a "
        "variable first, not because the program varies."
    ),
    (
        "coordinator_core/ops/bootstrap_repo.py",
        "main",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 resolves to sys.executable (python) on a one-hop read -- "
        "the ordinal-0 spawn in `main` is the Stage 4 conflict-warn call, "
        "`subprocess.run([sys.executable, ...])`, invoking "
        "check-install-divergence.py under this interpreter."
    ),
    (
        "coordinator_core/ops/cartography_churn.py",
        "_git_ls_files",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 resolves to 'git' on a one-hop read -- `cmd = [\"git\", "
        "\"ls-files\"]` is a literal built above the `subprocess.run` call; the "
        "variable indirection is what reads as <dynamic> to the detector."
    ),
    (
        "coordinator_core/ops/cartography_churn.py",
        "_git_name_only",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 resolves to 'git' on a one-hop read -- `cmd = [\"git\", "
        "\"log\", ...]` is a literal built above the `subprocess.run` call; same "
        "variable-indirection shape as `_git_ls_files` above."
    ),
    (
        "coordinator_core/ops/changelog_ops.py",
        "_get_hostname",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 resolves to 'hostname' on a one-hop read -- `args` iterates "
        "over the literal tuples `(\"hostname\", \"-s\")` / `(\"hostname\",)`; the "
        "loop variable is what reads as <dynamic> to the detector."
    ),
    (
        "coordinator_core/ops/copy_plugin_template.py",
        "_run_pytest",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 resolves to sys.executable (python) on a one-hop read -- "
        "`[sys.executable, \"-m\", \"pytest\", str(dest)]` is inline at the "
        "`subprocess.run` call; `sys.executable` itself is a runtime attribute "
        "read, which is what reads as <dynamic> to the detector, not an "
        "uncertain program choice."
    ),
    (
        "coordinator_core/ops/create_github_remote.py",
        "_run",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- `_run` is a "
        "generic subprocess.run wrapper distinct from this module's own "
        "already-named `_git`/`_gh` helpers; `cmd` is caller-supplied and not "
        "pinned to one program."
    ),
    (
        "coordinator_core/ops/cruft_sweep.py",
        "sweep_toolchain_caches",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- the "
        "dry-run argv comes from `_TOOLCHAIN_CACHE_TOOLS`'s per-row "
        "`dry_run_argv`, one of five different toolchain binaries (uv, pip, "
        "npm, pnpm, hf) resolved by `_resolve_toolchain_tool`."
    ),
    (
        "coordinator_core/ops/cruft_sweep.py",
        "sweep_toolchain_caches",
        "<dynamic>",
        1,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- the "
        "apply-mode `prune_argv` comes from the same per-row, per-tool table "
        "as ordinal 0, resolved to whichever of the five toolchain binaries "
        "that row names."
    ),
    (
        "coordinator_core/ops/cutover_gate.py",
        "_run_pytest_batch",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 resolves to sys.executable (python) on a one-hop read -- "
        "`[sys.executable, \"-m\", \"pytest\", *refs, ...]` is inline at the "
        "`subprocess.run` call; the attribute read is what the detector sees "
        "as <dynamic>."
    ),
    (
        "coordinator_core/ops/deliverable_rollup.py",
        "_machine_local_get",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 resolves to sys.executable (python) on a one-hop read -- "
        "`[sys.executable, impl, \"get\", key]` is inline at the `subprocess.run` "
        "call; `impl` is the fixed `machine-local` CLI script path, not the "
        "program itself."
    ),
    (
        "coordinator_core/ops/hibernate_machine.py",
        "_run_binary",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- `_run_binary` "
        "is the shared platform-binary spawn helper; `argv` is caller-supplied "
        "and varies across this module's own pmset/shutdown/systemctl call "
        "sites (each already separately named-argv0 in this inventory)."
    ),
    (
        "coordinator_core/ops/orphan_branch_sweep.py",
        "_run",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- `_run` is a "
        "generic subprocess.run wrapper shared across this module's git and "
        "`gh` plumbing call sites (each already separately named-argv0 in "
        "this inventory); `cmd` is caller-supplied."
    ),
    (
        "coordinator_core/ops/percolate_ci_smoke_check.py",
        "run_ci_smoke_check",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- `interp` "
        "comes from `resolve_python_interp()`, which resolves whichever of "
        "several candidate interpreters is present on PATH at call time."
    ),
    (
        "coordinator_core/ops/percolate_identity_check.py",
        "run_identity_check",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- same "
        "`resolve_python_interp()` candidate resolution as "
        "`percolate_ci_smoke_check.run_ci_smoke_check` above."
    ),
    (
        "coordinator_core/ops/plan_capture_persist.py",
        "invoke_coordinator_doc_new",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 resolves to sys.executable (python) on a one-hop read -- "
        "`argv = [sys.executable, str(doc_new), \"--type\", \"plan\", ...]` is "
        "built inline; `sys.executable` is a runtime attribute read, which is "
        "what the detector reports as <dynamic>."
    ),
    (
        "coordinator_core/ops/plan_suggest_completion_steps.py",
        "_git",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 resolves to 'git' on a one-hop read -- `_git`'s three call "
        "sites in this same module (lines ~248/289/316) each pass a literal "
        "`[\"git\", ...]` list; `args` being a parameter is what reads as "
        "<dynamic> to the detector, not an uncertain program."
    ),
    (
        "coordinator_core/ops/queue_append.py",
        "_machine_local_get",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 resolves to sys.executable (python) on a one-hop read -- "
        "same `[sys.executable, impl, \"get\", key]` inline shape as "
        "`deliverable_rollup._machine_local_get`/`orientation.regenerate_cache."
        "_machine_local_get`."
    ),
    (
        "coordinator_core/ops/release_tagging.py",
        "_run",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- `_run` is a "
        "generic subprocess.run wrapper distinct from this module's own "
        "already-named `_git`/`_gh` helpers; `cmd` is caller-supplied."
    ),
    (
        "coordinator_core/ops/repo_bootstrap.py",
        "_machine_local_get",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- unlike the "
        "sys.executable-prefixed `_machine_local_get` sites elsewhere, this "
        "one's argv0 is `machine_local_bin`, a caller-resolved CLI path passed "
        "in as a parameter with no fixed value at this call site."
    ),
    (
        "coordinator_core/ops/repo_bootstrap.py",
        "_machine_local_set",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- same "
        "caller-resolved `machine_local_bin` parameter as this module's own "
        "`_machine_local_get` above."
    ),
    (
        "coordinator_core/ops/review_trail_write.py",
        "_git_runner",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 resolves to 'git' on a one-hop read -- `_git_runner`'s own "
        "docstring pins its contract: `args` always includes the leading "
        "\"git\" token, matching `session_attribution.GitRunner`'s injected-"
        "callback contract every one of this module's call sites honours."
    ),
    (
        "coordinator_core/ops/run_pip_audit.py",
        "_run_pip_audit",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 resolves to sys.executable (python) on a one-hop read -- "
        "`cmd = [sys.executable, \"-m\", \"pip_audit\", ...]` is built inline; "
        "the attribute read is what the detector reports as <dynamic>."
    ),
    (
        "coordinator_core/ops/run_pre_ci_hooks.py",
        "_run_pre_ci_hooks",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 resolves to sys.executable (python) on a one-hop read -- "
        "`[sys.executable, str(hook), dest]` is inline at the `subprocess.run` "
        "call inside this function's own hook loop."
    ),
    (
        "coordinator_core/ops/session_baton_promote.py",
        "_scaffold_via_doc_new",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 resolves to sys.executable (python) on a one-hop read -- "
        "`[sys.executable, cli, *args]` is inline at the `subprocess.run` call, "
        "where `cli` is the fixed coordinator-doc-new.py path, not the program "
        "itself."
    ),
    (
        "coordinator_core/ops/updatedocs_gates.py",
        "_run",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- this "
        "function's own docstring documents the branch: a `.py` CLI gets "
        "`sys.executable` prefixed, a POSIX extensionless CLI execs directly, "
        "and Windows resolves via `_windows_exec_argv` -- three different "
        "argv0 shapes chosen by `cli_path.suffix`/`sys.platform` at call time."
    ),
    (
        "coordinator_core/orientation/regenerate_cache.py",
        "_find_uproject",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 resolves to 'find' on a one-hop read -- `args = [\"find\", "
        "str(repo_root), \"-maxdepth\", \"6\", ...]` is a literal built above "
        "the `subprocess.run` call."
    ),
    (
        "coordinator_core/orientation/regenerate_cache.py",
        "_machine_local_get",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 resolves to sys.executable (python) on a one-hop read -- "
        "same `[sys.executable, impl, \"get\", key]` inline shape as "
        "`deliverable_rollup._machine_local_get`/`queue_append."
        "_machine_local_get`."
    ),
    (
        "coordinator_core/plugin_health/sentinel.py",
        "_fetch_machine_json",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- `py_bin` "
        "is a resolved candidate interpreter, distinct from `sys.executable`, "
        "per this function's own isolation-boundary docstring."
    ),
    (
        "coordinator_core/plugin_health/sentinel.py",
        "_py_ident",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- same "
        "caller-resolved candidate `py_bin` as `_fetch_machine_json` above; "
        "identifying it requires asking that candidate interpreter directly."
    ),
    (
        "coordinator_core/plugin_health/sentinel.py",
        "_whoami_importable",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- same "
        "caller-resolved candidate `py_bin` pattern as this module's other "
        "P-series probes."
    ),
    (
        "coordinator_core/plugin_health/sentinel.py",
        "probe_p10",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- argv0 is "
        "`ch_cmd`, a resolved claude-home resolver path passed in as a "
        "parameter with no fixed value at this call site."
    ),
    (
        "coordinator_core/plugin_health/sentinel.py",
        "probe_p2",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- same "
        "caller-resolved candidate `py_bin` pattern as this module's other "
        "P-series probes."
    ),
    (
        "coordinator_core/plugin_health/sentinel.py",
        "probe_p3",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 ANOMALY, not a program resolution -- `probe_p3`'s current "
        "body (read by hand) makes no `subprocess.run`/`Popen` call at all; "
        "its own docstring says it 'no longer spawns `machine-local keys`'. "
        "Retained rather than silently dropped from the frozen inventory "
        "(shrinking that set is this file's own ratchet-and-ceiling "
        "bookkeeping, out of this sub-chunk's scope); flagged here for a "
        "follow-up trace of why the live detector still reports a spawn-"
        "bearing site here."
    ),
    (
        "coordinator_core/plugin_health/sentinel.py",
        "probe_p4",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- argv0 is "
        "`ml_cmd`, a resolved machine-local CLI path passed in as a parameter "
        "with no fixed value at this call site."
    ),
    (
        "coordinator_core/plugin_health/sentinel.py",
        "probe_p6",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- same "
        "caller-resolved candidate `py_bin` pattern as this module's other "
        "P-series probes."
    ),
    (
        "coordinator_core/plugin_health/sentinel.py",
        "probe_p6s",
        "<dynamic>",
        0,
    ): (
        "2026-08-23 dynamic argv0, runtime evidence not gathered -- same "
        "caller-resolved candidate `py_bin` pattern as this module's other "
        "P-series probes."
    ),
}


def test_dynamic_argv0_sites_are_dispositioned_on_their_own_terms():
    """AC9c: every `<dynamic>`-argv0 member of `_FROZEN_UNENROLLED_SPAWN_SITES`
    carries a dated rationale in `_DYNAMIC_ARGV0_DISPOSITIONS`, and that dict
    carries nothing else -- no stale entry for a site the frozen inventory no
    longer lists, and no entry for a named-argv0 site (those are a different
    sub-chunk's own disposition surface, not this one's)."""
    dynamic_sites = {
        key for key in _FROZEN_UNENROLLED_SPAWN_SITES if key[2] == "<dynamic>"
    }
    undispositioned = sorted(dynamic_sites - set(_DYNAMIC_ARGV0_DISPOSITIONS))
    assert not undispositioned, (
        f"{len(undispositioned)} <dynamic>-argv0 site(s) in "
        "_FROZEN_UNENROLLED_SPAWN_SITES have no entry in "
        "_DYNAMIC_ARGV0_DISPOSITIONS:\n"
        + "\n".join(f"  {k}" for k in undispositioned)
    )
    stale = sorted(set(_DYNAMIC_ARGV0_DISPOSITIONS) - dynamic_sites)
    assert not stale, (
        f"{len(stale)} entr(y/ies) in _DYNAMIC_ARGV0_DISPOSITIONS no longer "
        "name a <dynamic>-argv0 member of _FROZEN_UNENROLLED_SPAWN_SITES -- "
        "either the frozen inventory drained this site (lower "
        "_FROZEN_UNENROLLED_INVENTORY_HIGH_WATER to match, a different "
        "sub-chunk's job) or this entry is a leftover that should be removed:\n"
        + "\n".join(f"  {k}" for k in stale)
    )
    assert len(_DYNAMIC_ARGV0_DISPOSITIONS) == 42, (
        f"_DYNAMIC_ARGV0_DISPOSITIONS carries {len(_DYNAMIC_ARGV0_DISPOSITIONS)} "
        "entries, not the 42 <dynamic>-argv0 sites tranche dyn's inventory now "
        "names -- the dispatch brief's EM-measured figure was 43, and the 2026-08-29 "
        "gravestone deletion of review_trail_readjudication_report.py "
        "(docs/plans/2026-08-29-the-gravestoned-review-trail-surface-is-deleted.md, "
        "DR-374's last row) removed its `_run` site along with the whole module, "
        "taking the count from 43 to 42 -- a count drift here means either a "
        "site was missed or one was double-counted."
    )


#: C2b-a (2026-08-23): dispositions for the 35 named-argv0 sites belonging to
#: tranche a's twelve files (AC9). Every one of the 35 lands at
#: exempt-with-dated-rationale -- none of these twelve files' spawn sites
#: wants a live `_LEGITIMIZED_SITES` pin (the enrol route pays the full
#: three-leg MECHANISM+ASSERTION+EXECUTION bar, which none of these sites has
#: an existing exact-equality companion test to carry), and none is a spawn
#: that a hand trace showed never needed to exist (the delete-manifest this
#: sub-chunk emits carries zero rows).
#:
#: Two disposition SHAPES among the 35, both hand-traced (2026-08-23), not
#: assumed:
#:   - "not reachable from any _BUDGETED_ENTRYPOINTS op" -- the owning op is
#:     not one of the nine hand-verified rows nor does the site's own
#:     enclosing function sit on any budgeted op's live call graph; the site
#:     is residual purely because its OWN op has a non-empty function-granular
#:     reachable spawn set, not because a budgeted op reaches it.
#:   - "reachable from a _BUDGETED_ENTRYPOINTS op, exempted anyway" --
#:     `release_tagging.py::_git`/`_gh` (reachable from `_cut_tag_handler` /
#:     `_cut_tag_and_publish_handler`) and `guard_settings_integrity.py::
#:     evaluate_settings_integrity` (reachable from `session.guard_settings_
#:     integrity`'s `_handler` via `asyncio.to_thread(evaluate_settings_
#:     integrity, ...)`) are BOTH also listed among the C2a widening's 175
#:     "measured empty" rows in `_BUDGETED_ENTRYPOINTS` above -- a real
#:     discrepancy between that measurement and this by-hand trace, most
#:     likely `asyncio.to_thread(fn, ...)` not resolving as a call edge to
#:     `fn` the way a direct `fn(...)` call does. Not this sub-chunk's file
#:     to resolve (the C2a widening's own accuracy is that chunk's contract,
#:     not this file's disposition surface) -- flagged in this sub-chunk's
#:     own run-report sidecar for the EM, and exempted here on the same
#:     ordinary-landing-place terms as every other residual site in this
#:     tranche, since neither function fails any of the three routes'
#:     preconditions.
_NAMED_ARGV0_DISPOSITIONS: dict[tuple[str, str, str, int], str] = {
    (
        "coordinator_core/hooks/context_pressure_precompact.py",
        "_run_git",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- `_run_git` is a PreCompact-hook read helper "
        "(house 2.0s timeout, degrades to \"\" on any failure, per "
        "test_hot_path_subprocess_timeouts.py's own hardening); "
        "`hooks.context_pressure_precompact` is not a `_BUDGETED_ENTRYPOINTS` "
        "op and this site is not on any budgeted op's reachable set."
    ),
    (
        "coordinator_core/hooks/subagent_fabrication_check.py",
        "_git_porcelain_for_paths",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- one `git status --porcelain` call per hook "
        "invocation, already batched across every target path in a single "
        "spawn per this function's own docstring; "
        "`hooks.subagent_fabrication_check` is not a `_BUDGETED_ENTRYPOINTS` "
        "op."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "_check_windows_terminal_presence",
        "winget",
        0,
    ): (
        "2026-08-23 exempt -- Windows-only Step Zero install diagnostic "
        "(`winget list --id Microsoft.WindowsTerminal`), one call per probe "
        "run, gated behind a `sys.platform != \"win32\"` early return; no "
        "`_BUDGETED_ENTRYPOINTS` op reaches `install.prereq_probe`'s probe "
        "suite."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_clone_auth",
        "gh",
        0,
    ): (
        "2026-08-23 exempt -- install-time clone-auth diagnostic, first of "
        "this function's five-step auth-detection chain (gh, then glab, then "
        "SSH, then git, then an optional network probe), each step a single "
        "short-lived call; not reachable from any `_BUDGETED_ENTRYPOINTS` op."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_clone_auth",
        "git",
        3,
    ): (
        "2026-08-23 exempt -- `probe_clone_auth`'s step 4 (`git --version` "
        "presence check, gating the Git Credential Manager probe below it); "
        "same non-budgeted install-diagnostic scope as this function's other "
        "ordinals."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_clone_auth",
        "git",
        4,
    ): (
        "2026-08-23 exempt -- `probe_clone_auth`'s Git Credential Manager "
        "probe (`git credential fill`), one call per configured host in a "
        "short fixed loop (github.com, gitlab.com); same non-budgeted scope "
        "as this function's other ordinals."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_clone_auth",
        "git",
        5,
    ): (
        "2026-08-23 exempt -- `probe_clone_auth`'s optional "
        "`COORDINATOR_AUTH_PROBE_URL` network probe (`git ls-remote`), only "
        "reached when that env var is set and git is present; same "
        "non-budgeted scope as this function's other ordinals."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_clone_auth",
        "glab",
        1,
    ): (
        "2026-08-23 exempt -- `probe_clone_auth`'s step 2 (`glab auth "
        "status`); same non-budgeted install-diagnostic scope as this "
        "function's other ordinals."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_clone_auth",
        "ssh",
        2,
    ): (
        "2026-08-23 exempt -- `probe_clone_auth`'s SSH BatchMode probe "
        "against a short fixed host list (github.com, gitlab.com, plus an "
        "optional configured probe URL); same non-budgeted install-"
        "diagnostic scope as this function's other ordinals."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_gh",
        "gh",
        0,
    ): (
        "2026-08-23 exempt -- `gh --version` presence check, the first of "
        "this probe's three sequential gh calls; install-time diagnostic, "
        "not reachable from any `_BUDGETED_ENTRYPOINTS` op."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_gh",
        "gh",
        1,
    ): (
        "2026-08-23 exempt -- `gh auth status` check, the second of "
        "`probe_gh`'s three sequential gh calls; same non-budgeted scope."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_gh",
        "gh",
        2,
    ): (
        "2026-08-23 exempt -- the optional `gh repo view "
        "$COORDINATOR_GH_PROBE_REPO` check, only reached when that env var "
        "is set; same non-budgeted scope as `probe_gh`'s other two ordinals."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_git",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- `git --version` presence check, this probe's "
        "sole spawn; install-time diagnostic, not reachable from any "
        "`_BUDGETED_ENTRYPOINTS` op."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_git_lfs",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- `git --version` presence gate, the first of "
        "`probe_git_lfs`'s three sequential git calls; same non-budgeted "
        "install-diagnostic scope."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_git_lfs",
        "git",
        1,
    ): (
        "2026-08-23 exempt -- `git lfs version` check, the second of "
        "`probe_git_lfs`'s three sequential calls; same non-budgeted scope."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_git_lfs",
        "git",
        2,
    ): (
        "2026-08-23 exempt -- `git config --global --get filter.lfs.clean` "
        "read, the third of `probe_git_lfs`'s three sequential calls; same "
        "non-budgeted scope."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_longpaths",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- `git config --get core.longpaths` read, gated "
        "behind a Windows-only OS check; install-time diagnostic, this "
        "probe's sole spawn."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_node",
        "node",
        0,
    ): (
        "2026-08-23 exempt -- `node --version` presence check, this probe's "
        "sole spawn; install-time diagnostic, not reachable from any "
        "`_BUDGETED_ENTRYPOINTS` op."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_pwsh",
        "powershell",
        1,
    ): (
        "2026-08-23 exempt -- the Windows-PowerShell-5.1-fallback probe, "
        "only reached when `pwsh` itself is absent AND the host reads as "
        "MINGW/MSYS/CYGWIN; install-time diagnostic fallback branch."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_pwsh",
        "pwsh",
        0,
    ): (
        "2026-08-23 exempt -- `pwsh --version` presence + version check, "
        "this probe's primary spawn; install-time diagnostic, not reachable "
        "from any `_BUDGETED_ENTRYPOINTS` op."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_shell_login_env",
        "dscl",
        0,
    ): (
        "2026-08-23 exempt -- macOS-only login-shell lookup (`dscl . -read "
        "~ UserShell`), gated behind a Darwin-only OS check; install-time "
        "diagnostic."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "probe_uv",
        "uv",
        0,
    ): (
        "2026-08-23 exempt -- `uv --version` presence check, this probe's "
        "sole spawn; install-time diagnostic, not reachable from any "
        "`_BUDGETED_ENTRYPOINTS` op."
    ),
    (
        "coordinator_core/install/prereq_probe.py",
        "shell_login_env_reconstruction_source",
        "zsh",
        0,
    ): (
        "2026-08-23 exempt -- reads the intact macOS zsh login shell's PATH "
        "as a reconstruction anchor for `normalize_env.py`'s Darwin "
        "bash-profile repair path (module docstring's own explanation for "
        "why zsh, not bash, is invoked here); a repair-flow helper outside "
        "`_PROBE_ORDER`, not reachable from any `_BUDGETED_ENTRYPOINTS` op."
    ),
    (
        "coordinator_core/ops/ceremony/update_docs_scan.py",
        "_phase1_git_log_window",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- one bounded `git log --since=... --name-only` "
        "call per scan, timeout-capped to the op's own end-to-end budget, "
        "degrading to `available: False` on any failure; `ceremony.*_docs_"
        "scan` is not a `_BUDGETED_ENTRYPOINTS` op."
    ),
    (
        "coordinator_core/ops/distill_apply_disposal.py",
        "_run_git",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- awaited (asyncio.create_subprocess_exec, D4) "
        "git helper used by `distill.apply_disposal`'s refusal-gate and "
        "apply machinery; that op is not a `_BUDGETED_ENTRYPOINTS` row "
        "(distinct from its sibling `distill.stamp_disposal`, which is one "
        "of the C2a-widened 175 measured-empty rows)."
    ),
    (
        "coordinator_core/ops/merge_quiet_activity_gate.py",
        "_head_commit_epoch_seconds",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- single `git log -1 --format=%ct` read per "
        "module docstring's own negative-spec (\"never runs a mutating git "
        "command... a single read\"); `merge.quiet_activity_gate` is not a "
        "`_BUDGETED_ENTRYPOINTS` row."
    ),
    (
        "coordinator_core/ops/release_tagging.py",
        "_gh",
        "gh",
        0,
    ): (
        "2026-08-23 exempt -- reachable from `_cut_tag_and_publish_handler` "
        "(`release.cut_tag_and_publish`, listed among the C2a widening's "
        "175 measured-empty rows -- see this sub-chunk's own block comment "
        "above for the discrepancy this reveals); the sole `gh` test seam "
        "this module's own docstring names (\"all `gh` CLI traffic funnels "
        "through here\"), one call per publish-step decision, never per-item."
    ),
    (
        "coordinator_core/ops/release_tagging.py",
        "_git",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- reachable from `_cut_tag_handler`/"
        "`_cut_tag_and_publish_handler` (both listed among the C2a "
        "widening's 175 measured-empty rows -- see this sub-chunk's own "
        "block comment above); each of this module's git calls "
        "(`rev-parse`, `tag -a`, `push`) is a single idempotency-gated step "
        "per tag-cut decision, never per-item."
    ),
    (
        "coordinator_core/ops/run_semgrep_scan.py",
        "_diff_scoped_files",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- single `git diff --name-only` call scoping "
        "the semgrep target set for the whole scan; `ci.run_semgrep_scan` "
        "is not a `_BUDGETED_ENTRYPOINTS` row."
    ),
    (
        "coordinator_core/ops/run_semgrep_scan.py",
        "_run_semgrep",
        "semgrep",
        0,
    ): (
        "2026-08-23 exempt -- one `semgrep --config=... --json <files>` "
        "invocation per scan call (the whole scoped file list passed in a "
        "single argv, not one spawn per file); `ci.run_semgrep_scan` is not "
        "a `_BUDGETED_ENTRYPOINTS` row."
    ),
    (
        "coordinator_core/ops/session/guard_settings_integrity.py",
        "evaluate_settings_integrity",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- reachable from `session.guard_settings_"
        "integrity`'s `_handler` via `asyncio.to_thread(evaluate_settings_"
        "integrity, ...)` (this op is also listed among the C2a widening's "
        "175 measured-empty rows -- see this sub-chunk's own block comment "
        "above for the discrepancy this reveals); a single best-effort "
        "`git -C <config_dir> show HEAD:./settings.json` restore-rung read, "
        "only reached when the snapshot rung (rung 1) already failed."
    ),
    (
        "coordinator_core/orientation/regenerate_cache.py",
        "_git",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- module docstring's own words: \"one-shot cold "
        "calls -- no spawn-tax concern, this whole script IS the cold "
        "path\"; `orientation.regenerate_cache` is not a "
        "`_BUDGETED_ENTRYPOINTS` row at all (not even among the C2a "
        "widening's 175)."
    ),
}

#: The 11 files this sub-chunk (C2b-a, tranche a) is scoped to -- see the
#: dispatch brief's own file list. Scoping the completeness test to this set
#: (rather than asserting global completeness across all 105 named-argv0
#: sites) lets tranches a/b/c land independently without each one's
#: completeness assertion racing the others' still-unlanded entries.
_TRANCHE_A_FILES: frozenset = frozenset({
    "coordinator_core/install/prereq_probe.py",
    "coordinator_core/ops/release_tagging.py",
    "coordinator_core/ops/run_semgrep_scan.py",
    "coordinator_core/hooks/context_pressure_precompact.py",
    "coordinator_core/hooks/subagent_fabrication_check.py",
    "coordinator_core/ops/ceremony/update_docs_scan.py",
    "coordinator_core/ops/distill_apply_disposal.py",
    "coordinator_core/ops/merge_quiet_activity_gate.py",
    "coordinator_core/ops/session/guard_settings_integrity.py",
    "coordinator_core/orientation/regenerate_cache.py",
})


def test_named_argv0_sites_in_tranche_a_are_dispositioned_on_their_own_terms():
    """AC9 (tranche-a share): every named-argv0 member of
    `_FROZEN_UNENROLLED_SPAWN_SITES` belonging to one of tranche a's eleven
    files carries a dated rationale in `_NAMED_ARGV0_DISPOSITIONS`, and every
    entry in `_NAMED_ARGV0_DISPOSITIONS` names a real, still-frozen,
    tranche-a site -- no stale entry, no entry reaching outside this
    sub-chunk's own file scope (that would collide with tranche b/c's own
    disposition surface)."""
    tranche_a_named_sites = {
        key
        for key in _FROZEN_UNENROLLED_SPAWN_SITES
        if key[2] != "<dynamic>" and key[0] in _TRANCHE_A_FILES
    }
    undispositioned = sorted(tranche_a_named_sites - set(_NAMED_ARGV0_DISPOSITIONS))
    assert not undispositioned, (
        f"{len(undispositioned)} tranche-a named-argv0 site(s) in "
        "_FROZEN_UNENROLLED_SPAWN_SITES have no entry in "
        "_NAMED_ARGV0_DISPOSITIONS:\n"
        + "\n".join(f"  {k}" for k in undispositioned)
    )
    stale = sorted(set(_NAMED_ARGV0_DISPOSITIONS) - tranche_a_named_sites)
    assert not stale, (
        f"{len(stale)} entr(y/ies) in _NAMED_ARGV0_DISPOSITIONS no longer "
        "name a live tranche-a named-argv0 site -- either the frozen "
        "inventory drained this site (lower "
        "_FROZEN_UNENROLLED_INVENTORY_HIGH_WATER to match) or this entry "
        "reaches outside tranche a's own file scope:\n"
        + "\n".join(f"  {k}" for k in stale)
    )
    assert len(_NAMED_ARGV0_DISPOSITIONS) == 32, (
        f"_NAMED_ARGV0_DISPOSITIONS carries {len(_NAMED_ARGV0_DISPOSITIONS)} "
        "entries, not the 32 expected after fleet.archive_completed_plans's kill "
        "removed its 2 named-argv0 sites (archive_plans.py deleted whole) from "
        "the dispatch brief's own tranche-a slice of 35, and "
        "fleet.archive_shipped_handoffs's kill (2026-08-25, C1b) removed "
        "archive_handoffs.py's `_shipped_in_resolvable` site along with the "
        "whole module -- a count drift here means either a site was missed or "
        "one was double-counted."
    )


#: C2b-b (2026-08-23): dispositions for the 35 named-argv0 sites belonging to
#: tranche b's seventeen files (AC9). 33 of the 35 land at
#: exempt-with-dated-rationale, same terms as tranche a: none of these
#: files' spawn sites wants a live `_LEGITIMIZED_SITES` pin, and a hand
#: trace showed the underlying call is real, bounded, and either off any
#: `_BUDGETED_ENTRYPOINTS` op's reachable set or on one whose own
#: function-granular reachable set the C2a widening measured empty.
#:
#: The remaining 2 (`hooks/track_touched_files.py::_ensure_session_dir`,
#: ordinals 0/1) were delete-the-spawn -- `_ensure_session_dir` (and its
#: siblings `_needs_session_init`/`_bootstrap_session`) were deleted
#: outright by `docs/plans/2026-08-22-track-touched-files-pays-only-for-
#: the-append.md` § C1 -- and C6 (2026-08-23) drained both ordinals out of
#: `_FROZEN_UNENROLLED_SPAWN_SITES` (and this dict) and lowered
#: `_FROZEN_UNENROLLED_INVENTORY_HIGH_WATER` to match; this dict now carries
#: only the 33 exempt-with-dated-rationale entries.
#:
#: Reachability hand-trace method (2026-08-23), same discipline as tranche
#: a: for every site whose owning op IS a `_BUDGETED_ENTRYPOINTS` key, the
#: entrypoint function's own body (module-level, one hop) was read for a
#: direct call to the site's enclosing function. Where that direct call is
#: absent, the entry says "not reachable" and rests on the C2a widening's
#: own transitive-BFS measurement (empty reachable set) rather than
#: re-deriving a fresh BFS by hand. Where it IS present
#: (`ensure_python3_exe_shim.py::_classify_python3`, called directly from
#: `_detect_python3_appx_stub` via `asyncio.to_thread`), the entry names
#: the discrepancy explicitly, on the same terms tranche a's own
#: `release_tagging.py`/`guard_settings_integrity.py` entries did -- not
#: this sub-chunk's file to resolve, flagged for the EM in this sub-chunk's
#: own run-report sidecar.
_NAMED_ARGV0_DISPOSITIONS_B: dict[tuple[str, str, str, int], str] = {
    (
        "coordinator_core/install/clone_sibling_repo.py",
        "clone_idempotent",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- `git clone` invoked exactly once per call, "
        "short-circuited entirely (no subprocess) when `target_dir` "
        "already carries a `.git` directory per this function's own "
        "docstring; `install.clone_sibling_repo` is not a "
        "`_BUDGETED_ENTRYPOINTS` row."
    ),
    (
        "coordinator_core/ops/bootstrap_repo.py",
        "_which_git",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- `git --version` presence probe, this "
        "function's sole spawn; `bootstrap_repo` (the CLI script this "
        "module implements) is not a `_BUDGETED_ENTRYPOINTS` row -- it has "
        "no registered op at all."
    ),
    (
        "coordinator_core/ops/bootstrap_repo.py",
        "main",
        "git",
        1,
    ): (
        "2026-08-23 exempt -- one of `main`'s CLI-entry-only spawns "
        "(one-shot bootstrap script, module docstring); `bootstrap_repo` "
        "has no registered op and is not reachable from any "
        "`_BUDGETED_ENTRYPOINTS` row."
    ),
    (
        "coordinator_core/ops/commit_anchors.py",
        "_read_meta_from_staged",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- single `git show :<path>` read of the "
        "staged version of one plan file per commit-anchors resolution; "
        "`commit_anchors` module has no registered "
        "`_BUDGETED_ENTRYPOINTS` op."
    ),
    (
        "coordinator_core/ops/commit_anchors.py",
        "_staged_files",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- single `git diff --cached --name-only` read "
        "narrowing the staged set to this commit's own pathspec, shared by "
        "the `Plan:` resolver and the `Resolves:` gate rather than each "
        "spawning its own; not reachable from any `_BUDGETED_ENTRYPOINTS` "
        "row."
    ),
    (
        "coordinator_core/ops/completion_ops.py",
        "_canonicalize_stored_shas",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- one batched `git cat-file --batch-check` "
        "call replacing a per-sha `git rev-parse --verify` loop (this "
        "function's own docstring, C9 spawn-amplification fix); "
        "`completion.flip_to_released` and `completion.reconcile_commits` "
        "(both C2a-widened, measured-empty rows) neither call this "
        "function directly from their own handler bodies -- not on either "
        "handler's reachable set."
    ),
    (
        "coordinator_core/ops/cruft_sweep.py",
        "_batch_git_ignored_names",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- one batched `git check-ignore --stdin` call "
        "replacing a per-child `_is_git_ignored` loop (this function's own "
        "docstring); `cruft_sweep.run`'s own `_run_handler` (C2a-widened, "
        "measured-empty row) does not call this function directly -- it "
        "belongs to `sweep_empty_toplevel_dirs`'s own call path, not "
        "`_run_handler`'s."
    ),
    (
        "coordinator_core/ops/cruft_sweep.py",
        "_batch_is_untracked_dirs",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- one batched `git ls-files` call replacing a "
        "per-directory `_is_untracked` loop (this function's own "
        "docstring); belongs to `sweep_scratch`'s own call path, not "
        "`_run_handler`'s (C2a-widened, measured-empty)."
    ),
    (
        "coordinator_core/ops/cruft_sweep.py",
        "_delete_path",
        "rm",
        0,
    ): (
        "2026-08-23 exempt -- single-target best-effort `rm -rf` with a "
        "60s timeout, gated on post-hoc existence for its return value "
        "(this function's own docstring); not on `_run_handler`'s "
        "reachable set (C2a-widened, measured-empty)."
    ),
    (
        "coordinator_core/ops/cruft_sweep.py",
        "_delete_paths_batch",
        "rm",
        0,
    ): (
        "2026-08-23 exempt -- one `rm -rf` call removing every batch "
        "target at once, chunked at `_DELETE_BATCH_CHUNK_SIZE` (this "
        "function's own docstring, batched replacement for "
        "`_delete_path`'s per-item loop); not on `_run_handler`'s "
        "reachable set (C2a-widened, measured-empty)."
    ),
    (
        "coordinator_core/ops/cruft_sweep.py",
        "_is_git_ignored",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- single-name `git check-ignore -q` read, "
        "false-on-any-failure per this function's own docstring; not on "
        "`_run_handler`'s reachable set (C2a-widened, measured-empty)."
    ),
    (
        "coordinator_core/ops/cruft_sweep.py",
        "_is_inside_git_work_tree",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- single `git rev-parse --is-inside-work-tree` "
        "read, false-on-any-failure per this function's own docstring; not "
        "on `_run_handler`'s reachable set (C2a-widened, measured-empty)."
    ),
    (
        "coordinator_core/ops/cruft_sweep.py",
        "_is_untracked",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- single-path untracked check mirroring the "
        "bash oracle's own `_is_untracked` (this function's own "
        "docstring); not on `_run_handler`'s reachable set (C2a-widened, "
        "measured-empty)."
    ),
    (
        "coordinator_core/ops/cutover_gate.py",
        "_git_cat_file_batch_check",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- one `git cat-file --batch-check` call "
        "resolving many shas against one repo root, shared batching "
        "helper for C14/C18 per this function's own docstring; "
        "`cutover.gate`'s own `_cutover_gate` (C2a-widened, "
        "measured-empty) does not call this function directly."
    ),
    (
        "coordinator_core/ops/cutover_gate.py",
        "resolve_cutover_schema",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- one `git -C <doe> show HEAD:...` read "
        "resolving the DoE-side schema live, at call time, per this "
        "function's own docstring; not on `_cutover_gate`'s reachable set "
        "(C2a-widened, measured-empty)."
    ),
    (
        "coordinator_core/ops/ensure_python3_exe_shim.py",
        "_classify_python3",
        "python3",
        0,
    ): (
        "2026-08-23 exempt -- reachable from `install.detect_python3_appx_"
        "stub`'s `_detect_python3_appx_stub` via `asyncio.to_thread"
        "(_classify_python3)` (a real, hand-confirmed direct call -- this "
        "op is also listed among the C2a widening's 175 measured-empty "
        "rows, most likely the same `asyncio.to_thread(fn, ...)` not "
        "resolving as a call edge that tranche a's `guard_settings_"
        "integrity.py` entry names; not this sub-chunk's file to resolve, "
        "flagged in this sub-chunk's own run-report sidecar); a single "
        "`python3 --version` presence probe, timeout-capped, this own-"
        "machine diagnostic's sole spawn."
    ),
    (
        "coordinator_core/ops/orphan_branch_sweep.py",
        "_git",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- thin `git` argv-prefixing wrapper over "
        "`_run` (module body, line 136); none of this file's four "
        "`_BUDGETED_ENTRYPOINTS` handlers (`_compute_descendant_tip_"
        "handler`, `_detect_unpushed_commits_handler`, `_list_unmerged_"
        "work_handler`, `_verify_commit_in_review_window_handler`, all "
        "C2a-widened, measured-empty) call this function directly from "
        "their own bodies."
    ),
    (
        "coordinator_core/ops/orphan_branch_sweep.py",
        "main",
        "gh",
        1,
    ): (
        "2026-08-23 exempt -- one of the CLI entry point `main`'s own "
        "spawns (not one of this file's four registered op handlers, "
        "which `main` does not itself call into for their spawns); not "
        "reachable from any `_BUDGETED_ENTRYPOINTS` row."
    ),
    (
        "coordinator_core/ops/orphan_branch_sweep.py",
        "main",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- one of the CLI entry point `main`'s own "
        "spawns; same non-budgeted CLI-entry scope as this function's "
        "other ordinal."
    ),
    (
        "coordinator_core/ops/percolate_check_inverse_drift.py",
        "_run_git",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- generic single-call `subprocess.run` "
        "wrapper for git, timeout-capped with stdin=DEVNULL per this "
        "function's own docstring; `percolate.check_inverse_drift` has no "
        "`_BUDGETED_ENTRYPOINTS` row."
    ),
    (
        "coordinator_core/ops/propagate_body.py",
        "_commit_delivery",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- one commit-tree + compare-and-swap update-"
        "ref sequence landing a single scoped write per this function's "
        "own docstring (private-index plumbing, no git hooks run); "
        "`propagate_body` has no registered `_BUDGETED_ENTRYPOINTS` op."
    ),
    (
        "coordinator_core/ops/propagate_body.py",
        "_run_git",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- generic single-call `subprocess.run` "
        "wrapper for git, 15s timeout with stdin=DEVNULL; same "
        "non-budgeted scope as this file's other site."
    ),
    (
        "coordinator_core/ops/run_shellcheck_sweep.py",
        "_lint_one_file",
        "shellcheck",
        0,
    ): (
        "2026-08-23 exempt -- one `shellcheck -f json` invocation per "
        "tracked `.sh` file, bounded by a sweep-wide shrinking deadline "
        "rather than a per-file allowance (this function's own docstring, "
        "DR-349 self-raising-bound fix); `run_shellcheck_sweep` has no "
        "registered `_BUDGETED_ENTRYPOINTS` op."
    ),
    (
        "coordinator_core/ops/run_shellcheck_sweep.py",
        "_run_git",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- generic read-only single-call git wrapper, "
        "None-on-any-failure per this function's own docstring; same "
        "non-budgeted scope as this file's other site."
    ),
    (
        "coordinator_core/ops/session/resolve_chain_terminal_disposition.py",
        "_run_git",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- read-only list-argv git wrapper (CC-1, "
        "named binary, no shell) per this function's own docstring; "
        "`session.resolve_chain_terminal_disposition`'s own `_handler` "
        "(C2a-widened, measured-empty) does not call this function "
        "directly from its own body."
    ),
    (
        "coordinator_core/plugin_health/drift.py",
        "_run_git",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- generic single-call git wrapper with "
        "stdin/timeout hardening (module Review comment); `plugin_health."
        "drift` (this module) is not a `_BUDGETED_ENTRYPOINTS` row -- "
        "only `plugin_health.scan` (a different module, `scan.py`) is."
    ),
}

#: The 17 files this sub-chunk (C2b-b, tranche b) is scoped to -- see the
#: dispatch brief's own file list. Scoping the completeness test to this
#: set (rather than asserting global completeness across all 105
#: named-argv0 sites) lets tranches a/b/c/dyn land independently without
#: each one's completeness assertion racing the others' still-unlanded
#: entries.
_TRANCHE_B_FILES: frozenset = frozenset({
    "coordinator_core/ops/cruft_sweep.py",
    "coordinator_core/ops/orphan_branch_sweep.py",
    "coordinator_core/ops/bootstrap_repo.py",
    "coordinator_core/ops/commit_anchors.py",
    "coordinator_core/ops/cutover_gate.py",
    "coordinator_core/ops/propagate_body.py",
    "coordinator_core/ops/run_shellcheck_sweep.py",
    "coordinator_core/install/clone_sibling_repo.py",
    "coordinator_core/ops/completion_ops.py",
    "coordinator_core/ops/ensure_python3_exe_shim.py",
    "coordinator_core/ops/percolate_check_inverse_drift.py",
    "coordinator_core/ops/session/resolve_chain_terminal_disposition.py",
    "coordinator_core/plugin_health/drift.py",
})


def test_named_argv0_sites_in_tranche_b_are_dispositioned_on_their_own_terms():
    """AC9 (tranche-b share): every named-argv0 member of
    `_FROZEN_UNENROLLED_SPAWN_SITES` belonging to one of tranche b's
    seventeen files carries a dated rationale in
    `_NAMED_ARGV0_DISPOSITIONS_B`, and every entry in
    `_NAMED_ARGV0_DISPOSITIONS_B` names a real, still-frozen, tranche-b
    site -- no stale entry, no entry reaching outside this sub-chunk's own
    file scope (that would collide with tranche a/c's own disposition
    surface)."""
    tranche_b_named_sites = {
        key
        for key in _FROZEN_UNENROLLED_SPAWN_SITES
        if key[2] != "<dynamic>" and key[0] in _TRANCHE_B_FILES
    }
    undispositioned = sorted(tranche_b_named_sites - set(_NAMED_ARGV0_DISPOSITIONS_B))
    assert not undispositioned, (
        f"{len(undispositioned)} tranche-b named-argv0 site(s) in "
        "_FROZEN_UNENROLLED_SPAWN_SITES have no entry in "
        "_NAMED_ARGV0_DISPOSITIONS_B:\n"
        + "\n".join(f"  {k}" for k in undispositioned)
    )
    stale = sorted(set(_NAMED_ARGV0_DISPOSITIONS_B) - tranche_b_named_sites)
    assert not stale, (
        f"{len(stale)} entr(y/ies) in _NAMED_ARGV0_DISPOSITIONS_B no longer "
        "name a live tranche-b named-argv0 site -- either the frozen "
        "inventory drained this site (lower "
        "_FROZEN_UNENROLLED_INVENTORY_HIGH_WATER to match) or this entry "
        "reaches outside tranche b's own file scope:\n"
        + "\n".join(f"  {k}" for k in stale)
    )
    assert len(_NAMED_ARGV0_DISPOSITIONS_B) == 26, (
        f"_NAMED_ARGV0_DISPOSITIONS_B carries "
        f"{len(_NAMED_ARGV0_DISPOSITIONS_B)} entries, not the 26 "
        "still-frozen named-argv0 sites tranche b's own file list names "
        "now that C6 (2026-08-23) drained the 2 dead "
        "`_ensure_session_dir` ordinals, C5 of docs/plans/2026-08-22-the-"
        "boot-backstop-asks-git-nothing.md collapsed boot_sweep.py's four "
        "`_commit_consumed_metadata` ordinals into one `boot_backstop.py::"
        "_git` site -- which the 2026-08-27 gravestone of session.boot_sweep "
        "(K-059) then removed outright along with its module, taking this "
        "count from 29 to 28 -- and fleet.archive_shipped_handoffs's kill (2026-08-25, "
        "C1b) removed archive_shipped_handoffs.py's `_sha_reachable` site "
        "along with the whole module -- and the 2026-08-29 gravestone deletion of "
        "review_trail_readjudication_report.py removed its `_full_range_shas` and "
        "`_resolve_repo_root` sites the same way, taking this count from 28 to 26 "
        "-- a count drift "
        "here means either a site was missed or one was double-counted."
    )


#: C2b-c (2026-08-23): dispositions for the 35 named-argv0 sites belonging to
#: tranche c's sixteen files (AC9/AC9b). 33 of the 35 land at
#: exempt-with-dated-rationale, same terms as tranche a/b: a genuine,
#: hand-traced external-tool invocation (git, gh, pmset/shutdown/systemctl,
#: bash) serving a real caller, none wanting a live `_LEGITIMIZED_SITES`
#: pin. The remaining 2 (`fleet/memo_send.py::_commit_delivered_memo`,
#: ordinals 2/3) were delete-the-spawn.
#:
#: C10 (2026-08-23): the 5 sites that were `memo_send.py`'s own share of
#: those 33 exempt-with-dated-rationale entries, plus the 149th,
#: previously-uninventoried site (`fleet/memo_send.py::
#: _resolve_committed_sha`, 'git', 0), are all enrolled instead --
#: `memo.send` became a `_BUDGETED_ENTRYPOINTS` row, its then-shipped
#: ratchet (`test_memo_send_spawn_budget.py`) supplied the ASSERTION leg,
#: and every one of the 6 sites carried a `_LEGITIMIZED_SITES` entry below.
#:
#: SUPERSEDED THE SAME DAY, and this paragraph is history, not the live
#: shape -- read `_STATIC_SPAWN_COUNT_PINS` and `_BUDGETED_ENTRYPOINTS` for
#: that. `c07062c99` (2026-08-23) deleted `ops/fleet/memo_send.py` WHOLE
#: (3623 lines) under DR-344's kill bar, taking `test_memo_send_spawn_
#: budget.py` (818 lines) and `test_memo_send.py` with it; the op was then
#: rebuilt from first principles at `7c5785e58` (2026-08-25) as 651 lines.
#: So today `memo.send` is NOT enrolled, holds no `_LEGITIMIZED_SITES`
#: entry, and is covered by a `_STATIC_SPAWN_COUNT_PINS` entry of 8 --
#: D7's COUNT tier, which AC20c states in-band is not execution evidence.
#: The ratchet is not a regression awaiting repair: it asserted against an
#: implementation that no longer exists, and kill means kill forever. What
#: is genuinely open is whether the REBUILT op earns an execution-backed
#: legitimation; that is new work against new code, not a restore.
#: `_NAMED_ARGV0_DISPOSITIONS_C` therefore drops from 33 to 28 (the 5
#: memo_send.py rows removed); `_UNINVENTORIED_SITE_DISPOSITION` (the
#: dispatch brief's own uninventoried-site route, formerly used for the
#: 149th site) is retired empty -- that route was for leaving a site red
#: without enrolling its op, and this chunk's whole point is closing it.
#:
#: `_commit_delivered_memo`'s ordinals 2/3 (2026-08-23 hand trace, C2b-c):
#: the live function body makes exactly two direct
#: `asyncio.create_subprocess_exec("git", ...)` calls in its OWN top-level
#: body -- `git add -- <memo_relpath>` and, inside the empty-hooks-dir
#: context, `git -c core.hooksPath=<tmp> -c commit.gpgsign=false commit -m
#: <msg> -- <memo_relpath>` -- so ordinals 2 and 3 no longer exist at this
#: site_key in the live tree; the frozen inventory's four ordinals for this
#: enclosing function predate the ordinal renumbering that followed
#: `docs/plans/2026-08-21-memo-send-stops-asking-git-what-it-already-
#: knows.md` § C1's removal of the prior `git symbolic-ref -q HEAD` spawn.
#:
#: C6 RE-VERIFICATION (2026-08-23): C2b-c's own "exactly 2" count was of
#: `_commit_delivered_memo`'s top-level body only and did not name the
#: THIRD live `create_subprocess_exec("git", ...)` call this function's
#: family makes -- `git reset -- <memo_relpath>` (AC3 unstage-on-failure),
#: nested inside `_unstage_delivered_memo` and called from both the
#: non-idempotent commit-failure branch and the unconditional OSError
#: branch. Hand-traced against both call sites (this function's own
#: docstring "Graceful degradation" / "Never raises" sections, and
#: `docs/plans/2026-08-04-delivery-commit-silent-failure.md` C2, the
#: unstage requirement's own origin): load-bearing, not a spawn that never
#: needed to exist -- a failed `git add`/`git commit` in the RECEIVER's
#: foreign tree must not leave a lingering staged path behind (this
#: function's own "Branch-creation REMOVED" rationale: an unacceptable
#: foreign mutation). It was already carrying its own separate
#: exempt-with-dated-rationale entry below
#: (`_commit_delivered_memo._unstage_delivered_memo`, 'git', 0) at a
#: distinct, dot-qualified site_key -- C2b-c's ordinal-2/3 deletion was
#: never applied to the reset's own key, so no code or disposition change
#: was needed to correct this; C6's own re-trace is recorded here as the
#: confirming evidence, and the delete-manifest
#: (`state/spawn-deletions/2026-08-22-delete-manifest.md`) carries the same
#: note. One delete-manifest row still covers ordinals 2/3 together since
#: both died in the same live-tree renumbering, same shape as tranche b's
#: `track_touched_files.py::_ensure_session_dir` pair -- C6 (2026-08-23)
#: drained both ordinals out of `_FROZEN_UNENROLLED_SPAWN_SITES` (and this
#: dict) and lowered `_FROZEN_UNENROLLED_INVENTORY_HIGH_WATER` to match;
#: this dict now carries only the 33 exempt-with-dated-rationale entries.
#:
#: Reachability hand-trace method, same discipline as tranche a/b: for
#: every site whose owning op IS a `_BUDGETED_ENTRYPOINTS` key
#: (`changelog.append_day`/`changelog.cited_in_range_count` ->
#: changelog_ops.py; `plan.list_orphaned` -> draft_plan_aging.py;
#: `repo.create_and_push_remote` -> create_github_remote.py;
#: `review_trail.write` -> review_trail_write.py; `tracker.push_suggestion`
#: -> push_suggestion.py; `branch.merge_into_workstream` ->
#: merge_branch_into_workstream.py; `baton.resolve_swept_in_archive` ->
#: resolve_swept_baton.py), the entrypoint function's own body was read for
#: a direct call to the site's enclosing function. `changelog_ops.py`'s two
#: sites and `resolve_swept_baton.py`'s one site ARE directly reached (the
#: entrypoint's own body calls the enclosing function, or -- for
#: `_archiving_commit` -- via `asyncio.to_thread(_archiving_commit, ...)`)
#: and are exempted as reachable-anyway, same as tranche a's
#: `release_tagging.py`/`guard_settings_integrity.py` entries.
#: `merge_branch_into_workstream.py::_git`, `create_github_remote.py`'s two
#: sites, `review_trail_write.py`'s two sites, and `push_suggestion.py`'s
#: two sites all show the SAME `asyncio.to_thread(<module_fn>, ...)`
#: discrepancy tranche a/b already flagged: the handler's own body calls
#: `asyncio.to_thread(<module-level fn>, ...)` directly (one hop), and that
#: module-level function calls the named-argv0 site directly (a second
#: hop) -- a real, hand-confirmed two-hop reachable chain the C2a widening's
#: BFS measured empty, most likely for the same `asyncio.to_thread`
#: non-resolution reason tranche a/b's own entries name. Not this
#: sub-chunk's file to resolve (the C2a widening's own accuracy is that
#: chunk's contract), flagged again here for the EM in this sub-chunk's own
#: run-report sidecar. `draft_plan_aging.py`'s three sites belong to
#: `check_one`/`scan` (the CLI-entry code path), which `_plan_list_orphaned`
#: -> `list_orphaned` never calls -- genuinely not reachable from the
#: budgeted op.
_NAMED_ARGV0_DISPOSITIONS_C: dict[tuple[str, str, str, int], str] = {
    (
        "coordinator_core/ops/draft_plan_aging.py",
        "_batch_git_commit_epochs",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- one batched git-log walk resolving every "
        "candidate's most-recent-commit epoch (this function's own "
        "docstring, \"was one `_git_commit_epoch` spawn per candidate\"); "
        "belongs to `scan`'s CLI-entry code path, not `plan.list_orphaned`"
        "'s `_plan_list_orphaned` -> `list_orphaned` chain (hand-read "
        "2026-08-23: `list_orphaned` never calls `scan`, `check_one`, or "
        "either git-epoch helper)."
    ),
    (
        "coordinator_core/ops/draft_plan_aging.py",
        "_git_commit_epoch",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- the per-path git-log epoch read `_batch_git_"
        "commit_epochs` superseded (module's own C14 batching note); same "
        "non-reachable CLI-entry scope as this file's other two sites."
    ),
    (
        "coordinator_core/ops/draft_plan_aging.py",
        "_has_recent_real_work_commit",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- `check_one`'s Condition-3b recent-real-work-"
        "commit check, one call per plan file scanned by the CLI `scan`/"
        "`main` entry; same non-reachable scope as this file's other two "
        "sites."
    ),
    (
        "coordinator_core/ops/hibernate_machine.py",
        "hibernate",
        "pmset",
        0,
    ): (
        "2026-08-23 exempt -- the darwin branch of `hibernate`'s "
        "platform-dispatch (`pmset sleepnow`), reachable from "
        "`machine.hibernate`'s handler but a single OS-native power-state "
        "action, never per-item, and mutually exclusive with this "
        "function's other two ordinals (branch on `sys.platform`)."
    ),
    (
        "coordinator_core/ops/hibernate_machine.py",
        "hibernate",
        "shutdown",
        1,
    ): (
        "2026-08-23 exempt -- the win32 fallback branch (`shutdown /h`, "
        "only reached when the ctypes `SetSuspendState` path fails); same "
        "single-action, mutually-exclusive scope as this function's other "
        "ordinals."
    ),
    (
        "coordinator_core/ops/hibernate_machine.py",
        "hibernate",
        "systemctl",
        2,
    ): (
        "2026-08-23 exempt -- the linux branch (`systemctl hibernate`); "
        "same single-action, mutually-exclusive scope as this function's "
        "other ordinals."
    ),
    (
        "coordinator_core/ops/plan_suggest_completion_steps.py",
        "_plan_touching_shas",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- `git log --format=%H -- <rel_path>`, one "
        "call per unbatched path (superseded per-candidate by "
        "`_plan_touching_shas_batch` below, but a live, separately "
        "callable helper, this function's own docstring); "
        "`plan.suggest_completion_steps` is not a `_BUDGETED_ENTRYPOINTS` "
        "row."
    ),
    (
        "coordinator_core/ops/plan_suggest_completion_steps.py",
        "_plan_touching_shas_batch",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- one batched `git log --format=%H --name-"
        "only -- <rel_paths...>` call replacing a per-plan `git log` spawn "
        "(this function's own docstring, W8/C8 amplification "
        "disposition); same non-budgeted scope."
    ),
    (
        "coordinator_core/ops/plan_suggest_completion_steps.py",
        "_resolve_range_shas",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- `git rev-list` resolution of a review-trail "
        "record's `sha_range` to its concrete commit set (this function's "
        "own docstring); same non-budgeted scope."
    ),
    (
        "coordinator_core/ops/changelog_ops.py",
        "_batch_resolve_commits",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- reachable from `changelog.cited_in_range_"
        "count`'s own `_cited_in_range_count` (direct call, line 2023); "
        "one batched commit-token resolution shared across every cited "
        "token in a changelog day's body, not one spawn per token."
    ),
    (
        "coordinator_core/ops/changelog_ops.py",
        "_git_log_for_date",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- reachable from `changelog.append_day`'s own "
        "`_append_day_handler` (direct call, line 703); one `git log` "
        "call per day being appended, not one per commit."
    ),
    (
        "coordinator_core/ops/create_github_remote.py",
        "_gh",
        "gh",
        0,
    ): (
        "2026-08-23 exempt -- reachable via `_create_and_push_remote_"
        "handler`'s `asyncio.to_thread(create_and_push_remote, ...)` "
        "(direct one-hop call) -> `create_and_push_remote`'s `_gh` calls "
        "(a second, hand-confirmed hop); `repo.create_and_push_remote` is "
        "also listed among the C2a widening's 175 measured-empty rows, "
        "most likely the same `asyncio.to_thread` non-resolution "
        "discrepancy tranche a/b's own entries name -- not this "
        "sub-chunk's file to resolve, flagged in this sub-chunk's own "
        "run-report sidecar; each `gh` call is one existence/URL-"
        "resolution step per create-and-push decision, never per-item."
    ),
    (
        "coordinator_core/ops/create_github_remote.py",
        "_git",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- same two-hop reachable chain (`_create_and_"
        "push_remote_handler` -> `asyncio.to_thread(create_and_push_"
        "remote, ...)` -> `_git`) and same C2a-widening discrepancy as "
        "this file's `_gh` site above; each `git` call (rev-parse, "
        "remote get-url/add, push) is one fixed step per create-and-push "
        "decision, never per-item."
    ),
    (
        "coordinator_core/ops/record_history.py",
        "_is_git_worktree",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- the walk-only pre-check that routes a "
        "non-git candidate root to SKIPPED before `_run_git_log_pass`'s "
        "own spawn (this function's own docstring); `records.history` is "
        "not a `_BUDGETED_ENTRYPOINTS` row."
    ),
    (
        "coordinator_core/ops/record_history.py",
        "_run_git_log_pass",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- \"the one git spawn per type (AC2)\" (this "
        "function's own docstring), `git log -p -U0` over a directory "
        "pathspec; same non-budgeted scope as this file's other site."
    ),
    (
        "coordinator_core/ops/review_trail_write.py",
        "_batch_resolve_ref_pair",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- reachable via `_review_trail_write_"
        "handler`'s `asyncio.to_thread(write_review_trail_entry, ...)` "
        "(direct one-hop call) -> `write_review_trail_entry`'s direct "
        "call to `_resolve_symbolic_range` -> this function (a "
        "hand-confirmed third hop); `review_trail.write` is also listed "
        "among the C2a widening's 175 measured-empty rows, same "
        "`asyncio.to_thread` non-resolution discrepancy tranche a/b's own "
        "entries name -- flagged in this sub-chunk's own run-report "
        "sidecar; one batched ref-pair resolution per `sha_range`, "
        "preferred over the two-call fallback below it."
    ),
    (
        "coordinator_core/ops/review_trail_write.py",
        "_resolve_ref_to_sha",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- same three-hop reachable chain and C2a-"
        "widening discrepancy as this file's `_batch_resolve_ref_pair` "
        "site above; the per-endpoint fallback `_resolve_symbolic_range` "
        "falls back to only when the batched form returns None, at most "
        "two calls per `sha_range`."
    ),
    (
        "coordinator_core/ops/tracker/push_suggestion.py",
        "_commit_envelope",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- reachable via `_handler`'s `asyncio.to_"
        "thread(_push_suggestion_sync, ...)` (direct one-hop call) -> "
        "`_push_suggestion_sync`'s direct call to `_deliver_envelope` -> "
        "this function (a hand-confirmed third hop); `tracker.push_"
        "suggestion` is also listed among the C2a widening's 175 "
        "measured-empty rows, same `asyncio.to_thread` non-resolution "
        "discrepancy tranche a/b's own entries name -- flagged in this "
        "sub-chunk's own run-report sidecar; one commit-shaped git call "
        "per delivered suggestion envelope, never per-item."
    ),
    (
        "coordinator_core/ops/tracker/push_suggestion.py",
        "_commit_envelope._run",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- `_commit_envelope`'s own nested git-"
        "subprocess runner closure; same three-hop reachable chain and "
        "C2a-widening discrepancy as this file's `_commit_envelope` site "
        "above."
    ),
    (
        "coordinator_core/ops/cascade_retract.py",
        "_run_git",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- \"own local copy of the module-private git "
        "subprocess wrapper -- same established per-module convention\" "
        "(this function's own docstring); `deliverable.cascade_retract` "
        "is not a `_BUDGETED_ENTRYPOINTS` row."
    ),
    (
        "coordinator_core/ops/detect_changed_dependency_manifests.py",
        "_run_git",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- read-only git wrapper, every failure mode "
        "(not a repo, git missing, no commits, timeout) collapsing to "
        "\"nothing to report\" (this function's own docstring); "
        "`dependency.detect_changed_manifests` is not a "
        "`_BUDGETED_ENTRYPOINTS` row."
    ),
    (
        "coordinator_core/ops/merge_branch_into_workstream.py",
        "_git",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- reachable via `_merge_branch_into_"
        "workstream_handler`'s `asyncio.to_thread(merge_branch_into_"
        "workstream, ...)` (direct one-hop call) -> `merge_branch_into_"
        "workstream`'s several direct `_git` calls (a hand-confirmed "
        "second hop); `branch.merge_into_workstream` is also listed "
        "among the C2a widening's 175 measured-empty rows, same "
        "`asyncio.to_thread` non-resolution discrepancy tranche a/b's "
        "own entries name -- flagged in this sub-chunk's own run-report "
        "sidecar; each call is one fixed step (rev-parse x2, merge, "
        "optional merge --abort) per merge decision, never per-item."
    ),
    (
        "coordinator_core/ops/resolve_swept_baton.py",
        "_archiving_commit",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- reachable from `baton.resolve_swept_in_"
        "archive`'s own `_resolve_swept_baton_in_archive` via `await "
        "asyncio.to_thread(_archiving_commit, worktree_root, match)` (a "
        "real, hand-confirmed direct call -- this op is also listed "
        "among the C2a widening's 175 measured-empty rows, same "
        "`asyncio.to_thread` non-resolution discrepancy tranche a/b's "
        "own entries name; not this sub-chunk's file to resolve, flagged "
        "in this sub-chunk's own run-report sidecar); a single best-"
        "effort git read resolving the archiving commit for one matched "
        "swept baton, never per-item."
    ),
    (
        "coordinator_core/ops/verify_fix_files_changed.py",
        "_changed_files",
        "git",
        0,
    ): (
        "2026-08-23 exempt -- \"Windows-safe `git diff --name-only`\" "
        "(this function's own docstring), one call per verification pass; "
        "`bug_sweep.verify_fix_files_changed` is not a "
        "`_BUDGETED_ENTRYPOINTS` row."
    ),
    (
        "coordinator_core/plugin_health/sentinel.py",
        "probe_p20",
        "bash",
        0,
    ): (
        "2026-08-23 exempt -- `shutil.which(\"bash\")` PATH-resolution "
        "probe for this doctor probe's bash-version check (this "
        "function's own docstring, \"the same PATH-resolution order any "
        "shell script or `bash -c` caller gets\"); `plugin_health."
        "sentinel` diagnostic probe suite, not a `_BUDGETED_ENTRYPOINTS` "
        "row."
    ),
}

#: The 16 files this sub-chunk (C2b-c, tranche c) is scoped to -- see the
#: dispatch brief's own file list. Scoping the completeness test to this
#: set (rather than asserting global completeness across all 105
#: named-argv0 sites) lets tranches a/b/c/dyn land independently without
#: each one's completeness assertion racing the others' still-unlanded
#: entries.
_TRANCHE_C_FILES: frozenset = frozenset({
    "coordinator_core/ops/draft_plan_aging.py",
    "coordinator_core/ops/hibernate_machine.py",
    "coordinator_core/ops/plan_suggest_completion_steps.py",
    "coordinator_core/ops/changelog_ops.py",
    "coordinator_core/ops/create_github_remote.py",
    "coordinator_core/ops/record_history.py",
    "coordinator_core/ops/review_trail_write.py",
    "coordinator_core/ops/tracker/push_suggestion.py",
    "coordinator_core/ops/cascade_retract.py",
    "coordinator_core/ops/detect_changed_dependency_manifests.py",
    "coordinator_core/ops/merge_branch_into_workstream.py",
    "coordinator_core/ops/resolve_swept_baton.py",
    "coordinator_core/ops/verify_fix_files_changed.py",
    "coordinator_core/plugin_health/sentinel.py",
})


def test_named_argv0_sites_in_tranche_c_are_dispositioned_on_their_own_terms():
    """AC9/AC9b (tranche-c share): every named-argv0 member of
    `_FROZEN_UNENROLLED_SPAWN_SITES` belonging to one of tranche c's
    sixteen files carries a dated rationale in
    `_NAMED_ARGV0_DISPOSITIONS_C`, and every entry in
    `_NAMED_ARGV0_DISPOSITIONS_C` names a real, still-frozen, tranche-c
    site -- no stale entry, no entry reaching outside this sub-chunk's own
    file scope (that would collide with tranche a/b's own disposition
    surface)."""
    tranche_c_named_sites = {
        key
        for key in _FROZEN_UNENROLLED_SPAWN_SITES
        if key[2] != "<dynamic>" and key[0] in _TRANCHE_C_FILES
    }
    undispositioned = sorted(tranche_c_named_sites - set(_NAMED_ARGV0_DISPOSITIONS_C))
    assert not undispositioned, (
        f"{len(undispositioned)} tranche-c named-argv0 site(s) in "
        "_FROZEN_UNENROLLED_SPAWN_SITES have no entry in "
        "_NAMED_ARGV0_DISPOSITIONS_C:\n"
        + "\n".join(f"  {k}" for k in undispositioned)
    )
    stale = sorted(set(_NAMED_ARGV0_DISPOSITIONS_C) - tranche_c_named_sites)
    assert not stale, (
        f"{len(stale)} entr(y/ies) in _NAMED_ARGV0_DISPOSITIONS_C no "
        "longer name a live tranche-c named-argv0 site -- either the "
        "frozen inventory drained this site (lower "
        "_FROZEN_UNENROLLED_INVENTORY_HIGH_WATER to match) or this entry "
        "reaches outside tranche c's own file scope:\n"
        + "\n".join(f"  {k}" for k in stale)
    )
    assert len(_NAMED_ARGV0_DISPOSITIONS_C) == 25, (
        f"_NAMED_ARGV0_DISPOSITIONS_C carries "
        f"{len(_NAMED_ARGV0_DISPOSITIONS_C)} entries, not the 25 "
        "still-frozen named-argv0 sites tranche c's own file list names "
        "now that C6 (2026-08-23) drained the 2 dead "
        "`_commit_delivered_memo` ordinals, C10 (2026-08-23) enrolled "
        "`memo.send`, draining its own remaining 5 sites out of "
        "_FROZEN_UNENROLLED_SPAWN_SITES, and the 2026-08-29 gravestone plan "
        "(docs/plans/2026-08-29-the-gravestoned-review-trail-surface-is-deleted.md) "
        "deleted `coordinator_core/ops/review_trail_write.py` outright, taking its "
        "3 tranche-c sites with it and this count from 28 to 25. Its path is "
        "STILL LISTED in `_TRANCHE_C_FILES` and that is a known residual, not a "
        "considered retention -- the module's 3 sites are still in "
        "_FROZEN_UNENROLLED_SPAWN_SITES, so dropping only the scope member would "
        "orphan the 2 dispositions naming them and fail the stale check above. "
        "Draining it properly is 8+ coupled edits across the op map, the "
        "inventory, both disposition dicts and their pins; queued rather than "
        "half-done inside an unrelated close. A count drift here means either "
        "a site was missed or one was double-counted."
    )


#: The uninventoried-site route (C2b-c, 2026-08-23) -- for a site that
#: arrived after the frozen inventory's own freeze measurement and needed
#: disposing WITHOUT being added to the frozen inventory or having
#: `_FROZEN_UNENROLLED_INVENTORY_HIGH_WATER` raised to match (silencing a
#: currently-red site by freezing it in would convert a live gate failure
#: into a permanent exemption). Retired empty by C10 (2026-08-23): the one
#: site that ever used this route, `fleet/memo_send.py::
#: _resolve_committed_sha` ('git', 0) (the 149th site, arrived in peer
#: commit `f4f0b8a7a`), is now legitimized under `memo.send`'s own
#: enrolment (`_LEGITIMIZED_SITES`) instead of merely dispositioned red --
#: the whole point of enrolling the op is to close it out, not carry it
#: here indefinitely.
_UNINVENTORIED_SITE_DISPOSITION: dict[tuple[str, str, str, int], str] = {}


# ---------------------------------------------------------------------------
# D7 (2026-08-23): PIN EVERY OP'S REACHABLE SPAWN COUNT -- AC20/AC20b/AC20c/AC20d
#
# D2-D5 each closed their cluster by recording every uncovered (op, site) pair as an honest
# "not legitimized, not enrolled" disposition -- documentation, not measurement. D1 then found
# the deeper version: `ops/fleet/_common.py`'s spawns live in a SHARED HELPER FILE the
# module-granular frozen inventory structurally cannot hold and the legitimization path cannot
# reach without a per-op companion test that does not exist for any of the 12 ops that reach it.
#
# THIS SECTION'S OWN PREDICATE: for every live op NOT enrolled in `_BUDGETED_ENTRYPOINTS` and
# NOT already fully execution-legitimized (every one of its function-granular reachable spawn
# sites a `_LEGITIMIZED_SITES` entry keyed to that op), pin the SIZE of that op's own
# function-granular reachable spawn-site set -- `len(_on_path_spawn_sites(...))`, the same
# `_reachable_functions` walk C8 fixed, seeded at that op's own resolved entrypoint
# (`spawn_bearing_ops.resolve_op_entrypoints`). No per-op fixture, no per-op companion test:
# this is what lets it cover all 136 uncovered ops (including the 12 `ops/fleet/_common.py`
# ops D1 could not reach) where the execution-backed route cannot. This is a live-tree
# measurement, re-derived on every run via `_live_static_pin_targets`, not a frozen constant --
# the exact op population moves as sibling chunks and concurrent sessions land, and the two
# completeness tests below (`_cover_every_unlegitimized_residual_op`,
# `_pins_match_live_measurement`) are what keep the table below honest against that movement.
#
# WHAT THIS IS, STATED IN-BAND SO NO LATER READER MISTAKES IT FOR A `_Legitimation` (AC20c): a
# RATCHET, not execution evidence. `_STATIC_SPAWN_COUNT_PINS[op]` does not claim the op's spawns
# were OBSERVED to run -- `_LEGITIMIZED_SITES`' own leg 3 (EXECUTION) is what claims that, and an
# op holding a full execution-backed legitimation needs no entry here (measured 2026-08-23: zero
# of the 137 do). It claims only that the op CANNOT SILENTLY REACH MORE spawn sites than pinned:
# a spawn added anywhere on the op's reachable path -- directly, through a shared helper, or
# through a thread hop -- moves the live count away from the pin and fails this section's own
# equality test, by name.
#
# PRICING (AC20b): AC11 ratified 4 git-class spawns of headroom for a cold, import-paying edge,
# with count red above 8. `_STATIC_SPAWN_COUNT_OVER_BUDGET` is the resulting explicit list --
# every pinned op whose count exceeds that threshold, with its number -- the first per-op
# statement of composition cost this repo has had. It is a live filter over the pins below, not
# a hand-duplicated constant, so it cannot drift from them; its own test still pins the exact
# membership so a NEW op crossing the threshold fails loudly and by name rather than silently
# growing the list.
def _live_static_pin_targets() -> tuple[frozenset[str], dict]:
    """The op population `_STATIC_SPAWN_COUNT_PINS` must cover, and the live-registry
    machinery needed to measure it: every live op that is (a) not a key of
    `_BUDGETED_ENTRYPOINTS` and (b) not fully execution-legitimized -- at least one of its own
    function-granular reachable spawn sites is outside `_LEGITIMIZED_SITES` (or it has none
    recorded for that op at all). Returns `(target_op_names, entrypoints)` -- `entrypoints` is
    reused by the caller so the live-registry read happens once per test."""
    _hooks._eager_import_all()
    live = spawn_bearing_ops.live_registry_op_names()
    enrolled = frozenset(_BUDGETED_ENTRYPOINTS)
    residual = sorted(live - enrolled)

    entrypoints = spawn_bearing_ops.resolve_op_entrypoints(live)
    evidence = spawn_bearing_ops.ops_with_spawn_evidence(entrypoints, function_granular=True)

    # OP-KEYED, and the value tuple must match `site_keys` below element-for-element.
    # `_LEGITIMIZED_SITES` is `dict[tuple[str, str, str, str, int], _Legitimation]` --
    # (op_key, relpath, enclosing, argv0, ordinal), FIVE elements with the op FIRST. The prose in
    # this module's EXEMPTION MODEL paragraph says "keyed on (relpath, enclosing, argv0, ordinal)"
    # and omits the op; that prose is what misled two passes over this code on 2026-08-27.
    #
    # The shipped bug was the VALUE tuple, not the key: it built (key[1], key[2], key[3]) =
    # (relpath, enclosing, argv0) and compared it against site_keys' (enclosing, argv0, ordinal),
    # so the sets could never intersect and `legit_keys` was always empty. An intermediate fix the
    # same day rekeyed this to a FLAT site-key set, which cured the symptom by breaking AC19c --
    # a flat set closes a pair because SOME op legitimized that site, which is exactly the weaker
    # predicate this plan retired. Both are corrected here: keep the op key, fix the value tuple.
    # Masked either way, which is why the suite stayed green through both: every op currently
    # carries a D7 static pin and `if op in pinned: continue` returns before the subtraction.
    legitimized_ops: dict[str, set[tuple[str, str, int]]] = {}
    for key in _LEGITIMIZED_SITES:
        legitimized_ops.setdefault(key[0], set()).add((key[2], key[3], key[4]))

    (
        index,
        spawn_sites_by_file,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
    ) = _build_corpus()

    targets = []
    for op in residual:
        if op not in evidence:
            continue
        ep = entrypoints[op]
        entry_funcs = {(ep.relpath, ep.function_name)}
        reached = _reachable_functions(
            entry_funcs,
            index,
            import_aliases_by_file,
            func_aliases_by_file,
            local_aliases_by_file,
        )
        sites = _on_path_spawn_sites(reached, spawn_sites_by_file, exempt=frozenset())
        site_keys = {(site.enclosing, site.argv0, site.ordinal) for site in sites}
        legit_keys = legitimized_ops.get(op, set())
        if site_keys and site_keys <= legit_keys:
            continue
        targets.append(op)
    return frozenset(targets), entrypoints


def _measure_static_spawn_counts(op_names, entrypoints) -> dict[str, int]:
    """`{op_name: reachable-spawn-site-count}` for `op_names`, freshly re-derived against the
    live tree via the same `_reachable_functions` + `_on_path_spawn_sites` walk every other
    predicate in this file uses -- `exempt=frozenset()` deliberately, because this is a COUNT of
    everything reachable, not a residual after `_LEGITIMIZED_SITES` is subtracted out."""
    (
        index,
        spawn_sites_by_file,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
    ) = _build_corpus()
    counts: dict[str, int] = {}
    for op in op_names:
        ep = entrypoints[op]
        entry_funcs = {(ep.relpath, ep.function_name)}
        reached = _reachable_functions(
            entry_funcs,
            index,
            import_aliases_by_file,
            func_aliases_by_file,
            local_aliases_by_file,
        )
        sites = _on_path_spawn_sites(reached, spawn_sites_by_file, exempt=frozenset())
        counts[op] = len(sites)
    return counts


#: Measured 2026-08-23 against the live tree via `_live_static_pin_targets` +
#: `_measure_static_spawn_counts`: 136 live ops are neither enrolled in `_BUDGETED_ENTRYPOINTS`
#: nor fully execution-legitimized. This population and several individual counts moved multiple
#: times DURING this chunk's own measurement window -- `fleet.archive_actioned_memos` dropped out
#: of the live registry, `completion.reconcile_commits` dropped to an empty reachable spawn set,
#: `review_trail.write` newly entered as a live op with a non-empty reachable spawn set, and
#: `hooks.subagent_review_mark`/`session.boot_sweep` each saw their own live count shift by one --
#: all concurrent-session churn on this actively-shared tree (this session is not the only writer
#: to these op files), each caught rather than silently carried by
#: `test_static_spawn_count_pins_cover_every_unlegitimized_residual_op`'s missing/stale legs or
#: `test_static_spawn_count_pins_match_live_measurement`'s own exact-equality check. Every op gets
#: a pin here -- this dict IS the "number, not a note" AC20 asks for.
#: `test_static_spawn_count_pins_match_live_measurement`
#: re-derives every value on each run and fails the moment any op's live count no longer matches; a growth here
#: (an op reaching a NEW spawn site, directly or via a shared helper or a thread hop) is a
#: correctness signal, never something to silence by editing the number to match.
#: RAISED 2026-08-25 (fleet.archive_completed_handoffs/archive_paper_trail/archive_queue_entry/
#: archive_release_accumulator/archive_terminal_sizings/prune_closed_bugs/reap_integrated_findings/
#: reap_unintegrated_findings/migrate_handoff_vocabulary, handoff.archive_transition/
#: reconcile_close_terminal/transition): commit 648f2e4eb (C1b/C2/C5a) killed
#: `ops/fleet/archive_handoffs.py` (2000ms+ over the brightline) and rebuilt the terminal-handoff
#: archiver from scratch as `ops/fleet/archive_terminal_handoffs.py`, a shared module the fleet
#: archive/reap/prune ops and the handoff transition ops all reach through their own common
#: helpers. Verified, not assumed: the new module's own docstring is explicit about spawn
#: discipline ("does NOT spawn one git process per candidate... ZERO git spawns for the shipped_in
#: rail"), and the count here is a STATIC reachable-site ceiling (D7), not execution evidence --
#: these ops' own `_LEGITIMIZED_SITES`/spawn-budget companions (where they exist) still gate what
#: actually runs. Every value below is a fresh `_measure_static_spawn_counts` read against the
#: live tree post-rebuild, not a guess.
#: TIGHTENED 2026-08-26 (D7 dispatch, `test_static_spawn_count_pins_that_have_gone_loose`): the
#: 648f2e4eb rebuild's own pins above had gone slack against further shared-helper trimming on
#: `archive_terminal_handoffs.py` -- handoff.archive_transition/reconcile_close_terminal 18->13,
#: fleet.migrate_handoff_vocabulary/handoff.transition 17->12, fleet.archive_completed_handoffs
#: 15->10, fleet.reap_integrated_findings/reap_unintegrated_findings 15->14, and the five
#: remaining fleet.archive_*/prune_closed_bugs ops 13->9, each a fresh
#: `_measure_static_spawn_counts` read, not a guess. `ceremony.commit` is a NEWLY-appeared live
#: op with no prior pin (`test_static_spawn_count_pins_cover_every_unlegitimized_residual_op`'s
#: own missing leg), pinned at its measured 10.
#: TIGHTENED 2026-08-26/27 (D10 dispatch, `test_static_spawn_count_pins_that_have_gone_loose`):
#: a peer `archive_terminal_handoffs.py` consolidation landed after D7 measured, dropping the
#: `fleet.archive_*` family further -- fleet.archive_completed_handoffs 10->4;
#: fleet.archive_paper_trail/archive_queue_entry/archive_release_accumulator/
#: archive_terminal_sizings/prune_closed_bugs 9->3; fleet.reap_integrated_findings/
#: reap_unintegrated_findings 14->9; fleet.migrate_handoff_vocabulary 12->11;
#: handoff.archive_transition/reconcile_close_terminal 13->12; handoff.transition 12->11 --
#: each a fresh `_measure_static_spawn_counts` read against the live tree, not transcribed.
#: `merge_assemble.apply` is a NEWLY-appeared live op with no prior pin
#: (`test_static_spawn_count_pins_cover_every_unlegitimized_residual_op`'s own missing leg,
#: introduced by D8's new edge): its `_measure_static_spawn_counts` reachable-site count was 0
#: at that time (has spawn evidence per `ops_with_spawn_evidence`, but 0 sites survived the
#: `_reachable_functions`/`_on_path_spawn_sites` walk this file's predicates all share, since D8's
#: by-reference-dispatch-table edge alone reaches only spawn-free functions in `apply.py`) --
#: pinned at that measured 0 at the time.
#: RETIGHTENED 2026-08-27 (`_module_index`'s package-`__init__` bare-name key,
#: `state/audits/2026-08-27-package-init-resolver-gap-population.md`): `ops.py` also imports
#: `build_directives`/`resolve_repo_root` (and other helpers) from the PACKAGE
#: `coordinator_core.merge_assemble`, whose functions live in `merge_assemble/__init__.py` --
#: that route was the actual resolver gap, separate from D8's by-reference table, and it was
#: silently contributing zero rather than the one real `_run_git` site
#: (`coordinator_core/merge_assemble/__init__.py:123`) both `merge_assemble.apply` and
#: `merge_assemble.brief` share. Re-measured directly against the fixed corpus via
#: `_live_static_pin_targets` + `_measure_static_spawn_counts`: pre-fix both measure 0, post-fix
#: both measure 1 (the single shared `_run_git` site, deduplicated by `_on_path_spawn_sites`) --
#: not the audit census's own Leg-B narrative (0->1 / 2->3), whose throwaway script measured a
#: different delta than this file's own predicates reproduce; the census's raw counts are not
#: transcribed here without this file's own re-derivation, which is what these two entries are.
#: Both pinned at their freshly measured 1.
_STATIC_SPAWN_COUNT_PINS: dict[str, int] = {
    # Enrolled 2026-08-30 (second reconciliation pass): both entered the live
    # registry mid-close from concurrent peer work, measured at their live
    # reachable-site counts.
    "baton_assemble.apply": 6,
    "baton_assemble.brief": 5,
    # --- Raised 2026-08-30, cause identified before the raise (this dict's own
    # rule: a raised pin is a budget increase and needs the same evidence any
    # other one does). Two peer changes, not five independent regressions:
    #   be2562f692 gave hooks/auto_push.py a `from coordinator_core.warm import
    #   skew` import, which puts warm/skew.py::publish_lag's TWO git sites on
    #   the reachable path of every op that reaches auto_push -- hence the
    #   identical +2 on fleet.migrate_handoff_vocabulary, handoff.transition,
    #   memo.transition and workday.drain_pending_push (that op's own row is
    #   removed 2026-08-30, C2 -- op gravestoned, nothing left to pin).
    #   push.outstanding's 3->4 raise: pinned at 4, today's measured live count,
    #   cause unattributed -- full investigation narrative in
    #   state/bug-backlog/2026-08-30-the-spawn-ratchet-is-red-at-head-in-four-
    #   36f76f41cdf5.yaml (update_2026_08_30 section (1) and closing_note).
    # This is a reachability count, not execution evidence (see the ceiling
    # test's own docstring), so these raises record that the ops CAN reach more
    # sites -- not that they spend more processes per call.
    #
    # --- Enrolled 2026-08-30: five ops that reached spawn sites nobody had ever
    # pinned, so they escaped op-keyed coverage entirely. Each is pinned at its
    # measured live count, which RECORDS today's reach as the ceiling rather
    # than endorsing it.
    #
    # READ THE 13 ON housekeeping.cycle CORRECTLY -- it is reachability, not
    # spend, and the gap between the two is larger here than anywhere else in
    # this table. Measured 2026-08-30 via the op's own falsifier
    # (docs/plans/2026-08-29-the-housekeeping-cycle-stops-committing.falsifier.py
    # --entry module:coordinator_core.housekeeping.cycle:run): ONE git spawn,
    # 140.6ms process time warm, 328.1ms cold where the extra is the
    # once-per-checkout archive index build. Its plan's prime exit criterion is
    # <=200ms and <=1 spawn, and that RUNTIME guarantee is asserted
    # independently by housekeeping/tests/test_brightline.py (green), not by
    # this pin. The pin (13 when measured 2026-08-30, 6 after 5ae46cc1b9 removed the
# auto_push reach) counts what the call graph can reach through
    # archive_and_commit -- the auto_push and warm.skew machinery it does not
    # execute -- so raising an alarm about "13 spawns" from this row alone is a
    # misreading; go to the brightline test for what it actually costs.
    "ceremony.commit_v2": 1,
    "fleet.archive_actioned_memos": 4,
    "git.maintenance": 1,
    "housekeeping.cycle": 6,
    "session.safe_commit_offer": 1,
    "plugin_health.sentinel": 26,
    "fleet.migrate_handoff_vocabulary": 6,
    "handoff.transition": 6,
    "fleet.reap_integrated_findings": 9,
    "fleet.reap_unintegrated_findings": 9,
    # 4 -> 5, 2026-08-27: the value arrived via a concurrent peer commit (76c5cf07b,
    # "pre-docs quick-save", a different session, 10:44:52) landed ~2.5 min before this
    # session's own edit (9194ce8a5, 10:47:17) reached the file -- this session did NOT
    # raise the pin. What this session did do: independently corroborate the +1 from two
    # derivations that agree it's real -- the ceiling test's own pinned=4/live=5 report, and
    # the D3 cluster disposition naming the newly-reached site
    # (`coordinator_core/git/run.py::run_git`). Peer drift on this op is well-attested --
    # 10 -> 4 before this plan's handoff, 4 -> 5 during its verification run.
    "fleet.archive_completed_handoffs": 4,
    "fleet.archive_paper_trail": 3,
    "fleet.archive_queue_entry": 3,
    "fleet.archive_release_accumulator": 3,
    "fleet.archive_terminal_sizings": 3,
    "warm_guard.evaluate": 10,
    "distill.apply_disposal": 9,
    "memo.transition": 3,
    "merge_assemble.apply": 1,
    "cruft_sweep.run": 8,
    "memo.send": 2,
    "workflow.fire": 6,
    "machine.hibernate": 4,
    "orientation.regenerate_cache": 4,
    "branch.merge_into_workstream": 3,
    "distill.assemble_disposal_manifest": 3,
    "push.outstanding": 4,
    "plan.suggest_completion_steps": 3,
    "release.cut_tag_and_publish": 3,
    "repo.clone_and_register": 3,
    "repo.create_and_push_remote": 3,
    "tracker.push_suggestion": 3,
    "backlog.record": 2,
    "cartography.file_index": 2,
    "ceremony.update_docs_scan": 2,
    "changelog.backfill_gaps": 2,
    "hooks.cater_subagent_start": 2,
    "priority.drain": 2,
    "changelog.inject_anchor": 2,
    "ci.run_semgrep_scan": 2,
    "ci.run_shellcheck_sweep": 2,
    "commit.anchors": 2,
    "completion.flip_to_released": 2,
    "fleet.record_history": 2,
    "git_branch.compute_descendant_tip": 2,
    "git_branch.detect_unpushed_commits": 2,
    "git_branch.list_unmerged_work": 2,
    "git_branch.verify_commit_in_review_window": 2,
    "goal.append": 2,
    "goal.close_day": 2,
    "goal.close_day_apply": 2,
    "handoff.close_origin_stub": 2,
    "handoff.propagate": 2,
    "install.probe_windows_terminal_presence": 2,
    "plan.propagate": 2,
    "plugin_health.forwarder_drift": 2,
    "release.cut_tag": 2,
    "repo_setup.copy_console_subprocess_tripwire": 2,
    "schema.drift_gate": 2,
    "session.guard_settings_integrity": 2,
    "app_session.launch": 1,
    "baton.resolve_path_and_repo": 1,
    "baton.resolve_swept_in_archive": 1,
    "bug_sweep.verify_fix_files_changed": 1,
    "cartography.chunk_table": 1,
    "research.archive_workdir": 1,
    "research.restructure_for_repeat_topic": 1,
    "cartography.tree": 1,
    "ceremony.chunk_commits": 1,
    "ceremony.init_anchor_injection_state": 1,
    "changelog.compute_day_fields": 1,
    "changelog.upsert_reviewed": 1,
    "ci.run_pip_audit": 1,
    "commit.exec_bit_change": 1,
    "crossrepo.closure_status": 1,
    "cutover.gate": 1,
    "deliverable.cascade_backstop_sweep": 1,
    "deliverable.cascade_retract": 1,
    "deliverable.rollup": 1,
    "dependency.detect_changed_manifests": 1,
    "distill.curation_status": 1,
    "distill.scope": 1,
    "engine.drift": 1,
    "git.push_failure_verdict": 1,
    "goals.reassess_krs": 1,
    "handoff.author_fork": 1,
    "handoff.backfill_claim_stamp": 1,
    "handoff.columns": 1,
    "handoff.lineage_ancestry": 1,
    "handoff.repoint_origin": 1,
    # ADDED 2026-08-26 (C14, pln-reconcile-open-comes-back-under-the-bar): the C4 measurement
    # note above (this file, ~line 947) already found `handoff.reconcile_open`'s reachable set
    # NON-EMPTY -- one site, `coordinator_core/git/run.py:426 run_git`, reached via
    # `handoff_transition._read_gate_evidence_resolved` -> `sibling_fact.resolve_leg`'s
    # `gate_evidence` leg -- but left it an unpinned residual, which is what left this guard RED
    # (four failing tests, not the one C4 reported). Reachable-but-unexercised is the honest
    # disposition here, not empty-and-safe: an in-process subprocess.run/Popen spy over a full
    # warm sweep on the live corpus observed 0 spawns (resolve_leg's own cache served all 21
    # handoffs from 3 entries, so `run_git` was never called), which is a measured absence of
    # EXECUTION, not of REACHABILITY. Pinning that count at 1 here (this section's own D7 ratchet,
    # a reachable-SITE-count ceiling, never execution evidence) reports the site rather than
    # silently enrolling the op with an empty legitimization it does not have -- exactly the
    # "empty measurement is worse than an unresolved site" trap named in
    # state/bug-backlog/2026-08-26-ops-with-spawn-evidence-cannot-see-a-spa-0f0dad490422.yaml
    # (a different blind spot -- a by-reference dispatch dict, not present on this op's path --
    # but the same principle: an empty reading must never stand in for "genuinely spawn-free").
    "handoff.scaffold_from_queue": 1,
    "hooks.context_pressure_precompact": 1,
    "hooks.subagent_fabrication_check": 1,
    "hooks.subagent_review_mark": 1,
    "hooks.suggest_sonnet_research": 1,
    # Restored 2026-08-25 after being dropped on a "module killed" reading that the
    # tree does not support: coordinator_core/hooks/track_touched_files.py is on disk
    # and `hooks.track_touched_files` is in the live registry. Its reachable set is the
    # single `_git_run` site, measured live.
    "hooks.track_touched_files": 1,
    "install.clone_idempotent": 1,
    "install.detect_python3_appx_stub": 1,
    "install.probe_skill_frontmatter_valid": 1,
    "invoke.from_argv": 1,
    "memo.fate_backfill": 1,
    "merge.quiet_activity_gate": 1,
    "percolate.check_inverse_drift": 1,
    "percolate.run_ci_smoke_check": 1,
    "percolate.run_identity_check": 1,
    "percolate.run_pre_ci_hooks": 1,
    "plan.list_stale_executing": 1,
    "plan.persist_capture": 1,
    "plugin_health.drift": 1,
    "priority.set": 1,
    "queue.append": 1,
    "queue.promote": 1,
    "records.history": 1,
    "repo_setup.validate_target_root": 1,
    "review.freeze_diff": 1,
    "review.snapshot_diff_and_head": 1,
    "scratchpad.sweep": 1,
    "session.commits": 1,
    "session.guard_hooks_kill_switch_detail": 1,
    "session.reap_claims_for_repos": 1,
    "session.resolve_chain_terminal_disposition": 1,
    "session_baton.promote": 1,
    "session_ledger.aggregate_chain_loe": 1,
    "strategic.generate": 1,
    "tracker.mint_person": 1,
    "workflow.fire_status": 1,
}


#: AC11's ratified headroom ceiling: 4 git-class spawns of headroom for a cold, import-paying
#: edge, count red above 8.
_STATIC_SPAWN_COUNT_OVER_BUDGET_THRESHOLD = 8

#: AC20b's explicit output: every pinned op whose count exceeds the threshold above, with its
#: number. Derived as a filter over `_STATIC_SPAWN_COUNT_PINS` (never hand-duplicated, so it
#: cannot itself drift from the pins) and then pinned here as its own declared set, so a NEW op
#: crossing the threshold is a named, visible failure rather than a silent addition to a list
#: nothing asserts on.
#: Re-emitted 2026-08-26/27 (D10) against the D10-tightened pins above: the fleet.archive_*
#: family and fleet.prune_closed_bugs dropped below the threshold (fleet.archive_completed_handoffs
#: 10->4; the remaining fleet.archive_* + prune_closed_bugs 9->3) and fall OUT of this list, while
#: every op still above `_STATIC_SPAWN_COUNT_OVER_BUDGET_THRESHOLD` keeps its D10-measured number.
#: Re-emitted 2026-08-30 after the rot sweep: nine pins left the table entirely, but they split
#: into two DIFFERENT categories that must not be conflated (integrator correction, review
#: `coordinatorcode-reviewer.a637bcc18cac79d26`, Finding 1 -- the first pass recorded all nine
#: under one false "duplicate/dead weight (AC20c)" reason):
#:   -- GENUINELY DUPLICATE (AC20c: enrolled or fully legitimized elsewhere, so a pin beside
#:      either is dead weight): deliverable.cascade_terminal, handoff.archive_transition,
#:      fleet.prune_closed_bugs, handoff.has_live_children. Each is present in a D2/D3/D5 cluster
#:      disposition/entrypoint table -- that coverage is what makes the pin redundant.
#:   -- DEAD OP, not a duplicate (same transparent accounting the six explicit purges above use):
#:      ceremony.commit, merge_assemble.brief, cartography.churn, session.boot_sweep,
#:      handoff.reconcile_open. None of these five appears in `_BUDGETED_ENTRYPOINTS`, any
#:      `_CLUSTER_D{2,3,4,5}_*` table in this file, or `coordinator_core/ops/_registry_map.py` --
#:      confirmed by grep over both files at the pre-slice commit. They are dead the same way
#:      `review_trail.scan_unresolved_ubt` / `write_surface.emit_manifest` /
#:      `handoff.reconcile_close_terminal` / `ceremony.post_commit_tail` / `review_trail.write` /
#:      `session.warm_start` are dead -- the op no longer resolves to anything live, not that its
#:      coverage moved elsewhere. If any of the five turns out to still be live under a different
#:      key or a dynamic registration this file's static walker cannot see, that is a genuine hole
#:      and its pin must be restored, not re-justified as a duplicate.
#: fleet.migrate_handoff_vocabulary and handoff.transition move 11->13 and memo.transition 9->11
#: on be2562f692's warm.skew import, and housekeeping.cycle enters at 13 -- the largest single
#: composition cost now named here.
_STATIC_SPAWN_COUNT_OVER_BUDGET: dict[str, int] = {
    "plugin_health.sentinel": 26,
    "warm_guard.evaluate": 10,
    "distill.apply_disposal": 9,
    "fleet.reap_integrated_findings": 9,
    "fleet.reap_unintegrated_findings": 9,
}


def test_static_spawn_count_pins_cover_every_unlegitimized_residual_op():
    """AC20: every live op is either enrolled (`_BUDGETED_ENTRYPOINTS`), fully
    execution-legitimized (`_LEGITIMIZED_SITES` covers every one of its own reachable sites), or
    a key of `_STATIC_SPAWN_COUNT_PINS` -- never left uncovered by all three. A live op falling
    through all three routes is exactly the invisibility this whole plan exists to end; a stale
    pin for an op that no longer needs one (now enrolled, or now fully legitimized) is dead
    weight the same way an orphaned `_LEGITIMIZED_SITES` entry is."""
    targets, _entrypoints = _live_static_pin_targets()
    pinned = frozenset(_STATIC_SPAWN_COUNT_PINS)

    missing = sorted(targets - pinned)
    assert not missing, (
        f"{len(missing)} live op(s) are neither enrolled in _BUDGETED_ENTRYPOINTS nor fully "
        "execution-legitimized nor pinned in _STATIC_SPAWN_COUNT_PINS -- add a static "
        "reachable-spawn-count pin for each (AC20):\n" + "\n".join(f"  {op}" for op in missing)
    )

    stale = sorted(pinned - targets)
    assert not stale, (
        f"{len(stale)} _STATIC_SPAWN_COUNT_PINS entry(ies) no longer belong in this table -- "
        "EITHER the op is now enrolled in _BUDGETED_ENTRYPOINTS or fully execution-legitimized "
        "(this table must not duplicate a `_Legitimation`, AC20c) OR the op has dropped out of "
        "`targets` because it is no longer live at all (absent from _live_static_pin_targets(), "
        "e.g. deleted or renamed in ops/_registry_map.py) -- a dead op, not a covered one. Check "
        "which before recording a reason: a dead op belongs in the same transparent dead-op "
        "accounting the six explicit purges elsewhere in this file use, not filed as a "
        "duplicate/AC20c removal:\n" + "\n".join(f"  {op}" for op in stale)
    )


def test_static_spawn_count_pins_are_a_ceiling_never_exceeded():
    """AC20's ratchet, with CEILING semantics -- live <= pin, never live == pin.

    Exact equality was the shipped shape for about one hour and it was wrong on a tree ~50
    concurrent sessions write to: a peer REMOVING a spawn broke this assertion, so the gate
    punished exactly the improvement it exists to encourage (observed 2026-08-23,
    `session.boot_sweep` pinned=5 live=4 after a peer's boot_backstop work landed). The repo's
    own precedent is a ceiling -- `_FROZEN_UNENROLLED_INVENTORY_HIGH_WATER` asserts
    `len(...) <= high_water` for the same reason.

    This is a COUNT ratchet, not execution evidence (AC20c): it does not claim the sites run,
    only that an op cannot silently reach MORE of them than pinned. Growth fails here; shrinkage
    is reported by `test_static_spawn_count_pins_that_have_gone_loose` so it gets tightened
    deliberately rather than drifting."""
    targets, entrypoints = _live_static_pin_targets()
    live_counts = _measure_static_spawn_counts(frozenset(_STATIC_SPAWN_COUNT_PINS) & targets, entrypoints)

    grown = sorted(
        op
        for op in _STATIC_SPAWN_COUNT_PINS
        if op in live_counts and live_counts[op] > _STATIC_SPAWN_COUNT_PINS[op]
    )
    assert not grown, (
        f"{len(grown)} op(s) now reach MORE spawn sites than pinned -- reached a new site "
        "directly, through a shared helper, or through a thread hop. Find out which before "
        "raising a pin; a raised pin is a budget increase and needs the same evidence any other "
        "one does:\n"
        + "\n".join(
            f"  {op}: pinned={_STATIC_SPAWN_COUNT_PINS[op]} live={live_counts[op]} "
            f"(+{live_counts[op] - _STATIC_SPAWN_COUNT_PINS[op]})"
            for op in grown
        )
    )


def test_static_spawn_count_pins_that_have_gone_loose():
    """The other half of the ratchet: a pin sitting ABOVE its live count is slack, and slack is
    how a ceiling stops meaning anything. Failing here is good news -- an op got cheaper and the
    pin should come down to lock the win in. Tighten the pin to the live number; never raise a
    live number to meet a pin."""
    targets, entrypoints = _live_static_pin_targets()
    live_counts = _measure_static_spawn_counts(frozenset(_STATIC_SPAWN_COUNT_PINS) & targets, entrypoints)

    loose = sorted(
        op
        for op in _STATIC_SPAWN_COUNT_PINS
        if op in live_counts and live_counts[op] < _STATIC_SPAWN_COUNT_PINS[op]
    )
    assert not loose, (
        f"{len(loose)} pin(s) are now slack -- the op reaches FEWER spawn sites than pinned, so "
        "the ceiling is no longer tight. Lower each pin to the live number to keep the ratchet "
        "meaningful:\n"
        + "\n".join(
            f"  {op}: pinned={_STATIC_SPAWN_COUNT_PINS[op]} live={live_counts[op]} "
            f"(-{_STATIC_SPAWN_COUNT_PINS[op] - live_counts[op]})"
            for op in loose
        )
    )


def test_static_spawn_count_over_budget_list_matches_pins():
    """AC20b: `_STATIC_SPAWN_COUNT_OVER_BUDGET` must be exactly the filter of
    `_STATIC_SPAWN_COUNT_PINS` at `_STATIC_SPAWN_COUNT_OVER_BUDGET_THRESHOLD` -- the plan's own
    per-op composition-cost output, checked so a newly over-budget op cannot silently join the
    pins without also being named in the priced list AC20b asks for."""
    computed = {
        op: count
        for op, count in _STATIC_SPAWN_COUNT_PINS.items()
        if count > _STATIC_SPAWN_COUNT_OVER_BUDGET_THRESHOLD
    }
    assert computed == _STATIC_SPAWN_COUNT_OVER_BUDGET, (
        "_STATIC_SPAWN_COUNT_OVER_BUDGET has drifted from _STATIC_SPAWN_COUNT_PINS at threshold "
        f"{_STATIC_SPAWN_COUNT_OVER_BUDGET_THRESHOLD} -- computed="
        f"{sorted(computed.items())} declared={sorted(_STATIC_SPAWN_COUNT_OVER_BUDGET.items())}"
    )
