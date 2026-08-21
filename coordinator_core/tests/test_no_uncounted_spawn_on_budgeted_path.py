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
number of hops, from one of the nine live budgeted entrypoints (`_BUDGETED_ENTRYPOINTS`)? This
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
    one of `execute_plan_assemble.sibling_committed_chunk_ids_memo`'s two (its `_run_git` site).
  - 1 site (`sibling_committed_chunk_ids_memo`'s second hit, `git/repo_root.py::_spawn_rev_parse`)
    was NOT in the audit's own op-9 section but is a real hop the audit's own limits section
    predicted ("a different session re-running this same method could plausibly find 1-3 more
    sites") -- `_sibling_committed_chunk_ids` -> `_committed_chunk_ids` -> `_committed_chunk_shas`
    reaches it 3-4 hops deep, past where the by-hand trace stopped.
  - 8 sites belong to `ceremony.scoped_git_commit`, which the C-08 audit called CLEAN (0 sites) --
    verified by hand-tracing every one back to `_handler`: `_handler` calls
    `run_commit_pipeline` (`commit_pipeline.py`, imported), which directly imports and calls
    `git_native._git` (the op's own declared, intentionally-counted seam -- reachable and correct,
    simply not yet `_LEGITIMIZED_SITES`-entered) and `diverging_paths` (`git/divergence.py`,
    confirmed: its body calls `_run_git` directly), and separately -- past where `run_commit_pipeline`
    itself hands off to a best-effort post-sync helper -- `auto_push.drain_pending_push` (a real,
    confirmed one-hop `module.attr(...)` call at `commit_pipeline.py`'s own local `from
    coordinator_core.hooks import auto_push`), whose body calls `run_push_with_retry`,
    `_branch_resolves_locally`, and `_drain_dead_ref_record`, which in turn reach `push_once`,
    `_run_git`, and `_is_ancestor` (all confirmed by reading each function's own body); plus
    `_handler` ALSO calls `session_scope.release_committed_claims` /
    `release_phantom_claims` directly (both confirmed imported, both confirmed to call git-shaped
    helpers in `session/scope.py` reaching `_git_run`). Every one of these 8 was traced to an
    actual `ast.Call` node, not inferred -- ZERO of them are collision artifacts of the precise
    (per-import-pinned) resolution this gate uses.

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
    live caller -- it is not one of the nine LIVE entrypoints this gate enforces, and removing that
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
"""

from __future__ import annotations

import ast
import pathlib
import sys
import typing

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

#: The nine LIVE `spawn_count_budget` rows in `coordinator_core/benchmarks/budget-manifest.json`,
#: manifest key -> (relpath, tuple-of-top-level-entrypoint-function-names). Resolved and verified
#: against `state/audits/2026-08-19-opro-03-c08-budgeted-op-spawn-trace.md`'s per-op sections and
#: cross-checked against each row's own companion test (which function it actually exercises, not
#: the manifest key's prose shape) -- see this file's own run-report sidecar for the per-row
#: verification. The tenth manifest row, `coverage.diagnose_open_review_loop_dag_mode`, is
#: deliberately omitted -- see module docstring's negative spec.
#:
#: `ops.discover_working_repos` carries TWO entrypoints (`main()` calls both) -- the audit traced
#: both as independently reachable-with-a-spawn.
_BUDGETED_ENTRYPOINTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "ceremony.scoped_git_commit": (
        "coordinator_core/ops/ceremony/scoped_git_commit.py",
        ("_handler",),
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
    "execute_plan_assemble.sibling_committed_chunk_ids_memo": (
        "coordinator_core/execute_plan_assemble/close_out_and_stamp.py",
        ("_sibling_committed_chunk_ids",),
    ),
    "ops.discover_working_repos": (
        "coordinator_core/ops/discover_working_repos.py",
        ("_tier_a5", "_publish_mirror_keys"),
    ),
}

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
#: other. `close_out_and_stamp.py::_run_git` is exactly that -- measured executing under
#: `dispatch_ledger_delivered`'s exact-equality counter, and never reached under
#: `sibling_committed_chunk_ids_memo`'s memo-hit fixture. A site-keyed register cannot express
#: that difference and must resolve it in one direction or the other: refusing both leaves a
#: correctly-counted site permanently undischargeable, and admitting both silently exempts a site
#: nothing counts. Op-keying is what lets each pair carry its own verdict.
_LEGITIMIZED_SITES: dict[tuple[str, str, str, str, int], _Legitimation] = {
    (
        "ceremony.scoped_git_commit",
        "coordinator_core/ops/ceremony/git_native.py",
        "_git._invoke",
        "<dynamic>",
        0,
    ): _Legitimation(
        # `ceremony.scoped_git_commit`'s counting seam itself. `_count_op_spawns_both_ways` substitutes
        # the FUNCTION OBJECT `git_native._git` before invoking the op -- every call to
        # `git_native._git(...)`, whatever `_invoke`'s own body does internally, routes through the
        # substituted name first. This is not a call INTO the seam that the seam then hides from
        # the counter; it IS the seam, so it is counted by construction. Budget shape:
        # `overrides["ceremony.scoped_git_commit"].spawn_count_budget.green_path` (and siblings).
        counter=_SEAM,
        counted_by="coordinator_core/ops/ceremony/tests/test_commit_e2e_spawn_budget.py",
        executed="Seam substitution; every counted spawn on the op's green path routes through it.",
    ),
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
    # The three `ceremony.scoped_git_commit` bypasses that C6 closed. Each was a REAL bypass of
    # that op's `git_native._git` seam -- none of them route through it -- and none became counted
    # by being rerouted. What changed is the COUNTER: `test_commit_e2e_spawn_budget.py` now also
    # counts via the `subprocess.run` module attribute, scoped to the op invocation, against new
    # `op_total_*` manifest keys (23/24/28 vs the seam's 17/18/20). The seam counter is
    # routing-narrow by construction and could never have seen these; the global counter is
    # routing-agnostic and does. Rerouting them through `git_native._git` was the alternative and
    # was NOT taken: `auto_push` is a hook with callers outside this pipeline, so that would change
    # their behaviour to buy a measurement.
    (
        "ceremony.scoped_git_commit",
        "coordinator_core/session/scope.py",
        "_git_run",
        "git",
        0,
    ): _Legitimation(
        counter=_GLOBAL_SUBPROCESS_RUN,
        counted_by="coordinator_core/ops/ceremony/tests/test_commit_e2e_spawn_budget.py",
        executed="Measured 2026-08-19: origin-recorded at scope.py:420 during the op invocation, "
        "reached from `_handler`'s own `release_committed_claims`/`release_phantom_claims` calls. "
        "Counted by `op_total_green_path` and siblings (exact equality).",
    ),
    (
        "ceremony.scoped_git_commit",
        "coordinator_core/hooks/auto_push.py",
        "_run_git",
        "<dynamic>",
        0,
    ): _Legitimation(
        counter=_GLOBAL_SUBPROCESS_RUN,
        counted_by="coordinator_core/ops/ceremony/tests/test_commit_e2e_spawn_budget.py",
        executed="Measured 2026-08-19: origin-recorded at auto_push.py:360 during the op "
        "invocation, reached via `run_commit_pipeline` -> `_drain_pending_push_after_sync` -> "
        "`drain_pending_push`. Its three `auto_push` siblings (`push_once`, `_is_ancestor`, "
        "`_invoke_cockpit_publish`) were measured NOT to execute and stay red.",
    ),
    (
        "ceremony.scoped_git_commit",
        "coordinator_core/git/divergence.py",
        "_run_git",
        "git",
        0,
    ): _Legitimation(
        counter=_GLOBAL_SUBPROCESS_RUN,
        counted_by="coordinator_core/ops/ceremony/tests/test_commit_e2e_spawn_budget.py",
        executed="Measured 2026-08-19: origin-recorded at divergence.py:53 during the op "
        "invocation, reached from `run_commit_pipeline`'s `diverging_paths` call.",
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
        "coordinator_core/execute_plan_assemble/close_out_and_stamp.py",
        "_run_git",
        "git",
        0,
    ): _Legitimation(
        # The SAME physical site is deliberately NOT legitimized under
        # `execute_plan_assemble.sibling_committed_chunk_ids_memo` -- that op's fixture never
        # reaches it. See `_UNCOUNTED_MEASURED_UNREACHED`, and the op-keying note above for why
        # the register has to be able to say that.
        counter=_GLOBAL_SUBPROCESS_RUN,
        counted_by="coordinator_core/execute_plan_assemble/tests/"
        "test_dispatch_ledger_delivered_spawn_budget.py",
        executed="Measured 2026-08-19: origin-recorded at close_out_and_stamp.py:611 under the "
        "counter of `test_multiple_committed_rows_spawn_exactly_two_git_calls` "
        "(`assert spawns == budgeted`).",
    ),
    (
        "execute_plan_assemble.dispatch_ledger_delivered",
        "coordinator_core/execute_plan_assemble/close_out_and_stamp.py",
        "_batch_git_cat_file_check",
        "git",
        0,
    ): _Legitimation(
        counter=_GLOBAL_SUBPROCESS_RUN,
        counted_by="coordinator_core/execute_plan_assemble/tests/"
        "test_dispatch_ledger_delivered_spawn_budget.py",
        executed="Measured 2026-08-19: origin-recorded at close_out_and_stamp.py:2633 under the "
        "counter of `test_multiple_committed_rows_spawn_exactly_two_git_calls` "
        "(`assert spawns == budgeted`).",
    ),
    # The three CONDITIONAL-PATH `auto_push` bypasses C6 closed with a fixture, not a reroute
    # (`test_commit_e2e_spawn_budget.py::test_pending_drain_superseded_path_spawn_count_matches_
    # budget`, opro-03 C6, 2026-08-19). All three sit inside `run_push_with_retry`'s
    # non-fast-forward/superseded early return, reached only when the pending-push drain fires
    # AFTER a due record names a branch that is itself non-fast-forward-superseded against
    # origin -- a precondition no prior fixture here carried. `pending_drain_superseded`'s own
    # fixture plants that precondition on a SECOND, synthetic branch (`work/pending`) the op
    # never touches, so the op's own commit still lands cleanly on `work/main` and triggers the
    # drain, while `work/pending`'s divergence is what the drain's own push then resolves via
    # `_is_superseded`. Counted by `op_total_pending_drain_superseded` (39), never by the
    # `pending_drain_superseded` seam figure (23) -- none of the three route through
    # `git_native._git` (see that key's own manifest rationale).
    (
        "ceremony.scoped_git_commit",
        "coordinator_core/hooks/auto_push.py",
        "push_once",
        "git",
        0,
    ): _Legitimation(
        counter=_GLOBAL_SUBPROCESS_RUN,
        counted_by="coordinator_core/ops/ceremony/tests/test_commit_e2e_spawn_budget.py",
        executed="Measured 2026-08-19: `test_pending_drain_superseded_path_spawn_count_matches_"
        "budget` wraps `auto_push.push_once` via `monkeypatch.setattr` and asserts the wrapper "
        "fired (`push_once_calls >= 1`), against a fixture planting a due pending-push record "
        "naming a non-fast-forward-superseded branch. A DIRECT call measurement, not the "
        "control-flow argument that `run_push_with_retry`'s loop calls it unconditionally -- "
        "that argument is sound but is an inference that a measurement was implied, which is the "
        "shape this gate's third leg exists to refuse. Counted by "
        "`op_total_pending_drain_superseded` (exact equality).",
    ),
    (
        "ceremony.scoped_git_commit",
        "coordinator_core/hooks/auto_push.py",
        "_is_ancestor",
        "git",
        0,
    ): _Legitimation(
        counter=_GLOBAL_SUBPROCESS_RUN,
        counted_by="coordinator_core/ops/ceremony/tests/test_commit_e2e_spawn_budget.py",
        executed="Measured 2026-08-19: same fixture, DIRECTLY observed (not inferred from the "
        "spawn count) via a `monkeypatch.setattr(auto_push, \"_is_ancestor\", wrapper)` call "
        "counter asserted `>= 1` -- `_is_superseded`'s own call, deciding the local (schema-"
        "commit) sha is an ancestor of the freshly fetched `refs/remotes/origin/work/pending`, "
        "which is what drives the early-return branch these three sites share. Counted by "
        "`op_total_pending_drain_superseded` (exact equality).",
    ),
    (
        "ceremony.scoped_git_commit",
        "coordinator_core/hooks/auto_push.py",
        "_invoke_cockpit_publish",
        "<dynamic>",
        0,
    ): _Legitimation(
        counter=_GLOBAL_SUBPROCESS_RUN,
        counted_by="coordinator_core/ops/ceremony/tests/test_commit_e2e_spawn_budget.py",
        executed="Measured 2026-08-19: same fixture, DIRECTLY observed via a "
        "`monkeypatch.setattr(auto_push, \"_invoke_cockpit_publish\", wrapper)` call counter "
        "asserted `>= 1` -- the fixture plants `.github/scripts/publish_cockpit_contract.py` on "
        "the checked-out branch's working tree and a schema-touching commit on the synthetic "
        "branch, so `_maybe_publish_cockpit_contract`'s `_schema_touched` check (against the "
        "empty-tree fallback base, no prior `refs/remotes/origin/work/pending`) resolves True on "
        "the superseded early-return's own success leg. Counted by "
        "`op_total_pending_drain_superseded` (exact equality).",
    ),
    (
        "execute_plan_assemble.sibling_committed_chunk_ids_memo",
        "coordinator_core/execute_plan_assemble/close_out_and_stamp.py",
        "_run_git",
        "git",
        0,
    ): _Legitimation(
        # Was red for want of a SHAPE, not a fixture precondition -- this op's only budgeted shape
        # was `second_call_identical_inputs: 0`, the memo hit, which spawns nothing by
        # construction, so no counter of it could ever see any site. The first-call shape added
        # 2026-08-19 is what makes the op's own scan run at all. Note the counter here is the
        # module attribute `coas.subprocess.run`, which is routing-narrow by construction: it sees
        # this site precisely because the site spawns THROUGH that module's `subprocess`, and it
        # is why `repo_root.py::_spawn_rev_parse` stays red under this same op rather than riding
        # in on the same shape.
        counter=_GLOBAL_SUBPROCESS_RUN,
        counted_by="coordinator_core/execute_plan_assemble/tests/"
        "test_sibling_committed_chunk_ids_memo_spawn_budget.py",
        executed="Measured 2026-08-19: origin-recorded under a stack-recording `subprocess.run` "
        "wrapper, 3 of 3 spawns attributed to `close_out_and_stamp.py::_run_git` "
        "(`_chunk_evidence_log_range`'s merge-base plus two `_deliverable_log_records` "
        "queries), under the exact-equality counter of "
        "`test_first_call_with_one_sibling_spawn_count_matches_budget`.",
    ),
    (
        "ceremony.scoped_git_commit",
        "coordinator_core/hooks/auto_push.py",
        "_detach_and_run",
        "<dynamic>",
        0,
    ): _Legitimation(
        # The site put on this op's reachable set by `_replay_post_commit_auto_push` being wired
        # into `commit_scoped`'s private-index (diverged-path) branch -- NOT new from the
        # popup-guard/CREATE_NO_WINDOW work (verified: `git show d1feabb8c^:coordinator_core/
        # hooks/auto_push.py` has the identical `Popen` call and identical callers). Reached only
        # when `suppress_post_commit_auto_push` is False, i.e. the op's default `push_mode=
        # "deferred"` -- never on the `sync`/`never` shapes the other four `test_commit_e2e_
        # spawn_budget.py` fixtures exercise, which is why this was the ONLY site left standing
        # after opro-03 C6 emptied every other reachable bypass. Spawns via `subprocess.Popen`
        # (the Windows leg of `_detach_and_run`; POSIX forks instead and never reaches this
        # line), which the run-only `_GLOBAL_SUBPROCESS_RUN` counter structurally cannot see
        # regardless of reachability -- closed by widening the op's counter to also watch
        # `Popen`, not by rerouting the spawn (see `_GLOBAL_SUBPROCESS_SPAWN`'s own docstring for
        # why a second counter, not a loosened pin).
        counter=_GLOBAL_SUBPROCESS_SPAWN,
        counted_by="coordinator_core/ops/ceremony/tests/test_commit_e2e_spawn_budget.py",
        executed="Measured 2026-08-21: `test_deferred_diverged_commit_reaches_detached_push_"
        "spawn_count_matches_budget` wraps `auto_push._detach_and_run` via `monkeypatch.setattr` "
        "and asserts the wrapper fired (`detach_calls['n'] >= 1`), against a fixture whose sole "
        "diverged path (staged v2, worktree v3) routes `commit_scoped` down the hookless "
        "private-index branch under the op's default `push_mode='deferred'`. A DIRECT call "
        "measurement of the enclosing function, not an inference from the Popen count alone. "
        # Review: staff-eng Finding 12 — this legitimation cited a hardcoded
        # spawn count that drifted independently of the manifest pin it
        # names (recorded 28, then 26, then 25 across successive runs).
        # Cite the manifest key by name only; the exact figure lives at
        # `coordinator_core/benchmarks/budget-manifest.json`'s
        # `ceremony.scoped_git_commit.op_total_deferred_diverged_detach`.
        "Counted by the manifest key `op_total_deferred_diverged_detach` "
        "against that shape's own seam figure (`deferred_diverged_detach`, "
        "exact equality) -- see the manifest's own `_op_total_rationale` "
        "for the measured provenance and re-verification history.",
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
    """dotted module name -> relpath, over every in-scope `coordinator_core` file."""
    out: dict[str, str] = {}
    for record in records:
        dotted = _module_dotted_name(record.relpath)
        if dotted:
            out[dotted] = record.relpath
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
    func_defs: dict[tuple[str, str], object],
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
    func_defs: dict[tuple[str, str], object],
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


def _direct_call_targets(
    func_node,
    relpath: str,
    index: _FuncIndex,
    import_aliases_by_file: dict[str, dict[str, str]],
    func_aliases_by_file: dict[str, dict[str, tuple[str, str]]],
    local_aliases_by_file: dict[str, dict[str, tuple[str, str]]],
) -> set[tuple[str, str]]:
    """Every top-level `(relpath, func_name)` `func_node`'s body calls, one hop: same-module
    direct call, a PRECISE cross-module function import (`_import_function_aliases` -- pinned to
    the specific module named in the `from X import name` statement, never a repo-wide bare-name
    lookup; see that function's own docstring for why the imprecise version is unusable here),
    a module-level local-name-to-attribute alias (`_local_module_attr_aliases`), or a direct
    `module.attr(...)` attribute call through a tracked module-import alias
    (`_import_module_aliases`)."""
    out: set[tuple[str, str]] = set()
    aliases_here = import_aliases_by_file.get(relpath, {})
    func_aliases_here = func_aliases_by_file.get(relpath, {})
    local_here = local_aliases_by_file.get(relpath, {})
    for node in ast.walk(func_node):
        if node is func_node or not isinstance(node, ast.Call):
            continue
        callee = node.func
        if isinstance(callee, ast.Name):
            name = callee.id
            if (relpath, name) in index.func_defs:
                out.add((relpath, name))
            elif name in local_here:
                out.add(local_here[name])
            elif name in func_aliases_here:
                out.add(func_aliases_here[name])
        elif isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name):
            target_relpath = aliases_here.get(callee.value.id)
            if target_relpath and (target_relpath, callee.attr) in index.func_defs:
                out.add((target_relpath, callee.attr))
    return out


def _reachable_functions(
    entry_funcs: set[tuple[str, str]],
    index: _FuncIndex,
    import_aliases_by_file: dict[str, dict[str, str]],
    func_aliases_by_file: dict[str, dict[str, tuple[str, str]]],
    local_aliases_by_file: dict[str, dict[str, tuple[str, str]]],
) -> set[tuple[str, str]]:
    """Transitive closure (plain worklist BFS) over `_direct_call_targets`'s one-hop edges,
    seeded at `entry_funcs`. Terminates: `seen` grows monotonically over the finite domain of
    `(relpath, func_name)` pairs `index.func_defs` defines, so a round that adds nothing halts
    the loop -- same termination argument the reused module's own route-g fixed point makes."""
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


def _build_corpus():
    """One shared corpus build: scope files, `_FileRecord`s, the reused repo-wide `_FuncIndex`,
    and this gate's own import/local-alias indexes, each computed exactly once. Returns
    `(index, spawn_sites_by_file, import_aliases_by_file, func_aliases_by_file,
    local_aliases_by_file)`."""
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

    spawn_sites_by_file = {record.relpath: record.spawn_sites for record in records}
    return (
        index,
        spawn_sites_by_file,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
    )


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
    """The C-13 gate, now STANDING. For each of the nine live budgeted entrypoints, every
    `spawn_policy`-detected spawn site whose enclosing function is transitively reachable from
    that entrypoint must carry a `_LEGITIMIZED_SITES` entry for THAT op.

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
         undischargeable for want of a shape, not a precondition.
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

    Every `executed` leg cites a DIRECT measurement -- a call counter asserted `>= 1`, or a
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
    self-test above builds (`import helper` / `import wrapper` at top level, no package)."""
    out: dict[str, str] = {}
    for record in records:
        if record.relpath.endswith(".py"):
            out[record.relpath[:-3]] = record.relpath
    return out


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
    did until this chunk measured it."""
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
    function-level BFS the rot guard above uses for its own nine entrypoints
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
#: naturally produces MORE distinct sites than the nine-op rot guard's
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
        ("coordinator_core/hooks/track_touched_files.py", "_ensure_session_dir", "git", 0),
        ("coordinator_core/hooks/track_touched_files.py", "_ensure_session_dir", "git", 1),
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
        ("coordinator_core/ops/eol/census.py", "_check_attr_eol_text", "git", 0),
        ("coordinator_core/ops/eol/census.py", "_dirty_paths", "git", 0),
        ("coordinator_core/ops/eol/repair.py", "_index_blobs", "git", 0),
        ("coordinator_core/ops/fleet/archive_handoffs.py", "_shipped_in_resolvable", "git", 0),
        ("coordinator_core/ops/fleet/archive_plans.py", "_plan_worktree_dirty", "git", 0),
        ("coordinator_core/ops/fleet/archive_plans.py", "_plan_worktree_dirty_batch", "git", 0),
        ("coordinator_core/ops/fleet/archive_shipped_handoffs.py", "_sha_reachable", "git", 0),
        ("coordinator_core/ops/fleet/memo_send.py", "_commit_delivered_memo", "git", 0),
        ("coordinator_core/ops/fleet/memo_send.py", "_commit_delivered_memo", "git", 1),
        ("coordinator_core/ops/fleet/memo_send.py", "_commit_delivered_memo", "git", 2),
        ("coordinator_core/ops/fleet/memo_send.py", "_commit_delivered_memo", "git", 3),
        (
            "coordinator_core/ops/fleet/memo_send.py",
            "_commit_delivered_memo._unstage_delivered_memo",
            "git",
            0,
        ),
        ("coordinator_core/ops/fleet/memo_send.py", "_git_check_ignore", "git", 0),
        (
            "coordinator_core/ops/fleet/memo_send.py",
            "_verify_scoped_to_sha_resolvable._rev_parse",
            "git",
            0,
        ),
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
        ("coordinator_core/ops/review_trail_readjudication_report.py", "_full_range_shas", "git", 0),
        ("coordinator_core/ops/review_trail_readjudication_report.py", "_resolve_repo_root", "git", 0),
        ("coordinator_core/ops/review_trail_readjudication_report.py", "_run", "<dynamic>", 0),
        ("coordinator_core/ops/review_trail_write.py", "_git_runner", "<dynamic>", 0),
        ("coordinator_core/ops/review_trail_write.py", "_reject_empty_sha_range", "git", 0),
        ("coordinator_core/ops/review_trail_write.py", "_reject_empty_sha_range", "git", 1),
        ("coordinator_core/ops/review_trail_write.py", "_resolve_ref_to_sha", "git", 0),
        ("coordinator_core/ops/run_pip_audit.py", "_run_pip_audit", "<dynamic>", 0),
        ("coordinator_core/ops/run_pre_ci_hooks.py", "_run_pre_ci_hooks", "<dynamic>", 0),
        ("coordinator_core/ops/run_semgrep_scan.py", "_diff_scoped_files", "git", 0),
        ("coordinator_core/ops/run_semgrep_scan.py", "_run_semgrep", "semgrep", 0),
        ("coordinator_core/ops/run_shellcheck_sweep.py", "_lint_one_file", "shellcheck", 0),
        ("coordinator_core/ops/run_shellcheck_sweep.py", "_run_git", "git", 0),
        ("coordinator_core/ops/session/boot_sweep.py", "_commit_consumed_metadata", "git", 0),
        ("coordinator_core/ops/session/boot_sweep.py", "_commit_consumed_metadata", "git", 1),
        ("coordinator_core/ops/session/boot_sweep.py", "_commit_consumed_metadata", "git", 2),
        ("coordinator_core/ops/session/boot_sweep.py", "_commit_consumed_metadata", "git", 3),
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
_FROZEN_UNENROLLED_INVENTORY_HIGH_WATER = 149


def test_unenrolled_spawn_bearing_ops_are_declared_in_the_frozen_inventory():
    """THE defect this chunk exists to close, made structural: every spawn
    site belonging to a live op that is not a key of `_BUDGETED_ENTRYPOINTS`
    must already be a member of `_FROZEN_UNENROLLED_SPAWN_SITES`. A NEW
    unenrolled spawner -- an op added to the registry after this freeze,
    reachable from spawn evidence, and never enrolled -- produces a site key
    this frozenset does not contain, and fails loudly here instead of being
    invisible the way `ceremony.wsc_tail` was until an EM hand-built a probe
    while chasing an unrelated flaky KPI (this plan's own worked example)."""
    keys_to_ops = _live_unenrolled_spawn_site_keys()
    undeclared = sorted(set(keys_to_ops) - _FROZEN_UNENROLLED_SPAWN_SITES)
    assert not undeclared, (
        f"{len(undeclared)} spawn site(s) belong to a live op that is neither enrolled in "
        "_BUDGETED_ENTRYPOINTS nor declared in _FROZEN_UNENROLLED_SPAWN_SITES -- an op that "
        "spawns and was never enrolled is invisible, which is the defect this gate exists to "
        "close. Enroll the op with a real op_total_* pin, delete the spawn, or (if this is a "
        "genuinely new-but-still-open gap) add the site_key to "
        "_FROZEN_UNENROLLED_SPAWN_SITES and raise _FROZEN_UNENROLLED_INVENTORY_HIGH_WATER to "
        "match -- never silently:\n"
        + "\n".join(
            f"  {k} (op: {sorted(keys_to_ops[k])})" for k in undeclared
        )
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
