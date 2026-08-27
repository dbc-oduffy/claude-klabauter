"""Amplification collector (G1): sibling to `spawn_policy`, resolving generic runners and
injected runners -- the third state neither existing gate expresses.

Spec backlink: `pln-kill-the-n-1-git-spawn-class-a-88897a`,
`## Tasks` chunk G1 (this collector) and G2 (the two assertions this collector feeds, landed
in a later wave over this same file). Widened past git by
`docs/plans/2026-08-15-composition-invocation-budgets.md` chunk C11 (AC11).

TWO LEGS, ONLY ONE OF THEM A GATE. Read a failure line before believing a failure count.
`test_no_new_amplification_sites_outside_known_inventory` is the STANDING gate: unmarked, in
the fast tier, and red only when a site outside `_KNOWN_SITES` exists.
`test_burn_down_known_preexisting_amplification_sites` carries `@pytest.mark.designed_red`,
is deselected from the fast tier, and is red BY DESIGN over a ~40-entry list its own plan
(`docs/plans/2026-08-15-composition-invocation-budgets.md`) calls "a non-gating burn-down
list, not budgets". An unfiltered run of this file therefore reports `2 failed` in the healthy
state, and the burn-down leg's diff dwarfs the standing leg's -- two peers independently read
that as ~42 new violations on 2026-08-26 when the real answer was four. The standing leg's
site list is the only one that means "something regressed".

EVERY SPAWN VERB, NOT JUST GIT (AC11). This collector was built for the N+1 GIT spawn class and
counted only calls whose argv0 resolved to the literal "git". A composition budget cares about
processes, and the ops census's single most expensive per-item site is `[sys.executable, "-m",
"pytest", *refs]` -- a git-only filter cannot see it, and neither can any widening keyed on an
argv0 ALLOWLIST: `spawn_policy._resolve_argv0` reports a `sys.executable`-fronted argv as
`<dynamic>`, because the program name is an `ast.Attribute` with no static value. So the argv0
filter was REMOVED rather than extended, and the routes now resolve on "does this callee reach a
spawn" alone. Measured repo-wide over `coordinator_core` + `coordinator/bin`: 90 -> 154 distinct
site keys, 53 -> 100 files, and no site the git-only collector found was lost. What that bought
is the whole reason AC11 exists -- `cutover_gate`'s pytest fan-out, `cruft_sweep`'s deletes,
`find_polluter`'s `npm test`, `backfill_initiative_fk`'s CLI attach, `setup_chain_walker`'s
probes, and all four machine-local rows were invisible by construction before it.

The FILE keeps its git-shaped name: five other modules cite this path, its inventory audit is
published under it, and the git class it was built for is a subset of what it now covers. The
SYMBOLS do not -- see `AmpSite` / `find_unbatched_per_item_spawns`.

THE GAP THIS COLLECTOR CLOSES. `test_no_bare_hot_path_spawn.py` asserts a property of each
individual call -- console suppression -- with no concept of call COUNT: a maximally-compliant
spawn inside a 3000-iteration loop is the defect and passes. `test_no_spawn_per_item_loop.py`
asserts an amplification-shaped property but the opposite discrimination: it fires only where
argv is INVARIANT with respect to the loop target (hoistable to one call), and by its own tested
acceptance criterion must stay SILENT on varying argv -- that silence is a negative control, not
an omission (see this module's own negative-spec block below; do NOT read this collector as
relaxing that silence). This collector's class is a third state neither expresses: *varying argv,
but batchable into a single call* -- one spawn per loop item, where the callee itself directly
reaches a spawn, reachable through a local helper, a cross-module import, a dependency-injected
runner, a runner bound as a parameter default, or a generic `_run(argv)` wrapper only the call
site can see an argv for.

REUSE FROM `spawn_policy`, UNMODIFIED (pinned API, `tasks/shell-spawn-regrowth-gate/
PINNED-API.md`): `discover_source_files`, `sites_in_source`, `is_test_tree_site`,
`DEFAULT_EXCLUDE`, `SpawnParseError`. This module does NOT extend `SpawnSite` -- it is a frozen
dataclass under that pinned API -- and instead defines a sibling, `AmpSite`, the same
precedent `LoopSpawnSite` (`test_no_spawn_per_item_loop.py`) and `BareSpawnSite`
(`test_no_bare_hot_path_spawn.py`) already set.

ONE collector, TWO assertions -- this wave ships only the collector plus its own planted-fixture
self-tests. G2 (next wave, same file) adds the standing frozen-inventory subset assertion (bites
on any NEW site) and the non-gating `designed_red` worklist over the known-114 high-precision
stratum, sharing this collector exactly as `_STANDING_GATE_FAMILIES` / `_ALL_FAMILIES` share
`find_bare_hot_path_spawns` in `test_no_bare_hot_path_spawn.py`. Do not add either assertion here.

SCOPE. `_GATE_SCOPE_ROOTS` names `coordinator_core` AND `coordinator/bin` -- AC4. Neither
existing gate scans `coordinator/bin/`, and that is where the worst site in this plan's audit
lives. Restricted to the HIGH-PRECISION STRATUM: the callee must DIRECTLY contain a spawn
(one hop, by one of the six routes below), never a transitive/multi-hop reach. The prototype
measured the transitive deep tail at 32% TP with no static discriminator separating true from
false positives at any depth -- deliberately excluded here, tracked instead as G2's named
residual.

STRUCTURAL DISCRIMINATORS -- SEVEN TOTAL, not the three this heading historically named (measured:
32.4% naive FP -> 4.2% with discriminators 1-3 applied, zero true positives lost). Those rates
were measured in 2026-08-08 against the GIT-ONLY collector and have NOT been re-measured for the
widened one -- the discriminators are argv0-independent, so there is no reason to expect them to
move, but that is an argument, not a measurement, and it should not be cited as one. What was
actually checked at the AC11 re-freeze is narrower and worth exactly what it is: the 83
newly-added keys were read individually, and the single false-positive class found (a helper
sharing a spawn's line) was closed in route a rather than frozen into the inventory.

Discriminators 1-3 are listed below; 4 (chunking-stride iterables) and 5 (verb-gated dispatch
chokepoints) are documented at their definition sites further down rather than repeated here --
a pre-existing drift between this heading's enumeration and the module's actual count that this
change does not take on fixing. Discriminators 6 (varying argv0, added 2026-08-17) and 7
(argv-splicing loop target, added 2026-08-19) are listed here in full because they belong to the
same "does this loop even qualify" family as 1-3. Neither is covered by the 32.4%/4.2% figures
above (those predate them) and neither has had its own FP rate measured -- state plainly rather
than let them inherit a number they did not earn.

BOTH 6 AND 7 SUPPRESS, which inverts the safety direction every other discriminator here runs
in: an over-broad match silences a real amplification site and nothing downstream notices. Any
future discriminator whose action is EXCLUDE inherits that inversion -- see
`_tainted_names_for_loop`'s docstring, where a version of this reasoning shipped backwards and
caused a real false suppression.

  1. Loop-ITERABLE-expression exclusion. A `for`/comprehension's `iter` expression is evaluated
     ONCE, before the first iteration -- a call appearing there is not a per-item spawn. This
     collector visits only the loop BODY under loop context, never the `iter`/generator-0 `iter`
     subexpression.
  2. Constant-literal-sequence exclusion. `for x in <literal tuple/list/set/dict>` (or a `Name`
     bound at module scope to one, optionally through `enumerate`/`sorted`/`reversed`/`.items()`)
     has an iteration count fixed at author time -- excluded wholesale.
  3. `while`-loop exclusion. All measured `while` FPs were retry loops, interactive prompts, or
     calendar walks bounded by a constant, a human, or a fixed window, never by input size --
     `while` loops are excluded wholesale (only 11 hits repo-wide carried this shape; the
     false-negative exposure is accepted, matching this collector's stated bias).
  6. Varying-argv0 exclusion. A loop that spawns a DIFFERENT PROGRAM on each iteration cannot be
     batched into one call -- there is no single argv0 for the batch to share. Decided by a
     bounded one-hop taint pass, seeded from the enclosing loop's own target names and grown by
     one `ast.Assign`/`ast.AnnAssign` fixed-point hop per round (`_tainted_names_for_loop`): the
     call's own `argv[0]` (`_argv0_expr`, the two shapes measured to occur -- a `List` literal,
     or `<List> + <rest>`) qualifies for exclusion when it references a tainted name
     (`_argv0_varies_with_loop_target`). NOT FP-measured against the 32.4%/4.2% figures above --
     added 2026-08-17, retiring two `_EXEMPT_SITES` entries this pass mechanically decides
     (`path_resolution_report._check_windows`, `cruft_sweep.sweep_toolchain_caches`); see that
     constant's own comment for what still requires human judgment.
  7. Argv-splicing-loop-target exclusion. A loop whose target is concatenated into argv as a
     SEQUENCE (`base + chunk`, `[*base, *chunk]`) rather than placed in it as one element
     carries the whole group in ONE call, so its spawn count is O(total_argv_bytes / ceiling),
     never O(items). This is the byte-budget chunking idiom -- a chunk list built at runtime
     against Windows' 32767 `CreateProcess` cap, which discriminator 4 cannot see because it
     reads only a literal `range(start, stop, stride)`. Added 2026-08-19 for the two measured
     sites (`publish._git_status_porcelain`, `percolate-round._dest_paths_exist`), both of which
     hit a git subcommand with no `--pathspec-from-file` form; keyed on the loop's OWN target
     names, never discriminator 6's grown taint set. See `_argv_splices_loop_target`.
  8. Varying-argv0-through-one-helper exclusion. Discriminator 6's fact -- a different PROGRAM
     each iteration, so no single argv0 exists for a batch to share -- reached across ONE call
     hop, for the routes where 6 is deliberately forbidden (b/c). Added 2026-08-19 because 6 was
     measured structurally blind to most of this repo: of the 65 call sites behind the exemption
     register's 53 entries, 41 are route `b-local-helper` and only 16 are route a. Resolves the
     callee, requires the HELPER's own argv0 to be one of the helper's parameters, then requires
     the argument supplied for that parameter to be loop-tainted -- so a verb-gated
     `_run_git([verb, ...], root)`, whose argv0 is a literal, is never reached, and 6's route-a
     restriction is not relaxed. One hop, no chaining, no fallback. See
     `_argv0_varies_through_helper`. SUPPRESSES: measured at introduction to retire exactly one
     key (`maximalist._run_body`) and silence nothing outside the register.
  9. Repetition-loop exclusion. A loop whose TARGET IS DISCARDED over a count-bounded `range`
     spawns the SAME argv N times -- a repetition, not a fan-out over items. There is no set for
     a batch to carry, and the sibling gate's remedy (hoist out of the loop) would delete N-1
     intentional repetitions, changing meaning rather than batching. Applies at BOTH loop forms:
     the retired benchmark keys each carried a `for _ in range(warmup)` statement AND a sampling
     comprehension, so a statement-only matcher would have retired none of them. Safety rests
     entirely on `_is_count_bounded_range` -- `range(len(items))` scales with input size and is
     real amplification, so any `Call` in the argument position declines. Added 2026-08-19,
     retiring the five `measurement-is-the-loop` sampling entries whose own comment block
     asserted that no static pass could ever decide them; measured, zero collateral. Does NOT
     reach the MISCLASSIFIED `retry-loop` rows, which read their target back. See
     `_is_repetition_loop`.
 10. Retry-loop exclusion. Discriminator 3 excludes `while` loops wholesale because every
     measured `while` false positive was a retry, a prompt, or a calendar walk -- bounded by a
     constant, a human, or a window, never by input size. The same retry spelled `for attempt in
     range(_MAX_ATTEMPTS)` was still flagged, which was an accident of SPELLING. Two halves,
     both required: the LOOP is a count-bounded range with an early exit (`_is_retry_loop`), and
     at the CALL, none of the loop's tainted names reach the spawn's own arguments
     (`_names_in_call_args`) -- so every iteration issues an identical argv. The call half is
     what separates a retry from a fan-out, and without it `for _ in range(3): run([..., item])`
     nested in a per-item loop would be falsely suppressed. Added 2026-08-19, retiring the three
     MISCLASSIFIED `retry-loop` keys structurally; measured, zero collateral. See
     `_is_retry_loop`.

SEVEN DETECTION ROUTES (six per gate-substrate.md Task C, plus g added out-of-band -- see its
own entry below), restricted to the high-precision stratum:

  a-direct       -- the call itself is a recognized `subprocess`/`os`/`asyncio` spawn (via
                    `sites_in_source`). Matched on the detected spawn LINE *and* a recognized
                    spawn-API callee name (`_SPAWN_API_NAMES`), because a line routinely carries
                    a second call -- `subprocess.call(argv, **no_console_passthrough_kwargs())`
                    -- and matching on line alone reported the helper as the site.
  b-local-helper -- the callee is a function DEFINED IN THE SAME MODULE whose own body directly
                    contains a spawn site.
  c-cross-module -- the callee is imported (`from X import name`) and resolves, via a repo-wide
                    name index built over the same scope, to a function in another module whose
                    own body directly contains a spawn site. Resolution is by the ORIGINAL
                    imported name AND its resolved SOURCE MODULE (`_import_resolves_to`), not
                    merely the local binding: an aliased import (`from a import f as g`) makes
                    the local binding (`g`) and the definition name (`f`) diverge, and a lookup
                    keyed on the local binding alone can only ever find HOMONYMS of the alias --
                    a same-named-but-unrelated function elsewhere, never the one actually
                    imported. The resolver (`_resolve_reexport_chain`) handles three cases: an
                    `__init__.py` IS the module it packages, never `pkg.__init__`; a re-export
                    hop (the named module itself imports the same original name from somewhere
                    else) is followed, carrying and REWRITING the name at each hop, bounded at
                    `_REEXPORT_HOP_BOUND` hops with a star re-export (`from .impl import *`)
                    declining to constrain rather than pruning to nothing; and a `level > 0`
                    relative import resolves against the importing file's own package, where an
                    `__init__.py`'s package is itself, not its parent. Fixture:
                    `undetermined` imported from `coordinator_core.plan_assemble.predicates`,
                    defined in that package's `__init__.py` -- see
                    `test_route_c_resolves_reexported_init_name` and its siblings. Landed
                    2026-08-26 (`pln-route-c-resolves-the-imported-name-not-the-local-alias`,
                    this file's own C1 chunk).
  d-injected     -- a bare-`Name` argument sits in a runner-shaped position (a kwarg named
                    `run`/`runner`/`git`/`git_runner`/`run_git`/`spawn`, OR the passed
                    identifier's own first token is `run`/`git`/`spawn`) and resolves, via the
                    same repo-wide index, to a function that directly makes ANY recognized spawn
                    call (not necessarily git-argv'd at its own definition site -- the injected
                    runner's git-ness is supplied by the CALLER, exactly the
                    `session_attribution.trailer_foreign_shas(..., run=_run)` shape). TIGHT rule,
                    deliberately: a loose "resolves to any transitive spawner" version measured
                    189 near-all-false hits against this repo; requiring a runner-SHAPED position
                    is what keeps it at the measured 1 true positive.
  e-generic-runner -- the callee resolves to a "generic runner" -- a single-parameter function
                    whose body forwards that parameter, unchanged, as the argv-bearing arg of a
                    recognized spawn call (the `_run(argv)` wrapper idiom) -- and the ACTUAL
                    argument passed at THIS call site is argv-SHAPED (a non-empty list/tuple
                    literal, or a non-empty command string). Shape is read at the call site
                    because the wrapper's own body only ever sees a bare parameter name.
  f-default-runner -- the callee is a PARAMETER of the enclosing function whose default binds a
                    module-level function that directly spawns -- the injectable-seam idiom
                    (`def resync(..., *, run_git=_update_index_with_retry)`), where the loop body
                    calls the parameter rather than the function. Route d reads a runner passed
                    AT a call site; this reads one bound a hop up as a default, which route d's
                    own docstring already named as a miss. Added by AC11, and load-bearing: it is
                    what keeps the two `_common.py` index-resync sites visible. Until AC11 those
                    were found only by an accidental name collision with an unrelated `run_git`
                    in another module -- a true site on a false route -- and tightening route e
                    removed the collision, so without route f the widening would have LOST two
                    real sites while adding sixty-four.
  g-forwarded-runner -- a bidirectional FIXED POINT over parameters, resolving an injected
                    runner by where it actually FLOWS rather than by what it is called or what
                    it is named at its own definition site. A parameter is "invoked" when the
                    loop body calls it directly, or when it is forwarded into another
                    function's own invoked parameter (the forwarding closure); it is "tainted"
                    when a direct spawner (route b's same-module resolution, or route c's
                    imported-name resolution) actually flows into it at some REAL call site --
                    never a parameter default, that stays route f's job -- through any length
                    of forwarding chain. SPAWN-BEARING = invoked ∩ tainted; requiring both is
                    what keeps precision, since tainted alone would flag every
                    dependency-injection seam whether or not the loop body ever calls it.
                    Detected at an already-qualifying loop call site (discriminators 1-3/6 and
                    `_EXEMPT_SITES` still apply ahead of it) when the loop body calls a
                    spawn-bearing parameter of its enclosing function, or forwards one into a
                    callee at a position that is itself spawn-bearing. Closes the identifier-
                    renaming half of route d's own by-name blind spot -- a runner forwarded
                    through a parameter spelled differently from the function it is bound to
                    (`resolve_range_shas` forwarding `_resolve_range_shas`) -- but resolution
                    is still by bare `ast.Name` at each forwarding hop, same-module-first-
                    else-imported like routes b/c/f; it does NOT resolve the transitive deep
                    tail this module excludes wholesale (a callee reached only by chaining
                    past what leg 1/leg 2's own fixed point tracks is still out of scope,
                    matching every other route's restriction to the high-precision stratum).

Routes `d` and `e` are kept deliberately, even though they are individually rare (14 combined
measured hits), because they are the ONLY reason the audit's three worst sites are visible at
all -- the prototype's own first cut, without them, missed all three. Dropping either reproduces
that exact gap.

NO `# amplification-ok:` PRAGMA. The discriminators above are structural, not a checklist a call
site can opt out of by comment -- a pragma would let the class regrow behind a comment, the
inverse of the discharge test this whole plan answers to.

RE-ENTRANCY SENTINEL (anti-scope 20). This gate must sit OUTSIDE the corpus it measures. Because
it lives at `coordinator_core/tests/`, `is_test_tree_site` already filters it out of every real
scan -- but trusting that silently is exactly what anti-scope 20 forbids. `_discover_scope_files`
therefore asserts, LOUDLY (a raised `RuntimeError`, never a silently-skipped check), that this
module's own file never appears in a discovered file list it is about to walk.

NEGATIVE SPEC -- what this collector deliberately does NOT do:

  - Does not touch, import from, or relax `test_no_spawn_per_item_loop.py`'s invariant-argv gate
    in any way. That gate's silence on varying argv is its own tested negative control; this
    collector is a sibling, never a widening of it.
  - Does not extend `spawn_policy.detect.SpawnSite`; see `AmpSite` below.
  - Does not import `spawn_policy.detect._RECOGNIZED`, despite `_SPAWN_API_NAMES` projecting it.
    That name is private and this module's reuse is restricted to the pinned API; the two are
    held together by an assertion (`test_spawn_api_names_track_spawn_policy`) rather than by an
    import that would widen the pinned surface.
  - Does not resolve the transitive deep tail (multi-hop call chains). A callee that only
    *eventually* reaches a git spawn is out of scope for every route above.
  - Does not report reachability, hot-path status, or live cost -- matching `spawn_policy`'s own
    negative-spec convention, this collector reports call-SITES only.
  - Does not ship any standing/designed_red assertion in this wave -- see "ONE collector, TWO
    assertions" above; that is G2's job, over this same file, in the next wave.

KNOWN BLIND SPOTS (false-negative-biased, matching every sibling gate's stated preference):

  - Route b/e resolution is by function NAME only, not full import-graph resolution -- a
    same-named function in two unrelated modules can collide (the prototype's own `dict.get()`
    mis-resolution artifact, in the deep tail it excludes). Accepted here because routes b/e
    are restricted to the high-precision stratum, where this collector independently verifies the
    resolved function's body via `sites_in_source`/spawn-detection before counting a route, not
    by name alone. Route c is narrower than this bullet historically described: it resolves the
    ORIGINAL imported name and its SOURCE MODULE (`_import_resolves_to`), not the local binding
    alone -- see route c's own entry above for what that does and does not cover (a re-export
    chain past `_REEXPORT_HOP_BOUND` hops, or one behind an unresolvable star re-export, still
    degrades to the by-name rule this bullet describes for b/e).
  - Nested-generator constant-literal detection (discriminator 2) is applied to a comprehension's
    FIRST generator only; a non-literal outer generator with a literal inner one is not
    specially handled.
  - `_generic_runner_param`'s single-spawn-forwarding detection does not exclude nested function
    definitions from its `ast.walk` scan the way `test_no_spawn_per_item_loop`'s `_iter_own_scope`
    does -- a spawn call inside a nested closure inside a would-be runner can be mis-attributed to
    the outer function. Accepted per this module's stated false-negative-over-false-positive
    preference (a broader match here can only ADD candidate runners, and route e still requires
    the call site's own argv to look git-shaped before counting a violation).
  - Route e currently matches NOTHING repo-wide. Its two pre-AC11 hits were the `run_git`
    name collision described under route f, and requiring the runner to forward into a
    recognized SPAWN call retired both. The route is kept, with its self-tests, because the
    cross-module `_run(argv)` shape it exists for is a real idiom this repo can reintroduce at
    any time -- a route that currently matches nothing is not the same as a route that cannot.
  - Route f resolves a parameter default by NAME within the file that declares it, and only for
    a default that is a bare `Name`. A seam defaulting to an attribute (`run=mod.helper`), to a
    lambda, or to `None` with the real runner chosen inside the body is not traced.
  - `_generic_runner_param` (route e) only recognizes a runner with EXACTLY ONE
    positional-or-keyword parameter (`len(params) != 1: return None`). This repo's dominant
    `GitRunner` idiom takes TWO (`argv`, `cwd`) -- e.g. `chain_attribution.GitRunner =
    Callable[[List[str], Optional[str]], ...]`, `generate_session_attribution_golden._run(args,
    cwd)`, `wsc-coverage-gate-runner._git_run_for_session_attribution(cmd, cwd=None)` -- so any
    cross-module generic runner shaped like the codebase's own real convention is invisible to
    route e, even though the module's own self-test only exercises the one-param shape. Accepted
    per this module's stated false-negative bias; route e's parameter-count check is not widened
    here (see G2's frozen `_KNOWN_SITES` inventory, the actual regrowth guard).
  - Route g's `_forwarded_arg_slots` matches a bare `ast.Name` argument at each forwarding
    hop only. An attribute-forwarded runner (`obj.run`), one wrapped in a `lambda`, or one
    selected by a branch inside the forwarding function's own body is invisible to it -- the
    same bare-`Name`-only restriction every other route in this module already accepts. Route
    g closes the IDENTIFIER-RENAMING half of route d's by-name blind spot (a runner forwarded
    through a parameter spelled differently from the function it is bound to); it does NOT
    close the transitive deep tail this module excludes wholesale, nor route f's own separate
    gap (a default that is not a bare `Name`) -- those remain real and unaddressed here.
  - `AmpSite.key` (`(path, enclosing, callee)`) excludes `route` by design, so when the SAME
    key is independently reachable via two different routes in one run (e.g. a callee that is
    both a same-module direct spawner and a parameter-default-bound name in the same scope),
    dedup silently keeps whichever route was appended first. This can only suppress route
    diversity in a report, never lose a true violation or admit a false one -- acceptable per
    this module's stated false-negative-biased design.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
import re
from typing import Callable

import pytest

from coordinator_core.spawn_policy import (
    SpawnParseError,
    is_test_tree_site,
    sites_in_source,
)
from coordinator_core.spawn_policy.detect import DEFAULT_EXCLUDE, discover_source_files

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_THIS_FILE = pathlib.Path(__file__).resolve()

#: Leaf function names of every call `spawn_policy.detect` recognizes as a spawn. Route a
#: matches a call by LINE (`SpawnSite.lineno`), and a line often carries more than one call --
#: `subprocess.call(argv, **no_console_passthrough_kwargs())` is two. Without a callee check the
#: kwargs helper is reported as the amplification site, which was a visible artifact even before
#: AC11 (`int`, `join` and `Path` all sit in the frozen inventory for exactly this reason) and
#: got worse once every spawn verb started counting rather than git alone.
#:
#: SSOT is `spawn_policy.detect._RECOGNIZED`; this is a leaf-name projection of it, kept local
#: because that name is private and this module's reuse is restricted to the pinned API (see
#: "Reuse from spawn_policy"). `test_spawn_api_names_track_spawn_policy` pins the two together
#: so the copy cannot drift silently.
_SPAWN_API_NAMES: frozenset[str] = frozenset(
    {
        "run", "call", "check_call", "check_output", "Popen",
        "system", "popen",
        "execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe",
        "spawnl", "spawnle", "spawnlp", "spawnlpe",
        "spawnv", "spawnve", "spawnvp", "spawnvpe",
        "posix_spawn", "posix_spawnp",
        "spawn",
        "create_subprocess_shell", "create_subprocess_exec",
    }
)

#: AC4 -- coordinator/bin/ MUST be in scope; neither existing gate scans it.
_GATE_SCOPE_ROOTS: tuple[str, ...] = ("coordinator_core", "coordinator/bin")

#: What a GREEN run of this gate does and does not mean -- the published horizon.
#:
#: This collector is ONE-HOP by construction (see "SCOPE" above and the negative control
#: `test_deep_tail_not_flagged`, which pins that silence as intended). A per-item spawn site more
#: than one call hop from its driving loop is outside it, and carries no exemption, because the
#: predicate never reaches it to file one against. The cost of that choice is measured, not
#: theoretical: `session/scope.py :: compute_scope` spent 219-391ms over 35 git spawns through a
#: four-hop chain this gate reads as clean, and it was found by hand measurement on a P1 record
#: rather than by the gate whose job is finding it.
#:
#: So: green here means "no NEW one-hop per-item spawn site outside the frozen inventory". It
#: does NOT mean "this tree has no per-item spawn sites". Anything citing this file as THE
#: enforcement mechanism for the composition spawn-count budget is overclaiming, and
#: `test_the_one_hop_horizon_is_published_where_this_gate_is_cited` holds the citers to saying so.
#: Widening past one hop was declined on measurement (32% TP in the deep tail, no static
#: discriminator separating true from false positives at any depth); a separate ADVISORY
#: deeper-reachability collector covers what this gate cannot --
#: `coordinator_core/tests/test_deep_per_item_spawn_worklist.py`. Its published precision is
#: 33%/75%/83% at depths 2/3/4 (n=12 per stratum, single unblinded judge, wide overlapping
#: intervals -- read as an order-of-magnitude sanity check, not a floor). SUPERSEDED
#: 2026-08-26: every reachability edge in that sample was resolved by route c's pre-fix
#: alias-blind rule (`pln-route-c-resolves-the-imported-name-not-the-local-alias`), so the
#: figures are an upper bound rather than a measured property of the fixed instrument. No
#: revised figure is asserted here; the re-sample is tracked at
#: `state/improvement-queue/2026-08-26-re-sample-the-deep-collector-precision-post-seam-fix.yaml`.
#: It runs at `@pytest.mark.cadence`, never fast tier: a full run is ~60-80s process time, roughly two
#: orders over the 500ms brightline, so it is offline-advisory only and never gating.
#:
#: Public (no leading underscore) unlike its module-private siblings below: that successor
#: instrument (`coordinator_core/tests/test_deep_per_item_spawn_worklist.py`) names this
#: constant as the thing it updates, which requires it importable.
COVERAGE_HORIZON = (
    "one-hop by construction: a per-item spawn more than one call hop from its loop is invisible "
    "to this gate, by design and without an exemption. Green means no new one-hop site, never "
    "that no per-item spawn sites exist."
)

#: Closed list of the doctrine surfaces that cite this gate as an enforcement mechanism. Each must
#: name the horizon, so "the gate is green" cannot be read as blanket coverage anywhere it is
#: cited. A new citer that omits the marker fails this gate; the fix is one qualifying clause in
#: the citing doc, never an entry dropped from here.
#:
#: CLAUDE.md is the other live citer and is deliberately NOT here. It cites this gate without the
#: qualifier; the clause was put to the PM on 2026-08-25 and DECLINED -- always-loaded doctrine
#: stays terse, and the horizon is published in this module and in the wiki instead. So its absence
#: is a ruling, not an omission: do not add it back without a new PM decision, and do not read
#: CLAUDE.md's unqualified sentence as the gate's actual coverage.
_HORIZON_CITERS: tuple[str, ...] = ("docs/wiki/cost-budgets-and-the-kill-disposition.md",)

#: The marker every citer must carry. The shortest phrase that cannot be written by accident and
#: cannot be satisfied by a passing mention of the filename.
_HORIZON_MARKER = "one-hop"

_RUNNER_KWARG_NAMES: frozenset[str] = frozenset(
    {"run", "runner", "git", "git_runner", "run_git", "spawn"}
)
_RUNNER_NAME_PREFIXES: tuple[str, ...] = ("run", "git", "spawn")

#: THE REGISTER'S REPLACEMENT, and where it is going. `_EXEMPT_SITES` holds a claim a human
#: wrote once; `_ORACLE_CLAIMS` below holds the same kind of claim bound to a TEST THAT MEASURES
#: IT. Both suppress a site. Only one of them can be wrong without anybody finding out.
#:
#: A claim about a command-line surface -- "`git config --unset` accepts exactly one key", "this
#: sibling CLI writes one record per invocation" -- is not decidable from any AST, which is why
#: those entries survived the discriminators. It IS decidable by running the thing. So each
#: entry moved to `_ORACLE_CLAIMS` names an oracle in `coordinator_core/tests/oracles/`, the
#: oracles run in the same suite as this gate, and `test_every_oracle_claim_names_a_real_oracle`
#: refuses a claim whose oracle does not exist. A claim that stops being true turns the gate RED
#: instead of quietly continuing to suppress.
#:
#: The oracle layer earned this on its first run: two entries claiming "one record per
#: invocation" failed immediately. On reading, the FIRST DRAFT ORACLE was wrong, not the
#: exemptions -- it asserted "no argument accepts multiple values", when a queue entry may
#: legitimately carry several deliverables. The right assertion is over the record-IDENTITY
#: fields. Recorded because it cuts both ways: an oracle can be as wrong as a comment, and the
#: difference is that this one failed loudly on the day it was written rather than in a year.
#:
#: 2026-08-19 -- ADVERSARIAL RE-VERIFICATION, and what it cost this register. The PM rejected
#: wave 4's shape ("I don't like having blanket exemptions, that's how we got into a situation
#: where we had a windows-poison system in claude-klabauter"). All 75 entries were then re-read AT THE
#: CALL SITE by eight independent readers instructed to REFUTE each exemption and default to
#: NOT PROVEN. Result: 53 upheld, 14 REFUTED with a named batch primitive, 8 NOT PROVEN. All 22
#: were returned to `_KNOWN_SITES` (see its OVERTURNED block for the per-key reason); this
#: constant went 75 -> 53. Evidence:
#: `state/subagent-share/f74c1de4-c0f3-4db0-9282-313c8f0c91ad/refute-{a..h}.md`.
#:
#: WHY THE FAILURE RATE WAS 29% AND CONCENTRATED WHERE IT WAS. It tracks block size almost
#: exactly: the 40-key `structural-floor` run lost 13 of 40, the 12-key run lost 2, the 6-key
#: `retained-fallback` run lost 4 of 6, and the SINGLETON-BLOCK rows lost 1 of 13. A rationale
#: written for one site and read against that site holds up; the same rationale stretched over
#: forty does not, because entries 2..N were never checked against it -- they were checked
#: against the CLASS. That is the mechanism `CLAUDE.md`'s shell-out rule already names:
#: satisfying a carve-out's rationale is not membership in it.
#:
#: TWO DEFECTS THIS REGISTER STILL HAS, both found only by reading source:
#:   - THE KEY SHAPE IS ITSELF BLANKET. `(relpath, enclosing, callee)` carries no call anchor,
#:     so one entry silences EVERY qualifying call to that callee in that function.
#:     `orphan_branch_sweep.main` and `register_discovered_repos.main` each hold one call the
#:     governing rationale describes and one it does not; one key covered both. A lineno pin is
#:     the wrong fix (see the stale-by-default argument below) -- an anchor that survives edits
#:     is owed.
#:   - AC8 IS BLOCK-SCOPED, so this register still grows by category.
#:     `_exempt_entry_comment_blocks` walks UP past sibling entries to a shared block, so a new
#:     key appended into an existing run inherits that block's date and class tag and passes
#:     `test_every_exemption_carries_a_dated_rationale` silently. That test's own docstring
#:     claims it makes a wrong exemption "visible to the next reader instead of anonymous";
#:     for an inherited block it does not. Entry-scoped rationales are owed.
#:
#: DECIDED 2026-08-19 (PM ruling, "I don't know why we have any exemptions, honestly"):
#: THIS REGISTER'S TARGET STATE IS ZERO ENTRIES. Not a smaller register, not a better-argued
#: one -- none. An earlier EM call this same day proposed keeping the four classes and moving
#: membership from the comment block to the entry; that was reversed on the ruling above, and
#: reversed correctly. It improved the QUALITY OF THE ARGUING when the finding underneath says
#: to stop arguing. Recorded rather than deleted, because the wrong call is the instructive one:
#: a register defends itself most convincingly right when it should be dissolved.
#:
#: HOW MUCH OF THIS IS A COLLECTOR DEFECT -- MEASURED, after a first estimate was wrong. The
#: first pass at this note read the class tags and concluded that 43 of the 53 were places the
#: collector was under-powered: the 30 `structural-floor` rows looked like discriminator 6's own
#: idea (a different PROGRAM per iteration) merely out of its reach. Discriminator 8 was then
#: built to test exactly that hypothesis, and MEASURED it. It retired ONE key
#: (`maximalist._run_body`, genuinely N distinct interpreters), with zero collateral.
#:
#: So 43 was wrong, and the shape of being wrong is worth keeping. Reading the argv at all 30
#: `structural-floor` call sites shows argv0 is almost always CONSTANT -- `ssh`, `git`, a
#: sibling CLI under `sys.executable`. What varies is a LATER operand: the host, the `-C` root,
#: the config key, the one record the callee accepts. So these rows are not claiming "a
#: different program each time" (mechanical); they are claiming "THIS CLI HAS NO MULTI-ITEM
#: FORM" -- whether `git config --unset` takes two keys, whether `ssh` batches hosts, whether
#: `npm view` takes two packages. That is knowledge of a command-line surface, and NO AST PASS
#: HAS IT. It is the same claim `no-primitive` makes, just tagged differently.
#:
#: The measured split, replacing the estimate:
#:
#:      1  decided by discriminator 8 (built here, retired, entry deleted)
#:     10  `measurement-is-the-loop` -- `for _ in range(n)`, target DISCARDED, results reduced
#:                                     to a statistic. Structurally identical to the retry-loop
#:                                     shape the MISCLASSIFIED bucket already wants a
#:                                     discriminator for. STILL A COLLECTOR DEFECT; next.
#:      3  `retained-fallback`       -- the call is dominated by the failure branch of an
#:                                     already-batched primary. A dominator check over the
#:                                     enclosing try/returncode structure. Mechanical, harder.
#:     ~39  a claim about a CLI's argument surface (the `structural-floor` bulk plus
#:                                     `no-primitive`). NOT statically decidable, by anyone.
#:
#: THE ~39 STILL DO NOT GET PROSE. The right form already exists in this tree, once:
#: `TestOwnFrozenDiffShas::test_ranges_resolve_independently` MEASURES the rev-list
#: global-exclusion narrowing rather than asserting it, so the claim fails loudly if git's
#: behaviour ever changes. "`git config --unset` accepts one key" is a RUNNABLE assertion, not
#: an opinion -- and a runnable one is the only kind that cannot rot silently, which is the
#: whole defect the adversarial pass found. An exemption that runs is a test; an exemption that
#: does not is a claim.
#:
#: So every entry here is owed one of two things, and a comment is neither:
#:
#:     a widened discriminator   (14 -- the collector is wrong), or
#:     an executable oracle      (~39 -- the claim is real, unmeasured, and rots silently).
#:
#: Sequencing note for whoever builds this: BOTH 6 AND 7 SUPPRESS, and so does everything
#: proposed here. An over-broad widening silences a REAL amplification site and nothing
#: downstream notices -- the inverted safety direction this module's own docstring warns about,
#: and the one place this program can do harm. Each extension ships MEASURED: suppresses exactly
#: the intended keys and nothing else -- discriminator 8 shipped that way (measured: exactly one
#: key retired, zero keys outside the register silenced), and that measurement is also what
#: refuted the 43 estimate above. Measuring the widening was worth more than the widening.
#:
#: Next, in order of remaining mechanical yield: `measurement-is-the-loop` (10 keys, the
#: `for _ in range(n)` discard-target shape, which also retires 3 of the MISCLASSIFIED
#: retry-loop rows with the same matcher), then `retained-fallback` (3). After that the
#: register is entirely CLI-surface claims, and the work stops being discriminators and becomes
#: oracles. Until then, entries 2..N of any shared block remain UNVERIFIED BY CONSTRUCTION.
#:
#: Known, LIVE, outstanding exemptions -- what remains AFTER the varying-argv0 discriminator
#: (see module docstring, discriminator 4). G1's comment here reserved this register for G2
#: ("where a real exemption register, if any, would live"); G2 landed it, and this pass retired
#: the two entries the discriminator now decides mechanically
#: (`path_resolution_report._check_windows`, `cruft_sweep.sweep_toolchain_caches` -- both were
#: "argv0 varies per item" claims a static pass can verify). What survives here is UNBATCHABLE
#: for a reason NO STATIC PASS CAN SEE -- the measured subject IS the per-item spawn loop, not a
#: property of the call site's own AST -- so growing this register back up with a mechanically
#: decidable reason is a regression, not a convenience.
#:
#: Keyed on (relpath, enclosing function, callee) -- the SAME key `_KNOWN_SITES` and `AmpSite.key`
#: use, NOT the sibling gates' (relpath, lineno). A lineno pin in this repo is stale-by-default:
#: it silently drifts onto an unrelated call on the next edit above it, and a drifted exemption
#: fails OPEN (the real site re-fires, an innocent one goes quiet) rather than merely failing.
#: Three separate lineno pins in this same test tree had drifted off their subjects by 2026-08-17.
#:
#: An entry here is a claim that the site is UNBATCHABLE BY CONSTRUCTION -- not "expensive but
#: accepted", not "not now". That is the whole discriminator: `_KNOWN_SITES` is a burn-down
#: worklist for sites that CAN be batched and have not been, and growing it to absorb new code
#: destroys the class-regrowth property the standing gate exists to buy. A site that could be
#: batched belongs there (or fixed); a site that cannot belongs here, with the reason.
#:
#: Negative spec: do NOT add a site here because the loop is short, because the caller is a
#: benchmark, or because the spawn is off a hot path. `benchmarks/floor.py` and
#: `benchmarks/harness.py` sit in `_KNOWN_SITES`, not here -- being a benchmark is not the
#: exemption; being a benchmark WHOSE MEASURED SUBJECT IS THE SPAWN ITSELF is.
_DATED_RATIONALE = re.compile(r"#.*\b(20\d\d)-(\d\d)-(\d\d)\b")
_CLASS_TAG = re.compile(r"#\s*class:\s*([a-zA-Z0-9-]+)")

_EXEMPT_SITES: set[tuple[str, str, str]] = {
    # 2026-08-17 -- # class: measurement-is-the-loop. The spawn loop IS the measurement. `_spawn_n_processes` times N sequential
    # `python -c "import <module>"` children as the fan-in arm's control; batching the N imports
    # into one child measures a different quantity and voids the comparison the module exists for.
    # 2026-08-17 -- # class: measurement-is-the-loop. One FRESH login shell per entrypoint is
    # the subject under test: the probe
    # reports how each entrypoint resolves on the PATH a login shell builds. The two spawns per
    # entrypoint were already folded into one combined `-lc` payload; folding ACROSS entrypoints
    # would report one shell's resolution N times. NOT decided by the varying-argv0 discriminator
    # -- `shell` (argv0) is loop-invariant here; only the script text varies, which the four
    # measured argv0 shapes do not reach.
    # RETIRED 2026-08-19 -- the five sampling-loop entries that stood here are now decided
    # structurally by DISCRIMINATOR 9 (`_is_repetition_loop`), and their keys are deleted.
    #
    # Kept as a marker because of what the block ARGUED, which was this, verbatim:
    #
    #     "Deliberately exempted rather than decided by a discriminator: the shape here (a spawn
    #      whose arguments are entirely loop-invariant) is indistinguishable to a static pass
    #      from genuinely redundant repeated work, which IS amplification. Only a human can say
    #      which one a given repetition is, which is what this register is for."
    #
    # That is false, and it was refuted by building the pass it said could not exist:
    # discriminator 9 retired all five, measured, with ZERO keys silenced outside the register.
    # The distinguisher the argument called impossible is the DISCARDED TARGET -- a loop that
    # never reads its own target back is a repetition, and one whose count derives from a
    # collection's size is a fan-out. Both are visible in the AST.
    #
    # Worth leaving here, because this is the register's own self-justification failing on the
    # merits: it did not merely record a wrong exemption, it argued that mechanising the class
    # was IMPOSSIBLE, and that argument is what kept five decidable keys in prose. When a future
    # entry says a static pass cannot decide it, this is the precedent for trying anyway before
    # believing it.
    # 2026-08-19 -- # class: retained-fallback. Retained per-item fallback behind a batched
    # hot path. The batch is the
    # primary call; the per-item loop the collector counts fires only when that batch spawn
    # fails, and it replaces mapping the whole set to a single degraded verdict. The collector
    # cannot distinguish a primary path from a fallback, so it counts the fallback. Deleting
    # the fallback to clear the key would trade a degrade-on-failure posture for a metric --
    # forbidden by name in `state/ledgers/amp-cfinal-exemption-ledger.md`. Precedent:
    # `orphan_branch_sweep.py::main`, frozen in `_KNOWN_SITES` on the same argument.
    # 2026-08-19 -- # class: no-primitive-MEASURED-wrong. No batch primitive, and this one was
    # MEASURED after a batched version of it
    # was written and found wrong. `git rev-list` cannot express a union of ranges: its
    # exclusions are GLOBAL, so `rev-list A..B C..D` means `B D ^A ^C`, and for two adjacent
    # frozen ranges the `^C` cancels the `C` that `A..B` contributed. The batch silently
    # NARROWS the frozen sha set -- an under-admission in an anti-forgery gate, invisible at
    # the call site. Pinned by `TestOwnFrozenDiffShas::test_ranges_resolve_independently`,
    # which fails against the batched form. Checked against the walking seams per the ledger's
    # primitive-absence doctrine correction: inapplicable, this needs commit history.
    # 2026-08-19 -- # class: structural-floor. N ROOTS, N SPAWNS, not an unbatched loop. No git
    # invocation spans multiple `-C` roots, so one spawn per DISTINCT destination worktree is
    # the minimum however the loop is arranged; relocating it only moves the flag. Checked
    # against the walking seams per the ledger's primitive-absence doctrine correction and they
    # do not apply -- this needs `git status`, which no filesystem walk can serve. Precedent,
    # architecturally identical and already frozen: `publish.py::_publish_relevant_allowlist_
    # leg` -> `_git_ls_tree_entries_files`.
    # 2026-08-19 -- # class: structural-floor. Isolation is the contract, evidenced by a live
    # failure. `scan-secrets` is
    # target-scoped (peer-repo pattern, `registry_codenames` guard), so each row must be handed
    # ITS OWN file list. An earlier revision fed every row the whole run's list on an
    # "over-inclusive is safe" reading and raised HIGH-tier findings against other rows'
    # sources under the wrong ruleset (observed 2026-08-18, recorded at the call site).
    # Batching reintroduces exactly that defect.
    # 2026-08-19 -- # class: structural-floor. Isolation is the contract (anti-forgery gate). Each
    # `ForeignSessionRangeRefused` names the specific offending `reviewed_range` entry; a
    # batched resolve destroys the per-entry attributability AC1c depends on, and this seam's
    # whole job is refusing a range with a named reason rather than failing opaquely.
    # 2026-08-19 (wave 4) -- # class: retained-fallback. Each of the six below is the per-item
    # leg BEHIND an already-batched hot path: the batch is the primary call, and the loop the
    # collector counts fires only when that batch's own spawn fails, recovering per-item
    # attribution instead of mapping the whole set to one degraded verdict. The collector
    # cannot tell a fallback from a primary path, so it counts the fallback. Deleting one to
    # clear a key trades a degrade-on-failure posture for a metric -- forbidden by name in the
    # plan's anti-scope. `_first_invalid_pathspec` is this wave's own: its batched
    # `git ls-files -- <all>` confirms "all valid" in one spawn and falls back to the per-item
    # check only to name WHICH entry is bad.
    # 2026-08-19 (wave 4) -- # class: measurement-is-the-loop. The loop body IS the subject,
    # so collapsing it measures a different thing or destroys the property it exists for.
    # `_interactive_gate` spawns only when the OPERATOR types `d` for one file's diff --
    # bounded by keypresses, not by item count, and pre-computing every diff defeats the
    # on-demand review it exists to provide. `find_polluter.main` IS the bisection primitive:
    # attribution requires observing one test file's filesystem side effect before deciding
    # whether to continue. The two `_is_tracked` rows are act-time TOCTOU rechecks fired
    # immediately before their own `unlink()`; both already carry an in-source refusal naming
    # this same sweep, because hoisting either widens the window the recheck exists to narrow.
    # 2026-08-19 (wave 4) -- # class: no-primitive-MEASURED-wrong. Absence was measured at the
    # callee, never asserted from the call site. The `rev-list` cluster is one finding: git
    # cannot express a UNION of ranges, since its exclusions are global (`rev-list A..B C..D`
    # means `B D ^A ^C`), so batching adjacent ranges silently NARROWS the result -- the same
    # defect that shipped and was reverted on 2026-08-19 in `_own_frozen_diff_shas` above.
    # `_resolve_ref_to_sha`: `git rev-parse --verify HEAD HEAD~1` was RUN and returns
    # `fatal: Needed a single revision`, rc=128. `path_rename_or_move`: `git log --follow`
    # takes exactly one pathspec. The three `_run_reconcile_*` rows plus `_run_sibling_cli`:
    # `reconcile-completion-commits.py`'s own arg parser hard-refuses a second positional, and
    # its `--session-id` is per-entry (read from each entry's `authored_by`), so even a
    # hypothetical batch form would need per-item demultiplexing rather than a collapse.
    # Every row checked against the walking seams first: none is a `--show-toplevel`/`--git-dir`
    # shape, so none converts to a zero-spawn removal.
    # 2026-08-19 (wave 4, C13) -- # class: structural-floor. N ROOTS, N SPAWNS. The loop here
    # iterates contributing SOURCE ROOTS, not allowlist entries, and hands every entry belonging
    # to a root to `_git_ls_tree_entries_files` in ONE call -- per-entry batching is already
    # done. No git invocation spans multiple tree roots, so one `ls-tree` per distinct root is
    # the floor however the loop is arranged; the in-source comment above it says so. This is
    # the site the N-roots-N-spawns block below already cited as its own precedent while the key
    # itself still sat in `_KNOWN_SITES` -- disposing it makes that citation true. C13 ran late
    # and EM-inline: it was held out of both dispatch waves because `publish.py` carried a
    # concurrent session's uncommitted work, and ran once that landed and the file went clean.
    # Re-derived at disposition: the two REGROWTH keys the plan predicted from
    # `_commit_published_dests` are NOT observed, though the function is present at HEAD.
    # 2026-08-19 (wave 4) -- # class: structural-floor. N distinct EXECUTABLES, so no shared
    # batch target exists at all. `_run_legs` iterates DROP-IN HEALTH LEGS -- independently
    # authored programs discovered at runtime, each with its own argument surface -- and the
    # argv it builds comes wholly from the loop target. There is no callee to ask about its
    # arity (discriminator 8 is blind here because argv0 is not a helper parameter), and no
    # batch form to measure, because a batch would have to span programs that share nothing.
    # Relocating the call only moves the flag.
    #
    # NOT owed an oracle for the same reason: an oracle measures a claim about a callee's
    # argument surface, and this row's claim is that there IS no single callee. The honest
    # remaining move is the hand-rolled-parser rearchitecture named in the successor handoff --
    # give the legs a uniform argument surface and ONE oracle covers them -- not a bespoke
    # oracle per leg.
    ('coordinator_core/ops/install_health_run.py', '_run_legs', 'call'),
    # RETIRED 2026-08-19 -- `_common.py::archive_and_commit::create_subprocess_exec` and
    # `updatedocs_gates.py::_gate_queue_prune_sweep::_run` stood here under the same
    # `structural-floor` block. Both are now `_ORACLE_CLAIMS` entries: the first's "M + C" floor
    # and the second's batched/override split are both MEASURED, each with a fails-when-inverted
    # leg, so neither claim can rot into a description. This is the block shrinking the way it is
    # supposed to -- a rationale stretched over three sites now covers the one it was written for.
    # RETIRED 2026-08-19 -- `install/first_run.py::_seed_machine_local_registry::_run` stood here
    # as "N distinct EXECUTABLES". The site is GONE, not decided by any discriminator: commit
    # `9d7f1472a` ("install: first-run seeds the machine-local registry again, in-process")
    # removed the function's last `_run(...)` call entirely -- it now writes the machine-local
    # registry in-process via `registry_set()` (`coordinator_core/machine_resolver.py`). The
    # function itself still exists; the spawn does not.
    #
    # RETIRED 2026-08-19 -- `consolidate_assemble/__init__.py::brief::worktree_is_dirty` stood
    # here as "N distinct WORKTREE ROOTS". True, and decidable: `worktree_is_dirty` does not
    # itself spawn -- it calls its own injected `run_git` parameter, which the marked call site
    # binds through `brief`'s own `run_git = run_git or default_run_git` seam, one hop up.
    # Discriminator 12 leg B already reads a resolved helper's scope param; it declined only
    # because the spawn lived behind that one further injected-runner hop, invisible to it.
    # `_root_scoped_through_injected_runner` follows the hop and re-derives `default_run_git`'s
    # scope param the normal way before trusting it. Measured: exactly this key, zero collateral.
    #
    # RETIRED 2026-08-19 -- `workweek_reverse_drift_gate.py::run_gate::run` stood here as "N
    # distinct EXECUTABLES". True, and decidable: `argv = shlex.split(cmd)` is a bare `Call` RHS
    # `_argv0_expr` always declined, so the one-hop binding `_loop_argv0_bindings` builds for the
    # call site's bare `Name` argv was never recorded. `shlex.split` is a closed, provably
    # input-derived exception to the no-`Call` rule (see `_shlex_split_subject_expr`); the
    # predicate itself (`_argv0_varies_with_loop_target`) already asked the right question.
    # Measured: exactly this key, zero collateral.
    # RETIRED 2026-08-19 -- `percolate/engine.py::run_entrypoint_gate::_run_one_entrypoint` stood
    # here as "N distinct EXECUTABLES". True, and already decidable: the helper spells it
    # `script_path = root / rel` then `[interpreter, str(script_path), "--help"]`, which is the
    # shape `_helper_spawn_argv0_params`' docstring names as its motivating case. It declined
    # only because that function merged its two binding maps with the argv0-HEAD map last, so
    # `argv` resolved to the bare `sys.executable` and nothing could look past the interpreter.
    # One merge order, measured: exactly this key, zero collateral.
}


@dataclasses.dataclass(frozen=True)
class AmpSite:
    """Sibling to `spawn_policy.SpawnSite` -- carries the loop/route signal that frozen
    dataclass deliberately does not. NOT a subtype or extension of `SpawnSite`; see module
    docstring's "Reuse from spawn_policy" section for why a sibling, not an extension.

    Was `GitAmpSite` until AC11 widened the collector past git. The rename is contained: no
    module outside this file referenced the symbol, only the file PATH, which is unchanged."""

    path: str
    lineno: int
    enclosing: str
    route: str  # "a-direct" | "b-local-helper" | "c-cross-module" | "d-injected"
    #             | "e-generic-runner" | "f-default-runner" | "g-forwarded-runner"
    callee: str

    @property
    def key(self) -> tuple[str, str, str]:
        """Structural identity for a frozen-inventory subset assertion (G2): (path, enclosing,
        callee). Deliberately excludes `lineno` and `route`, matching `spawn_policy.site_key`'s
        own exclusion of `lineno` from identity -- a line renumbering must not look like a new
        site, and a route reclassification (e.g. b becoming c after a refactor) is the same
        underlying site, not a new one."""
        return (self.path, self.enclosing, self.callee)


def _relpath(path: pathlib.Path, root: pathlib.Path) -> str:
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.resolve().relative_to(root.resolve()).as_posix()


def _assert_not_self_scanned(files: list[tuple[str, pathlib.Path]]) -> None:
    """Anti-scope 20's loud re-entrancy sentinel. `is_test_tree_site` already filters this
    module's own file out of every real scan (it lives under `coordinator_core/tests/`) -- this
    check exists so that filtering is verified, not merely trusted. A silent recursion guard
    makes the gate pass vacuously; this one raises instead of returning an empty/clean result."""
    for _relpath_str, file_path in files:
        if file_path.resolve() == _THIS_FILE:
            raise RuntimeError(
                "re-entrancy: the amplification gate scanned its own file "
                f"({_relpath_str}) -- this would make the gate pass vacuously. "
                "is_test_tree_site's test-tree filtering was bypassed or misconfigured."
            )


def _discover_scope_files(roots: tuple[pathlib.Path, ...]) -> list[tuple[str, pathlib.Path]]:
    """Discovery for one collector pass: every non-test-tree source file under `roots`, as
    `(repo-or-root-relative posix path, absolute path)`. Reuses `discover_source_files`
    (traversal) and `is_test_tree_site` (post-walk partition) unmodified -- see module
    docstring's "Reuse from spawn_policy" section."""
    out: list[tuple[str, pathlib.Path]] = []
    for root in roots:
        if not root.exists():
            continue
        discovered, _excluded = discover_source_files(root, exclude=DEFAULT_EXCLUDE)
        for rel_posix, file_path in discovered:
            relpath = _relpath(file_path, root)
            if is_test_tree_site(relpath):
                continue
            out.append((relpath, file_path))
    _assert_not_self_scanned(out)
    return out


# --------------------------------------------------------------------------
# Discriminator 2: constant-literal loop sequences
# --------------------------------------------------------------------------

_LITERAL_WRAPPERS = {"enumerate", "sorted", "reversed"}


def _module_level_literal_names(tree: ast.Module) -> set[str]:
    """Names bound at module scope directly to a List/Tuple/Set/Dict literal -- the `Name`
    half of discriminator 2's "a literal tuple/list/set/dict, or a Name bound at module scope
    to one" rule."""
    names: set[str] = set()
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, (ast.List, ast.Tuple, ast.Set, ast.Dict))
        ):
            names.add(node.targets[0].id)
        #: `_ARCHIVE_SWEEP_SCRIPTS: tuple[str, ...] = (...)` is the same constant wearing an
        #: annotation, and reading only `ast.Assign` missed every one of them. The literal is
        #: what discriminator 2 keys on; whether the author annotated it is not a property of
        #: the loop. Retired one register entry that had been filed as "N distinct EXECUTABLES".
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, (ast.List, ast.Tuple, ast.Set, ast.Dict))
        ):
            names.add(node.target.id)
    return names


def _unwrap_literal_wrapper(node: ast.expr) -> ast.expr:
    """Strips one layer of `enumerate(...)`/`sorted(...)`/`reversed(...)`/`X.items()` around
    `node`, returning the inner expression it wraps (or `node` unchanged if it isn't one of
    those wrapper shapes)."""
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in _LITERAL_WRAPPERS and node.args:
            return node.args[0]
        if isinstance(func, ast.Attribute) and func.attr == "items":
            return func.value
    return node


def _function_local_literal_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names bound INSIDE `fn` to a literal sequence and only ever grown by `.append(...)`.

    Discriminator 2's safety argument is "the iteration count is fixed at author time, not by
    input size", and that argument is untouched by an append: `hosts = ['git@github.com',
    'git@gitlab.com']` followed by a conditional `hosts.append(probe_url)` runs 2 or 3 times, both
    compile-time constants. The rule already accepted exactly this shape at MODULE scope; the
    only thing keeping `prereq_probe.probe_clone_auth` in the register was that its constant is
    function-local.

    Declines on anything that can grow by an unknown amount -- `extend`, `+=`, `.update`, a
    rebind, a comprehension, or handing the name to a call that could mutate it. Those are the
    shapes where length stops being author-time knowable, and this SUPPRESSES, so an uncertain
    name must not qualify."""
    bound: dict[str, int] = {}
    disqualified: set[str] = set()

    #: An `append` INSIDE a loop runs once per iteration, so the sequence's length is the loop's
    #: length -- input-sized, not author-time. Counting append STATEMENTS without this check was
    #: measured over-broad on 2026-08-19: it silenced seven sites where one was intended,
    #: including two act-time TOCTOU rechecks and `review_trail_write._own_frozen_diff_shas`.
    #: `results = []` grown in a loop and then iterated is the single most common shape in this
    #: tree, and it is genuine amplification every time.
    for loop in ast.walk(fn):
        if not isinstance(loop, (ast.For, ast.AsyncFor, ast.While, *_COMPREHENSIONS)):
            continue
        for inner in ast.walk(loop):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and isinstance(inner.func.value, ast.Name)
            ):
                disqualified.add(inner.func.value.id)

    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                for name in _names_in(target):
                    if isinstance(target, ast.Name) and isinstance(
                        node.value, (ast.List, ast.Tuple, ast.Set, ast.Dict)
                    ):
                        bound[name] = bound.get(name, 0) + 1
                    else:
                        disqualified.add(name)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            for name in _names_in(node.target):
                if isinstance(node.target, ast.Name) and isinstance(
                    node.value, (ast.List, ast.Tuple, ast.Set, ast.Dict)
                ):
                    bound[name] = bound.get(name, 0) + 1
                else:
                    disqualified.add(name)
        elif isinstance(node, ast.AugAssign):
            disqualified |= _names_in(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            disqualified |= _names_in(node.target)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.attr != "append":
                    disqualified.add(func.value.id)
            #: Passed as an argument -- the callee may mutate it by any amount.
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if isinstance(arg, ast.Name):
                    disqualified.add(arg.id)

    return {name for name, count in bound.items() if count == 1 and name not in disqualified}


def _is_constant_literal_iterable(node: ast.expr, literal_names: set[str]) -> bool:
    inner = _unwrap_literal_wrapper(node)
    if isinstance(inner, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
        return True
    if isinstance(inner, ast.Name) and inner.id in literal_names:
        return True
    return False


# --------------------------------------------------------------------------
# Discriminator 4: chunking stride loops
# --------------------------------------------------------------------------


def _is_chunking_stride_iterable(node: ast.expr) -> bool:
    """True for `range(<start>, <stop>, <step>)` where `step` is not the literal `1` --
    the CHUNKING idiom (`for i in range(0, len(shas), CHUNK): chunk = shas[i:i+CHUNK]`),
    which is the batched shape this collector's own remedy asks for, not the per-item
    shape it names.

    Why this is a discriminator and not a widened exemption: a 3-arg `range` with a
    non-unit stride runs `ceil(n / step)` times, not `n` times, so a spawn in its body
    is one call PER BATCH by construction. Without this, `coverage.py::
    _filter_shas_by_scope_paths` -- explicitly documented as "batched in chunks of
    _SCOPE_FILTER_CHUNK_SIZE SHAs fed over stdin per git invocation ... not one call per
    SHA" -- was reported as an amplification site, i.e. the collector flagged the exact
    fix it recommends. Measuring a property that contradicts the defect it names is what
    this discriminator removes.

    Deliberately narrow, matching this module's false-negative-over-false-positive
    preference: a literal `1` step, a 1- or 2-arg `range`, and any non-`range` iterable
    all still qualify as per-item loops. A step that is a Name (the ordinary
    `_CHUNK_SIZE` constant shape) is taken at face value -- a module constant named as a
    stride is not statically resolvable here, and the alternative is re-flagging every
    correctly batched site.
    """
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "range"):
        return False
    if len(node.args) != 3:
        return False
    step = node.args[2]
    if isinstance(step, ast.Constant) and step.value == 1:
        return False
    return True


# --------------------------------------------------------------------------
# Discriminator 6: varying-argv0 exclusion (see module docstring). A loop that spawns a
# DIFFERENT PROGRAM per iteration cannot be batched into one call -- there is no single argv0
# for the batch to share.
#
# Spec backlink: docs/plans/2026-08-17-the-amplification-gate-decides-varying-argv0-itself.md,
# chunk C1. Measured against the four real sites that motivated it (this plan's sizing object,
# `premise.evidence`): accepts `cruft_sweep.sweep_toolchain_caches` (argv0 `resolved`) and
# `path_resolution_report._check_windows` (argv0 `where`); rejects `path_resolution_report.
# _check_posix` (argv0 loop-invariant -- the shell is fixed, only the script text varies) and
# `shim_fanin_measure._spawn_n_processes` (argv0 is `sys.executable`, never the loop target).
# NOT measured for false-positive rate the way discriminators 1-3 above are -- see module
# docstring.
# --------------------------------------------------------------------------


def _names_in(node: ast.expr) -> set[str]:
    """Every `ast.Name` identifier referenced anywhere inside `node`."""
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _loop_target_names(target: ast.expr) -> set[str]:
    """The set of names a `for`/`async for` (or comprehension generator) target binds --
    `for a, b in ...` binds both. Same three lines as `test_no_spawn_per_item_loop.py:146`'s
    `_loop_target_names`; kept as a local copy rather than a cross-module import, matching this
    module's stated pinned-API discipline (see module docstring's "Reuse from spawn_policy") --
    the shape is trivial enough that a coupling neither gate's contract asks for is not worth
    the drift-tracking a shared import would need."""
    return _names_in(target)


def _paired_assign_elements(
    target: ast.expr, value: ast.expr
) -> list[tuple[ast.expr, ast.expr]] | None:
    """Element-wise `(target, value)` pairs for an unpacking assignment whose two sides are
    statically correlatable -- both `Tuple`/`List`, equal length, no `Starred` on either side
    (a star absorbs an unknown span, so positions stop lining up). `None` for every other
    shape, which is the signal to fall back to the coarse whole-RHS rule.

    Exists so `_tainted_names_for_loop` does not taint `b` in `a, b = item, "always-git"` --
    see its docstring for why over-tainting is the dangerous direction for a suppressor."""
    if not (
        isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List))
    ):
        return None
    if len(target.elts) != len(value.elts):
        return None
    if any(isinstance(e, ast.Starred) for e in (*target.elts, *value.elts)):
        return None
    return list(zip(target.elts, value.elts))


def _tainted_names_for_loop(loop: ast.AST, seed: set[str]) -> frozenset[str]:
    """Fixed-point taint set over `loop`'s subtree: starts at `seed` (the loop target's own
    names) and grows by one `ast.Assign`/`ast.AnnAssign` hop per round -- a local becomes
    tainted when its RHS mentions anything already tainted. Bounded at 10 rounds, matching the
    measured prototype this discriminator was sized against; every real site converges in one
    or two rounds.

    Deliberately narrow, per this discriminator's own measured scope (plan Anti-scope): one
    `ast.Assign`/`ast.AnnAssign` hop, nothing deeper.

    BROADER IS THE UNSAFE DIRECTION HERE, and an earlier version of this docstring had it
    exactly backwards (Review: code-reviewer 2026-08-17, BLOCK). Every other discriminator in
    this module ACCEPTS work -- for those, over-inclusion costs a false positive the gate
    reports and a human dismisses. This one SUPPRESSES, so an over-broad taint set silences a
    real amplification site and nothing downstream ever notices. "It can only make more names
    eligible for suppression" is the harm, not the safety argument. Any future discriminator
    whose action is EXCLUDE inherits this inversion: check it explicitly.

    Concretely, that reasoning shipped a real false suppression:

        for item in items:
            a, b = item, "always-git"
            subprocess.run([b, "--version"])   # argv0 is CONSTANT -- must stay reported

    The whole-RHS test saw `item` in the tuple and tainted BOTH `a` and `b`, so a loop-invariant
    argv0 read as varying. Fixed by correlating targets to values ELEMENT-WISE when both sides
    are same-length `Tuple`/`List` (`_paired_assign_elements`); every other shape keeps the
    coarse whole-RHS rule, which is correct for them -- unpacking an opaque call result really
    can carry the taint into any element.

    RESIDUAL, stated rather than fixed: this pass is flow-INSENSITIVE (an `ast.walk` over the
    subtree, no statement ordering). A name assigned from the loop target and then REBOUND to
    something invariant later in the body stays tainted for a call sitting between the two.
    That is the same over-suppression direction as the bug above, with a narrower trigger, and
    closing it needs statement ordering this discriminator's scope deliberately excludes."""
    tainted = set(seed)
    for _ in range(10):
        grew = False
        for node in ast.walk(loop):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None:
                continue
            if not (_names_in(value) & tainted):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                pairs = _paired_assign_elements(t, value)
                if pairs is None:
                    newly = _names_in(t)
                else:
                    newly = {
                        name
                        for sub_target, sub_value in pairs
                        if _names_in(sub_value) & tainted
                        for name in _names_in(sub_target)
                    }
                for name in newly:
                    if name not in tainted:
                        tainted.add(name)
                        grew = True
        if not grew:
            break
    return frozenset(tainted)


def _argv0_expr(
    argv_arg: ast.expr, bindings: dict[str, ast.expr] | None = None
) -> ast.expr | None:
    """`argv[0]` as an expression, for the shapes measured to occur at a real per-item spawn
    call site: a `List` literal's first element, `<List> + <rest>` (recurse left -- the
    `[resolved] + list(prune_argv)` idiom), or a bare `Name` resolved one hop through
    `bindings` (the real `cruft_sweep.sweep_toolchain_caches` shape: `dry_argv = [resolved] +
    list(dry_run_argv)` as its own statement, THEN `subprocess.run(dry_argv, ...)` -- the List/
    BinOp shape sits one assignment away from the call site, not on it). `None` for anything
    else -- declining rather than guessing is deliberate, this chunk's hard constraint is no
    fallback escape hatch: a site this pass cannot decide must not be suppressed."""
    if isinstance(argv_arg, ast.List) and argv_arg.elts:
        return argv_arg.elts[0]
    if isinstance(argv_arg, ast.BinOp) and isinstance(argv_arg.op, ast.Add):
        return _argv0_expr(argv_arg.left, bindings)
    if bindings and isinstance(argv_arg, ast.Name) and argv_arg.id in bindings:
        return bindings[argv_arg.id]
    return None


def _argv_list_elts(
    argv_arg: ast.expr, bindings: dict[str, ast.expr] | None = None
) -> list[ast.expr] | None:
    """The LEADING argv list's elements, for the same shapes `_argv0_expr` resolves -- a `List`
    literal, the left arm of an `Add` chain, or a bare `Name` resolved one hop through
    `bindings`. `None` when no list literal is reachable.

    `_argv0_expr` answers "what is at position 0"; this answers "what are the first few
    positions", which is what discriminator 11 needs to look PAST a fixed interpreter."""
    if isinstance(argv_arg, ast.List):
        return list(argv_arg.elts)
    if isinstance(argv_arg, ast.BinOp) and isinstance(argv_arg.op, ast.Add):
        return _argv_list_elts(argv_arg.left, bindings)
    if bindings and isinstance(argv_arg, ast.Name) and argv_arg.id in bindings:
        return _argv_list_elts(bindings[argv_arg.id], bindings)
    return None


def _is_fixed_interpreter_expr(expr: ast.expr) -> bool:
    """True for `sys.executable` -- the one program name in this tree that is FIXED while the
    thing actually being run varies.

    Deliberately just this. A parameter named `interpreter` might hold a fixed interpreter or
    might be the very thing varying per iteration, and a suppressor that guessed would silence
    a real fan-out over interpreters. `sys.executable` is unambiguous: it is this process's own
    interpreter, constant for the run."""
    return (
        isinstance(expr, ast.Attribute)
        and expr.attr == "executable"
        and isinstance(expr.value, ast.Name)
        and expr.value.id == "sys"
    )


def _program_identity_expr(
    argv_arg: ast.expr, bindings: dict[str, ast.expr] | None = None
) -> ast.expr | None:
    """The expression that names WHICH PROGRAM this spawn runs -- argv[0] normally, but argv[1]
    when argv[0] is a fixed interpreter.

    Discriminator 6 reads argv[0] and asks whether it varies with the loop. That is the right
    question and the wrong slot for most of this repo: the dominant spawn shape here is
    `[sys.executable, str(SCRIPT), ...]`, where argv[0] is constant for every iteration and the
    SCRIPT is what differs. Sibling CLIs, per-plugin drift commands, drop-in health legs and
    bare entrypoint probes all wear it. Reading argv[0] alone, discriminator 6 sees one fixed
    program and correctly declines -- while the loop is in fact running N different programs.

    DECLINES when a flag sits between the interpreter and the script (`[sys.executable, "-m",
    ...]`): what a flag consumes is not knowable from the AST, so argv[1] stops being the
    program-identity slot and guessing would suppress on a position that means something else.
    Falls back to argv[0] whenever no list literal is reachable."""
    elts = _argv_list_elts(argv_arg, bindings)
    if not elts:
        return _argv0_expr(argv_arg, bindings)
    head = elts[0]
    if not _is_fixed_interpreter_expr(head):
        return head
    if len(elts) < 2:
        return None
    second = elts[1]
    if isinstance(second, ast.Constant) and isinstance(second.value, str):
        if second.value.startswith("-"):
            return None
    return second


def _program_identity_varies_with_loop_target(
    call: ast.Call, tainted: frozenset[str], bindings: dict[str, ast.expr] | None = None
) -> bool:
    """DISCRIMINATOR 11 -- a different PROGRAM each iteration, read at the slot that actually
    names the program.

    Same fact as discriminator 6, and the same unbatchability argument: there is no single
    program for a batched call to share, so the sibling gate's remedy has nothing to hoist. The
    only change is WHERE the program name is read from -- `_program_identity_expr` looks past a
    fixed `sys.executable` to the script path behind it.

    Carries discriminator 6's own restriction unchanged: the caller must apply this only to a
    call that IS the recognized spawn syscall. At a wrapper call site `args[0]` is the wrapper's
    parameter, not an OS argv, and reading a program identity out of it is the false suppression
    discriminator 6 already shipped once."""
    if not tainted or not call.args:
        return False
    identity = _program_identity_expr(call.args[0], bindings)
    if identity is None:
        return False
    return bool(_names_in(identity) & tainted)


def _shlex_split_subject_expr(value: ast.expr) -> ast.expr | None:
    """The STRING being tokenized, for `shlex.split(X)` -- and ONLY that call, not `Call` in
    general.

    `_argv0_expr` declines every `Call` RHS on purpose (see its own docstring): a repo-defined
    resolver like `build_argv(item)` can hold a CONSTANT argv0 inside its body no matter what
    `item` is, and treating any `Call` as argv0-transparent would suppress that real
    amplification site. `shlex.split` is a closed, one-name exception to that rule, not a
    widening of it -- it is a stdlib function with no internal branch that could produce a
    fixed argv[0] regardless of its input: every element of its output, INCLUDING position 0,
    is carved directly out of `X`. So if `X` is loop-tainted, argv[0] of the split result is
    provably tainted too, by construction rather than by guessing.

    `workweek_reverse_drift_gate.run_gate` is the measured site this exists for: `argv =
    shlex.split(cmd)` then `subprocess.run(argv, ...)`, where `cmd` is built from the loop row.
    Before this, `_argv0_expr(value, bindings)` saw a bare `Call` node and returned `None` --
    the binding was never recorded, so the downstream `Name` lookup at the call site had
    nothing to resolve through. This is the binding-CAPTURE gap named in
    `state/audits/2026-08-19-amplification-register-remaining-fourteen-dispositions.md`; the
    predicate at the call site (`_argv0_varies_with_loop_target`) already asked the right
    question and was declining only because the extraction never reached it.

    Deliberately just `shlex.split` -- not `str.split`/`str.rsplit` (an arbitrary attribute
    call on an arbitrary object, not provably a plain string tokenizer by shape alone) and not
    any other `Call`. Widening past this one name re-opens the `build_argv(item)` hole."""
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "split"
        and isinstance(value.func.value, ast.Name)
        and value.func.value.id == "shlex"
        and value.args
        and not any(isinstance(a, ast.Starred) for a in value.args)
    ):
        return value.args[0]
    return None


def _loop_argv0_bindings(loop: ast.AST) -> dict[str, ast.expr]:
    """Name -> its resolved `argv0` expression, for every single-target `ast.Assign`/
    `ast.AnnAssign` in `loop`'s subtree whose RHS itself resolves via `_argv0_expr`, OR via
    `_shlex_split_subject_expr` when `_argv0_expr` declines -- the one-hop intermediate-variable
    idiom (`dry_argv = [resolved] + list(dry_run_argv)`, or `argv = shlex.split(cmd)`) that a
    call site passing the bare `Name` (`subprocess.run(dry_argv, ...)`) needs resolved before
    either extractor can see a shape at all. Threaded through as `bindings` so a chain of two
    such assignments resolves transitively; real sites only ever use one hop."""
    bindings: dict[str, ast.expr] = {}
    for node in ast.walk(loop):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            continue
        head = _argv0_expr(value, bindings)
        if head is None:
            head = _shlex_split_subject_expr(value)
        if head is not None:
            bindings[targets[0].id] = head
    return bindings


def _argv0_varies_with_loop_target(
    call: ast.Call, tainted: frozenset[str], bindings: dict[str, ast.expr] | None = None
) -> bool:
    """True when `call`'s own first positional argument's `argv[0]` (`_argv0_expr`, resolved
    through `bindings`) references a name in `tainted` -- the enclosing loop's target itself,
    or a local the loop body derives from it in one assignment hop (`_tainted_names_for_loop`).
    When true, the site is unbatchable by construction: no single argv0 exists for a batched
    call to share, so there is nothing for the sibling `test_no_spawn_per_item_loop` gate's
    remedy (hoist to one call outside the loop) to hoist.

    Callers must restrict this to a call that IS itself the recognized spawn syscall (route
    a's own condition, `node.lineno in spawn_linenos and callee in _SPAWN_API_NAMES`) --
    `args[0]` at a b/c/d/e/f-route call site is a WRAPPER's own parameter, not an OS-level
    argv, and applying this discriminator there produced a false suppression (a verb-gated
    chokepoint call like `_run_git([verb, '--quiet'], root)`, where `verb` is the loop target
    but is a git SUBCOMMAND, not a program name) -- see this module's self-test
    `test_discriminator_verb_gated_requires_a_statically_known_verb`.

    `tainted` empty (no enclosing qualifying loop, or a comprehension with no assignment
    possible) or `call` with no positional args both decline rather than guess -- see
    `_argv0_expr`'s docstring for the same discipline applied to the extraction itself."""
    if not tainted or not call.args:
        return False
    head = _argv0_expr(call.args[0], bindings)
    if head is None:
        return False
    return bool(_names_in(head) & tainted)


# --------------------------------------------------------------------------
# Discriminator 7: argv-splicing loop target (byte-budget chunking). A loop whose target is
# concatenated into argv as a SEQUENCE, rather than placed in it as one element, spawns once
# per GROUP by construction -- its spawn count is O(total_argv_bytes / ceiling), never
# O(items).
#
# Why a seventh discriminator and not two more `_EXEMPT_SITES` entries: discriminator 4 already
# recognises chunking, but only through a literal `range(start, stop, stride)` with a non-unit
# stride. Both real byte-budget chunkers in this tree build their chunk list at RUNTIME, bounded
# by accumulated argv bytes against Windows' 32767 `CreateProcess` cap, so no `range` appears
# and discriminator 4 is blind to them:
#
#   - `coordinator/bin/percolate-round.py::_dest_paths_exist` -- `for chunk in
#     _chunk_paths_by_argv_bytes(to_probe, cap=_LS_FILES_ARGV_BYTE_CAP)`, then
#     `_run([... , "--"] + chunk)`. `git ls-files` has no `--pathspec-from-file`, so a deletion
#     set over the cap cannot be one spawn.
#   - `coordinator/bin/publish.py::_git_status_porcelain` -- an in-function accumulator loop
#     builds `batches`, then `subprocess.run(base + batch)`. `git status` does not accept
#     `--pathspec-from-file` either (only the commit/add family does).
#
# Exempting the two by name would freeze a register entry per byte-budget chunker written from
# here on; the shape is what is decidable, so the shape is what this reads.
#
# THIS DISCRIMINATOR SUPPRESSES, so it inherits the inversion `_tainted_names_for_loop`'s
# docstring states: an over-broad match silences a real amplification site and nothing
# downstream notices. Kept narrow accordingly -- the spliced name must be the loop's OWN target
# (never the one-hop taint set discriminator 6 grows, which would reach locals derived from it),
# and it must appear as a bare `Name`, `list(<Name>)`/`tuple(<Name>)`, or `*<Name>`. An
# `ast.Attribute` (`item.args`), a call result, or a subscript all decline.
#
# Unlike discriminator 6 this is NOT restricted to a direct spawn call, because it reads a
# different property: not "which program does argv0 name" -- which is meaningless at a wrapper,
# the false suppression `_argv0_varies_with_loop_target` guards against -- but "how many items
# does ONE call carry". A wrapper handed N items in one argument spawns once for those N
# whatever it does with them, so the argument holds at every route.
# --------------------------------------------------------------------------


def _splices_name_from(node: ast.expr, target_names: set[str]) -> bool:
    """True when `node` contributes a whole SEQUENCE bound to one of `target_names` to the argv
    it sits in -- a bare `Name`, or that name wrapped in `list(...)`/`tuple(...)`. Anything
    else, including an attribute or subscript off the target, declines: only a name the loop
    itself binds is known to be the group the iterable yielded."""
    if isinstance(node, ast.Name):
        return node.id in target_names
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("list", "tuple")
        and len(node.args) == 1
    ):
        return _splices_name_from(node.args[0], target_names)
    #: An ELEMENTWISE comprehension over the group is the same proof one step later:
    #: `*[str(s) for s in batch]` puts every member of `batch` into this one argv, exactly as a
    #: bare `*batch` would. `age-sweep-lessons._batched_git_mv_into_dir` is the real shape --
    #: already byte-budget chunked, so its spawn count is O(argv_bytes), never O(items), and it
    #: sat in the register as a `structural-floor` claim only because the matcher stopped at a
    #: bare `Name`. Restricted to ONE generator with no `async`: a nested comprehension does not
    #: carry one loop's group, and `ifs` are harmless because filtering shrinks the group rather
    #: than multiplying spawns.
    if isinstance(node, (ast.ListComp, ast.GeneratorExp, ast.SetComp)):
        if len(node.generators) == 1 and not node.generators[0].is_async:
            return _splices_name_from(node.generators[0].iter, target_names)
    return False


def _argv_splices_loop_target(
    call: ast.Call, target_names: set[str], bindings: dict[str, ast.expr] | None = None
) -> bool:
    """True when `call`'s first positional argument is an argv-shaped expression that splices a
    loop-target name into itself as a sequence -- `<List> + chunk`, `chunk + <List>`, a chain of
    either, or `[*base, *chunk]`.

    The argument must itself be argv-shaped (a `List` or an `Add` `BinOp`); a bare `Name` argv
    declines, because a call passing the loop target ALONE says nothing about whether that
    target is one item or a group -- `for path in paths: _run(argv_for(path))` and `for chunk in
    chunks: _run(chunk)` are indistinguishable at this seam, and declining is the safe direction
    for a suppressor.

    ONE-HOP LOCAL BINDING (2026-08-19). That bare-`Name` decline was over-strict for the most
    common spelling of the very idiom this discriminator exists for: the chunker builds argv as
    its own statement and then passes the local --

        cmd = log_cmd_base + revision_args + ["--"] + batch
        subprocess.run(cmd, ...)

    -- so `args[0]` is `Name('cmd')` and the splice is one assignment away. `bindings`
    (`_loop_expr_bindings`, argv-shaped RHSs only) resolves that hop, which is the SAME
    treatment discriminator 6 has always given argv0. Measured cost of not having it: two keys
    sat in the registers on a stated reason that was wrong -- the MISCLASSIFIED note for
    `percolate-gate._git_log_batched` blamed a missing multi-operand `BinOp(Add)` walk, but
    `_argv_expr_splices` already recurses through chains; the local binding was the actual gap,
    and the fix the note prescribed would not have retired the key.

    Resolving a binding is NOT the same relaxation the bare-`Name` rule refuses: after the hop
    the real `List`/`BinOp` shape is in hand and the sequence-splice test runs against it
    unchanged. A name bound to anything not argv-shaped still declines, and the loop target
    itself is never resolvable -- it is not assigned in the loop body."""
    if not target_names or not call.args:
        return False
    argv = call.args[0]
    if isinstance(argv, ast.Name) and bindings and argv.id in bindings:
        argv = bindings[argv.id]
    return _argv_expr_splices(argv, target_names)


def _loop_expr_bindings(loop: ast.AST) -> dict[str, ast.expr]:
    """Name -> its full right-hand-side expression, for every single-target `ast.Assign` in
    `loop`'s subtree whose RHS is argv-SHAPED (a `List` or an `Add` `BinOp`).

    Discriminator 7's one-hop resolution. `_loop_argv0_bindings` is the sibling of this for
    discriminator 6 and keeps only the extracted argv0 HEAD; discriminator 7 needs the whole
    expression, because what it asks is whether the loop target is spliced in as a SEQUENCE
    anywhere in argv, not what sits at position zero.

    Restricted to argv-shaped RHSs on purpose: a name bound to anything else tells this
    discriminator nothing, and resolving it would only widen a suppressor."""
    bindings: dict[str, ast.expr] = {}
    for node in ast.walk(loop):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if isinstance(node.value, ast.List) or (
            isinstance(node.value, ast.BinOp) and isinstance(node.value.op, ast.Add)
        ):
            bindings[target.id] = node.value
    return bindings


def _argv_expr_splices(argv: ast.expr, target_names: set[str]) -> bool:
    """`_argv_splices_loop_target`'s recursion, over the argv expression itself -- separated so
    a nested concatenation chain (`base + mid + chunk`, which parses as
    `BinOp(BinOp(base, mid), chunk)`) recurses on operands directly."""
    if isinstance(argv, ast.BinOp) and isinstance(argv.op, ast.Add):
        return any(
            _splices_name_from(operand, target_names) or _argv_expr_splices(operand, target_names)
            for operand in (argv.left, argv.right)
        )
    if isinstance(argv, ast.List):
        return any(
            isinstance(elt, ast.Starred) and _splices_name_from(elt.value, target_names)
            for elt in argv.elts
        )
    return False


def _argv_accumulates_loop_target(
    loop: ast.AST | None,
    call: ast.Call,
    target_names: set[str],
    bindings: dict[str, ast.expr] | None,
) -> bool:
    """Discriminator 7's ACCUMULATION leg: argv is built up by `.extend()`/`.append()` inside a
    NESTED loop over the outer loop's target, then spawned once -- so the single call carries
    the whole group, exactly like the `base + chunk` splice the concatenation leg already reads.

    The splice leg cannot see this spelling. `_loop_expr_bindings` resolves the argv name to its
    `ast.Assign` RHS, and here that RHS is the BARE PREFIX (`argv = ["git", "restore",
    "--staged", "--"]`) with the batch arriving afterwards through mutation; no `Starred` element
    and no `BinOp(Add)` operand ever names the loop target, so `_argv_expr_splices` declines by
    construction. Measured cost of not having it: `ops/fleet/_common.py::
    _resync_main_index_for_moves` and `::_resync_main_index_for_reaps`, whose bounded
    `_argv_group_chunks` loops are the 2026-08-21 FIX for a Windows `WinError 206` argv overflow
    -- the collector flagged the remedy it recommends, discriminator 4's own stated failure mode
    one spelling over.

    Not a widening of the bare-`Name` decline `_argv_splices_loop_target` documents. Four
    conjuncts, all structural: the argv name is locally bound to an argv-shaped RHS inside this
    loop (so it is built per iteration, not carried in from outside); a nested `for` iterates
    something naming the outer target; that nested loop mutates THIS name via `extend`/`append`;
    and the mutation's arguments name the nested loop's own target, so what lands in argv is the
    per-item payload rather than a constant. A nested loop over an unrelated collection, or one
    that appends only invariants, still declines."""
    if loop is None or not target_names or not call.args:
        return False
    argv = call.args[0]
    if not isinstance(argv, ast.Name):
        return False
    if not bindings or argv.id not in bindings:
        return False
    for inner in ast.walk(loop):
        if inner is loop or not isinstance(inner, (ast.For, ast.AsyncFor)):
            continue
        if not (_names_in(inner.iter) & target_names):
            continue
        inner_targets = _loop_target_names(inner.target)
        if not inner_targets:
            continue
        for sub in ast.walk(inner):
            if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)):
                continue
            grower = sub.func
            if not (
                isinstance(grower.value, ast.Name)
                and grower.value.id == argv.id
                and grower.attr in ("extend", "append")
            ):
                continue
            if any(_names_in(arg) & inner_targets for arg in sub.args):
                return True
    return False


# --------------------------------------------------------------------------
# Repo-wide, one-hop function index (routes b/c/d/e)
# --------------------------------------------------------------------------


@dataclasses.dataclass
class _FuncIndex:
    #: top-level function name -> list of (relpath, func_name) whose body directly contains ANY
    #: recognized spawn site, regardless of argv0 (routes b/c/d).
    #:
    #: Was two dicts until AC11: a git-argv0-only one for routes b/c, and this any-spawn one for
    #: route d alone. Widening b/c to every spawn verb made them compute the same thing, so they
    #: are one field rather than two identical ones.
    direct_spawn_funcs: dict[str, list[tuple[str, str]]] = dataclasses.field(default_factory=dict)
    #: top-level function name -> forwarded parameter name, for a single-parameter function whose
    #: body forwards that parameter unchanged into exactly one recognized spawn call (route e)
    runner_shaped_funcs: dict[str, str] = dataclasses.field(default_factory=dict)
    #: (relpath, func_name) -> True, restricted to route-b's SAME-MODULE lookup
    same_module_direct_spawn: dict[tuple[str, str], bool] = dataclasses.field(default_factory=dict)
    #: relpath -> set of names imported via `from X import name` in that file (route c's gate)
    imported_names_by_file: dict[str, set[str]] = dataclasses.field(default_factory=dict)
    #: (relpath, dotted_scope) -> {parameter name: the module-level function name its default
    #: binds to}, for parameters whose default is a bare `Name` (route f). Covers positional and
    #: keyword-only defaults alike -- the resync seams this exists for are keyword-only.
    param_runner_defaults: dict[tuple[str, str], dict[str, str]] = dataclasses.field(
        default_factory=dict
    )
    #: (relpath, func_name) -> the set of argv verbs that function actually SPAWNS for, when
    #: every spawn in its body is dominated by an `if <param>[0] in <MODULE_SET>:` branch
    #: (discriminator 5). Any other verb is served without a process.
    verb_gated_spawn_verbs: dict[tuple[str, str], frozenset[str]] = dataclasses.field(
        default_factory=dict
    )
    #: (relpath, func_name) -> that top-level function's own `ast` node (route g's substrate --
    #: it must read parameter lists and walk bodies of functions OTHER than the one it is
    #: currently visiting, which every other route avoids needing).
    func_defs: dict[tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef] = dataclasses.field(
        default_factory=dict
    )
    #: bare function name -> every (relpath, func_name) defining it (route g's cross-module
    #: resolution, gated by the same imported-name check routes c/f apply).
    funcs_by_name: dict[str, list[tuple[str, str]]] = dataclasses.field(default_factory=dict)
    #: (relpath, func_name, param_name) for every parameter that is BOTH invoked (leg 1) and
    #: reached by a direct spawner (leg 2) -- route g's fixed point. See
    #: `_spawn_bearing_params`.
    spawn_bearing_params: frozenset[tuple[str, str, str]] = frozenset()
    #: relpath -> {local_binding: {(original_imported_name, resolved_absolute_module), ...}},
    #: additive alongside `imported_names_by_file` (route c's original by-local-binding gate,
    #: UNCHANGED -- routes other than c still read it). A local name bound twice from different
    #: sources (`try: from a import f / except ImportError: from b import f`) keeps BOTH arms
    #: as separate pairs rather than collapsing to one, matching the deliberate
    #: over-approximation `ast.walk` already relies on elsewhere in this index. A module of
    #: `"*"` is the star-reexport decline-to-constrain sentinel -- see `_resolve_reexport_chain`.
    #: Populated by `_build_func_index`'s existing `ast.ImportFrom` walk (no new parse), then
    #: resolved by a second in-memory pass over the per-file raw records after the file loop
    #: completes -- see that function's docstring for why the second pass cannot run mid-loop.
    resolved_imports_by_file: dict[str, dict[str, set[tuple[str, str]]]] = dataclasses.field(
        default_factory=dict
    )


def _generic_runner_param(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef, spawn_linenos: set[int]
) -> str | None:
    """A single-parameter function that forwards that parameter, unchanged, as the FIRST
    positional argument of a RECOGNIZED SPAWN call in its body -- the `_run(argv)` wrapper
    idiom.

    The spawn-lineno gate is what the call site's git-argv0 check used to stand in for. While
    route e additionally required the caller's own argv to begin with the literal `"git"`, a
    runner that merely forwarded its parameter into some arbitrary call could not produce a
    violation on its own, so matching any `ast.Call` here was harmless. Once the collector
    counts every spawn verb (AC11), that looseness becomes the dominant false-positive source:
    any one-parameter function forwarding to any callee would qualify as a runner. Requiring the
    forwarding call to sit on a line `sites_in_source` independently detected as a spawn
    restores the precision the argv0 check was carrying, and matches what this route's own
    description always claimed.

    See module docstring's blind-spots note: this does not exclude nested function scopes from
    the walk, a deliberate false-negative-biased looseness."""
    params = [a.arg for a in func_node.args.args]
    if len(params) != 1:
        return None
    only_param = params[0]
    for node in ast.walk(func_node):
        if node is func_node:
            continue
        if isinstance(node, ast.Call) and node.args and node.lineno in spawn_linenos:
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id == only_param:
                return only_param
    return None


def _module_level_str_set_members(tree: ast.Module) -> dict[str, frozenset[str]]:
    """Names bound at module scope to a set/frozenset/tuple/list of STRING LITERALS, mapped to
    those literals -- the allowlist half of discriminator 5."""
    out: dict[str, frozenset[str]] = {}
    for node in tree.body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            continue
        value = node.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            if value.func.id not in {"set", "frozenset"} or not value.args:
                continue
            value = value.args[0]
        if not isinstance(value, (ast.Set, ast.Tuple, ast.List)):
            continue
        members = [e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if members and len(members) == len(value.elts):
            out[node.targets[0].id] = frozenset(members)
    return out


def _verb_gated_spawn_verbs(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    spawn_linenos: set[int],
    set_members: dict[str, frozenset[str]],
) -> frozenset[str] | None:
    """Discriminator 5. Return the set of argv verbs `func_node` actually SPAWNS a process
    for, when EVERY spawn site in its body is dominated by a single
    `if <param>[0] in <MODULE_LEVEL_STRING_SET>:` branch -- otherwise `None`.

    Why this is a discriminator and not an exemption: routes b/c resolve a callee by asking
    "does this function's body contain a git spawn?", which is a property of the FUNCTION.
    A verb-DISPATCHING chokepoint makes that the wrong question -- `pickup_assemble.
    _run_git` spawns real git for `status`/`diff`/`add`/`commit` and serves every other verb
    from an in-process read model, so `_run_git(["cat-file", "-e", sha], root)` in a loop
    creates ZERO processes. Reporting it as git amplification is not a conservative
    over-report; it is a claim about cost that is false, and its stated remedy ("batch it into
    a single call") would replace a working in-process read with a `--batch` form that
    chokepoint's read model does not serve.

    The evidence is entirely static and local: the verb is a string literal at the call site,
    the allowlist is a module-level literal set, and the branch dominating the spawn is an
    `ast.If` whose body's line range contains every spawn lineno. Nothing is inferred about
    runtime.

    Deliberately narrow, in this module's false-negative-over-false-positive direction: a
    chokepoint whose gate is not this exact shape, whose allowlist is not statically
    resolvable, or which has even one spawn outside the gated branch, resolves to `None` and
    is treated exactly as before.
    """
    if not spawn_linenos:
        return None
    params = [a.arg for a in func_node.args.args]
    for node in ast.walk(func_node):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.In)
            and isinstance(test.left, ast.Subscript)
            and isinstance(test.left.value, ast.Name)
            and test.left.value.id in params
            and isinstance(test.left.slice, ast.Constant)
            and test.left.slice.value == 0
            and isinstance(test.comparators[0], ast.Name)
        ):
            continue
        allowlist = set_members.get(test.comparators[0].id)
        if allowlist is None:
            continue
        body_lines = {
            lineno
            for stmt in node.body
            for lineno in range(stmt.lineno, (stmt.end_lineno or stmt.lineno) + 1)
        }
        if spawn_linenos <= body_lines:
            return allowlist
    return None


def _call_literal_verb(call: ast.Call) -> str | None:
    """The first argv element of `call`'s first positional argument, when that argument is a
    list/tuple literal starting with a string literal (`_run_git(["cat-file", ...], root)` ->
    `"cat-file"`). `None` for anything not statically decidable."""
    if not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, (ast.List, ast.Tuple)) and arg.elts:
        first = arg.elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


@dataclasses.dataclass(frozen=True)
class _FileRecord:
    """One file's read+parse+spawn-detect result, computed exactly ONCE and shared between
    `_build_func_index` and `find_unbatched_per_item_spawns`'s own violation-detection pass
    below. Pure memoization -- see `_load_file_records`'s docstring for why this exists; it
    changes cost, never output."""

    relpath: str
    file_path: pathlib.Path
    text: str
    tree: ast.Module
    spawn_sites: list


def _load_file_records(files: list[tuple[str, pathlib.Path]]) -> list[_FileRecord]:
    """Reads, parses (`ast.parse`), and spawn-detects (`sites_in_source`, which does its own
    internal `ast.parse`) each file in `files` exactly ONCE.

    Perf note (G3, 2026-08-08): the prior implementation had `_build_func_index` and
    `find_unbatched_per_item_spawns`'s own loop each independently re-read, re-`ast.parse`,
    and re-run `sites_in_source` (itself another `ast.parse`) over every file in the ~1287-file
    scoped corpus -- four parses per file, two full read+parse+detect passes, for identical
    results both times. Measured repo-wide: `_build_func_index` alone cost ~8.3s and the
    violation-detection loop (re-reading/re-parsing/re-detecting the same files) cost a further
    ~12.5s on top, out of a ~20-22s total. Sharing one `_FileRecord` list between both passes
    removes that duplication -- pure memoization, byte-identical output (same files, same
    order, same read/parse/detect results), never a change in what either pass computes.

    A file that fails to read, parse, or spawn-detect is skipped here exactly as it was skipped
    independently in each prior pass (both `_build_func_index` and the violation-detection loop
    applied the identical read/parse/detect try-except triplet before this change)."""
    records: list[_FileRecord] = []
    for relpath, file_path in files:
        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(text, filename=str(file_path))
        except SyntaxError:
            continue
        try:
            spawn_sites = sites_in_source(text, relpath)
        except SpawnParseError:
            continue
        records.append(_FileRecord(relpath, file_path, text, tree, spawn_sites))
    return records


#: Bound on a re-export hop chain (`_resolve_reexport_chain`). A re-export chain deeper than
#: this is a corpus smell, not a case this resolver supports -- see that function's docstring
#: for what happens on exhaustion (resolve to the syntactically named module, not an
#: iteration-order-dependent "last module reached").
_REEXPORT_HOP_BOUND = 4

#: Sentinel module value meaning "decline to constrain by module" -- a star re-export
#: (`from .impl import *`) cannot be followed by name, so the pair reached so far is kept but
#: its module is widened to match anything rather than pruned to nothing (Finding 5).
_UNCONSTRAINED_MODULE = "*"


def _relpath_to_module(relpath: str) -> str:
    """Dotted module path for `relpath`, collapsing `pkg/sub/__init__.py` to the package
    `pkg.sub` -- never `pkg.sub.__init__` -- because an `__init__.py` file IS the module it
    packages, not a submodule of it (route c resolver case 1: the `__init__` collapse)."""
    posix = relpath.replace("\\", "/")
    if posix.endswith("/__init__.py"):
        posix = posix[: -len("/__init__.py")]
    elif posix == "__init__.py":
        posix = ""
    elif posix.endswith(".py"):
        posix = posix[: -len(".py")]
    return posix.replace("/", ".")


def _relpath_is_package_init(relpath: str) -> bool:
    """True for `pkg/sub/__init__.py` (or a top-level `__init__.py`) -- route c resolver case
    3 needs this distinct from `_relpath_to_module`'s collapse: an `__init__.py`'s OWN package,
    for relative-import resolution, is itself, never its parent."""
    posix = relpath.replace("\\", "/")
    return posix.endswith("/__init__.py") or posix == "__init__.py"


def _absolute_import_module(relpath: str, node: ast.ImportFrom) -> str:
    """The absolute dotted module `node` (a `from X import ...`, any `level`) names, resolved
    against `relpath`'s own package -- route c resolver case 3. `level == 0` is already
    absolute (`node.module` as written). For `level > 0`, the base package is `relpath`'s own
    module UNLESS `relpath` is a package `__init__.py`, in which case the base package is that
    module itself (case 1's collapse applies here too, so a level-1 relative import inside a
    package's `__init__.py` resolves against the package, not its grandparent) -- then `level -
    1` further trailing components are stripped, matching Python's own relative-import
    algorithm."""
    if node.level == 0:
        return node.module or ""
    own_module = _relpath_to_module(relpath)
    if _relpath_is_package_init(relpath):
        package = own_module
    elif "." in own_module:
        package = own_module.rsplit(".", 1)[0]
    else:
        package = ""
    parts = package.split(".") if package else []
    strip = node.level - 1
    if strip > 0:
        parts = parts[: len(parts) - strip] if strip <= len(parts) else []
    base = ".".join(parts)
    if node.module:
        return f"{base}.{node.module}" if base else node.module
    return base


def _resolve_reexport_chain(
    raw_imports_by_file: dict[str, dict[str, set[tuple[str, str]]]],
    module_to_relpath: dict[str, str],
    start_name: str,
    start_module: str,
) -> tuple[str, str]:
    """Follow a `(original_name, module)` pair across re-export hops to the module that
    actually DEFINES the name, CARRYING AND REWRITING THE NAME at each hop (route c resolver
    case 2) -- not just the module, since a renaming re-export (`from .impl import f as g` in
    an `__init__.py`) must rewrite `original_name` from `g` back to `f` at the hop into `impl`,
    or the pair matches nothing downstream.

    Bounded at `_REEXPORT_HOP_BOUND` hops. On cycle detection OR bound exhaustion, resolves to
    the pair reached so far -- the SYNTACTICALLY NAMED module as written at that point, never an
    iteration-order-dependent "last module reached" -- which degrades cleanly to the naive
    dotted-path rule already measured (6279 kept).

    A star re-export (`from .impl import *`) cannot be followed by name -- the target file's
    raw imports carry no per-name record to hop through. Rather than pruning the binding to
    nothing, this DECLINES TO CONSTRAIN it: the module is widened to `_UNCONSTRAINED_MODULE`,
    which the match helper (`_import_resolves_to`) treats as matching any module, falling back
    to the current all-candidates behaviour for that one binding (Finding 5)."""
    name, module = start_name, start_module
    seen: set[tuple[str, str]] = set()
    for _ in range(_REEXPORT_HOP_BOUND):
        if (name, module) in seen:
            break
        seen.add((name, module))
        tgt_relpath = module_to_relpath.get(module)
        if tgt_relpath is None:
            break
        tgt_imports = raw_imports_by_file.get(tgt_relpath, {})
        candidates = tgt_imports.get(name)
        if not candidates:
            if "*" in tgt_imports:
                return name, _UNCONSTRAINED_MODULE
            break
        if len(candidates) != 1:
            # Ambiguous hop target (a try/except import binding the same name from two
            # sources) -- stop rather than picking one arm arbitrarily.
            break
        name, module = next(iter(candidates))
    return name, module


def _resolve_imports_by_file(
    raw_imports_by_file: dict[str, dict[str, set[tuple[str, str]]]],
    module_to_relpath: dict[str, str],
) -> dict[str, dict[str, set[tuple[str, str]]]]:
    """Second in-memory pass (route c resolver, AC2): resolves every raw `(original_name,
    module-as-written)` pair collected during `_build_func_index`'s file loop into its final
    `(original_name, resolved_module)` pair, following re-export hops via
    `_resolve_reexport_chain`. Runs AFTER the file loop completes because a re-export hop needs
    the TARGET file's own import data, which may not exist yet mid-loop -- not a second walk
    over the filesystem or the ASTs, and no subprocess."""
    resolved: dict[str, dict[str, set[tuple[str, str]]]] = {}
    for relpath, bindings in raw_imports_by_file.items():
        resolved_bindings: dict[str, set[tuple[str, str]]] = {}
        for local_binding, pairs in bindings.items():
            resolved_bindings[local_binding] = {
                _resolve_reexport_chain(raw_imports_by_file, module_to_relpath, name, module)
                for name, module in pairs
            }
        resolved[relpath] = resolved_bindings
    return resolved


def _resolve_imported_defs(
    index: _FuncIndex, relpath: str, local_name: str
) -> list[tuple[str, str]]:
    """Route c's RESOLUTION (AC3), and the reason this is a resolver rather than a predicate.

    Answers: which definitions does `local_name`, as imported into `relpath`, actually name?
    The candidate pool is built FROM the resolved ORIGINAL name -- `funcs_by_name[orig_name]`
    -- and then constrained to the resolved source module. It is NOT `funcs_by_name[local_name]`
    filtered afterwards.

    THAT DISTINCTION IS THE WHOLE DEFECT, and it is invisible until you write an aliased
    fixture. `funcs_by_name` is keyed by DEFINITION names; a local alias is not one. So a
    candidate pool keyed on the local binding contains only homonyms OF THE ALIAS, and no
    predicate applied to that pool -- however correct -- can ever yield the definition actually
    imported. Filtering it prunes the false positives to nothing and reports the true callee as
    unreachable, which is a false NEGATIVE wearing a green test: the shape
    `pln-route-c-resolves-the-imported-name-not-the-local-alias` rejected in its own Considered
    alternatives, then shipped anyway on 2026-08-26 because AC3 was phrased as "resolves a
    candidate ONLY WHEN ..." -- a necessary condition, satisfiable by inert code -- and every
    test pinned the predicate in isolation. Goal probe:
    `test_route_c_resolves_the_imported_name_not_the_local_alias`.

    A star-reexport hop (`_UNCONSTRAINED_MODULE`) declines to constrain the MODULE, never the
    name: all definitions of the original name match. Definitions in `relpath` itself are
    excluded -- a same-file definition is route b's job, resolved before this is consulted."""
    pairs = index.resolved_imports_by_file.get(relpath, {}).get(local_name)
    if not pairs:
        return []
    out: list[tuple[str, str]] = []
    for orig_name, module in pairs:
        for cand in index.funcs_by_name.get(orig_name, []):
            if cand[0] == relpath or cand in out:
                continue
            if module == _UNCONSTRAINED_MODULE or _relpath_to_module(cand[0]) == module:
                out.append(cand)
    return out


def _import_resolves_to(
    index: _FuncIndex, relpath: str, local_name: str, def_relpath: str, def_name: str
) -> bool:
    """Route c's shared match helper (AC3), the single site all three route-c resolution
    sites call (`_resolve_callee_def`, `_is_direct_spawner_name`, and
    `find_unbatched_per_item_spawns`'s own `imported_here` leg) -- see module docstring's
    route-c section for why route c needs both the ORIGINAL imported name and the resolved
    SOURCE MODULE, not merely the local binding `imported_names_by_file` already gates on.

    True when `local_name`, as imported into `relpath`, resolves -- through
    `index.resolved_imports_by_file` -- to the definition at `(def_relpath, def_name)`: a
    candidate matches when ANY pair in the local binding's resolved set agrees on both name and
    module (Finding 4's set, not a single pair, so a name bound twice from different sources
    keeps both arms). A resolved module of `_UNCONSTRAINED_MODULE` (a star-reexport hop,
    Finding 5) matches any module.

    For an UNALIASED import, `original_name` IS the local binding, so this narrows exactly the
    homonym case an alias creates: an aliased import makes the local binding and the
    definition name diverge, and a lookup keyed on the local binding alone (the prior rule)
    can only ever find homonyms of the ALIAS, never the name actually defined at the resolved
    source."""
    pairs = index.resolved_imports_by_file.get(relpath, {}).get(local_name)
    if not pairs:
        return False
    def_module = _relpath_to_module(def_relpath)
    for orig_name, module in pairs:
        if orig_name == def_name and (module == def_module or module == _UNCONSTRAINED_MODULE):
            return True
    return False


def _build_func_index(records: list[_FileRecord]) -> _FuncIndex:
    """One pass over the scoped corpus, building the repo-wide name index routes b/c/d/e/f
    resolve against, single-hop only for those five. Route g's `spawn_bearing_params` is the
    exception: `_compute_spawn_bearing_params`, called at the end of this function once
    `func_defs`/`funcs_by_name`/`same_module_direct_spawn`/`direct_spawn_funcs` are populated,
    runs a bidirectional FIXED POINT over forwarded parameters -- see module docstring's
    route-g section for the algorithm.

    It terminates: both of its taint sets (`invoked`, `tainted`) grow MONOTONICALLY -- an
    element, once added, is never removed -- over a FINITE domain, `(relpath, func_name,
    param_name)` triples bounded by the scoped corpus's own function and parameter count. Each
    fixed-point loop can therefore add a new element at most that many times before a round
    adds nothing and its `changed` flag stays `False`, so both loops halt.

    Consumes pre-computed `_FileRecord`s (G3) rather than re-reading/re-parsing/re-detecting
    each file itself -- see `_load_file_records`'s docstring.

    `resolved_imports_by_file` (route c's resolver, AC1/AC2) is populated in two passes over
    this SAME `ast.ImportFrom` walk's raw output, not a second parse: the walk below collects
    each file's raw `(original_name, module-as-written)` pairs into `raw_imports_by_file` as it
    already visits every file once for `imported_names_by_file`; `_resolve_imports_by_file`
    then runs ONCE, after this loop completes, over that in-memory dict -- a re-export hop
    needs the TARGET file's own raw import data, which may not exist yet mid-loop."""
    index = _FuncIndex()
    raw_imports_by_file: dict[str, dict[str, set[tuple[str, str]]]] = {}

    for record in records:
        relpath = record.relpath
        tree = record.tree
        spawn_sites = record.spawn_sites

        spawning_enclosing = {s.enclosing for s in spawn_sites}
        # Review: reviewer -- keyed by the spawn's OWN dotted enclosing scope (e.g.
        # "outer._forward"), not the bare top-level function name a lookup by `name` alone
        # would use. A runner candidate's forwarding call can sit inside a nested closure
        # (own_spawn_linenos below matches `name` itself AND any dotted scope nested under
        # it), so this dict is built keyed on the raw dotted `enclosing` strings and matched
        # by prefix at lookup time, not collapsed to bare names here.
        spawn_linenos_by_func: dict[str, set[int]] = {}
        for site in spawn_sites:
            spawn_linenos_by_func.setdefault(site.enclosing, set()).add(site.lineno)
        set_members = _module_level_str_set_members(tree)

        _ParamDefaultTracker(relpath, index.param_runner_defaults).visit(tree)

        imported: set[str] = set()
        raw_imports: dict[str, set[tuple[str, str]]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                abs_module = _absolute_import_module(relpath, node)
                for alias in node.names:
                    imported.add(alias.asname or alias.name)
                    local_binding = alias.asname or alias.name
                    raw_imports.setdefault(local_binding, set()).add((alias.name, abs_module))
        index.imported_names_by_file[relpath] = imported
        raw_imports_by_file[relpath] = raw_imports

        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = node.name

            # Route g's substrate: every top-level function's own node, plus a bare-name ->
            # defining-sites index for cross-module resolution (`_resolve_callee_def`).
            index.func_defs[(relpath, name)] = node
            index.funcs_by_name.setdefault(name, []).append((relpath, name))

            # Review: reviewer -- `name` is this function's own bare (top-level) name, but a
            # spawn the function reaches only through a nested closure is filed under a
            # DOTTED scope ("name.inner"), not bare "name" -- matching `spawn_linenos_by_func`
            # by exact key alone would miss it (`_generic_runner_param` walks into nested
            # defs, so it can see that lineno). Own linenos are every spawn whose recorded
            # enclosing is this function itself OR nested under it.
            own_spawn_linenos: set[int] = set()
            for enclosing_key, linenos in spawn_linenos_by_func.items():
                if enclosing_key == name or enclosing_key.startswith(name + "."):
                    own_spawn_linenos |= linenos

            if name in spawning_enclosing:
                index.direct_spawn_funcs.setdefault(name, []).append((relpath, name))
                index.same_module_direct_spawn[(relpath, name)] = True
                gated = _verb_gated_spawn_verbs(node, own_spawn_linenos, set_members)
                if gated is not None:
                    index.verb_gated_spawn_verbs[(relpath, name)] = gated

            runner_param = _generic_runner_param(node, own_spawn_linenos)
            if runner_param is not None and name not in index.runner_shaped_funcs:
                index.runner_shaped_funcs[name] = runner_param

    module_to_relpath = {_relpath_to_module(r.relpath): r.relpath for r in records}
    index.resolved_imports_by_file = _resolve_imports_by_file(raw_imports_by_file, module_to_relpath)

    index.spawn_bearing_params = _compute_spawn_bearing_params(index)
    return index


# --------------------------------------------------------------------------
# Route g: bidirectional fixed-point over forwarded parameters. Ported from the validated
# prototype (see module docstring's route-g description for the algorithm in prose); this is
# a direct transcription, not a re-derivation.
# --------------------------------------------------------------------------


def _func_params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    a = fn.args
    return [p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)]


def _func_positional_params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    a = fn.args
    return [p.arg for p in (*a.posonlyargs, *a.args)]


def _forwarded_arg_slots(
    call: ast.Call, callee_fn: ast.FunctionDef | ast.AsyncFunctionDef, name: str
) -> list[str]:
    """Parameter names of `callee_fn` that receive a bare-`Name` argument `name` at `call` --
    positional by index, keyword by name. Route g's forwarding-hop primitive: a fact about
    `name` in the CALLER (invoked, or tainted by a spawner) becomes the same fact about
    whichever parameter receives it in the CALLEE, one hop at a time."""
    positional = _func_positional_params(callee_fn)
    out: list[str] = []
    for i, arg in enumerate(call.args):
        if isinstance(arg, ast.Name) and arg.id == name and i < len(positional):
            out.append(positional[i])
    for kw in call.keywords:
        if kw.arg and isinstance(kw.value, ast.Name) and kw.value.id == name:
            out.append(kw.arg)
    return out


def _arg_expr_for_param(
    call: ast.Call, callee_fn: ast.FunctionDef | ast.AsyncFunctionDef, param: str
) -> ast.expr | None:
    """The ARGUMENT EXPRESSION `call` supplies for `callee_fn`'s parameter `param` -- positional
    by index, keyword by name. `None` when the slot is not filled at this call site (the
    parameter falls back to its default, which is loop-invariant by construction and therefore
    cannot be what varies).

    Distinct from `_forwarded_arg_slots`, which runs the other direction and only recognises a
    bare `Name`: discriminator 8 needs the whole expression (`entry["cmd"]`, `spec.exe`,
    `_resolve(tool)`) because the program name at a real per-item spawn is rarely a bare local.
    Never guesses a slot -- `*args`/`**kwargs` forwarding declines, since which parameter
    receives what is not statically known there and a suppressor must not guess."""
    positional = _func_positional_params(callee_fn)
    for i, arg in enumerate(call.args):
        if isinstance(arg, ast.Starred):
            return None
        if i < len(positional) and positional[i] == param:
            return arg
    for kw in call.keywords:
        if kw.arg is None:
            return None
        if kw.arg == param:
            return kw.value
    return None


def _helper_spawn_argv0_params(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, spawn_linenos: set[int]
) -> set[str]:
    """Parameters of `fn` that ARE the argv0 of a spawn `fn` itself performs.

    The interprocedural half of discriminator 8. Walks `fn`'s own body for a recognized spawn
    call, extracts that call's `argv[0]` with `_argv0_expr` (resolved through `fn`-local
    assignment bindings, the same one-hop idiom discriminator 6 already handles at route a),
    and keeps the result only when it is a bare `Name` naming one of `fn`'s own parameters.

    Deliberately narrow. The program-identity slot being caller-supplied is the only shape that
    lets a caller's loop vary the program. An identity built from a module constant, a literal,
    or a closure is loop-invariant no matter what the caller does, and returning it here would
    suppress a real amplification site.

    Reads the identity via `_program_identity_expr`, not `_argv0_expr`, so it looks past a fixed
    `sys.executable` to the script behind it -- and MATCHES ON REFERENCE rather than on a bare
    parameter `Name`. Both were measured necessities, not generalisation for its own sake: the
    real helpers spell it `script_path = root / rel` then `[interpreter, str(script_path), ...]`,
    so the identity expression REFERENCES the parameter through an assignment and a call instead
    of being one. Requiring a bare `Name` retired one key of six.

    Referencing a parameter from the identity slot is still a strong claim: it says the caller
    supplies part of WHICH PROGRAM RUNS, never merely an argument to a fixed one. The slot does
    the discriminating; the reference only says who filled it."""
    params = set(_func_params(fn))
    if not params:
        return set()
    bindings = _loop_argv0_bindings(fn)
    local = _loop_expr_bindings(fn)
    out: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or node.lineno not in spawn_linenos:
            continue
        if _call_callee_name(node) not in _SPAWN_API_NAMES or not node.args:
            continue
        #: MERGE ORDER IS LOAD-BEARING, and was backwards until 2026-08-19. `bindings`
        #: (`_loop_argv0_bindings`) holds only the extracted argv0 HEAD; `local`
        #: (`_loop_expr_bindings`) holds the FULL argv expression. With `bindings` last, a name
        #: present in both resolved to the bare head -- `sys.executable` -- and
        #: `_program_identity_expr` could never look past the interpreter to the script behind
        #: it, which is the exact shape this function's docstring above says it was built for.
        #: `local` is strictly more informative wherever both hold a name, so it wins.
        identity = _program_identity_expr(node.args[0], {**bindings, **local})
        if identity is None:
            continue
        out |= _names_in_through_assignments(identity, fn) & params
    return out


def _names_in_through_assignments(expr: ast.expr, fn: ast.AST) -> set[str]:
    """Every name `expr` references, followed back through single-target assignments in `fn`.

    `script_path = root / rel` then `str(script_path)` references `rel` as far as this is
    concerned. Bounded by a fixed-point over `fn`'s own assignments, so it terminates and never
    leaves the function."""
    assigns: dict[str, ast.expr] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                assigns.setdefault(target.id, node.value)
    seen = _names_in(expr)
    while True:
        grown = set(seen)
        for name in list(seen):
            if name in assigns:
                grown |= _names_in(assigns[name])
        if grown == seen:
            return seen
        seen = grown


def _argv0_varies_through_helper(
    call: ast.Call,
    callee: str,
    index: _FuncIndex,
    relpath: str,
    tainted: frozenset[str],
    spawn_linenos_by_file: dict[str, set[int]],
) -> bool:
    """DISCRIMINATOR 8 -- argv0 varies with the loop target ACROSS ONE HELPER HOP.

    Discriminator 6 answers "does this loop spawn a different PROGRAM each iteration", which is
    unbatchable by construction: there is no single argv0 for a batched call to share, so the
    sibling gate's remedy (hoist to one call outside the loop) has nothing to hoist. But 6 is
    restricted to route a -- a call that IS the spawn syscall -- because `args[0]` at a wrapper
    call site is the wrapper's own parameter, not an OS argv, and applying 6 there caused a real
    false suppression (a verb-gated `_run_git([verb, '--quiet'], root)`, where `verb` is the loop
    target but names a git SUBCOMMAND, not a program).

    That restriction is correct and is also why 6 could not see most of this repo: MEASURED
    2026-08-19 over the 53 entries the exemption register held after adversarial
    re-verification, 41 of their 65 call sites are route `b-local-helper` and only 16 are route
    a. Discriminator 6 was structurally blind to three quarters of the population, and the gap
    was filled with 30 hand-written `structural-floor` exemptions instead of a pass that decides
    it -- the register's own comment records that trade.

    This closes the gap WITHOUT relaxing 6's guard, by resolving the program name through the
    helper rather than assuming it:

      1. resolve `callee` to its definition (same-module, else imported -- routes b/c's own
         discipline, via `_resolve_callee_def`);
      2. ask which of that function's PARAMETERS is the argv0 of a spawn it performs itself
         (`_helper_spawn_argv0_params`) -- never a constant, literal, or closure, which no
         caller can vary;
      3. read the argument this call site actually supplies for that parameter
         (`_arg_expr_for_param`); and
      4. require that argument to reference a loop-tainted name.

    Only then is the program genuinely different each iteration. The verb-gated false
    suppression cannot recur here: `_run_git([verb, ...], root)` fails step 2, because
    `_run_git`'s argv0 is the literal `"git"` in its own body, never one of its parameters.

    One hop only, and no fallback: a helper that forwards to a second helper declines rather
    than chaining. This SUPPRESSES, so it inherits the inversion this module's docstring warns
    about -- an over-broad match silences a REAL site and nothing downstream notices. Declining
    a site it cannot decide is the safe direction; suppressing one it guessed at is not."""
    if not tainted:
        return False
    for tgt_relpath, tgt_name in _resolve_callee_def_wide(index, relpath, callee):
        fn = index.func_defs.get((tgt_relpath, tgt_name))
        if fn is None:
            continue
        argv0_params = _helper_spawn_argv0_params(
            fn, spawn_linenos_by_file.get(tgt_relpath, set())
        )
        for param in argv0_params:
            supplied = _arg_expr_for_param(call, fn, param)
            if supplied is not None and (_names_in(supplied) & tainted):
                return True
    return False


# --------------------------------------------------------------------------
# Discriminator 14: the loop is a LINEAR SEARCH whose subject is an out-of-band observation.
# The spawn's job is to perturb something, and the loop exists to find WHICH iteration perturbed
# it -- so collapsing the loop destroys attribution, which is the whole output. `find_polluter`
# is the type specimen: it runs one test file, then asks the filesystem whether a stray artifact
# appeared, and stops at the first one that did.
#
# Discriminator 15: the spawn fires only on a branch gated by an OPERATOR KEYPRESS read inside
# the loop. Spawn count is bounded by human input, not by item count, and the modal path spawns
# zero.
# --------------------------------------------------------------------------

#: Expressions that read a fresh answer from the operator. `input()` and `sys.stdin.read*`.
_OPERATOR_READ_ATTRS = frozenset({"readline", "readlines", "read"})


def _reads_operator_input(expr: ast.expr) -> bool:
    """True when `expr` obtains a value from the interactive operator."""
    for node in ast.walk(expr):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "input":
            return True
        if isinstance(func, ast.Attribute) and func.attr in _OPERATOR_READ_ATTRS:
            value = func.value
            #: `sys.stdin.readline()` -- require the stdin qualifier, so an ordinary
            #: `path.read_text()` or a socket read is not mistaken for a human.
            if isinstance(value, ast.Attribute) and value.attr == "stdin":
                return True
            if isinstance(value, ast.Name) and value.id == "stdin":
                return True
    return False


def _cache_guard_covers(loop: ast.AST, call: ast.Call, name: str) -> bool:
    """True when *call* is reachable only through an `if <name> is None:` / `if not <name>:`
    test inside *loop* -- the "have I resolved this yet" gate.

    `is not None` and `!=` are deliberately NOT accepted: a call behind `if cache is not None`
    runs on every iteration after the first, which is per-item amplification wearing a guard."""
    for node in ast.walk(loop):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        matches = False
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == name
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Is)
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is None
        ):
            matches = True
        elif (
            isinstance(test, ast.UnaryOp)
            and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Name)
            and test.operand.id == name
        ):
            matches = True
        if matches and any(sub is call for stmt in node.body for sub in ast.walk(stmt)):
            return True
    return False


def _bound_before_loop(
    fn: "ast.FunctionDef | ast.AsyncFunctionDef", loop: ast.AST, name: str
) -> bool:
    """True when *name* is assigned in *fn* strictly BEFORE *loop* and outside it.

    This is the clause that separates a once-per-scan cache from a per-iteration local. A name
    first bound INSIDE the loop is re-created every pass, so its `is None` test is true every
    pass and the call fires every pass -- amplification, not memoization."""
    loop_start = getattr(loop, "lineno", None)
    if loop_start is None:
        return False
    inside_loop = {id(n) for n in ast.walk(loop)}
    for node in ast.walk(fn):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if id(node) in inside_loop:
            continue
        if getattr(node, "lineno", loop_start) >= loop_start:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(name in _names_in(t) for t in targets):
            return True
    return False


def _cache_reset_inside_loop(loop: ast.AST, call: ast.Call, name: str) -> bool:
    """True when *name* is reassigned inside *loop* by anything OTHER than the memoizing call.

    A cache cleared (or rebound) mid-loop is resolved again on the next pass, so the site really
    does spawn per item. Without this clause the discriminator would silence exactly the shape it
    must keep reporting."""
    for node in ast.walk(loop):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is not None and any(sub is call for sub in ast.walk(value)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(name in _names_in(t) for t in targets):
            return True
    return False


def _is_lazily_memoized_resolution(
    call: ast.Call,
    loop: ast.AST | None,
    fn: "ast.FunctionDef | ast.AsyncFunctionDef | None",
) -> bool:
    """DISCRIMINATOR 16 -- the call resolves ONCE PER SCAN behind a lazy cache, so the loop it
    sits in does not multiply it. Reporting it asks for a batching that has already happened.

    The shape, and every clause is load-bearing:

        cache = None                      # bound BEFORE the loop  (`_bound_before_loop`)
        for item in items:
            if cache is None:             # unset-test gate        (`_cache_guard_covers`)
                cache = resolve(...)      # the flagged call, assigned to that same name
            use(cache, item)              # and never rebound      (`_cache_reset_inside_loop`)

    RETAINED ON ITS OWN MERITS (2026-08-26, plan
    `2026-08-26-route-c-resolves-the-imported-name-not-the-local-alias`, chunk C4). This
    discriminator's RULE stands on its own: a single-slot latch resolves once per scan, and that
    is true independently of any one site -- it carries a positive control
    (`test_discriminator_lazy_memo_not_flagged`, a route-a direct-spawn-in-loop fixture that
    route c's fix cannot subsume, since it never touches an import at all) plus its own
    load-bearing negatives, and is not a stand-in for either register.

    The motivating site's OWN false-positive cause, previously unexplained, IS now understood
    and fixed upstream: the P1 that owns it
    (`state/bug-backlog/2026-08-25-the-amplification-gate-resolves-an-alias-bf22411daeda.yaml`)
    originally blamed route c resolving an alias to a same-named sibling THAT SPAWNS. That
    mechanism was re-measured mid-investigation and looked like it did not hold; it was later
    confirmed correct at a finer grain by the route-c-resolves-the-imported-name-not-the-local-
    alias plan (`4ca622718` and the chunk-C1 fix that followed it): route c was matching a local
    binding name against `funcs_by_name` without checking that the resolved definition's own
    source module and original imported name agreed, so an aliased import could resolve to an
    unrelated same-named sibling. C1's route-c fix (this plan) now excludes the
    `promote_shipped_in_flight_stubs.py:493` site UPSTREAM, on that ground, independently of this
    discriminator. This predicate is retained anyway: it decides the general
    lazily-memoized-single-slot-cache shape on its own terms, not as a proxy for the alias bug,
    and continues to keep quiet any other site with the same shape that route c's fix does not
    reach.

    Motivating site, and why this is a FALSE POSITIVE rather than debt:
    `coordinator_core/ops/promote_shipped_in_flight_stubs.py :: _run_promotions` resolves
    `_git_common_dir` exactly once per scan behind this guard. The memoization is not incidental
    -- it is a code-reviewer F3 fix with a comment at the site saying so. Neither register could
    hold it: `_EXEMPT_SITES` requires one of four closed classes that all assert "unbatchable by
    construction", which is false of a call already batched to one; and `_KNOWN_SITES` means
    "real debt, batch it later", which invites deleting the reviewer's cache as the repair. The
    honest fix was for the collector to stop reporting it.

    OUT OF SCOPE, deliberately -- a keyed cache (`if key not in cache: cache[key] = resolve(key)`)
    is NOT this shape and stays reported. Its call count is bounded by DISTINCT KEYS, which is a
    function of the iterable and may be N; only a single-slot cache is provably once-per-scan.
    This SUPPRESSES, so it takes the narrow reading."""
    if loop is None or fn is None:
        return False

    cached_names: set[str] = set()
    for node in ast.walk(loop):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        if not any(sub is call for sub in ast.walk(node.value)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            cached_names |= _names_in(target)

    for name in sorted(cached_names):
        if not _cache_guard_covers(loop, call, name):
            continue
        if not _bound_before_loop(fn, loop, name):
            continue
        if _cache_reset_inside_loop(loop, call, name):
            continue
        return True
    return False


def _is_operator_gated_spawn(call: ast.Call, loop: ast.AST | None) -> bool:
    """DISCRIMINATOR 15 -- the call is reachable only through a test on a value the operator
    typed DURING this iteration.

    The inside-the-loop-body requirement is the whole narrowness. A stdin read BEFORE the loop is
    the ITERABLE'S SOURCE, not a per-item gate, and suppressing on that would silence the ordinary
    "read a work list from stdin, then fan out over it" shape -- which is real amplification and
    common in this tree."""
    if loop is None:
        return False
    gate_names: set[str] = set()
    for node in ast.walk(loop):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            if _reads_operator_input(node.value):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    gate_names |= _names_in(t)
    if not gate_names:
        return False
    for node in ast.walk(loop):
        if not isinstance(node, ast.If) or not (_names_in(node.test) & gate_names):
            continue
        if any(sub is call for stmt in node.body for sub in ast.walk(stmt)):
            return True
    return False


def _is_attribution_search(
    call: ast.Call, loop: ast.AST | None, tainted: frozenset[str]
) -> bool:
    """DISCRIMINATOR 14 -- see the block comment above.

    Requires, inside the enclosing loop and AFTER this call:
      1. an `If` whose body terminates the iteration (`Return`/`Break`), and
      2. whose test references NEITHER a loop-tainted name NOR the name this call's result was
         bound to.

    Clause 2 is what excludes ordinary fail-fast. `if result.returncode: return` reads the
    SPAWN'S OWN result and is a per-item check on a batchable fan-out; `if os.path.exists(marker)`
    reads state the spawn perturbed as a side effect, which only a per-item invocation can
    attribute. Without clause 2 this would silence every early-exit loop in the tree."""
    if loop is None:
        return False

    result_names: set[str] = set()
    for node in ast.walk(loop):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            if any(sub is call for sub in ast.walk(node.value)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    result_names |= _names_in(t)

    #: Every name the loop BINDS -- targets and assignments alike. A test built only from these
    #: is reading the loop's own working state, never out-of-band state the spawn perturbed.
    bound_in_loop: set[str] = set()
    for node in ast.walk(loop):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            bound_in_loop |= _names_in(node.target)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                bound_in_loop |= _names_in(t)
        elif isinstance(node, ast.AnnAssign):
            bound_in_loop |= _names_in(node.target)
        elif isinstance(node, ast.comprehension):
            bound_in_loop |= _names_in(node.target)
        #: MUTATED counts as bound. `covered.add(rel_path)` makes `covered` the loop's own
        #: accumulator, so `if len(covered) == len(candidate_shas): break` is a
        #: work-is-complete check, NOT an observation of state the spawn perturbed. Measured
        #: 2026-08-19: without this, `plan_suggest_completion_steps` matched -- its spawn is
        #: memoized behind a cache and may well deserve to be quiet, but not for THIS reason.
        #: Out-of-band means the loop never touches it.
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                bound_in_loop.add(node.func.value.id)

    excluded = set(tainted) | result_names | bound_in_loop
    for node in ast.walk(loop):
        if not isinstance(node, ast.If) or node.lineno <= call.lineno:
            continue
        test_names = _names_in(node.test)
        if not test_names or (test_names & excluded):
            continue
        #: MEASURED necessity, 2026-08-19: without requiring a name from OUTSIDE the loop, this
        #: matched `plan_suggest_completion_steps._plans_with_review_trail_coverage`, whose spawn
        #: is MEMOIZED behind a cache check. That site may well deserve to be quiet, but not for
        #: this reason -- a right answer reached by a wrong mechanism is the register's failure
        #: wearing a discriminator's clothes.
        if not (test_names - excluded):
            continue
        if node.body and isinstance(node.body[-1], (ast.Return, ast.Break)):
            return True
    return False


# --------------------------------------------------------------------------
# Discriminator 13: retained per-item fallback behind a batched primary. The batch IS the
# primary call; the loop the collector counts fires only when that batch fails, recovering
# per-item attribution instead of collapsing the whole set to one degraded verdict. Deleting the
# fallback to clear the key would trade a degrade-on-failure posture for a metric.
#
# The relationship is entirely local -- no cross-module analysis -- but it wears THREE
# control-flow shapes, and a predicate handling only the obvious one (loop nested in an `except`)
# decides one of the three real sites.
# --------------------------------------------------------------------------


def _derived_names(seed: set[str], fn: ast.AST) -> set[str]:
    """`seed` plus every name assigned from an expression referencing something already in it.

    `outcomes = fut.result()` then `missing = [r for r in refs if r not in outcomes]` -- the
    guard that gates the fallback tests `missing`, not `outcomes`, so a predicate matching only
    the directly-bound name misses it. Same bounded fixed-point idiom as
    `_tainted_names_for_loop`, and bounded for the same reason."""
    out = set(seed)
    for _ in range(10):
        grew = False
        for node in ast.walk(fn):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            if not (_names_in(node.value) & out):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for name in _names_in(t):
                    if name not in out:
                        out.add(name)
                        grew = True
        if not grew:
            break
    return out


def _carries_whole(expr: ast.expr, names: set[str]) -> bool:
    """True when `expr` hands one of `names` over WHOLE -- `*N`, a bare `N`, or `N` on either
    arm of an `Add` chain. This is clause 2: a "batched primary" that does not actually carry the
    collection is not a primary at all, and without this the predicate would accept any earlier
    spawn as cover for an unrelated fan-out."""
    if isinstance(expr, ast.Starred):
        return _carries_whole(expr.value, names)
    if isinstance(expr, ast.Name):
        return expr.id in names
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return _carries_whole(expr.left, names) or _carries_whole(expr.right, names)
    if isinstance(expr, (ast.List, ast.Tuple)):
        return any(_carries_whole(e, names) for e in expr.elts)
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name):
        if expr.func.id in ("list", "tuple", "sorted", "set") and len(expr.args) == 1:
            return _carries_whole(expr.args[0], names)
    return False


def _batched_primary_result_names(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    callee: str | None,
    group_names: set[str],
    spawn_linenos: set[int],
) -> set[str]:
    """Names bound to the result of a BATCHED PRIMARY inside `fn`.

    A primary is either a recognized spawn call, or a call/reference naming the SAME callee the
    per-item loop calls -- the second clause is what reaches `cutover_gate`, whose primary is
    `pool.submit(_run_pytest_batch, root, root_refs)` and whose spawn lives one hop inside that
    callee rather than in this function at all. Either way it must CARRY THE COLLECTION WHOLE."""
    out: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        for call in ast.walk(node.value):
            if not isinstance(call, ast.Call):
                continue
            is_spawn = call.lineno in spawn_linenos and _call_callee_name(call) in _SPAWN_API_NAMES
            names_here = {
                n.id for n in ast.walk(call) if isinstance(n, ast.Name)
            }
            is_same_callee = callee is not None and (
                _call_callee_name(call) == callee or callee in names_here
            )
            if not (is_spawn or is_same_callee):
                continue
            operands = list(call.args) + [kw.value for kw in call.keywords]
            if not any(_carries_whole(a, group_names) for a in operands):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                out |= _names_in(t)
    return out


def _loop_is_gated_on_failure(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    loop: ast.AST,
    result_names: set[str],
) -> bool:
    """Clause 3, the load-bearing one -- the ONLY thing separating a real fallback from "calls a
    batch, ignores the result, fans out anyway". Relaxing it makes this discriminator silently
    over-broad, and it SUPPRESSES, so that is the dangerous direction.

    Three shapes, all decided from straight-line statement structure:
      3a  the loop sits in an `except` handler of a `Try` whose body holds the primary;
      3b  the loop sits inside an `If` whose test references the primary's result;
      3c  the loop FOLLOWS an `If` that returns on success -- dominance by early return rather
          than by nesting, which is `coordinator-safe-commit`'s shape and which a nesting-only
          matcher misses entirely.
    """
    gated = _derived_names(result_names, fn)

    for node in ast.walk(fn):
        #: 3a -- an except handler containing the loop.
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                if any(sub is loop for sub in ast.walk(handler)):
                    return True
        #: 3b -- an `if` on the primary's result (or a name derived from it).
        if isinstance(node, ast.If) and (_names_in(node.test) & gated):
            if any(sub is loop for sub in node.body) or any(
                sub is loop for stmt in node.body for sub in ast.walk(stmt)
            ):
                return True

    #: 3c -- a preceding sibling `if` that RETURNS on the success path, at any statement-bearing
    #: level of the function.
    for parent in ast.walk(fn):
        body = getattr(parent, "body", None)
        if not isinstance(body, list):
            continue
        loop_index = next((i for i, stmt in enumerate(body) if stmt is loop), None)
        if loop_index is None:
            continue
        for stmt in body[:loop_index]:
            if not isinstance(stmt, ast.If) or not (_names_in(stmt.test) & gated):
                continue
            if stmt.body and isinstance(stmt.body[-1], (ast.Return, ast.Raise)):
                return True
    return False


_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _enclosing_loop_of(fn: ast.AST, call: ast.Call) -> ast.AST | None:
    """The innermost loop CONSTRUCT in `fn` whose subtree contains `call` -- a `for`/`async for`
    statement, or a comprehension.

    Comprehensions are not an afterthought here. `publish.py`'s fallback is spelled
    `return {rm: _pairing(rm, ...) for rm in rel_modules}` inside an `except` handler, so a
    statement-only matcher decides its sibling and not it -- the same lesson discriminator 9's
    docstring already records about covering both loop forms."""
    best: ast.AST | None = None
    best_lineno = -1
    for node in ast.walk(fn):
        if not isinstance(node, (ast.For, ast.AsyncFor, *_COMPREHENSIONS)):
            continue
        if any(sub is call for sub in ast.walk(node)) and node.lineno > best_lineno:
            best, best_lineno = node, node.lineno
    return best


def _loop_iterables(loop: ast.AST) -> list[ast.expr]:
    """The expression(s) a loop construct iterates -- `.iter` for a statement, every generator's
    `.iter` for a comprehension."""
    if isinstance(loop, (ast.For, ast.AsyncFor)):
        return [loop.iter]
    if isinstance(loop, _COMPREHENSIONS):
        return [gen.iter for gen in loop.generators]
    return []


def _source_names(seed: set[str], fn: ast.AST) -> set[str]:
    """`seed` plus every name it was DERIVED FROM, following assignments backwards.

    Deliberately NOT a general backward closure over names, and this is the second time this
    module has had to learn the lesson in the same direction (see `_tainted_names_for_loop`'s
    inversion warning). A general walk over `_names_in(value)` was written first and MEASURED
    over-broad on 2026-08-19: from `incoming` in `refresh-plugin-live-install._interactive_gate`
    it reached `_git`, `checkout_ref`, `live_path` and `diff` -- including the callee's own name
    -- after which almost any earlier call "carried the collection" and the fallback test passed
    on a site that is not a fallback at all.

    So: ONE hop, and only through a FILTERING shape -- a comprehension over a bare name, or
    `list`/`tuple`/`sorted`/`set` of one. That is exactly the provenance `cutover_gate` needs
    (`missing = [r for r in root_refs if r not in outcomes]`, the only thing connecting its loop
    to the collection its primary carried) and nothing wider. A derived collection whose
    provenance runs through a method call or a slice declines, because at that point what the
    loop iterates is no longer demonstrably the set the primary was handed."""

    def _one_hop(expr: ast.expr) -> set[str]:
        if isinstance(expr, _COMPREHENSIONS):
            if len(expr.generators) == 1 and isinstance(expr.generators[0].iter, ast.Name):
                return {expr.generators[0].iter.id}
            return set()
        if (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id in ("list", "tuple", "sorted", "set")
            and len(expr.args) == 1
        ):
            return _one_hop(expr.args[0])
        if isinstance(expr, ast.Name):
            return {expr.id}
        return set()

    out = set(seed)
    for _ in range(3):
        grew = False
        for node in ast.walk(fn):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(_names_in(t) & out for t in targets):
                continue
            for name in _one_hop(node.value):
                if name not in out:
                    out.add(name)
                    grew = True
        if not grew:
            break
    return out


def _is_batched_primary_fallback(
    call: ast.Call,
    callee: str | None,
    fn: ast.FunctionDef | ast.AsyncFunctionDef | None,
    spawn_linenos: set[int],
) -> bool:
    """DISCRIMINATOR 13 -- see the block comment above."""
    if fn is None:
        return False
    loop = _enclosing_loop_of(fn, call)
    if loop is None:
        return False
    iterables = _loop_iterables(loop)
    if not iterables:
        return False
    #: Walk BACKWARDS from the iterable: the primary may have carried the collection this loop's
    #: iterable was filtered out of, which is `cutover_gate`'s `missing` <- `root_refs`.
    group_names = _source_names(
        {name for it in iterables for name in _names_in(it)}, fn
    )
    if not group_names:
        return False
    result_names = _batched_primary_result_names(fn, callee, group_names, spawn_linenos)
    if not result_names:
        return False
    return _loop_is_gated_on_failure(fn, loop, result_names)


# --------------------------------------------------------------------------
# Discriminator 12: root-scoped spawn. argv0 is the CONSTANT `git`, and what varies per
# iteration is the worktree the invocation is scoped TO. No git process spans two `-C` roots,
# so one spawn per distinct root is the floor however the loop is arranged -- relocating the
# call only moves the flag. Discriminators 6, 8 and 11 all read the PROGRAM-IDENTITY slot and
# decline here correctly, because the program really is invariant; nothing in this module read a
# SCOPING slot until now, and that gap was filled with hand-written `structural-floor` register
# entries instead (measured 2026-08-19: five of them).
# --------------------------------------------------------------------------

#: Flags whose FOLLOWING argv element names the tree a git invocation is scoped to. Closed set,
#: deliberately: these three are the only ones whose operand is a repository/worktree root, and
#: the discriminator's whole safety argument is that one process cannot serve two of them.
_SCOPING_FLAGS = frozenset({"-C", "--git-dir", "--work-tree"})


def _scope_operand(elts: list[ast.expr]) -> ast.expr | None:
    """The argv element a scoping flag scopes to -- `elts[i + 1]` for the first `elts[i]` that is
    a literal in `_SCOPING_FLAGS`. `None` when no scoping flag is present or it is last.

    Requires `elts[0]` to be the literal `"git"`. Without that the flag semantics are unknowable:
    `-C` means "change directory" to git and something else entirely to another program, and a
    suppressor that guessed would silence a real fan-out on a coincidence of spelling."""
    if not elts:
        return None
    head = elts[0]
    if not (isinstance(head, ast.Constant) and head.value == "git"):
        return None
    for i, elt in enumerate(elts[:-1]):
        if isinstance(elt, ast.Constant) and elt.value in _SCOPING_FLAGS:
            return elts[i + 1]
    return None


def _only_tainted_in(scope_expr: ast.expr, other_exprs: list[ast.expr], tainted: frozenset[str]) -> bool:
    """The precision constraint, and the reason this discriminator is not over-broad.

    Suppression is only sound when the loop target reaches the SCOPE and nothing else. Consider
    `for path in paths: _git(["status", path], root=str(path.parent))` -- the root varies, but so
    does the pathspec, and THAT dimension is genuinely batchable within one root. Requiring the
    tainted name to appear nowhere but the scope slot is what separates "N roots, N spawns" from
    "N items that happen to carry a root with them".

    All five real sites this was measured against satisfy it: their non-scope argv is entirely
    literals or an already-spliced per-root batch."""
    if not (_names_in(scope_expr) & tainted):
        return False
    for expr in other_exprs:
        if _names_in(expr) & tainted:
            return False
    return True


def _root_scoped_direct(
    call: ast.Call, tainted: frozenset[str], bindings: dict[str, ast.expr] | None
) -> bool:
    """Discriminator 12, LEG A -- route a, where the scope is visible in this call's own argv
    (`["git", "-C", root, ...]`) or in its `cwd=` keyword."""
    if not tainted or not call.args:
        return False

    elts = _argv_list_elts(call.args[0], bindings)
    cwd = next((kw.value for kw in call.keywords if kw.arg == "cwd"), None)

    if elts:
        scope = _scope_operand(elts)
        if scope is not None:
            others = [e for e in elts if e is not scope]
            if cwd is not None:
                others.append(cwd)
            if _only_tainted_in(scope, others, tainted):
                return True

    #: `cwd=` carries the same fact without a flag -- `subprocess.run(["git", *args], cwd=root)`.
    #: The argv0 check still applies, so a non-git program's cwd never reaches here.
    if cwd is not None and elts:
        head = elts[0]
        if isinstance(head, ast.Constant) and head.value == "git":
            if _only_tainted_in(cwd, list(elts), tainted):
                return True
    return False


def _helper_spliced_params(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, spawn_linenos: set[int]
) -> set[str]:
    """Parameters of `fn` that reach a spawn's argv as a `Starred` SPLICE -- the batch dimension.

    Exists for the precision constraint's one legitimate exception. A root-scoped helper is
    typically called as `_batch(root, entries_for_that_root)`, where BOTH arguments co-vary with
    the loop: the root is the scope, and `entries` is the per-root group already carried whole in
    one spawn. Refusing to suppress because a second tainted argument exists would refuse exactly
    the sites that did their batching correctly -- and a `Starred` splice is the same proof
    discriminator 7 reads to decide that one call carries a whole group."""
    params = set(_func_params(fn))
    if not params:
        return set()
    bindings = _loop_argv0_bindings(fn)
    local = _loop_expr_bindings(fn)
    out: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or node.lineno not in spawn_linenos:
            continue
        if _call_callee_name(node) not in _SPAWN_API_NAMES or not node.args:
            continue
        for elt in _argv_list_elts(node.args[0], {**bindings, **local}) or []:
            if isinstance(elt, ast.Starred):
                out |= _names_in_through_assignments(elt.value, fn) & params
    return out


def _helper_spawn_scope_params(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, spawn_linenos: set[int]
) -> set[str]:
    """Parameters of `fn` that fill the SCOPE slot of a spawn `fn` performs itself.

    Literally `_helper_spawn_argv0_params`' test moved one slot to the right: same resolution,
    same reference-not-bare-`Name` matching (the real helpers spell it `["git", "-C", str(root)]`
    or `cwd=str(cwd)`), same refusal to accept an identity built from a module constant. What
    differs is only WHICH slot is read, and therefore what the suppression claims -- not "the
    caller chooses the program" but "the caller chooses the tree"."""
    params = set(_func_params(fn))
    if not params:
        return set()
    bindings = _loop_argv0_bindings(fn)
    local = _loop_expr_bindings(fn)
    out: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or node.lineno not in spawn_linenos:
            continue
        if _call_callee_name(node) not in _SPAWN_API_NAMES or not node.args:
            continue
        elts = _argv_list_elts(node.args[0], {**bindings, **local})
        scope: ast.expr | None = None
        if elts:
            scope = _scope_operand(elts)
            if scope is None:
                head = elts[0]
                if isinstance(head, ast.Constant) and head.value == "git":
                    scope = next((kw.value for kw in node.keywords if kw.arg == "cwd"), None)
        if scope is None:
            continue
        #: The helper's OWN non-scope argv must not carry a parameter either, or a batchable
        #: pathspec dimension could ride in behind the root. `*rel_modules` is exempt from this
        #: by construction: a `Starred` splice IS the batch, which is what discriminator 7 reads.
        others = [e for e in (elts or []) if e is not scope and not isinstance(e, ast.Starred)]
        other_params = set()
        for expr in others:
            other_params |= _names_in_through_assignments(expr, fn) & params
        scope_params = _names_in_through_assignments(scope, fn) & params
        out |= scope_params - other_params
    return out


def _own_param_runner_invocation(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[str, ast.Call] | None:
    """The first call in `fn`'s body whose CALLEE is one of `fn`'s OWN parameters and whose name
    is runner-shaped -- `fn` invoking an injected runner directly, distinct from route d's own
    `_find_injected_runner_name`, which looks for a runner-shaped name PASSED as an ARGUMENT at
    some other call. `worktree_is_dirty`'s `run_git([...], Path(worktree_path))` is the measured
    shape: the parameter IS the callee. First match only, no chaining -- one hop, matching every
    other route's discipline in this module."""
    params = set(_func_params(fn))
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = node.func.id
        if name not in params:
            continue
        if name in _RUNNER_KWARG_NAMES or name.lower().startswith(_RUNNER_NAME_PREFIXES):
            return name, node
    return None


def _one_hop_or_default_fallback(name: str, enclosing_fn: ast.AST | None) -> ast.expr | None:
    """`Y` in a top-level `<name> = <name> or Y` statement in `enclosing_fn`'s own body -- the
    "parameter or production default" seam this repo spells that way
    (`consolidate_assemble.brief`'s `run_git = run_git or default_run_git`), distinct from route
    f's bare-`Name` SIGNATURE default because the fallback lives in a body statement, not the
    `def` line.

    Restricted to `enclosing_fn.body`'s own TOP-LEVEL statements -- not `ast.walk`, and
    deliberately so: a same-shaped reassignment sitting inside the per-item loop would be a
    different, per-iteration rebinding this one-hop resolution must not reach past."""
    if enclosing_fn is None or not hasattr(enclosing_fn, "body"):
        return None
    for stmt in enclosing_fn.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not (isinstance(target, ast.Name) and target.id == name):
            continue
        value = stmt.value
        if not (isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or)):
            continue
        others = [v for v in value.values if not (isinstance(v, ast.Name) and v.id == name)]
        if len(others) == 1 and isinstance(others[0], ast.Name):
            return others[0]
    return None


def _resolve_named_git_scope_param(
    name: str,
    index: _FuncIndex,
    relpath: str,
    spawn_linenos_by_file: dict[str, set[int]],
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, set[str]] | None:
    """Resolves a bare function NAME (route d/g's own by-name discipline: same-module preferred,
    else any indexed definition) and confirms it is a real git-prepending spawner by RE-DERIVING
    its own scope params (`_helper_spawn_scope_params`, unmodified) -- never assumed from the
    name alone. `None` when nothing resolves or nothing actually scopes on a literal `git`
    argv0."""
    candidates = [k for k in index.funcs_by_name.get(name, []) if k[0] == relpath] or list(
        index.funcs_by_name.get(name, [])
    )
    for tgt_relpath, tgt_name in candidates:
        fn = index.func_defs.get((tgt_relpath, tgt_name))
        if fn is None:
            continue
        scope_params = _helper_spawn_scope_params(fn, spawn_linenos_by_file.get(tgt_relpath, set()))
        if scope_params:
            return fn, scope_params
    return None


def _root_scoped_through_injected_runner(
    call: ast.Call,
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    index: _FuncIndex,
    relpath: str,
    tainted: frozenset[str],
    spawn_linenos_by_file: dict[str, set[int]],
    enclosing_fn: ast.AST | None,
) -> bool:
    """Discriminator 12, LEG B's ROUTE-D EXTENSION -- the resolved callee `fn` does not itself
    directly spawn (`_helper_spawn_scope_params(fn, ...)` found nothing), because the spawn
    lives inside a runner `fn` receives as a PARAMETER and calls directly
    (`consolidate_assemble.worktree_is_dirty`'s `run_git(["--no-optional-locks", "status",
    "--porcelain"], Path(worktree_path))`). The `elts[0] == "git"` guard that leg B's normal
    path relies on cannot fire here because `fn`'s own argv never spells `git` -- the runner
    prepends it -- so this leg proves the same fact a different way: resolve WHAT the runner
    actually is, and read ITS confirmed scope param instead of guessing from `fn`'s argv.

    Three hops, none skippable:
      1. `_own_param_runner_invocation(fn)` -- `fn` calls one of its OWN parameters directly,
         under the same runner-name heuristic route d already trusts.
      2. `_arg_expr_for_param(call, fn, param)` -- what THIS call site supplies for that
         parameter (`run_git`, a bare `Name` in `brief`'s own scope) -- then
         `_one_hop_or_default_fallback` follows the ONE-HOP `run_git = run_git or
         default_run_git` seam that binds it, in the CALL's own enclosing function (`brief`),
         never the resolved callee's.
      3. `_resolve_named_git_scope_param` resolves that fallback NAME to a real definition
         (`default_run_git`) and RE-DERIVES its scope param the normal way -- so nothing here
         is trusted on the strength of a name alone; a same-named function that does NOT scope
         on a literal `git` argv0 fails this leg identically to leg B's original path.

    Only once all three confirm does this read the inner call's OWN second positional argument
    (`Path(worktree_path)`) as the scope -- the `RunGit` calling convention's own shape
    (`Callable[[list[str], Path], ...]`), not an argv flag -- and apply the SAME precision
    constraint every other leg of discriminator 12 applies: the tainted name reaching that
    scope slot must reach nothing else this call passes."""
    if not tainted:
        return False
    found = _own_param_runner_invocation(fn)
    if found is None:
        return False
    param, inner_call = found
    if len(inner_call.args) < 2 or any(isinstance(a, ast.Starred) for a in inner_call.args[:2]):
        return False
    runner_arg = _arg_expr_for_param(call, fn, param)
    if not isinstance(runner_arg, ast.Name):
        return False
    fallback = _one_hop_or_default_fallback(runner_arg.id, enclosing_fn)
    if fallback is None:
        return False
    resolved = _resolve_named_git_scope_param(fallback.id, index, relpath, spawn_linenos_by_file)
    if resolved is None:
        return False
    resolved_fn, resolved_scope_params = resolved
    #: The inner call's own 2nd-positional-argument PARAMETER, at the RESOLVED runner's own
    #: definition, must be one of the params that definition's re-derived scope check actually
    #: confirmed -- not merely "some param scopes somewhere". Ties the position this call fills
    #: to the position the resolved runner proved is its scope slot.
    resolved_positional = _func_positional_params(resolved_fn)
    if len(resolved_positional) < 2 or resolved_positional[1] not in resolved_scope_params:
        return False

    #: The inner call's own scope argument (`Path(worktree_path)`), traced back to which of
    #: `fn`'s OWN parameters it references, then to what THIS call supplies for that parameter
    #: -- `worktree_path` -> `wt_path`. Declines (empty set, or a param `_arg_expr_for_param`
    #: cannot resolve) rather than guessing.
    inner_scope_param = next(
        iter(_names_in_through_assignments(inner_call.args[1], fn) & set(_func_params(fn))), None
    )
    scope_at_call = (
        _arg_expr_for_param(call, fn, inner_scope_param) if inner_scope_param else None
    )
    if scope_at_call is None or not (_names_in(scope_at_call) & tainted):
        return False
    others = [
        arg for arg in call.args if arg is not scope_at_call and not isinstance(arg, ast.Starred)
    ] + [kw.value for kw in call.keywords if kw.value is not scope_at_call]
    return _only_tainted_in(scope_at_call, others, tainted)


def _root_scoped_through_helper(
    call: ast.Call,
    callee: str,
    index: _FuncIndex,
    relpath: str,
    tainted: frozenset[str],
    spawn_linenos_by_file: dict[str, set[int]],
    enclosing_fn: ast.AST | None = None,
) -> bool:
    """Discriminator 12, LEG B -- routes b/c, the same fact reached through one helper hop.
    Mirrors `_argv0_varies_through_helper` step for step, reading the scope slot instead of the
    program slot, and under the same one-hop, no-fallback discipline.

    `enclosing_fn` is optional and used only by `_root_scoped_through_injected_runner`, this
    leg's own route-d extension -- see that function's docstring for what it resolves and why
    the ordinary path above cannot see it."""
    if not tainted:
        return False
    for tgt_relpath, tgt_name in _resolve_callee_def_wide(index, relpath, callee):
        fn = index.func_defs.get((tgt_relpath, tgt_name))
        if fn is None:
            continue
        spawn_linenos = spawn_linenos_by_file.get(tgt_relpath, set())
        scope_params = _helper_spawn_scope_params(fn, spawn_linenos)
        #: Arguments filling a SPLICED parameter are the batch dimension, already carried whole
        #: in one spawn -- they may co-vary with the loop without defeating the constraint.
        #: Restricted to a BARE NAME, and the restriction is load-bearing (measured 2026-08-19).
        #: That the helper splices a parameter says only what the helper does with the list; it
        #: says nothing about whether the CALLER handed it a group or a per-item argv. The real
        #: refutation: `_run_git(['log', '--format=...', '%s..HEAD' % target], cwd=git_cwd)` in
        #: `dispatch_checks.check_destructive_git_orphan` splices `args` too, but its list is
        #: built per iteration around a loop-variant `target` -- a genuinely batchable site, and
        #: the in-source comment there says so in as many words. A bare `Name` is the shape that
        #: carries a collection whole (`rel_modules`); a `List` literal is where per-item argv
        #: hides, so it stays subject to the constraint.
        batch_exprs = {
            id(expr)
            for param in _helper_spliced_params(fn, spawn_linenos)
            if isinstance((expr := _arg_expr_for_param(call, fn, param)), (ast.Name, ast.Attribute))
        }
        for param in scope_params:
            supplied = _arg_expr_for_param(call, fn, param)
            if supplied is None or not (_names_in(supplied) & tainted):
                continue
            #: Same precision constraint as leg A, applied at the CALL SITE: the tainted name
            #: that fills the scope slot must fill nothing else this call passes -- except the
            #: batch dimension above.
            others = [
                arg
                for arg in call.args
                if arg is not supplied
                and not isinstance(arg, ast.Starred)
                and id(arg) not in batch_exprs
            ] + [
                kw.value
                for kw in call.keywords
                if kw.value is not supplied and id(kw.value) not in batch_exprs
            ]
            if _only_tainted_in(supplied, others, tainted):
                return True
        if not scope_params and _root_scoped_through_injected_runner(
            call, fn, index, relpath, tainted, spawn_linenos_by_file, enclosing_fn
        ):
            return True
    return False


def _resolve_callee_def(
    index: _FuncIndex, relpath: str, callee: str
) -> list[tuple[str, str]]:
    """Callee resolution for route g: same-module first, else a name imported into this file --
    the same discipline routes b/c/f already use, narrowed the same way route c is by
    `_import_resolves_to` (the ORIGINAL imported name and its resolved SOURCE MODULE, not
    merely the local binding). Kept local rather than shared with those routes because route g
    is the only one that needs the resolved function's own NODE (to read its parameter list and
    walk its body), not merely a yes/no "does it spawn"."""
    if (relpath, callee) in index.func_defs:
        return [(relpath, callee)]
    if callee in index.imported_names_by_file.get(relpath, set()):
        return _resolve_imported_defs(index, relpath, callee)
    return []


def _resolve_callee_def_wide(
    index: _FuncIndex, relpath: str, callee: str
) -> list[tuple[str, str]]:
    """Callee resolution for the two SUPPRESSOR legs only (Disc 8
    `_argv0_varies_through_helper` and Disc 12 leg B `_root_scoped_through_helper`) --
    deliberately WIDE, unlike `_resolve_callee_def`'s narrow (`_import_resolves_to`-filtered)
    resolution used by the positive routes (route g's fixed point and route c's
    `_is_direct_spawner_name`). All same-name candidates imported into this file are returned,
    with no `_import_resolves_to` pruning by resolved source module.

    This asymmetry is deliberate, not an oversight: a SUPPRESSOR that resolves narrowly can miss
    the helper definition that actually backs a call (wrong-module false negative in the
    resolution step) and, missing it, decline to suppress -- which SURFACES a site rather than
    hiding one. A suppressor over-approximating its resolution is safe in the direction this
    module's docstring already requires (decline is safe, suppress-when-unsure is not); a
    suppressor under-approximating it is not. The positive routes have the opposite risk
    profile -- a wide resolution there would let an unrelated same-named import manufacture a
    false suppression of a real site -- so they keep the narrow, `_import_resolves_to`-filtered
    form.

    Cites AC4b and the measurement that settled this: narrowing this path (as a prior pass to
    this module did by routing the suppressor legs through the narrow `_resolve_callee_def`)
    took the reported baseline from 25 to 26 sites, surfacing exactly one false positive --
    `coordinator_core/ops/cascade_baton_rows.py :: _first_deliverable_commit_range_base`, whose
    enclosing loop is a search whose branches all return, so `_run_git` runs at most once. That
    surfaced site is the canary a later 'consistency' cleanup must not silently reintroduce by
    collapsing this function back into the narrow one."""
    if (relpath, callee) in index.func_defs:
        return [(relpath, callee)]
    if callee in index.imported_names_by_file.get(relpath, set()):
        return [k for k in index.funcs_by_name.get(callee, []) if k[0] != relpath]
    return []


def _is_direct_spawner_name(index: _FuncIndex, relpath: str, ident: str) -> bool:
    """True when the bare identifier `ident`, referenced in `relpath`, names a direct spawner
    -- same-module (route b's resolution) or imported (route c's, narrowed by
    `_import_resolves_to` the same way `_resolve_callee_def` is). Route g's leg-2 seed."""
    if (relpath, ident) in index.same_module_direct_spawn:
        return True
    if ident not in index.imported_names_by_file.get(relpath, set()):
        return False
    return any(
        any(spawner_relpath == def_relpath for spawner_relpath, _ in index.direct_spawn_funcs.get(def_name, []))
        for def_relpath, def_name in _resolve_imported_defs(index, relpath, ident)
    )


def _compute_spawn_bearing_params(index: _FuncIndex) -> frozenset[tuple[str, str, str]]:
    """Route g's bidirectional fixed point over `(relpath, func_name, param_name)` triples.
    See module docstring's route-g section for the algorithm in prose; termination is argued
    in `_build_func_index`'s docstring, which is where this is called from.

    LEG 1 -- invoked: the parameter is called directly in its own function's body, plus the
    forwarding closure (forwarding a parameter into another function's invoked parameter makes
    it invoked too).

    LEG 2 -- tainted: a direct spawner is passed into the parameter at some REAL call site
    (never a parameter default -- that is route f's job, not this fixed point's), plus the same
    forwarding closure.

    SPAWN-BEARING = LEG 1 intersect LEG 2. Requiring both is what keeps precision -- leg 2
    alone would flag every dependency-injection seam regardless of whether the loop body ever
    calls it."""
    invoked: set[tuple[str, str, str]] = set()
    for (rp, name), fn in index.func_defs.items():
        params = set(_func_params(fn))
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in params
            ):
                invoked.add((rp, name, node.func.id))

    changed = True
    while changed:
        changed = False
        for (rp, name), fn in index.func_defs.items():
            params = set(_func_params(fn))
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                callee = _call_callee_name(node)
                if callee is None:
                    continue
                for tgt in _resolve_callee_def(index, rp, callee):
                    tgt_fn = index.func_defs[tgt]
                    for p in params:
                        for slot in _forwarded_arg_slots(node, tgt_fn, p):
                            if (tgt[0], tgt[1], slot) in invoked and (rp, name, p) not in invoked:
                                invoked.add((rp, name, p))
                                changed = True

    tainted: set[tuple[str, str, str]] = set()
    for (rp, name), fn in index.func_defs.items():
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            callee = _call_callee_name(node)
            if callee is None:
                continue
            for tgt in _resolve_callee_def(index, rp, callee):
                tgt_fn = index.func_defs[tgt]
                for arg in (*node.args, *[kw.value for kw in node.keywords]):
                    if isinstance(arg, ast.Name) and _is_direct_spawner_name(index, rp, arg.id):
                        for slot in _forwarded_arg_slots(node, tgt_fn, arg.id):
                            tainted.add((tgt[0], tgt[1], slot))

    changed = True
    while changed:
        changed = False
        for (rp, name), fn in index.func_defs.items():
            params = {p for p in _func_params(fn) if (rp, name, p) in tainted}
            if not params:
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                callee = _call_callee_name(node)
                if callee is None:
                    continue
                for tgt in _resolve_callee_def(index, rp, callee):
                    tgt_fn = index.func_defs[tgt]
                    for p in params:
                        for slot in _forwarded_arg_slots(node, tgt_fn, p):
                            if (tgt[0], tgt[1], slot) not in tainted:
                                tainted.add((tgt[0], tgt[1], slot))
                                changed = True

    return frozenset(invoked & tainted)


# --------------------------------------------------------------------------
# Call-site argv-shape helpers (route e's "read git-ness at the call site")
# --------------------------------------------------------------------------


def _call_arg_is_argv_shaped(call: ast.Call, param_index: int) -> bool:
    """True if the argument `call` passes at `param_index` looks like an argv the runner will
    spawn: a non-empty list/tuple literal, or a non-empty string/f-string command line.

    Reads the ARGV SHAPE, never the program name. `[sys.executable, "-m", "pytest", *refs]`
    -- the most expensive per-item spawn in the ops census -- fronts its argv with an
    `ast.Attribute`, which `spawn_policy._resolve_argv0` reports as `<dynamic>`; any check
    keyed on a resolvable program name is blind to it by construction. That blindness is the
    concrete half of the git-argv-only blind spot AC11 closes.

    Deliberately conservative (false-negative preferred): a bare `Name`, a call, or anything
    else not statically shaped as an argv is treated as NOT argv-shaped."""
    if param_index >= len(call.args):
        return False
    arg = call.args[param_index]
    if isinstance(arg, (ast.List, ast.Tuple)):
        return bool(arg.elts)
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return bool(arg.value.strip())
    if isinstance(arg, ast.JoinedStr) and arg.values:
        first = arg.values[0]
        return (
            isinstance(first, ast.Constant)
            and isinstance(first.value, str)
            and bool(first.value.strip())
        )
    return False


# --------------------------------------------------------------------------
# Loop-context visitor: qualifying loops only (discriminators 1-3 applied)
# --------------------------------------------------------------------------


def _is_discarded_target(target: ast.expr, body: list[ast.AST]) -> bool:
    """True when `target` binds nothing the loop body ever reads back.

    Canonically `_`, but decided by USE rather than by name: a target named `i` that no
    statement references is just as discarded, and a target named `_` that the body somehow
    reads is not. Any non-`Name` target (tuple unpack, subscript, attribute) declines --
    unpacking a per-item structure is the opposite of discarding it."""
    if not isinstance(target, ast.Name):
        return False
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name) and node.id == target.id:
                return False
    return True


def _is_count_bounded_range(iterable: ast.expr) -> bool:
    """True for `range(...)` whose every argument is a bare `Name` or a `Constant`.

    The argument restriction is the whole safety of discriminator 9. `range(n)` is a repeat
    count; `range(len(paths))` scales with input size and is exactly the amplification this gate
    exists to catch, so ANY `Call` in the argument position declines -- `len()` first among
    them. A `Name` is accepted because a repeat count reaching the loop as a parameter or module
    constant (`warmup`, `_SAMPLES`, `_RETRIES`) is the shape every real site here uses; the
    residual risk is a local pre-bound to `len(...)`, which is bounded by the discarded-target
    requirement above -- with nothing per-item in scope, all N spawns carry IDENTICAL argv, so
    the loop is a repetition rather than a fan-out over items whatever the count came from."""
    if not isinstance(iterable, ast.Call):
        return False
    if not isinstance(iterable.func, ast.Name) or iterable.func.id != "range":
        return False
    if iterable.keywords or not iterable.args:
        return False
    return all(isinstance(a, (ast.Name, ast.Constant)) for a in iterable.args)


def _names_in_call_args(call: ast.Call) -> set[str]:
    """Every `Name` referenced anywhere in `call`'s arguments -- positional, starred, and
    keyword -- but NOT in the callee expression itself.

    Discriminator 10 asks whether the loop's target reaches what is SPAWNED. The callee is
    excluded deliberately: `runners[kind](argv)` varies the runner with the loop, which is
    discriminator 6/8's question, not this one."""
    out: set[str] = set()
    for arg in call.args:
        out |= _names_in(arg)
    for kw in call.keywords:
        out |= _names_in(kw.value)
    return out


def _is_retry_bounded_range(iterable: ast.expr) -> bool:
    """True for `range(...)` whose arguments carry an ATTEMPT COUNT rather than a collection
    size. Looser than `_is_count_bounded_range` (discriminator 9's, which takes only a bare
    `Name`/`Constant`) because the real retry sites in this tree spell their bound as
    `range(max(1, _ATTEMPTS))` and `range(1, _MAX_ATTEMPTS + 1)` -- arithmetic and a clamp, not
    a different KIND of quantity.

    The line that matters is unchanged: `len(...)` anywhere in the arguments declines, and so
    does any call that is not `max`/`min`. A bound derived from a collection's size is a fan-out
    however it is spelled, and this discriminator must never reach one."""
    if not isinstance(iterable, ast.Call):
        return False
    if not isinstance(iterable.func, ast.Name) or iterable.func.id != "range":
        return False
    if iterable.keywords or not iterable.args:
        return False
    for arg in iterable.args:
        for node in ast.walk(arg):
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name) or node.func.id not in {"max", "min"}:
                    return False
    return True


def _loop_has_own_early_exit(loop: ast.For | ast.AsyncFor) -> bool:
    """True when `loop`'s body can leave the loop early -- a `break` that belongs to THIS loop,
    or any `return`. Nested loops and nested function bodies are not descended into: a `break`
    inside an inner `for` binds to that inner loop and says nothing about this one."""
    stack: list[ast.AST] = list(loop.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, (ast.Break, ast.Return)):
            return True
        stack.extend(ast.iter_child_nodes(node))
    return False


def _is_retry_loop(loop: ast.For | ast.AsyncFor) -> bool:
    """DISCRIMINATOR 10 -- a bounded RETRY of one operation, not one spawn per item.

    Discriminator 3 excludes `while` loops wholesale on exactly this reasoning: every measured
    `while` false positive was a retry, an interactive prompt, or a calendar walk -- bounded by
    a constant, a human, or a fixed window, never by input size. That a retry spelled
    `for attempt in range(_MAX_ATTEMPTS)` was still flagged, while the same retry spelled
    `while attempts < _MAX_ATTEMPTS` was not, is an accident of SPELLING, and the three
    MISCLASSIFIED `retry-loop` rows in `_KNOWN_SITES` were parked waiting for this matcher.

    Two conditions, both required, and the second is the load-bearing one:

      - the bound is an attempt count, not a collection size (`_is_retry_bounded_range`); and
      - the loop can exit early (`_loop_has_own_early_exit`) -- a retry stops when it succeeds.
        A loop that runs all N iterations unconditionally is not retrying anything.

    The caller supplies the third and sharpest condition, at the call rather than the loop: the
    loop's tainted names must not appear in the spawning call's OWN arguments. That is what
    separates a retry from a fan-out, and it is checked in
    `find_unbatched_per_item_spawns` because it is a property of the call, not the loop.
    MEASURED 2026-08-19: all three real sites name their target `attempt` and read it only for
    backoff and logging -- never into argv -- so every iteration issues an IDENTICAL spawn, and
    there is nothing for a batched call to carry.

    SUPPRESSES. The trap this must refuse is `for _ in range(3): run([..., item])` inside an
    outer per-item loop, where the argv does vary -- caught by the tainted-names condition, not
    by anything here."""
    return _is_retry_bounded_range(loop.iter) and _loop_has_own_early_exit(loop)


def _comp_elt_expr(
    node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
) -> ast.AST:
    """A comprehension's element expression as ONE node, for the discarded-target check --
    a `DictComp` has `key`/`value` rather than `elt`, and reading only one of the two would let
    a target referenced in the other half pass as discarded. Wrapped in a `Tuple` so both halves
    are walked. Mirrors `_QualifyingLoopVisitor._visit_comp_elt`'s own split."""
    if isinstance(node, ast.DictComp):
        return ast.Tuple(elts=[node.key, node.value], ctx=ast.Load())
    return node.elt


def _is_repetition_loop(target: ast.expr, iterable: ast.expr, body: list[ast.AST]) -> bool:
    """DISCRIMINATOR 9 -- N repetitions of ONE operation, not one spawn per item.

    `for _ in range(n)` with the target discarded is not a fan-out: nothing per-item is in
    scope, so every iteration's argv is IDENTICAL. There is no set for a batched call to carry
    and nothing for the sibling gate's remedy (hoist to one call outside the loop) to hoist --
    hoisting would delete N-1 repetitions, which is a change of MEANING, not a batching.

    Two real populations in this repo wear this shape, and the register currently pays for both
    in prose:

      - SAMPLING (`_EXEMPT_SITES` class `measurement-is-the-loop`): `samples = [time_invocation(
        op, params) for _ in range(n)]`, reduced to a min / mean / coefficient of variation.
        Collapsing N draws into one does not measure the same quantity faster; it measures a
        different quantity. MEASURED 2026-08-19: these keys each carry TWO call sites, a
        `for _ in range(warmup)` statement AND a comprehension, which is why this discriminator
        is applied at both loop forms rather than only at `ast.For`.
    NOT the MISCLASSIFIED `retry-loop` group, though the first draft of this docstring claimed
    it was. MEASURED: all three of those rows (`cross-repo-memo._verify_delivery_landed`,
    `detached_render_commit.commit_own_artifact`, `_common._update_index_with_retry`) name their
    target `attempt` and READ IT BACK for backoff and logging, so `_is_discarded_target`
    declines and none went quiet. Reaching them needs the retry shape decided on its own terms
    -- a count-bounded loop that `break`s or returns on success, which is discriminator 3's
    `while` reasoning ported to `range` -- and that is a DIFFERENT discriminator, not a widening
    of this one. Stretching 9 to cover a neighbouring class would be the register's own failure
    ("satisfying a rationale is not membership") committed in code instead of prose.

    SUPPRESSES, so it inherits the inversion this module's docstring warns about. The guard that
    keeps it honest is `_is_count_bounded_range`: a count derived from a collection's size is a
    fan-out wearing a repeat's clothes, and declines."""
    return _is_count_bounded_range(iterable) and _is_discarded_target(target, body)


class _QualifyingLoopVisitor(ast.NodeVisitor):
    """Marks every `ast.Call` node that sits directly inside a qualifying loop's body -- a
    `for`/`async for`/comprehension whose iterable is NOT a constant-literal sequence
    (discriminator 2). `while` loops never qualify (discriminator 3) and are descended into
    without pushing loop context. A loop's own `iter` expression is visited OUTSIDE any loop
    context it introduces (discriminator 1). Function/lambda/class boundaries reset loop
    context, matching `test_no_spawn_per_item_loop`'s own nearest-enclosing-loop rule."""

    def __init__(self, literal_names: set[str]) -> None:
        self._literal_names = literal_names
        self._in_qualifying_loop_depth = 0
        self.marked_calls: set[tuple[int, int]] = set()
        #: (lineno, col_offset) -> taint set of the NEAREST enclosing qualifying loop, for
        #: discriminator 6 (varying-argv0). Populated alongside `marked_calls`, never for a key
        #: absent from it. See `_tainted_names_for_loop`.
        self.call_loop_taint: dict[tuple[int, int], frozenset[str]] = {}
        #: (lineno, col_offset) -> the same loop's one-hop argv0 bindings (`_loop_argv0_
        #: bindings`), for the intermediate-variable idiom `_argv0_expr` resolves through.
        self.call_argv0_bindings: dict[tuple[int, int], dict[str, ast.expr]] = {}
        #: (lineno, col_offset) -> the names the NEAREST enclosing qualifying loop's target
        #: itself binds, for discriminator 7 (argv-splicing loop target). Deliberately the raw
        #: target names, NOT `call_loop_taint`'s one-hop growth: this discriminator suppresses,
        #: so it takes the narrower of the two sets. See `_argv_splices_loop_target`.
        self.call_loop_targets: dict[tuple[int, int], set[str]] = {}
        #: (lineno, col_offset) -> True when the NEAREST enclosing qualifying loop is a bounded
        #: retry (`_is_retry_loop`), for discriminator 10. Only half the test: the collector
        #: pairs this with "none of the loop's tainted names reach this call's own arguments",
        #: which is what separates a retry from a fan-out and is not knowable here.
        self.call_loop_is_retry: dict[tuple[int, int], bool] = {}
        #: (lineno, col_offset) -> the loop's argv-shaped assignment bindings
        #: (`_loop_expr_bindings`), for discriminator 7's one-hop resolution of a call that
        #: passes a local (`cmd = base + batch; run(cmd)`) rather than the splice itself.
        self.call_expr_bindings: dict[tuple[int, int], dict[str, ast.expr]] = {}
        #: (lineno, col_offset) -> the NEAREST enclosing qualifying loop NODE, for discriminator
        #: 7's accumulation leg (`_argv_accumulates_loop_target`), which reads the loop's own
        #: nested structure rather than a name map derived from it. Recorded rather than
        #: re-derived: the collector has no other handle on which loop marked a call.
        self.call_loop_node: dict[tuple[int, int], ast.AST] = {}
        self._loop_expr_bindings_stack: list[dict[str, ast.expr]] = []
        self._loop_node_stack: list[ast.AST] = []
        self._loop_retry_stack: list[bool] = []
        self._loop_taint_stack: list[frozenset[str]] = []
        self._loop_argv0_bindings_stack: list[dict[str, ast.expr]] = []
        self._loop_target_stack: list[set[str]] = []

    def _scope_boundary(self, node: ast.AST) -> None:
        saved = self._in_qualifying_loop_depth
        self._in_qualifying_loop_depth = 0
        self.generic_visit(node)
        self._in_qualifying_loop_depth = saved

    def _function_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """A function boundary resets loop context AND admits that function's own author-time
        literal sequences (`_function_local_literal_names`). Pushed and popped exactly where the
        loop depth is, so a name qualifying inside one function never leaks into a sibling."""
        saved_literals = self._literal_names
        self._literal_names = saved_literals | _function_local_literal_names(node)
        self._scope_boundary(node)
        self._literal_names = saved_literals

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function_scope(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._scope_boundary(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope_boundary(node)

    def _visit_for(self, node: ast.For | ast.AsyncFor) -> None:
        # Discriminator 1: iter is evaluated outside any loop context this loop introduces.
        self.visit(node.iter)
        if (
            _is_constant_literal_iterable(node.iter, self._literal_names)
            or _is_chunking_stride_iterable(node.iter)
            # Discriminator 9 (repetition loop): target discarded over a count-bounded
            # `range` -- N identical spawns, not one per item. See `_is_repetition_loop`.
            or _is_repetition_loop(node.target, node.iter, list(node.body))
        ):
            # Discriminators 2, 4 (chunking-stride) and 9: excluded wholesale -- body still
            # visited (a nested qualifying loop inside it may exist), but WITHOUT this loop's
            # own context pushed.
            for stmt in node.body:
                self.visit(stmt)
            return
        self._in_qualifying_loop_depth += 1
        # Discriminator 6 (varying-argv0): seed from this loop's own target, grow by one
        # assignment hop over its body. Pushed/popped in lockstep with loop depth so a call
        # marked while this loop is active resolves against its taint, never an outer or
        # unrelated loop's.
        self._loop_taint_stack.append(
            _tainted_names_for_loop(node, _loop_target_names(node.target))
        )
        self._loop_argv0_bindings_stack.append(_loop_argv0_bindings(node))
        self._loop_target_stack.append(_loop_target_names(node.target))
        # Discriminator 10 (retry loop): the LOOP half of the test, recorded per call so the
        # collector can pair it with the call-level half. See `_is_retry_loop`.
        self._loop_retry_stack.append(_is_retry_loop(node))
        self._loop_expr_bindings_stack.append(_loop_expr_bindings(node))
        self._loop_node_stack.append(node)
        for stmt in node.body:
            self.visit(stmt)
        self._loop_node_stack.pop()
        self._loop_expr_bindings_stack.pop()
        self._loop_retry_stack.pop()
        self._loop_target_stack.pop()
        self._loop_argv0_bindings_stack.pop()
        self._loop_taint_stack.pop()
        self._in_qualifying_loop_depth -= 1

    def visit_For(self, node: ast.For) -> None:
        self._visit_for(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_for(node)

    def visit_While(self, node: ast.While) -> None:
        # Discriminator 3: while loops never qualify. Body still descended (a nested
        # qualifying for-loop inside a while must still be found), condition visited plainly.
        self.visit(node.test)
        for stmt in node.body:
            self.visit(stmt)

    def _visit_comprehension_container(
        self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
    ) -> None:
        generators = node.generators
        if not generators:
            self.generic_visit(node)
            return
        # Discriminator 1: generator-0's iter is evaluated once, outside loop context.
        self.visit(generators[0].iter)
        if _is_constant_literal_iterable(
            generators[0].iter, self._literal_names
        ) or _is_repetition_loop(
            generators[0].target,
            generators[0].iter,
            # A comprehension has no statement body; its element expression (and this
            # generator's own `if` clauses) is what could read the target back, so that is
            # what `_is_discarded_target` is handed. `[time_invocation(op) for _ in range(n)]`
            # never mentions `_`, which is precisely the sampling shape.
            [_comp_elt_expr(node), *generators[0].ifs],
        ):
            # Discriminator 2 (first generator only -- see module docstring blind spots) and
            # discriminator 9.
            for gen in generators:
                for if_clause in gen.ifs:
                    self.visit(if_clause)
            self._visit_comp_elt(node)
            return
        self._in_qualifying_loop_depth += 1
        # Discriminator 6: a comprehension has no statements, so no assignment-hop growth is
        # possible -- taint is exactly the generator-0 target's own names, first generator
        # only (matching discriminator 2's stated blind spot above).
        self._loop_taint_stack.append(frozenset(_loop_target_names(generators[0].target)))
        self._loop_argv0_bindings_stack.append({})
        self._loop_target_stack.append(_loop_target_names(generators[0].target))
        for gen in generators[1:]:
            self.visit(gen.iter)
            for if_clause in gen.ifs:
                self.visit(if_clause)
        for if_clause in generators[0].ifs:
            self.visit(if_clause)
        self._visit_comp_elt(node)
        self._loop_target_stack.pop()
        self._loop_argv0_bindings_stack.pop()
        self._loop_taint_stack.pop()
        self._in_qualifying_loop_depth -= 1

    def _visit_comp_elt(self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp) -> None:
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension_container(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension_container(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension_container(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension_container(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self._in_qualifying_loop_depth > 0:
            key = (node.lineno, node.col_offset)
            self.marked_calls.add(key)
            if self._loop_taint_stack:
                self.call_loop_taint[key] = self._loop_taint_stack[-1]
                self.call_argv0_bindings[key] = self._loop_argv0_bindings_stack[-1]
                self.call_loop_targets[key] = self._loop_target_stack[-1]
            if self._loop_retry_stack:
                self.call_loop_is_retry[key] = self._loop_retry_stack[-1]
            if self._loop_expr_bindings_stack:
                self.call_expr_bindings[key] = self._loop_expr_bindings_stack[-1]
            if self._loop_node_stack:
                self.call_loop_node[key] = self._loop_node_stack[-1]
        self.generic_visit(node)


# --------------------------------------------------------------------------
# Route resolution for one marked call
# --------------------------------------------------------------------------


def _call_callee_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _resolve_runner_shaped_arg_name(value: ast.expr) -> str | None:
    return value.id if isinstance(value, ast.Name) else None


def _name_is_locally_bound_data(fn: ast.AST | None, name: str) -> bool:
    """True when `name` is bound inside `fn` to something that provably is NOT a callable, so a
    runner-shaped IDENTIFIER at a call site there cannot denote a runner.

    Route-d resolution precision, not a discriminator: this is the same defect route f already
    fixed on its own leg (see the `default_name` scoping comment in the collector). Route d
    matches a bare `Name` argument by PREFIX -- `run`, `git`, `spawn` -- and then confirms it
    repo-wide against `index.direct_spawn_funcs`. Neither half asks what the identifier is bound
    to HERE, so an ordinary local holding a string collides with any same-named spawner anywhere
    in the scanned tree. Both live instances are that exact shape and neither has anything to
    batch:

      `block_subagent_commit._fold_template_is_bounded` -- `all(int(run) <= W for run in
      re.findall(r"\\d+", template))`; `run` is a DIGIT RUN, the callee is the builtin `int`.
      `fix_concrete_path_citations.fenced_line_numbers` -- `run = m.group(1) if m else ""`;
      `run` is a fence's backtick run, the callee is the builtin `len`.

    Two decisive binding shapes, both structural and both narrow enough to leave the real
    injected-runner sites standing:

      ITERATION TARGET -- a `for`/comprehension target is the loop's per-item value. Nothing in
      this codebase iterates a collection OF runners and injects the element.
      CONSTANT-BEARING RHS -- an assignment whose right-hand side can evaluate to a non-`None`
      literal. `run = m.group(1) if m else ""` declines on the `""` branch. The real seam idiom
      `run_git = run_git or default_run_git` (`consolidate_assemble.brief`, reached on route d
      and the site two frozen inventory rows depend on) has no literal leaf and survives, as
      does any plain `run = _run` alias.

    A parameter binding is deliberately NOT a decline: the injectable-runner seam this route
    exists to see is spelled exactly that way."""
    if fn is None:
        return False
    for node in ast.walk(fn):
        if isinstance(node, (ast.For, ast.AsyncFor)) and name in _loop_target_names(node.target):
            return True
        if isinstance(node, ast.comprehension) and name in _loop_target_names(node.target):
            return True
        if isinstance(node, ast.Assign) and any(
            name in _loop_target_names(target) for target in node.targets
        ):
            if any(
                isinstance(leaf, ast.Constant) and leaf.value is not None
                for leaf in ast.walk(node.value)
            ):
                return True
    return False


def _find_injected_runner_name(call: ast.Call) -> str | None:
    """Route d: a bare-`Name` argument in a runner-shaped position. Checked over both keyword
    and positional arguments -- see module docstring's route-d description.

    Resolution against `index.direct_spawn_funcs` is BY NAME: the identifier passed at
    THIS call site must literally match the target function's own defined name. A default-
    parameter alias one hop up the call chain (`def check(shas, run=_run): ...; g(run=run)`,
    where the passed identifier is `run`, not `_run`) is not traced and will be missed -- the
    same by-name-only limitation the module docstring's blind-spots section already states for
    routes b/c/e. Route g (added later, see module docstring) closes exactly this
    identifier-renaming gap for a bare-`Name` forwarding chain resolved same-module-first-
    else-imported; it does not close the attribute/lambda/branch-selected variants the module
    docstring's blind-spots section states for route g itself."""
    for kw in call.keywords:
        if kw.arg is None:
            continue
        name = _resolve_runner_shaped_arg_name(kw.value)
        if name is None:
            continue
        if kw.arg in _RUNNER_KWARG_NAMES or name.lower().startswith(_RUNNER_NAME_PREFIXES):
            return name
    for arg in call.args:
        name = _resolve_runner_shaped_arg_name(arg)
        if name is not None and name.lower().startswith(_RUNNER_NAME_PREFIXES):
            return name
    return None


def _spawn_linenos(spawn_sites) -> set[int]:
    """Every recognized spawn site's line, regardless of argv0 (AC11). Was git-argv0-only; see
    `_call_arg_is_argv_shaped` for why filtering on a resolvable program name cannot see the
    `sys.executable`-fronted spawns that dominate the ops census."""
    return {s.lineno for s in spawn_sites}


def find_unbatched_per_item_spawns(
    roots: tuple[pathlib.Path, ...],
    index: _FuncIndex | None = None,
    index_transform: Callable[[_FuncIndex], _FuncIndex] | None = None,
) -> list[AmpSite]:
    """Core collector. Walk `roots` (via the shared `discover_source_files` traversal),
    restricted to the high-precision stratum (callee directly contains a spawn, one hop),
    applying all three structural discriminators and all six detection routes described in
    the module docstring.

    `index`, when provided, lets a caller reuse a pre-built `_FuncIndex` instead of building
    one here -- when omitted, a fresh index is built over the same `roots`, which is what
    every self-test below does.

    `index_transform`, when provided, is applied to the index this function builds from its
    own single parse of its own `_FileRecord`s (this plan's deep-reachability collector is
    the first real caller, widening a `base` index built and parsed exactly once) -- the
    visited `ast.Call` nodes and the transformed index's nodes therefore always come from the
    SAME parse, which is what makes the cross-parse configuration a caller-supplied `index`
    could previously construct unreachable through this parameter: `index_transform` always
    runs against this function's OWN single-parse build over `roots`, never against a
    caller-supplied `index` -- there is no argument through which a foreign-parse index can
    reach the discriminators when `index_transform` is used. `index_transform` had no
    callers until this use, which is why the cross-parse unsoundness it forecloses was never
    observed in practice.
    """
    files = _discover_scope_files(roots)
    records = _load_file_records(files)
    if index_transform is not None:
        index = index_transform(_build_func_index(records))
    elif index is None:
        index = _build_func_index(records)

    #: Discriminator 8 resolves argv0 inside the CALLEE's module, which is not the module being
    #: walked when the callee was imported (route c). Built once over every record rather than
    #: read off the current one for that reason.
    spawn_linenos_by_file: dict[str, set[int]] = {
        rec.relpath: _spawn_linenos(rec.spawn_sites) for rec in records
    }

    violations: list[AmpSite] = []

    for record in records:
        relpath = record.relpath
        tree = record.tree
        spawn_sites = record.spawn_sites

        spawn_linenos = _spawn_linenos(spawn_sites)
        literal_names = _module_level_literal_names(tree)

        loop_visitor = _QualifyingLoopVisitor(literal_names)
        loop_visitor.visit(tree)
        if not loop_visitor.marked_calls:
            continue

        enclosing_by_call: dict[tuple[int, int], str] = {}
        _EnclosingTracker(enclosing_by_call).visit(tree)

        imported_here = index.imported_names_by_file.get(relpath, set())

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            key = (node.lineno, node.col_offset)
            if key not in loop_visitor.marked_calls:
                continue

            enclosing = enclosing_by_call.get(key, "<module>")
            callee = _call_callee_name(node)
            if (relpath, enclosing, callee) in _EXEMPT_SITES:
                continue
            # Same suppression point as `_EXEMPT_SITES`, and deliberately adjacent to it: an
            # oracle claim IS an exemption, differing only in that a test measures it. When this
            # dict holds every claim and that set is empty, the register is gone.
            if (relpath, enclosing, callee) in _ORACLE_CLAIMS:
                continue
            is_direct_spawn_call = node.lineno in spawn_linenos and callee in _SPAWN_API_NAMES
            # Discriminator 6: argv0 derives from this call's own enclosing loop target (or an
            # in-loop assignment hop off it) -- unbatchable by construction, same suppression
            # point as `_EXEMPT_SITES` above so it applies to both the standing gate and the
            # `designed_red` burn-down worklist that share this collector. Restricted to a call
            # that IS ITSELF the recognized spawn syscall (route a's own condition): `args[0]`
            # at a b/c/d/e/f-route call site is a wrapper's own parameter, not an OS-level argv
            # -- see `_argv0_varies_with_loop_target`'s docstring for the false-suppression this
            # guard exists to prevent.
            if is_direct_spawn_call and _argv0_varies_with_loop_target(
                node,
                loop_visitor.call_loop_taint.get(key, frozenset()),
                loop_visitor.call_argv0_bindings.get(key),
            ):
                continue
            # Discriminator 11: the PROGRAM varies per iteration, read at the slot that names
            # the program rather than at argv[0]. `[sys.executable, str(SCRIPT), ...]` is the
            # dominant spawn shape in this tree, and discriminator 6 sees only the constant
            # interpreter there. Same route-a restriction as 6, for the same reason.
            if is_direct_spawn_call and _program_identity_varies_with_loop_target(
                node,
                loop_visitor.call_loop_taint.get(key, frozenset()),
                loop_visitor.call_argv0_bindings.get(key),
            ):
                continue
            # Discriminator 8: argv0 varies with the loop target ACROSS ONE HELPER HOP -- the
            # same "different program each iteration" fact discriminator 6 decides, reached
            # through a wrapper instead of read off the syscall. Applies where 6 is deliberately
            # forbidden (routes b/c), and is NOT the relaxation 6's guard exists to prevent: it
            # resolves the callee and requires the helper's OWN argv0 to be one of its
            # parameters before looking at what this call site supplies for it, so a verb-gated
            # `_run_git([verb, ...], root)` -- whose argv0 is the literal "git" in its own body
            # -- is never reached. See `_argv0_varies_through_helper` for the measurement that
            # motivated it (41 of 65 exempt call sites were route b, invisible to 6).
            if (
                not is_direct_spawn_call
                and callee is not None
                and _argv0_varies_through_helper(
                    node,
                    callee,
                    index,
                    relpath,
                    loop_visitor.call_loop_taint.get(key, frozenset()),
                    spawn_linenos_by_file,
                )
            ):
                continue
            # Discriminators 14 and 15 both need the enclosing loop NODE rather than the taint
            # set the visitor already carries, so it is resolved once here and shared.
            _enclosing_fn = index.func_defs.get((relpath, enclosing))
            _loop_node = (
                _enclosing_loop_of(_enclosing_fn, node) if _enclosing_fn is not None else None
            )
            # Discriminator 15: the spawn fires only behind a test on a value the OPERATOR typed
            # during this iteration -- bounded by keypresses, zero on the modal path.
            if _is_operator_gated_spawn(node, _loop_node):
                continue
            # Discriminator 16: the call is memoized behind a single-slot lazy cache bound
            # before the loop, so it resolves once per scan however long the loop runs. The
            # batching this gate asks for is already there.
            if _is_lazily_memoized_resolution(node, _loop_node, _enclosing_fn):
                continue
            # Discriminator 14: the loop is a linear search for WHICH iteration perturbed some
            # out-of-band state, so collapsing it destroys the attribution that IS the output.
            if _is_attribution_search(
                node, _loop_node, loop_visitor.call_loop_taint.get(key, frozenset())
            ):
                continue
            # Discriminator 13: the loop is the RETAINED PER-ITEM FALLBACK behind a batched
            # primary -- it runs only when the batch failed, recovering per-item attribution
            # instead of collapsing the set to one degraded verdict. Deleting it to clear a key
            # would trade a degrade-on-failure posture for a metric.
            if _is_batched_primary_fallback(
                node,
                callee,
                index.func_defs.get((relpath, enclosing)),
                spawn_linenos,
            ):
                continue
            # Discriminator 12: the spawn is SCOPED to a tree that varies per iteration -- argv0
            # is the constant `git` and the loop target reaches only the `-C`/`--git-dir`/
            # `--work-tree` operand (or `cwd=`). One process cannot serve two roots, so N roots
            # is N spawns however the loop is arranged. Both legs carry the precision constraint
            # that the tainted name appears in NO other argument: without it a per-item fan-out
            # that merely carries a root with it would be silenced, and this SUPPRESSES, so that
            # is the dangerous direction (see `_tainted_names_for_loop`'s inversion warning).
            if is_direct_spawn_call and _root_scoped_direct(
                node,
                loop_visitor.call_loop_taint.get(key, frozenset()),
                loop_visitor.call_expr_bindings.get(key),
            ):
                continue
            if (
                not is_direct_spawn_call
                and callee is not None
                and _root_scoped_through_helper(
                    node,
                    callee,
                    index,
                    relpath,
                    loop_visitor.call_loop_taint.get(key, frozenset()),
                    spawn_linenos_by_file,
                    _enclosing_fn,
                )
            ):
                continue
            # Discriminator 10 (retry loop): the enclosing loop is a bounded retry with an early
            # exit, AND none of its tainted names reach this call's own arguments -- so every
            # iteration issues an IDENTICAL spawn and there is no set for a batch to carry. Both
            # halves are required: the loop half alone would suppress `for _ in range(3):
            # run([..., item])` nested inside a per-item loop, where the argv genuinely varies.
            if loop_visitor.call_loop_is_retry.get(key) and not (
                _names_in_call_args(node)
                & loop_visitor.call_loop_taint.get(key, frozenset())
            ):
                continue
            # Discriminator 7: this call's argv splices its enclosing loop's target in as a
            # SEQUENCE, so one call carries the whole group -- the byte-budget chunking shape
            # discriminator 4's literal-`range` stride test cannot see. Same suppression point
            # as `_EXEMPT_SITES` and discriminator 6 above, so it applies to both the standing
            # gate and the `designed_red` worklist. NOT gated on `is_direct_spawn_call`: it
            # reads how many items one call carries, not what program argv0 names, and that
            # holds at a wrapper route too -- see `_argv_splices_loop_target`'s docstring.
            if _argv_splices_loop_target(
                node,
                loop_visitor.call_loop_targets.get(key, set()),
                loop_visitor.call_expr_bindings.get(key),
            ):
                continue
            # Discriminator 7, accumulation leg: the same "one call carries the whole group"
            # property, spelled as mutation of a locally-built argv inside a nested loop over
            # the outer target rather than as a concatenation at the call. Separate predicate,
            # same suppression point, so it is discovered and pinned like every other.
            if _argv_accumulates_loop_target(
                loop_visitor.call_loop_node.get(key),
                node,
                loop_visitor.call_loop_targets.get(key, set()),
                loop_visitor.call_expr_bindings.get(key),
            ):
                continue
            route: str | None = None

            # route a-direct: the call itself is a recognized spawn. Both halves are
            # required -- the line carries a detected spawn AND this call is the spawn on it,
            # not a helper sharing the line (see `_SPAWN_API_NAMES`).
            if is_direct_spawn_call:
                route = "a-direct"

            if route is None and callee is not None:
                # route b-local-helper: same-module function directly spawns.
                if (relpath, callee) in index.same_module_direct_spawn:
                    # Discriminator 5: a verb-dispatching chokepoint spawns only for the
                    # verbs in its own statically-resolvable allowlist. This call site's
                    # literal verb is not one, so it creates no process.
                    gated = index.verb_gated_spawn_verbs.get((relpath, callee))
                    verb = _call_literal_verb(node)
                    if not (gated is not None and verb is not None and verb not in gated):
                        route = "b-local-helper"

                # route c-cross-module: the local binding RESOLVES -- via
                # `_resolve_imported_defs`, by the ORIGINAL imported name constrained to its
                # resolved source module -- to a function elsewhere that directly spawns. The
                # candidate pool comes from the original name, never from `callee` itself:
                # `direct_spawn_funcs` is keyed by DEFINITION names, so gating on
                # `callee in index.direct_spawn_funcs` would ask whether a spawner is named
                # after the ALIAS and miss every aliased import. See module docstring's route-c
                # section and `_resolve_imported_defs`.
                if route is None and callee in imported_here:
                    _resolved_defs = _resolve_imported_defs(index, relpath, callee)
                    _reaches_spawner = any(
                        any(
                            spawner_relpath == def_relpath
                            for spawner_relpath, _ in index.direct_spawn_funcs.get(def_name, [])
                        )
                        for def_relpath, def_name in _resolved_defs
                    )
                else:
                    _reaches_spawner = False
                if _reaches_spawner:
                    route = "c-cross-module"

                # route e-generic-runner: callee is runner-shaped (a single-parameter
                # `_run(argv)`-style wrapper forwarding into a recognized spawn --
                # `_generic_runner_param` always resolves to that sole parameter, i.e.
                # argument position 0) and THIS call passes an argv-shaped argument.
                if (
                    route is None
                    and callee in index.runner_shaped_funcs
                    and _call_arg_is_argv_shaped(node, 0)
                ):
                    route = "e-generic-runner"

            if route is None:
                # route d-injected: a runner-shaped argument resolves to ANY direct spawner.
                runner_name = _find_injected_runner_name(node)
                # `_name_is_locally_bound_data`: route d's own scoping leg, the counterpart of
                # route f's import check below. The repo-wide `direct_spawn_funcs` lookup is
                # load-bearing and stays (see `_write_route_d_injected_runner_bare_name_
                # collision`), so the collision is refused at the identifier instead: a name
                # bound HERE to a loop item or a literal is not the runner it collides with.
                if (
                    runner_name is not None
                    and runner_name in index.direct_spawn_funcs
                    and not _name_is_locally_bound_data(
                        index.func_defs.get((relpath, enclosing.split(".")[0])), runner_name
                    )
                ):
                    route = "d-injected"

            if route is None and callee is not None:
                # route f-default-runner: the callee is a PARAMETER of the enclosing function
                # whose default binds a module-level direct spawner -- the injectable-seam
                # idiom (`def resync(..., *, run_git=_update_index_with_retry)`), where the
                # loop body calls the parameter, not the function. Route d reads a runner
                # passed AT the call site; this reads one bound one hop up as a default, the
                # gap route d's own docstring names.
                default_name = index.param_runner_defaults.get((relpath, enclosing), {}).get(
                    callee
                )
                # Review: reviewer -- a parameter default can only bind a name resolvable in
                # the DEFINING MODULE's own scope: either a same-module function, or a name
                # imported into this file. The prior unscoped `default_name in
                # index.direct_spawn_funcs` fallback was a repo-wide bare-name lookup with no
                # import check (unlike route c's `callee in imported_here` gate), so a
                # same-named but unrelated, unimported spawner defined elsewhere would
                # false-positive -- the exact "true site on a false route" collision route f
                # exists to correctly resolve.
                if default_name is not None and (
                    (relpath, default_name) in index.same_module_direct_spawn
                    or (
                        default_name in imported_here
                        and default_name in index.direct_spawn_funcs
                    )
                ):
                    route = "f-default-runner"

            if route is None and callee is not None:
                # route g-forwarded-runner: bidirectional fixed-point taint over parameters --
                # resolves an injected runner by where it actually FLOWS, not by what it is
                # called or what it is named at its own definition. Ordered after route f (per
                # this route's own spec) so an existing route still wins the key where both
                # match -- `AmpSite.key` dedup ignores `route`. See module docstring's route-g
                # section and `_compute_spawn_bearing_params`.
                top_level_enclosing = enclosing.split(".")[0]
                enclosing_fn = index.func_defs.get((relpath, top_level_enclosing))
                if enclosing_fn is not None:
                    if (relpath, top_level_enclosing, callee) in index.spawn_bearing_params:
                        # (a) the loop body calls a spawn-bearing parameter directly.
                        route = "g-forwarded-runner"
                    else:
                        # (b) the loop body forwards a spawn-bearing parameter into a callee
                        # at a position that is itself spawn-bearing.
                        for tgt in _resolve_callee_def(index, relpath, callee):
                            tgt_fn = index.func_defs.get(tgt)
                            if tgt_fn is None:
                                continue
                            found = False
                            for p in _func_params(enclosing_fn):
                                if (relpath, top_level_enclosing, p) not in index.spawn_bearing_params:
                                    continue
                                for slot in _forwarded_arg_slots(node, tgt_fn, p):
                                    if (tgt[0], tgt[1], slot) in index.spawn_bearing_params:
                                        route = "g-forwarded-runner"
                                        found = True
                                        break
                                if found:
                                    break
                            if found:
                                break

            if route is not None:
                violations.append(
                    AmpSite(
                        path=relpath,
                        lineno=node.lineno,
                        enclosing=enclosing,
                        route=route,
                        callee=callee or "<unknown>",
                    )
                )

    return violations


class _ParamDefaultTracker(ast.NodeVisitor):
    """Records each function scope's parameters that default to a bare `Name` -- route f's
    index. Tracks the same dotted scope stack as `_EnclosingTracker` so a call site's
    `enclosing` string keys straight into the result."""

    def __init__(self, relpath: str, out: dict[tuple[str, str], dict[str, str]]) -> None:
        self._relpath = relpath
        self._stack: list[str] = []
        self._out = out

    def _record(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        args = node.args
        pairs: list[tuple[ast.arg, ast.expr | None]] = []
        positional = args.posonlyargs + args.args
        # `defaults` right-aligns against the positional parameters; `kw_defaults` is
        # index-aligned against `kwonlyargs` with `None` for the ones that have no default.
        if args.defaults:
            pairs.extend(zip(positional[-len(args.defaults) :], args.defaults))
        pairs.extend(zip(args.kwonlyargs, args.kw_defaults))
        bound = {
            arg.arg: default.id
            for arg, default in pairs
            if isinstance(default, ast.Name)
        }
        if bound:
            scope = ".".join(self._stack)
            self._out.setdefault((self._relpath, scope), {}).update(bound)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._stack.append(node.name)
        self._record(node)
        self.generic_visit(node)
        self._stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()


class _EnclosingTracker(ast.NodeVisitor):
    """Records `(lineno, col_offset) -> dotted enclosing scope name` for every `ast.Call`,
    matching `test_no_spawn_per_item_loop`'s own reporting convention (dotted scope stack)."""

    def __init__(self, out: dict[tuple[int, int], str]) -> None:
        self._stack: list[str] = []
        self._out = out

    def _enclosing(self) -> str:
        return ".".join(self._stack) if self._stack else "<module>"

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        self._out[(node.lineno, node.col_offset)] = self._enclosing()
        self.generic_visit(node)


# --------------------------------------------------------------------------
# G2: standing subset assertion + designed_red burn-down worklist, sharing the
# collector above. See module docstring's "ONE collector, TWO assertions".
# --------------------------------------------------------------------------

#: Frozen inventory of already-known amplification sites. RE-FROZEN 2026-08-16 (AC11) over a
#: repo-wide run of `find_unbatched_per_item_spawns((coordinator_core, coordinator/bin))`:
#: 191 violations / 100 files, 154 distinct `AmpSite.key` identities after dedup by
#: (path, enclosing, callee).
#:
#: Two independent movements are folded into this one re-freeze, and they run in opposite
#: directions -- read the delta as their sum, never as a regression:
#:   +83 keys, because AC11 stopped filtering on a `"git"` argv0 and the collector now sees
#:        every spawn verb (see the module docstring's AC11 section for what that exposed).
#:   -14 keys, of which 11 are genuine burn-downs by the batching chunks that landed alongside
#:        this one (C3/C4/C7/C14/C16/C17 of
#:        docs/plans/2026-08-15-composition-invocation-budgets.md) and 3 are a RE-KEY, not a
#:        fix: `coordinator/bin/coordinator-safe-commit` gained a `.py` extension, and this
#:        inventory keys on the path, so the same three sites re-entered under new keys.
#:
#: 2026-08-17, -1 key: `coordinator/bin/lib/coordinator_registry.py` `<module>` `run` retired as
#:   MISCLASSIFIED, not fixed and not exempted -- the distinction is the point. Discriminator 6
#:   surfaced it, and reading the site shows it was never amplification -- but NOT for the reason
#:   first recorded here (Review: code-reviewer 2026-08-17, WARN; the original note claimed
#:   "breaks on first hit, so at most one spawn", which is not what the code does). The `break`
#:   fires only on SUCCESS: a candidate that exists but answers with empty stdout falls through
#:   and the next existing candidate spawns its own `subprocess.run`. What actually makes it
#:   non-amplifying is that `machine_local_bin_candidates()`
#:   (`coordinator/bin/lib/machine_local_impl_resolve.py`) returns a FIXED-SIZE list -- two bases,
#:   up to ~four with the Windows `.cmd`-first expansion -- so the spawn count is bounded by a
#:   CONSTANT, never by input size. That is the same exclusion class discriminator 3 already
#:   accepts for `while` loops, not a single-spawn claim. Recorded at length because the wrong
#:   version was load-bearing prose that would have been cited later: a first-hit-wins resolution
#:   ladder reads like a per-item spawn loop, and reads like a single spawn once you notice the
#:   `break` -- both readings are wrong, and only the candidate list's size settles it. A burn-down
#:   worklist carrying a site that cannot be burned down overstates the debt; removing it is a
#:   correction to the ledger, never a graduation.
#:
#: 2026-08-19, RE-FROZEN 149 -> 94 keys: 55 retired, 0 added (`docs/plans/2026-08-19-burn-down-
#:   the-amplification-hitlist.md`, chunk C-final; per-key attribution in `state/ledgers/
#:   amp-cfinal-exemption-ledger.md`). The shrink reconciles in BOTH directions and is not
#:   a pure subtraction -- the inventory was simultaneously stale (keys no longer matching
#:   reality) and short (sites observed that it never held, which had the standing G2 assertion
#:   RED at 7 keys). It is now exactly the collector's observed key set, so G2 is green by
#:   construction. The 55 retired keys break down by DISPOSITION, and the classes are NOT
#:   interchangeable -- a burn-down count that folds them together overstates the work done:
#:
#:     43  FIXED -- genuinely batched by this plan's chunks and the T1/T2/T3 waves.
#:      5  REMOVED CODE -- the five `migrate-*.py` one-shot scripts were deleted outright. Their
#:          sites are gone, not batched; counting them as burn-down would overstate it.
#:      5  MOVED TO `_EXEMPT_SITES` (chunk C6) -- the benchmark sampling loops, where drawing N
#:          samples IS the measurement. A transfer between registers, not a reduction in debt.
#:      1  REMOVED CODE (K-007) -- `review_brightline_gate._compute_chain_oracle`, whose
#:          `--from-handoff` mode a concurrent session deleted under a PM kill-ledger ruling.
#:      1  MISCLASSIFIED -- `reap-stale-subagent-sidecars.main` -> `_is_tracked`: the callee no
#:          longer exists under that name, the site having been re-shaped to scope by directory.
#:
#:   One of the 43 is worth naming because the ledger's prediction about it was wrong:
#:   `subagent_fabrication_check._targets_changed` was expected to need RE-KEYING to
#:   `_git_porcelain_for_paths` (plural, renamed in `894d0754b`). That commit also HOISTED the
#:   call out of its loop, so the site is fixed and retires outright; no re-key is owed. Checked
#:   at the call site rather than inferred from the rename.
#:
#:   The 7 keys that had G2 red were resolved WITHOUT growing this constant, which stays
#:   shrink-only: 2 decided structurally by discriminator 7 (added here -- `publish.
#:   _git_status_porcelain` and `percolate-round._dest_paths_exist` chunk against the Windows
#:   argv ceiling, so their spawn count is O(argv_bytes / cap), never O(items); neither was ever
#:   amplification, the collector simply could not see the shape), and 5 routed to
#:   `_EXEMPT_SITES` with dated reasons. See that constant's own entries.
#:
#:   NOT retired, and deliberately so: `ac27_differential_oracle._git_show_blob` is dead code
#:   whose only remaining reference was this constant. Its entry retires here with the rest, but
#:   DELETING the function is a separate change in another file and gets its own disposition --
#:   it is not folded into an inventory shrink.
#:
#: The prior freeze (2026-08-08, 85 keys) and its published inventory --
#: `state/audits/2026-08-08-git-amplification-gate-known-sites.md` -- describe the GIT-ONLY
#: collector. That audit is still an accurate record of that run; it is not a description of
#: this constant.
#:
#: The standing assertion below is a SUBSET check, not a bare `violations == []` (blocked on
#: this volume) -- it bites immediately on any NEW site outside this frozen set. Do NOT grow
#: this constant to silence a new violation; fix the site, or route a genuine deliberate
#: exception through the collector's own `_EXEMPT_SITES` with a dated reason (never a
#: `# amplification-ok:` pragma -- § Anti-scope 17, the discriminators are structural). Shrink
#: this constant as sites are fixed -- that is the designed_red worklist's job below.
#:
#: ---------------------------------------------------------------------------------------
#: WAVE 4 (2026-08-19) -- 94 -> 14. `docs/plans/2026-08-19-burn-down-the-amplification-
#: worklist-to-zero.md`, thirteen disposition chunks plus an EM second-reader pass. Every one
#: of the 80 keys retired here carries a per-key disposition with evidence; the per-chunk
#: records are `state/ledgers/wave4-dispositions/c1.md` .. `c12.md`, the second-reader pass is
#: `second-reader.md` beside them, and the durable per-key artifact is
#: `state/ledgers/amp-cfinal-exemption-ledger.md` (promoted out of `-draft` by this wave).
#:
#:     19  FIXED    -- batched, memoized, or taken to ZERO spawns. 18 are this wave's chunks;
#:                    the 19th (`coverage.py::_derive_dag_chain_set`) a concurrent session
#:                    cleared while the plan was being drafted, recorded rather than claimed.
#:                    Every fix states its spawn count before and after as a function of input
#:                    size (N->1, N->ceil(N/budget), N->0); "the key no longer fires" was not
#:                    accepted as evidence, because hoisting a spawn up a frame or renaming the
#:                    callee clears a key and changes nothing.
#:     61  EXEMPT   -- SUPERSEDED 2026-08-19 by the adversarial re-verification recorded on
#:                    `_EXEMPT_SITES`: 22 of the 75 entries this left standing did not survive
#:                    being read at the call site, and returned here (OVERTURNED block below),
#:                    taking the remainder 14 -> 36. The wave-4 numbers are kept as written
#:                    rather than silently restated, because the gap between them and the
#:                    re-verified ones IS the finding -- a prose-only second read over shared
#:                    rationale blocks reported 61 sound exemptions where reading source found
#:                    53. As originally recorded --
#:                    moved to `_EXEMPT_SITES` with a dated, class-tagged rationale. A
#:                    transfer between registers, never a reduction in debt, which is why AC7
#:                    reports the split rather than a fix RATIO: a target ratio would just
#:                    pressure the next wave into false exemptions.
#:
#: `_EXEMPT_SITES` gained 63 entries, not 61: two FIXED rows RE-KEYED rather than vanishing
#: (`age-sweep-lessons::_batched_git_mv_into_dir`, `coordinator-safe-commit::
#: _first_invalid_pathspec`), so their residual keys were never in this constant to retire from
#: it. Both were read at the call site rather than inferred from the key change -- the chunker
#: loops over BATCHES (O(argv_bytes/cap), never O(items)) and the validator's per-item leg fires
#: only when its batched `ls-files` call fails. Real reductions, residuals classed accordingly.
#:
#: THE SPLIT IS NOT FLATTERING AND IS NOT MEANT TO BE. Roughly three exemptions per fix. The
#: honest reading is that this class was mostly NOT a batching backlog: it was a collector
#: pointed at a repo whose per-item spawns are largely isolation contracts, N-distinct-target
#: structural floors, and retained fallbacks behind hot paths that were batched in earlier
#: waves. That is worth knowing, and it is the reason the register now has to justify itself
#: per entry (`test_every_exemption_carries_a_dated_rationale`).
#:
#: What the second-reader pass bought, and why the EM leg is not ceremony: FOUR rows arrived
#: proposed as EXEMPT whose own text named a working batch primitive and then declined it on
#: budget, verification-cost, or regression-risk grounds. All four were overturned to OPEN --
#: they are on this list, not in `_EXEMPT_SITES`. An exemption is permanent and silently
#: pre-approves whatever next takes that key; "expensive" is not "unbatchable by construction".
#: Nobody but a row's own author reads an exemption otherwise.
#:
#: STILL OWED, scoped and deliberately not done here: four discriminator extensions that would
#: retire the 10 MISCLASSIFIED keys below STRUCTURALLY, which is the disposition the anti-scope
#: demands for a mechanically-decidable class (precedent: discriminators 6 and 7 each retired
#: sites exactly this way). They are gate-collector surgery with their own regression surface
#: and measurement obligation -- "suppresses exactly the intended keys and nothing else" -- so
#: they are named here as work, not smuggled in beside a disposition fold.
#: Site key -> the oracle that MEASURES its exemption claim, as `<module>::<test function>`
#: relative to `coordinator_core.tests.oracles`.
#:
#: Suppresses exactly like `_EXEMPT_SITES`, and is a different object in the one way that
#: matters: membership requires a test that exists and passes. `test_every_oracle_claim_names_a
#: _real_oracle` refuses an entry whose oracle is missing, and the oracles run in this same
#: suite, so a claim that stops holding fails the gate instead of silently continuing to
#: suppress a site that should now be batched.
#:
#: What belongs here and nowhere else: a claim about an EXTERNAL COMMAND'S ARGUMENT SURFACE.
#: Whether `git config --unset` takes two keys, whether a sibling CLI can describe two records
#: in one invocation. No static pass can know these; running them can. A claim decidable from
#: the AST is a discriminator's job and must not be parked here -- that would be the register's
#: growth-by-category failure wearing the oracle layer's clothes.
#: The value is `(oracle_ref, tier)`. TIER IS RECORDED, NOT INFERRED, because the two tiers are
#: not equally strong and a reader must not have to guess which they are looking at:
#:
#:   "fast"    -- argparse introspection, no subprocess, ~0.1s. Runs on the same tier as this
#:                gate, so a claim that stops holding fails on the next commit.
#:   "cadence" -- spawns a real binary, so the spawn ratchet requires `spawns_process` and the
#:                test lands on the cadence tier. Re-measured at cadence gates, not per commit.
#:                The suppression is still unconditional in between, so a git claim can be stale
#:                for longer than a sibling-CLI one. That is a real weakness and it is written
#:                here rather than discovered later.
_ORACLE_CLAIMS: dict[tuple[str, str, str], tuple[str, str]] = {
    # --- sibling CLI writes one record per invocation (fast tier) ---
    ("coordinator/bin/percolate-mirror.py", "_run_gate_legs", "_run"): (
        "test_sibling_cli_single_record::test_percolate_gate_scan_secrets_takes_one_target",
        "fast",
    ),
    ("coordinator/bin/coordinator-harvest-deferrals.py", "_harvest", "_run_lesson_promote"): (
        "test_sibling_cli_single_record::test_lesson_promote_takes_one_record",
        "fast",
    ),
    ("coordinator/bin/coordinator-harvest-deferrals.py", "_harvest", "_run_queue_append"): (
        "test_sibling_cli_single_record::test_queue_append_takes_one_record",
        "fast",
    ),
    # --- act-time TOCTOU rechecks: the property, not a description of it (fast tier) ---
    #: Both files already batch the CLASSIFICATION pass; what remains is the recheck fired
    #: immediately before `unlink()`. Any hoist lands before the first unlink and widens exactly
    #: the window the recheck exists to narrow, so the oracle flips a survivor's tracked status
    #: between classification and unlink and asserts the row is refused with a named reason.
    ("coordinator_core/ops/distill_apply_disposal.py", "apply_disposal_manifest", "_is_tracked"): (
        "test_toctou_act_time_recheck::"
        "test_apply_disposal_manifest_recheck_fires_adjacent_to_its_own_unlink",
        "fast",
    ),
    ("coordinator_core/ops/fleet/_findings_reap.py", "reap_findings", "_is_tracked"): (
        "test_toctou_act_time_recheck::test_reap_findings_recheck_fires_adjacent_to_its_own_unlink",
        "fast",
    ),
    # --- the N-spawn cost IS the measured quantity (fast tier) ---
    #: The control arm of an A/B measurement whose opposing arm already does the batched form in
    #: the same module. Pinning both arms' spawn counts turns any future "batching fix" red.
    ("coordinator_core/benchmarks/shim_fanin_measure.py", "_spawn_n_processes", "run"): (
        "test_benchmark_fanin_spawn_count::test_spawn_n_processes_issues_one_spawn_per_module",
        "fast",
    ),
    # --- varying program, observed rather than inferred (fast tier) ---
    ("coordinator_core/ops/setup_chain_walker.py", "dep_probe_all", "dep_probe"): (
        "test_dep_probe_varying_program::"
        "test_dep_probe_all_python_import_spawns_have_distinct_argv",
        "fast",
    ),
    # --- per-row isolation contract, which is what this site is REALLY about (fast tier) ---
    #: Re-classed off `structural-floor`: the load-bearing reasons are an explicit per-row
    #: isolation contract (a TimeoutExpired must fail THIS row only) and per-item rc
    #: demultiplexing -- neither of which the register's prose named. A static predicate was
    #: considered and rejected: "callee's per-iteration return drives per-item control flow"
    #: would silence a large fraction of real amplification, since a batched call can return
    #: per-item results too.
    (
        "coordinator/bin/workday-complete-close.py",
        "cmd_backfill_dispatch_rows",
        "_dispatch_step9_row",
    ): (
        "test_step9_backfill_row_isolation::test_one_row_failure_does_not_abort_remaining_rows",
        "fast",
    ),
    # --- a third-party CLI's arity, and honestly the weakest claim here (cadence tier) ---
    #: `npm view` takes ONE package-spec; a second positional parses as a FIELD, so a batched
    #: form would silently query the wrong thing rather than error. Not our binary to change.
    #: Weaker than its siblings by construction: it needs npm on PATH, so it is skip-guarded and
    #: degrades to "passes when runnable". Recorded rather than hidden.
    ("coordinator/bin/check-mcp-versions.py", "main", "_npm_latest"): (
        "test_npm_view_single_package_spec::test_npm_view_usage_names_exactly_one_package_spec",
        "cadence",
    ),
    # --- git's own argument surface (cadence tier) ---
    (
        "coordinator_core/install/uninstall_legs.py",
        "uninstall_reverse_git_config_group",
        "config_unset",
    ): ("test_git_argument_surface::test_git_config_unset_takes_exactly_one_key", "cadence"),
    #: RETIRED 2026-08-19 -- `configure_git.py::main::_git_config_set` no longer needs a claim of
    #: any kind. Its loop runs over `_SETTINGS`, a module-level literal tuple fixed at author
    #: time, and discriminator 2 has always decided that shape; it was invisible only because
    #: the constant carries a type annotation and `_module_level_literal_names` read `ast.Assign`
    #: alone. The git-arity oracle it named still exists and still passes -- it is simply no
    #: longer load-bearing for this site, which is the better outcome: a bounded loop decided
    #: structurally beats the same loop decided by asking git about its argument surface.
    ("coordinator_core/ops/fleet/_common.py", "rm_and_commit", "create_subprocess_exec"): (
        "test_git_argument_surface::test_git_rm_is_atomic_across_its_pathspec",
        "cadence",
    ),
    # --- `git rev-list` cannot express a union of ranges (cadence tier) ---
    # One measured fact, six call sites. This is the shape the prose register got RIGHT and
    # could not prove: the claim was true, and nothing tested it at five of the six sites.
    ("coordinator_core/coverage.py", "_reviewed_via_graph_walk", "_run"): (
        "test_git_argument_surface::test_git_rev_list_exclusions_are_global",
        "cadence",
    ),
    ("coordinator_core/ops/review_coverage_core.py", "build_reviewed_set", "_run"): (
        "test_git_argument_surface::test_git_rev_list_exclusions_are_global",
        "cadence",
    ),
    ("coordinator_core/ops/review_coverage_core.py", "build_segments", "_run"): (
        "test_git_argument_surface::test_git_rev_list_exclusions_are_global",
        "cadence",
    ),
    (
        "coordinator_core/ops/plan_suggest_completion_steps.py",
        "_plans_with_review_trail_coverage",
        "_resolve_range_shas",
    ): ("test_git_argument_surface::test_git_rev_list_exclusions_are_global", "cadence"),
    (
        "coordinator_core/ops/review_trail_readjudication_report.py",
        "compute_readjudication_report",
        "_full_range_shas",
    ): ("test_git_argument_surface::test_git_rev_list_exclusions_are_global", "cadence"),
    # --- the spawn FLOOR claim is RETIRED 2026-08-26: the site it named no longer fires ---
    #: `_common.py::archive_and_commit::create_subprocess_exec` claimed a measured spawn floor
    #: ("M + C") for a loop issuing one `git mv` per move. A peer's spawn-free rework of the
    #: archival landing path removed the spawn entirely (`cf2574ee4`, `dccf2fc01`, then
    #: `cffa6e99f` "retire drift gate, use single hash-object call" and `fc97db465`), so the
    #: function's body now holds no `create_subprocess_exec` call at all and the key can never be
    #: observed again. Deleted rather than left to age, per
    #: `test_oracle_claims_still_name_live_sites`'s own ruling: a claim whose site no longer fires
    #: silently pre-approves whatever next takes that key. The oracle test named above still
    #: stands on its own; only this claim binding is gone.
    # --- batched default, per-item OVERRIDE seam, and the split is the property (fast tier) ---
    #: The default callee already carries N queue files in ONE spawn; what the collector counts is
    #: the operator-supplied override CLI, which is an arbitrary third-party program with no batch
    #: contract to assume. The oracle pins BOTH arms -- collapsing the override arm loses the
    #: per-queue failure attribution, and per-item-ising the default arm is the amplification this
    #: gate exists to catch -- and each arm ships its own fails-when-inverted leg.
    ("coordinator_core/ops/updatedocs_gates.py", "_gate_queue_prune_sweep", "_run"): (
        "test_queue_prune_sweep_spawn_split::test_override_cli_issues_one_spawn_per_queue_file",
        "fast",
    ),
}


_KNOWN_SITES: frozenset[tuple[str, str, str]] = frozenset(
    {
        # OPEN (3) -- wave 4 left these UNDECIDED, and that is recorded rather than laundered.
        # Each chunk named a real batch primitive for its row and then declined it on budget,
        # verification-cost, or regression-risk grounds; the C-review second-reader pass
        # (`state/ledgers/wave4-dispositions/second-reader.md`) overturned all four from EXEMPT
        # back to OPEN, because an exemption claims batching is WRONG here, never that it is
        # expensive. They stay on this worklist, which is exactly what it is for.
        #
        # GRADUATED 2026-08-21 (G6 of docs/problems/2026-08-21-the-over-budget-timeout-hitlist.md):
        # `schema_drift_watch.py::_scan -> check_schema_drift_advisory` is FIXED, not exempted.
        # `_scan` now calls `schema_validate.check_schema_drift_advisory_batch`, which hoists the
        # loop-invariant `foreign_repo_unusable_reason` probe and folds the per-schema
        # `git show HEAD:<path>` into one `git_scope.scoped_cat_file_batch` -- the same one the
        # sibling cockpit batch on this module already used. Two spawns for the whole vendored
        # set, whatever N is. Pinned by `test_schema_drift_watch.py::TestSchemaAdvisoryBatch::
        # test_process_count_does_not_grow_with_the_set`.
        ('coordinator_core/bash_guards/dispatch_checks.py', 'check_destructive_rm', '_run_git'),
        ('coordinator_core/ops/orphan_branch_sweep.py', 'main', '_run'),
        # OVERTURNED (22) -- returned here from `_EXEMPT_SITES` by the 2026-08-19 ADVERSARIAL
        # RE-VERIFICATION, after the PM rejected wave 4's blanket-exemption shape. Wave 4's own
        # C-review re-argued the twelve disposition sidecars' PROSE; it did not re-derive the
        # sites from source, so it could only catch rows that incriminated themselves in their
        # own text. This pass read all 75 register entries at the CALL SITE with instructions to
        # REFUTE each exemption and default to NOT PROVEN: 53 upheld, 14 refuted with a named
        # batch primitive, 8 not proven. Per-key evidence:
        # `state/subagent-share/f74c1de4-c0f3-4db0-9282-313c8f0c91ad/refute-{a..h}.md`.
        #
        # This is NOT the forbidden "grow the inventory to silence a violation" move the
        # constant's own docstring bans -- these keys were ON this list, were moved off it on a
        # claim that did not survive reading, and are returning to the worklist they never
        # should have left. An exemption asserts batching is WRONG at that site; a row that
        # cannot show that at its own call site is debt, and debt belongs here.
        #
        # The single most-cited failure was the one doctrine already names: a row joined its
        # class by SATISFYING A RATIONALE rather than by being unbatchable, and the shared
        # comment block was never re-read against the code beneath it.
        #
        #   REFUTED (13) -- a working batch primitive exists at this call site:
        #   `__init__.py::brief` -> `tip_author`: git for-each-ref --format='%(refname:short)
        #   %(authoremail)' returns every ref's tip author in one call; the callee's own
        #   docstring asserts no such form exists
        ('coordinator_core/consolidate_assemble/__init__.py', 'brief', 'tip_author'),
        #   `sidecar_sweep.py::sweep_sidecars` -> `active_reference_guard`: rg -f
        #   <patternfile> unions all needles in one call; needle->file attribution moves into
        #   the per-file read this guard already does
        #   `agent_worktree_sweep.py::_sweep_one` -> `_cherry_pick_with_env`: commits IS
        #   rev-list --reverse active_branch..HEAD; cherry-pick -x active_branch..HEAD applies
        #   the same commits in order in one call and still stops on first conflict
        #   `tail_ops.py::fire_tracker_and_roadmap_detached` -> `spawn_detached`: the spawned
        #   script delegates to refresh_queries.main, which natively takes a comma-list of
        #   files; single-item-callee was asserted from the class
        (
            'coordinator_core/ops/ceremony/tail_ops.py',
            'fire_tracker_and_roadmap_detached',
            'spawn_detached',
        ),
        #   `configure_git.py::main` -> `_git_config_get`: git config --global --get-regexp
        #   reads all global keys in one call; the block conflated the unbatchable SET side
        #   with the batchable GET side
        #   `distill_apply_disposal.py::_delete_tracked_and_append_log` -> `_run_git`: git
        #   rm/add/checkout HEAD -- all accept N pathspecs; nothing here needs per-item
        #   isolation, unlike the rm_and_commit sibling
        (
            'coordinator_core/ops/distill_apply_disposal.py',
            '_delete_tracked_and_append_log',
            '_run_git',
        ),
        #   `migrate_branch_canonical_case.py::_migrate` -> `_git`: the per-ref show-ref
        #   --verify is redundant: _enumerate_work_refs already fetched the full
        #   refs/heads/work/* listing in one for-each-ref earlier in the same function
        ('coordinator_core/ops/migrate_branch_canonical_case.py', '_migrate', '_git'),
        #   `migrate_completion_log_legacy.py::main` -> `_git_mv`: git mv takes N sources into
        #   one destination DIRECTORY; every call in this loop targets the same legacy_dir
        #   `migrate_cross_repo_layout.py::main` -> `_move_one`: both legs (ls-files
        #   trackedness, git mv/add) accept multiple pathspecs and the per-phase destination
        #   is constant
        #   `normalize_claimed_frontmatter.py::main` -> `get_tracked_files`: git ls-files
        #   accepts multiple directory pathspecs; the per-directory calls collapse to one,
        #   partitioned client-side by prefix
        #   `run_shellcheck_sweep.py::run_shellcheck_sweep` -> `_lint_one_file`: shellcheck -f
        #   json f1 f2 ... is standard multi-file usage and its JSON already carries the
        #   per-finding `file` field this code rewrites
        #   `validate_frontmatter_schema_advisory.py::_reviewed_range_offer` ->
        #   `_resolve_ref_to_sha`: git cat-file --batch-check takes N ref tokens on stdin and
        #   emits per-token sha/missing -- a DIFFERENT primitive from the rev-parse --verify
        #   form the register tested and rejected
        #   `composition_graph.py::path_rename_or_move` -> `_run_git`: not a range union at
        #   all; --follow forbids >1 pathspec but is not required by the predicate's stated
        #   contract, so one git log --diff-filter=R --name-status -- <all paths> serves it
        #   `path_resolution_report.py::_check_posix` -> `run`: PATH is built once at
        #   login-shell startup, not per name looked up inside it, so one -lc script looping
        #   the entrypoints keeps the fresh-shell property and drops N spawns to 1
        #
        #   NOT PROVEN (8) -- the block's stated reason is not evidenced at this call site. Two
        #   are the OVER-BROAD KEY defect, which no prose review could have caught: an
        #   `(relpath, function, callee)` key carries no call anchor, so ONE key silences EVERY
        #   qualifying call to that callee in that function -- including calls the governing
        #   rationale does not describe:
        #   `curation_status.py::compute_curation_status` -> `active_reference_guard`: ripgrep
        #   has a native multi-pattern mode, unlike the git/npm CLIs the block cites as
        #   precedent; the block never measured this call site
        #   `central_run_due.py::main` -> `_count_universals`: shells to a DoE-resident
        #   extract-lessons.py whose argv surface is out of tree and could not be verified;
        #   shape matches none of the block's three named classes
        ('coordinator_core/ops/central_run_due.py', 'main', '_count_universals'),
        #   `orphan_branch_sweep.py::main` -> `_git`: OVER-BROAD KEY: main contains one _git
        #   loop-call matching the block's claim and one that does not; a single key silences
        #   both
        ('coordinator_core/ops/orphan_branch_sweep.py', 'main', '_git'),
        #   `register_discovered_repos.py::main` -> `run`: OVER-BROAD KEY: same defect -- one
        #   matching call, one non-matching call, one key
        ('coordinator_core/ops/register_discovered_repos.py', 'main', 'run'),
        #   `setup_chain_walker.py::_sibling_fallback` -> `_functional_probe_ok`: no batch
        #   primary exists anywhere in this function, so the block's retained-fallback shape
        #   does not describe this call site at all
        #   `setup_chain_walker.py::command_succeeds_native` -> `_run_probe_argv`: same: no
        #   antecedent batch call; this is a ||-chain short-circuit search over heterogeneous
        #   commands
        (
            'coordinator_core/ops/setup_chain_walker.py',
            'command_succeeds_native',
            '_run_probe_argv',
        ),
        #   `__init__.py::brief` -> `unique_commits`: the exclusion base (`current`) is
        #   IDENTICAL across every range here, so the block's differing-base-narrows defect
        #   does not apply; the real blocker (per-branch attribution) is unstated and untested
        ('coordinator_core/consolidate_assemble/__init__.py', 'brief', 'unique_commits'),
        # MISCLASSIFIED (10) -- COLLECTOR FALSE POSITIVES, parked here deliberately rather than
        # routed to `_EXEMPT_SITES`. An exemption asserts the SITE is unbatchable; these sites
        # have nothing to batch at all, so exempting them would file a collector defect under a
        # register that means something else -- the "MISCLASSIFIED is not an unpoliced third
        # door" rule. Each names the structural class that makes it not-amplification, and each
        # class is mechanically decidable, so the correct disposition is a NEW OR EXTENDED
        # DISCRIMINATOR (precedent: discriminators 6 and 7 each retired sites exactly this way),
        # not a ledger note. That work is scoped and NOT done here -- see the delta log below.
        #
        #   retry-loop (3): RETIRED 2026-08-19 by DISCRIMINATOR 10 (`_is_retry_loop`), which is
        #   the disposition the anti-scope demanded for a mechanically-decidable class all
        #   along. Its predicted shape was slightly wrong and the code was read rather than the
        #   note believed: all three spell their target `attempt` and READ IT BACK for backoff
        #   and logging, so the discarded-target test discriminator 9 uses does not reach them.
        #   What decides them is the pair "count-bounded range WITH an early exit" plus "the
        #   loop's tainted names never reach the spawn call's own arguments" -- identical argv
        #   every iteration, nothing for a batch to carry. Measured: exactly these three keys,
        #   zero collateral.
        #   single-shot (4): the call sits lexically inside a `for` body but on a path that
        #   `break`s or `return`s in the same iteration, so it executes AT MOST ONCE per call
        #   regardless of collection size. Frozen precedent for the shape:
        #   `check_destructive_git_orphan`, "single-shot call on a returning branch".
        (
            'coordinator_core/bash_guards/dispatch_checks.py',
            'check_destructive_git_orphan',
            '_run_git',
        ),
        ('coordinator_core/ops/find_polluter.py', 'main', '_existence_detail'),
        (
            'coordinator_core/ops/percolate_preflight_scratch_publish.py',
            'check_allowlist_string',
            '_run_child',
        ),
        #   discriminator-7 gap (1): RETIRED 2026-08-19 by giving discriminator 7 a ONE-HOP
        #   LOCAL BINDING resolution (`_loop_expr_bindings`). The note that stood here named the
        #   wrong mechanism: it blamed `_argv_splices_loop_target` for not walking a
        #   multi-operand `BinOp(Add)` chain, but `_argv_expr_splices` has always recursed
        #   through chains. The actual gap was that `_git_log_batched` assigns argv to a local
        #   (`cmd = log_cmd_base + revision_args + ["--"] + batch`) and passes THAT, so the
        #   matcher saw a bare `Name` and declined by design. Implementing the widening as
        #   written would have retired nothing. Read the code, not the ledger.
        #   route-d name collision (1): RETIRED 2026-08-21 by `_name_is_locally_bound_data`,
        #   route d's own scoping leg -- the disposition this row's own note demanded (a
        #   mechanically-decidable class gets a matcher, not a ledger entry). The note stood
        #   correct: `_fold_template_is_bounded`'s comprehension variable is named `run` because
        #   it holds a regex digit-RUN, route d resolved that bare name to an unrelated
        #   module-level spawner, and the callee is the builtin `int`. What decides it is the
        #   BINDING at the call site, not the name: an identifier bound here to a loop item or
        #   to a literal cannot denote the runner it collides with. Measured: this key plus
        #   `fix_concrete_path_citations.fenced_line_numbers -> len` (the same collision, bound
        #   by `run = m.group(1) if m else ""`, which reached the gate after this inventory was
        #   frozen), zero collateral -- `consolidate_assemble.brief`'s
        #   `run_git = run_git or default_run_git` has no literal leaf and still resolves.
        #   range-base re-key (1): flagged at a site whose own loop was already resolved
        #   upstream; recorded MISCLASSIFIED by C3 against the source, not the ledger.
    }
)


def _gate_scope_paths() -> tuple[pathlib.Path, ...]:
    return tuple(_REPO_ROOT / root for root in _GATE_SCOPE_ROOTS)


def test_no_new_amplification_sites_outside_known_inventory():
    """Standing gate (G2), green at land: NOT a bare `violations == []` (blocked on volume --
    116 hits / 51 files, measured 2026-08-08 repo-wide run, `state/audits/
    2026-08-08-git-amplification-gate-known-sites.md`). A SUBSET-of-frozen-inventory assertion
    instead: `{site.key for site in violations} <= _KNOWN_SITES`. IS green at land at any volume,
    and bites immediately on any NEW amplification site outside the frozen inventory -- the
    class-regrowth property this whole plan exists to buy, satisfied at land rather than deferred
    to graduation."""
    violations = find_unbatched_per_item_spawns(_gate_scope_paths())
    observed = {site.key for site in violations}
    new_site_keys = observed - _KNOWN_SITES
    new_violations = [site for site in violations if site.key in new_site_keys]
    assert not new_site_keys, "\n\n".join(_format_violation(site) for site in new_violations)


def test_the_one_hop_horizon_is_published_where_this_gate_is_cited():
    """The blind spot is PUBLISHED, not remembered -- the discharge chosen for
    `state/bug-backlog/2026-08-25-the-amplification-gate-cannot-see-a-four-h-f955425bef7a.yaml`.

    That row's requirement was that the repo know which per-item spawn sites its gate can and
    cannot see. The one-hop horizon stays (widening it was declined on measurement, and the deeper
    collector is a separate advisory instrument), so what had to change is the READING: every
    doctrine surface citing this file as an enforcement mechanism must say that green here means
    "no new one-hop site", not "no per-item spawn sites exist".

    A prose fix alone decays -- the next edit drops the qualifier and nothing notices. This
    assertion is the artifact that stops it, and it is deliberately a completeness pin over a
    CLOSED citer list rather than a repo-wide scan: a new doctrine surface citing the gate is a
    human decision, and the register is where that decision gets recorded.

    SCOPED TO THE CITING BLOCK, not the file. A whole-file substring check passes while the citing
    bullet quietly loses its qualifier and the marker survives in some unrelated section -- the
    green-that-measures-nothing shape `state/lessons/2026-08-19-a-suppressor-pin-can-pass-
    vacuously.md` was written about, in this same file's history. So the marker must appear in a
    blank-line-delimited block that itself names this gate. The block unit is coarse -- a markdown
    bullet LIST carries no blank lines, so a block can be several adjacent bullets wide -- and that
    coarseness is the accepted residual. Mutation-checked at land: marker unfindable, citer stops
    citing, and marker moved out of the citing block each go RED."""
    missing: list[str] = []
    for rel in _HORIZON_CITERS:
        path = _REPO_ROOT / rel
        if not path.is_file():
            missing.append(f"{rel} -- listed citer does not exist")
            continue
        blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8"))
        citing = [block for block in blocks if _THIS_FILE.name in block]
        if not citing:
            missing.append(f"{rel} -- no longer cites this gate; drop it from _HORIZON_CITERS")
        elif not any(_HORIZON_MARKER in block for block in citing):
            missing.append(f"{rel} -- cites this gate without naming the {_HORIZON_MARKER} horizon")
    assert not missing, (
        "the gate's coverage horizon is not published where the gate is cited:\n"
        + "\n".join(f"  {row}" for row in missing)
        + f"\n\nhorizon: {COVERAGE_HORIZON}"
    )


def test_every_exemption_still_names_a_live_site(monkeypatch):
    """Self-invalidation for `_EXEMPT_SITES` -- the leg the sibling `_EXEMPT_SITES` in
    `test_no_hardcoded_paths.py` lacks and `_PRODUCTION_EXEMPT_SITES` in
    `test_no_bare_chain_terminal_literal.py` has. An exemption that no longer matches anything
    is not harmless: it is a standing, reviewed-looking claim about code that has since been
    batched, renamed, or deleted, and it silently pre-approves whatever next takes that key.

    Re-scan with the register emptied; every entry must reappear as a real violation. A failure
    here is a DELETE, not a re-key -- the site it named is gone."""
    declared = set(_EXEMPT_SITES)
    monkeypatch.setitem(globals(), "_EXEMPT_SITES", set())
    unexempted = {site.key for site in find_unbatched_per_item_spawns(_gate_scope_paths())}
    dead = declared - unexempted
    assert not dead, (
        "these _EXEMPT_SITES entries no longer match any detected site -- delete them:\n"
        + "\n".join(f"  {key}" for key in sorted(dead))
    )


#: The closed set of exemption classes. An executor who cannot pick one of these four has just
#: learned their row is not unbatchable-by-construction -- which is the point of a CLOSED set:
#: wave 4 drifted two ad-hoc names into the sidecars (`fallback-path-residue`,
#: `isolation-is-the-contract`) that read as new classes and were in fact `retained-fallback`
#: and `structural-floor` under other spellings. Growing this set is how the register stops
#: meaning anything; it is not a place to record a fifth shade of "expensive".
_EXEMPTION_CLASSES: frozenset[str] = frozenset(
    {
        "measurement-is-the-loop",
        "retained-fallback",
        "structural-floor",
        "no-primitive-MEASURED-wrong",
    }
)


def _exempt_entry_comment_blocks() -> dict[tuple[str, str, str], str]:
    """Map every `_EXEMPT_SITES` entry to the comment BLOCK that governs it.

    Comment-BLOCK-scoped, never "the line immediately above": the register's own style runs one
    dated rationale over a RUN of adjacent entries (the five sampling-loop tuples share one
    block), and entries are themselves multi-line parenthesised tuples. A naive line-above grep
    false-fails on both shapes, which is why AC8 is specified against the AST.

    An entry's block is the contiguous run of `#` lines found by walking UP from its first line,
    stepping over any preceding entries in the same run -- so a shared block governs every entry
    beneath it until the next block starts."""
    tree = ast.parse(_THIS_FILE.read_text(encoding="utf-8"))
    lines = _THIS_FILE.read_text(encoding="utf-8").splitlines()

    node = None
    for stmt in tree.body:
        targets = getattr(stmt, "target", None)
        if isinstance(stmt, ast.AnnAssign) and getattr(targets, "id", None) == "_EXEMPT_SITES":
            node = stmt.value
            break
    assert node is not None, "could not locate the _EXEMPT_SITES assignment in this file's AST"

    spans = [(elt.lineno, elt.end_lineno, ast.literal_eval(elt)) for elt in node.elts]
    starts = {start for start, _end, _key in spans}

    blocks: dict[tuple[str, str, str], str] = {}
    for start, _end, key in spans:
        cursor = start - 1
        while cursor >= 1:
            text = lines[cursor - 1].strip()
            if text.startswith("#"):
                break
            owner = next((s for s, e, _k in spans if s <= cursor <= e), None)
            if owner is None or owner not in starts:
                break
            cursor = owner - 1
        collected: list[str] = []
        while cursor >= 1 and lines[cursor - 1].strip().startswith("#"):
            collected.append(lines[cursor - 1].strip())
            cursor -= 1
        blocks[key] = "\n".join(reversed(collected))
    return blocks


def test_every_oracle_claim_names_a_real_oracle():
    """The binding that makes `_ORACLE_CLAIMS` a different object from `_EXEMPT_SITES`.

    A prose exemption costs one sentence and suppresses forever. An oracle claim cannot be added
    without a test function that EXISTS -- and since the oracles run in this same suite, cannot
    survive without that test PASSING. This is the leg that makes the claim falsifiable; without
    it the dict is just a register with a comment column."""
    import importlib

    broken: list[str] = []
    for key, (ref, tier) in sorted(_ORACLE_CLAIMS.items()):
        if tier not in {"fast", "cadence"}:
            broken.append(f"{key} -- unknown tier {tier!r}, expected 'fast' or 'cadence'")
            continue
        if "::" not in ref:
            broken.append(f"{key} -- malformed oracle ref {ref!r}, expected '<module>::<test>'")
            continue
        mod_name, func_name = ref.split("::", 1)
        try:
            module = importlib.import_module(f"coordinator_core.tests.oracles.{mod_name}")
        except ImportError as exc:
            broken.append(f"{key} -- oracle module {mod_name!r} does not import: {exc}")
            continue
        if not callable(getattr(module, func_name, None)):
            broken.append(f"{key} -- oracle {ref} names no test function")
    assert not broken, (
        "these `_ORACLE_CLAIMS` entries suppress a site on an oracle that does not exist, which "
        "makes them ordinary unverified exemptions:\n" + "\n".join(f"  {b}" for b in broken)
    )


def test_oracle_claims_and_exemptions_do_not_overlap():
    """A site is suppressed by an oracle or by prose, never both. An overlap would let a
    still-listed prose entry keep a site quiet after its oracle went red -- the fail-OPEN shape
    this whole layer exists to remove."""
    overlap = sorted(set(_ORACLE_CLAIMS) & set(_EXEMPT_SITES))
    assert not overlap, (
        "these keys are BOTH oracle-claimed and prose-exempt, so a failing oracle would not "
        "surface the site:\n" + "\n".join(f"  {k}" for k in overlap)
    )


def test_oracle_claims_still_name_live_sites(monkeypatch):
    """`_EXEMPT_SITES`'s self-invalidation leg, extended to the replacement. A claim whose site
    no longer fires is dead weight that will silently pre-approve whatever next takes that key,
    and it must be deleted rather than left to age."""
    declared = set(_ORACLE_CLAIMS)
    monkeypatch.setitem(globals(), "_ORACLE_CLAIMS", {})
    monkeypatch.setitem(globals(), "_EXEMPT_SITES", set())
    observed = {site.key for site in find_unbatched_per_item_spawns(_gate_scope_paths())}
    stale = sorted(declared - observed)
    assert not stale, (
        "these `_ORACLE_CLAIMS` entries no longer match any detected site -- delete them:\n"
        + "\n".join(f"  {key}" for key in stale)
    )


def test_every_exemption_carries_a_dated_rationale():
    """AC8 of `docs/plans/2026-08-19-burn-down-the-amplification-worklist-to-zero.md`.

    AC2-AC4 are all trivially satisfiable by exempting every remaining key: that would pass every
    mechanical check and deliver nothing. This is the leg that makes the register expensive to
    abuse -- an entry must carry a DATE (so the claim is attributable to a moment and a wave) and
    a CLASS from `_EXEMPTION_CLASSES` (so the claim is one of four arguable shapes, not free
    prose). Neither proves the exemption is right; both make a wrong one visible to the next
    reader instead of anonymous."""
    blocks = _exempt_entry_comment_blocks()

    undated = sorted(key for key, block in blocks.items() if not _DATED_RATIONALE.search(block))
    assert not undated, (
        "these _EXEMPT_SITES entries have no dated rationale comment block above them:\n"
        + "\n".join(f"  {key}" for key in undated)
    )

    untagged = sorted(key for key, block in blocks.items() if not _CLASS_TAG.search(block))
    assert not untagged, (
        "these _EXEMPT_SITES entries carry no `# class:` tag -- an exemption that cannot name "
        "its class is not unbatchable-by-construction:\n"
        + "\n".join(f"  {key}" for key in untagged)
    )

    bad_class = sorted(
        (key, match.group(1))
        for key, block in blocks.items()
        if (match := _CLASS_TAG.search(block)) and match.group(1) not in _EXEMPTION_CLASSES
    )
    assert not bad_class, (
        "these _EXEMPT_SITES entries name a class outside the closed set "
        f"{sorted(_EXEMPTION_CLASSES)}:\n"
        + "\n".join(f"  {key} -- {cls}" for key, cls in bad_class)
    )


#: Every SUPPRESSING discriminator, bound to the self-test(s) that prove it still DECLINES a
#: genuinely batchable site. See `test_every_discriminator_is_pinned_by_a_declining_test` for what
#: this buys and why an unpinned discriminator is the register's failure in new clothes.
_DISCRIMINATOR_PINS: dict[str, tuple[str, ...]] = {
    "_argv0_varies_with_loop_target": (
        "test_discriminator_varying_argv0_invariant_argv0_still_flagged",
        "test_discriminator_varying_argv0_unpacked_constant_argv0_still_flagged",
        "test_discriminator_argv_binding_declines_shlex_split_of_a_non_tainted_command",
        "test_discriminator_argv_binding_declines_a_non_shlex_call_rhs",
    ),
    "_program_identity_varies_with_loop_target": (
        "test_discriminator_program_identity_declines_behind_an_interpreter_flag",
        "test_discriminator_program_identity_declines_a_varying_value_argument",
        "test_discriminator_argv_binding_declines_shlex_split_of_a_non_tainted_command",
        "test_discriminator_argv_binding_declines_a_non_shlex_call_rhs",
    ),
    "_argv0_varies_through_helper": (
        "test_discriminator_argv0_through_helper_declines_a_constant_program",
        "test_discriminator_argv0_through_helper_declines_an_unfilled_slot",
    ),
    "_argv_splices_loop_target": (
        "test_discriminator_argv_element_loop_target_still_flagged",
        "test_discriminator_argv_splice_declines_bare_name_argv",
        "test_discriminator_argv_splice_declines_attribute_off_loop_target",
        "test_discriminator_argv_splice_binding_declines_a_non_argv_shaped_local",
        "test_discriminator_argv_splice_declines_a_comprehension_not_over_the_target",
    ),
    "_argv_accumulates_loop_target": (
        "test_discriminator_argv_accumulation_declines_a_nested_loop_over_another_collection",
        "test_discriminator_argv_accumulation_declines_an_invariant_payload",
    ),
    #: Discriminator 10's condition is the retry-loop flag AND this name-overlap test; the flag
    #: is set in the visitor and pinned under `_is_retry_loop`, so the overlap half is pinned by
    #: the same three tests -- they are what fails if either half stops discriminating.
    "_names_in_call_args": (
        "test_discriminator_retry_loop_declines_when_the_target_reaches_argv",
        "test_discriminator_retry_loop_declines_a_size_derived_bound",
        "test_discriminator_retry_loop_declines_without_an_early_exit",
    ),
    "_is_retry_loop": (
        "test_discriminator_retry_loop_declines_when_the_target_reaches_argv",
        "test_discriminator_retry_loop_declines_a_size_derived_bound",
        "test_discriminator_retry_loop_declines_without_an_early_exit",
    ),
    "_is_repetition_loop": (
        "test_discriminator_repetition_loop_declines_a_size_derived_count",
        "test_discriminator_repetition_loop_declines_a_target_read_back",
    ),
    "_is_operator_gated_spawn": (
        "test_discriminator_operator_gate_declines_a_read_before_the_loop",
    ),
    #: Discriminator 16's three clauses each get their own negative: the gate, the pre-loop
    #: binding, and the no-rebind requirement. Drop any one and a genuinely per-item spawn goes
    #: silent, which is why none of them is folded into the others.
    "_is_lazily_memoized_resolution": (
        "test_discriminator_lazy_memo_declines_an_unguarded_assignment",
        "test_discriminator_lazy_memo_declines_a_cache_bound_inside_the_loop",
        "test_discriminator_lazy_memo_declines_a_cache_rebound_inside_the_loop",
        "test_discriminator_lazy_memo_declines_a_keyed_cache",
    ),
    "_is_attribution_search": (
        "test_discriminator_attribution_search_declines_ordinary_fail_fast",
        "test_discriminator_attribution_search_declines_a_loop_target_test",
    ),
    "_is_batched_primary_fallback": (
        "test_discriminator_batched_primary_fallback_declines_an_ungated_loop",
        "test_discriminator_batched_primary_fallback_declines_without_a_primary",
    ),
    "_root_scoped_direct": (
        "test_discriminator_root_scoped_declines_a_non_git_program",
        "test_discriminator_root_scoped_declines_when_a_pathspec_also_varies",
    ),
    "_root_scoped_through_helper": (
        "test_discriminator_root_scoped_declines_when_a_pathspec_also_varies",
        "test_discriminator_root_scoped_declines_a_non_git_program",
        "test_discriminator_root_scoped_injected_runner_declines_a_non_git_program",
        "test_discriminator_root_scoped_injected_runner_declines_when_a_pathspec_also_varies",
    ),
    "_is_chunking_stride_iterable": ("test_discriminator_unit_stride_range_still_flagged",),
    "_is_constant_literal_iterable": (
        "test_discriminator_verb_gated_chokepoint_spawning_verb_still_flagged",
        "test_discriminator_annotated_module_name_without_a_literal_still_flagged",
        "test_discriminator_function_local_literal_declines_an_unbounded_grow",
    ),
}

#: Names a suppression site calls that are builtins or plumbing, not discriminators.
_NOT_A_DISCRIMINATOR = frozenset({"frozenset", "set", "list", "isinstance"})


def _suppressing_predicate_names() -> set[str]:
    """Every module-local predicate whose truth SUPPRESSES a site, discovered from the code
    rather than listed by hand -- which is the whole point: a discriminator added later is found
    by this walk and fails the pin guard until it carries a declining test.

    Two layers, because suppression happens at two. The collector suppresses with an `if <pred>:
    continue`; the loop visitor suppresses earlier, by declining to MARK a call at all, so its
    `_is_*` predicates never reach a `continue` and a collector-only walk would miss every one of
    discriminators 2, 4, 9 and 10."""
    tree = ast.parse(_THIS_FILE.read_text(encoding="utf-8"))

    def called_names(node: ast.AST) -> set[str]:
        return {
            n.func.id
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }

    module_funcs = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    found: set[str] = set()

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "find_unbatched_per_item_spawns":
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.If)
                    and len(sub.body) == 1
                    and isinstance(sub.body[0], ast.Continue)
                ):
                    found |= called_names(sub.test) & module_funcs
        if isinstance(node, ast.ClassDef) and node.name == "_QualifyingLoopVisitor":
            for method in node.body:
                if not isinstance(method, ast.FunctionDef):
                    continue
                found |= {
                    name
                    for name in called_names(method) & module_funcs
                    if name.startswith("_is_")
                }

    return found - _NOT_A_DISCRIMINATOR


def _asserts_a_site_is_still_reported(func: ast.FunctionDef) -> bool:
    """True when `func` asserts the collector STILL reports something -- the only assertion shape
    that pins a suppressor. A test that merely asserts `violations == []` is a POSITIVE control
    for a discriminator and proves nothing about its limits; forcing that discriminator maximally
    permissive would leave it green."""
    for node in ast.walk(func):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        #: `assert violations` / `assert keys`
        if isinstance(test, ast.Name):
            return True
        #: `assert len(violations) == 1`, or the same shape with a
        #: greater-or-equal / other constant comparison
        if isinstance(test, ast.Compare) and isinstance(test.left, ast.Call):
            func_node = test.left.func
            if isinstance(func_node, ast.Name) and func_node.id == "len":
                for comparator in test.comparators:
                    if isinstance(comparator, ast.Constant) and comparator.value:
                        return True
        #: `assert violations[0].route == "a-direct"`
        if isinstance(test, ast.Compare) and any(
            isinstance(n, ast.Subscript) for n in ast.walk(test.left)
        ):
            return True
        #: The dominant idiom in this module's own self-tests:
        #: `assert [site.enclosing for site in violations] == ["check"]`. The pin is the NON-EMPTY
        #: literal on the right -- an equality against `[]` is a positive control, not a pin, and
        #: must not be accepted here.
        if isinstance(test, ast.Compare):
            for comparator in test.comparators:
                if isinstance(comparator, (ast.List, ast.Set, ast.Tuple)) and comparator.elts:
                    return True
    return False


def test_every_discriminator_is_pinned_by_a_declining_test():
    """The keep-at-zero guard, and the one that actually matters once `_EXEMPT_SITES` is empty.

    An empty register is trivially reachable the wrong way: widen a discriminator until it
    silences everything. That buys the same silence the register bought, with none of the
    visibility -- a prose exemption at least names its site, while an over-broad matcher names
    nothing and nothing downstream notices. This module's own `_tainted_names_for_loop` docstring
    records exactly that failure shipping once already, as a real false suppression.

    So: every suppressing predicate, discovered from the code by
    `_suppressing_predicate_names()`, must be bound in `_DISCRIMINATOR_PINS` to at least one test
    that asserts a genuinely batchable site is STILL REPORTED. Adding a discriminator without one
    fails here, naming the predicate. The binding is checked in both directions -- a retired
    discriminator's stale pin entry fails too, matching the self-invalidation legs
    `_EXEMPT_SITES` and `_ORACLE_CLAIMS` already carry."""
    discovered = _suppressing_predicate_names()
    registered = set(_DISCRIMINATOR_PINS)

    unpinned = sorted(discovered - registered)
    assert not unpinned, (
        "these suppressing discriminators carry no declining test in `_DISCRIMINATOR_PINS`:\n"
        + "\n".join(f"  {name}" for name in unpinned)
        + "\n\nA suppressor without a negative pin can be widened until it silences every real "
        "site, and the gate stays green while it happens. Add a test that builds a genuinely "
        "batchable fixture and asserts the collector STILL reports it, then bind it here."
    )

    stale = sorted(registered - discovered)
    assert not stale, (
        "these `_DISCRIMINATOR_PINS` entries name a predicate that no longer suppresses "
        "anything -- delete them:\n" + "\n".join(f"  {name}" for name in stale)
    )

    tree = ast.parse(_THIS_FILE.read_text(encoding="utf-8"))
    tests_by_name = {
        n.name: n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
    }

    missing = sorted(
        f"{pred} -> {test}"
        for pred, tests in _DISCRIMINATOR_PINS.items()
        for test in tests
        if test not in tests_by_name
    )
    assert not missing, (
        "these `_DISCRIMINATOR_PINS` entries name a test that does not exist in this module:\n"
        + "\n".join(f"  {entry}" for entry in missing)
    )

    toothless = sorted(
        f"{pred} -> {test}"
        for pred, tests in _DISCRIMINATOR_PINS.items()
        for test in tests
        if not _asserts_a_site_is_still_reported(tests_by_name[test])
    )
    assert not toothless, (
        "these pin tests never assert that a site is STILL REPORTED, so they do not constrain "
        "the discriminator they are bound to:\n" + "\n".join(f"  {entry}" for entry in toothless)
    )


@pytest.mark.designed_red
def test_burn_down_known_preexisting_amplification_sites():
    """Red by design, 2026-08-08 -- reported, deliberately not gated. Narrowed to its correct
    job (§ staff-eng review, finding 4): a non-gating worklist burning the 85 already-known sites
    (`_KNOWN_SITES`) toward zero, so graduating a site off the frozen inventory as it gets fixed
    is a one-constant edit, same shape as `test_widened_spawn_families_surface_known_preexisting_
    sites` in `test_no_bare_hot_path_spawn.py`. Full inventory: `state/audits/
    2026-08-08-git-amplification-gate-known-sites.md`.

    THE BURN-DOWN IS PARTIAL, NOT COMPLETE, and `designed_red` STAYS for that reason. Wave 4
    (2026-08-19) took the inventory 94 -> 14, then the same day's adversarial re-verification
    returned 22 keys it had exempted on claims that did not survive being read at the call site;
    2026-08-21 retired one more to a discriminator and GRADUATED one by fixing it (the schema
    advisory, hitlist G6 -- see `_KNOWN_SITES`).

    WAVE 5, 2026-08-27 -- 27 -> 14, and this is the first wave that FIXED rather than
    re-classified. Thirteen keys graduated the only way a key is allowed to: the site stopped
    firing because the spawn stopped happening. Each has a process-count-does-not-grow-with-N
    test landed beside it. `agent_worktree_sweep::_sweep_one` (per-commit cherry-pick -> one
    ranged call, O(N) -> O(1)), `consolidate_assemble::brief -> branch_reachable` (per-branch
    `merge-base` -> one `branch --merged`), `curation_status` and `sidecar_sweep`
    (per-candidate `rg` -> one `rg -f`, through ONE shared `active_reference_guard_many`, not
    two copies), `run_shellcheck_sweep` (shellcheck takes many files),
    `migrate_completion_log_legacy` and `migrate_cross_repo_layout`,
    `normalize_claimed_frontmatter` (5 `ls-files` -> 1), `path_resolution_report`,
    `composition_graph`, and `validate_frontmatter_schema_advisory` (per-endpoint `rev-parse`
    -> one `cat-file --batch-check`).

    A BATCHED SITE THAT KEEPS A CORRECTNESS FALLBACK STILL FIRES HERE, and four rows below are
    exactly that -- do not read them as unfixed. `orphan_branch_sweep::main` (both keys),
    `migrate_branch_canonical_case`, `distill_apply_disposal`, and
    `consolidate_assemble::brief -> tip_author` all took a real batch on the fast path and kept
    a per-item call for the case the batch cannot answer: a ref the batch did not resolve, a
    `git rm` whose atomic form would defeat the denorm module's per-child TOCTOU gate, a
    case-folding filesystem where `for-each-ref` enumeration and `show-ref --verify` are not
    equivalent. This collector is STATIC, so it sees the fallback and cannot see that it is
    unreachable in the common case. That is a real discriminator gap, not a fix that failed --
    and it is the honest reason the count stopped at 14 rather than 10. Measuring these needs a
    runtime spawn count, which is what each site's new N-invariance test provides.

    One of the thirteen was never work at all:
    `close_out_and_stamp.py::_first_deliverable_commit_range_base` named a function that does not
    exist in that file -- it lives in `cascade_baton_rows.py`. It had been sitting in the frozen
    inventory pre-approving whatever next took that key. Same failure as the dead `_ORACLE_CLAIMS`
    entry retired the same day: A REGISTER THAT AGES SILENTLY DEFAULTS TO UNGUARDED, which is why
    `test_oracle_claims_still_name_live_sites` and this test's own subset assertion both exist.

    14 keys remain and this assertion is NOT yet a standing `violations == []`. A reader six
    months out must not mistake this for a weakened test, and must not mistake the shrunk
    inventory for a finished one. What is left is the genuinely hard residue -- the easy and
    the merely-stale are gone, so the next reader should expect every remaining row to argue
    back. They break down as:

      2  OPEN     -- `check_destructive_rm` and `orphan_branch_sweep::main -> _run`, both now
                    BATCHED with a fallback (see the block above), both still visible to this
                    static collector because the fallback call survives in the source.
                    `check_destructive_rm`'s per-target `git status` is one call per repo root
                    on the fast path; it declines rather than guesses on a porcelain shape it
                    cannot attribute exactly (a rename arrow, a `core.quotepath`-quoted path)
                    and pays the per-target call then. That is deliberate: this is the guard
                    between `rm` and a peer's uncommitted work, and a missed deny costs more
                    than a missed spawn. Pinned by `test_check_destructive_rm_status_batch.py`.
      9  OVERTURNED -- the REFUTED rows whose named batch primitive survived contact are fixed
                    and gone. What is left is NOT PROVEN rows, fallback-bearing rows, and
                    REFUTED rows whose primitive did NOT survive tracing: `tail_ops::
                    spawn_detached` (the CLI it spawns takes exactly one id, and collapsing the
                    spawns would collapse the per-id attribution its own negative spec
                    documents), `setup_chain_walker` (1-3 heterogeneous `||` sides, already
                    short-circuiting, never a scaling collection), `central_run_due` (shells to
                    an out-of-tree DoE script whose argv surface cannot be verified from here),
                    `register_discovered_repos` (the remaining call is genuinely per-repo --
                    distinct destination key and value each), and `consolidate_assemble::
                    unique_commits` (per-branch attribution blocker unstated and untested in its
                    own evidence). Per-key evidence inline below.
      5  MISCLASSIFIED -- collector false positives awaiting a DISCRIMINATOR, not a fix. See
                    `_KNOWN_SITES` for the mechanically-decidable classes and what each would
                    retire, and for the three (retry-loop, discriminator-7 gap, route-d name
                    collision) that have already been retired exactly that way.

    So 22 rows are amplification debt in the original sense. The other 5 are a
    collector-precision backlog. Closing this test means disposing all 27;
    `_KNOWN_SITES` shrinking to `frozenset()` is what flips the marker off.

    Why `designed_red`, not gated: burning these down is a follow-up workstream, not this
    chunk's job -- gating on them here would turn a collector this plan wants VISIBLE into a
    blocker for every other session sharing `main`. This test's failure output is exactly that
    worklist, in the marker's own terms: run it explicitly to see the current burn-down surface.
    """
    violations = find_unbatched_per_item_spawns(_gate_scope_paths())
    assert violations == [], "\n\n".join(_format_violation(site) for site in violations)


def _format_violation(site: AmpSite) -> str:
    return (
        f"{site.path}:{site.lineno} ({site.enclosing}) -- route {site.route}: a per-item call "
        f"to `{site.callee}` inside a qualifying loop reaches a spawn directly. Batch it "
        f"into a single call outside the loop (see this plan's safe-primitive map)."
    )


# --------------------------------------------------------------------------
# Self-tests: planted fixtures, positive AND negative, per route and per discriminator.
#
# NOTE (per dispatch instructions): this module's self-tests are the ONLY validation run for
# this chunk. The real gate is NOT run repo-wide here -- G3 (concurrent, `spawn_policy/
# detect.py`) is landing a prebuilt name-to-keys index that brings the equivalent prototype run
# from 33.8s to ~8.6s; G1 does not re-measure that cost and does not invoke
# `find_unbatched_per_item_spawns` against the real tree.
# --------------------------------------------------------------------------


def test_route_a_direct_positive(tmp_path):
    fixture = tmp_path / "route_a.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(paths):\n"
        "    for p in paths:\n"
        "        subprocess.run(['git', 'add', p], cwd='/repo')\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert len(violations) == 1
    assert violations[0].route == "a-direct"
    assert violations[0].lineno == 5


def test_route_b_local_helper_positive(tmp_path):
    fixture = tmp_path / "route_b.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def _git_add(path):\n"
        "    subprocess.run(['git', 'add', path], cwd='/repo')\n"
        "\n"
        "def check(paths):\n"
        "    for p in paths:\n"
        "        _git_add(p)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert len(violations) == 1
    assert violations[0].route == "b-local-helper"
    assert violations[0].callee == "_git_add"


def test_route_c_cross_module_positive(tmp_path):
    helper_mod = tmp_path / "git_helpers.py"
    helper_mod.write_text(
        "import subprocess\n"
        "\n"
        "def commit_one(path):\n"
        "    subprocess.run(['git', 'commit', path], cwd='/repo')\n",
        encoding="utf-8",
    )
    caller_mod = tmp_path / "caller.py"
    caller_mod.write_text(
        "from git_helpers import commit_one\n"
        "\n"
        "def check(paths):\n"
        "    for p in paths:\n"
        "        commit_one(p)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    matches = [v for v in violations if v.route == "c-cross-module"]
    assert len(matches) == 1
    assert matches[0].path.endswith("caller.py")
    assert matches[0].callee == "commit_one"


# --------------------------------------------------------------------------
# Route c resolver (AC1/AC2/AC3): the imported name AND its resolved source module, not the
# local binding alone. Fixture shape for the `__init__` collapse and re-export-hop cases below
# was measured this session, not invented from scratch: `undetermined` imported from
# `coordinator_core.plan_assemble.predicates`, defined in that package's `__init__.py`.
# --------------------------------------------------------------------------


def test_relpath_to_module_collapses_package_init():
    assert _relpath_to_module("coordinator_core/plan_assemble/predicates/__init__.py") == (
        "coordinator_core.plan_assemble.predicates"
    )
    assert _relpath_to_module("coordinator_core/plan_assemble/predicates.py") == (
        "coordinator_core.plan_assemble.predicates"
    )


def test_absolute_import_module_relative_import_in_package_init():
    # `pkg/sub/__init__.py`'s OWN package is itself (case 3) -- a level-1 relative import
    # there resolves against `pkg.sub`, not its parent `pkg`.
    node = ast.parse("from . import helper\n").body[0]
    assert _absolute_import_module("pkg/sub/__init__.py", node) == "pkg.sub"

    node2 = ast.parse("from .. import helper\n").body[0]
    assert _absolute_import_module("pkg/sub/__init__.py", node2) == "pkg"

    # A non-package module's own package is its PARENT.
    node3 = ast.parse("from . import helper\n").body[0]
    assert _absolute_import_module("pkg/sub/mod.py", node3) == "pkg.sub"


def test_resolve_reexport_chain_rewrites_the_name_across_a_renaming_hop():
    # `pkg/__init__.py`: `from .impl import spawn_it as g` -- a renaming re-export.
    raw = {
        "pkg/__init__.py": {"g": {("spawn_it", "pkg.impl")}},
        "pkg/impl.py": {},
    }
    module_to_relpath = {"pkg": "pkg/__init__.py", "pkg.impl": "pkg/impl.py"}
    assert _resolve_reexport_chain(raw, module_to_relpath, "g", "pkg") == ("spawn_it", "pkg.impl")


def test_resolve_reexport_chain_declines_to_constrain_past_a_star_reexport():
    raw = {
        "pkg/reexport.py": {"*": {("*", "pkg.real")}},
        "pkg/real.py": {},
    }
    module_to_relpath = {"pkg.reexport": "pkg/reexport.py", "pkg.real": "pkg/real.py"}
    name, module = _resolve_reexport_chain(raw, module_to_relpath, "real_spawn", "pkg.reexport")
    assert (name, module) == ("real_spawn", _UNCONSTRAINED_MODULE)


def test_resolve_reexport_chain_bound_exhaustion_resolves_to_syntactically_named_module():
    # A cycle: `a` re-exports `x` from `b`, `b` re-exports `x` from `a`. Cycle detection stops
    # the walk and resolves to the pair reached so far, not an iteration-order-dependent "last
    # module reached" -- pins `_REEXPORT_HOP_BOUND`.
    raw = {
        "a.py": {"x": {("x", "b")}},
        "b.py": {"x": {("x", "a")}},
    }
    module_to_relpath = {"a": "a.py", "b": "b.py"}
    name, module = _resolve_reexport_chain(raw, module_to_relpath, "x", "a")
    assert name == "x"
    assert module in ("a", "b")


def test_route_c_resolves_reexported_init_name(tmp_path):
    # `__init__` collapse (case 1): the callee is defined directly inside a package's
    # `__init__.py`, imported by its collapsed dotted module -- must not be mis-pruned by a
    # naive `pkg.sub.__init__` comparison.
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "import subprocess\n"
        "\n"
        "def spawn_one(path):\n"
        "    subprocess.run(['git', 'commit', path], cwd='/repo')\n",
        encoding="utf-8",
    )
    (tmp_path / "caller.py").write_text(
        "from pkg import spawn_one\n"
        "\n"
        "def check(paths):\n"
        "    for p in paths:\n"
        "        spawn_one(p)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    matches = [v for v in violations if v.route == "c-cross-module"]
    assert len(matches) == 1
    assert matches[0].callee == "spawn_one"


def test_route_c_resolves_a_renaming_reexport(tmp_path):
    # `pkg/__init__.py` re-exports `impl.g` under a DIFFERENT local name (`h`); the caller then
    # re-imports THAT under yet another alias that happens to collide, at the call site, with
    # an unrelated function elsewhere also named `g` (a decoy real spawner in a different
    # module). Route c's outer gate can only ever match a call-site spelling that IS some
    # def's own bare name (unchanged, additive-only architecture) -- this fixture is
    # constructed so that coincidence holds, and the point under test is which of the two
    # `g`-named candidates the resolver accepts: `_resolve_reexport_chain` must REWRITE the
    # name at the `.impl import g as h` hop (h -> g) to find the true target's module
    # (`pkg.impl`), and `_import_resolves_to` must then prune the unrelated decoy (`unrelated_g`)
    # whose module does not match.
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "impl.py").write_text(
        "import subprocess\n"
        "\n"
        "def g(path):\n"
        "    subprocess.run(['git', 'commit', path], cwd='/repo')\n",
        encoding="utf-8",
    )
    (pkg / "__init__.py").write_text("from .impl import g as h\n", encoding="utf-8")
    (tmp_path / "unrelated_g.py").write_text(
        "import subprocess\n"
        "\n"
        "def g(path):\n"
        "    subprocess.run(['git', 'commit', path], cwd='/repo')\n",
        encoding="utf-8",
    )
    (tmp_path / "caller.py").write_text(
        "from pkg import h as g\n"
        "\n"
        "def check(paths):\n"
        "    for p in paths:\n"
        "        g(p)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    matches = [v for v in violations if v.route == "c-cross-module"]
    assert len(matches) == 1
    assert matches[0].callee == "g"


def test_route_c_declines_to_constrain_past_a_star_reexport(tmp_path):
    # `pkg/reexport.py` re-exports `real.real_spawn` via `import *`, which cannot be followed
    # by name. Without the star's decline-to-constrain fallback, the resolved module would stay
    # `pkg.reexport` (the immediate import's own module) and never match `real_spawn`'s true
    # module `pkg.real` -- a false NEGATIVE this fixture pins against.
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "real.py").write_text(
        "import subprocess\n"
        "\n"
        "def real_spawn(path):\n"
        "    subprocess.run(['git', 'commit', path], cwd='/repo')\n",
        encoding="utf-8",
    )
    (pkg / "reexport.py").write_text("from .real import *\n", encoding="utf-8")
    (tmp_path / "caller.py").write_text(
        "from pkg.reexport import real_spawn\n"
        "\n"
        "def check(paths):\n"
        "    for p in paths:\n"
        "        real_spawn(p)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    matches = [v for v in violations if v.route == "c-cross-module"]
    assert len(matches) == 1
    assert matches[0].callee == "real_spawn"


def test_import_resolves_to_prunes_a_homonym_of_the_alias():
    # An aliased import's local binding equals a same-named-but-unrelated function's def name
    # -- must NOT match, since the module differs (the false-positive this chunk's resolver
    # exists to prune).
    index = _FuncIndex()
    index.resolved_imports_by_file["caller.py"] = {"g": {("f", "real_mod")}}
    assert _import_resolves_to(index, "caller.py", "g", "unrelated_mod.py", "g") is False
    assert _import_resolves_to(index, "caller.py", "g", "real_mod.py", "f") is True


def test_route_c_resolves_the_imported_name_not_the_local_alias(tmp_path):
    """THE GOAL PROBE for `pln-route-c-resolves-the-imported-name-not-the-local-alias`, and the
    test whose absence let that plan ship green while delivering the shape it had REJECTED.

    `caller.py` imports a spawning function under an alias, and an unrelated non-spawning
    homonym OF THE ALIAS exists elsewhere in the corpus. Both halves of the plan's title are
    asserted here, end-to-end through the real collector rather than through the match predicate
    in isolation:

      - the site IS reported -- route c reaches `pkg/impl.py :: spawn_it` by the ORIGINAL
        imported name, so an aliased import no longer hides a real per-item spawn (the leg the
        first delivery missed: it pruned the decoy and resolved to NOTHING, converting a false
        positive into a false negative);
      - and it is reported for the right reason -- `decoy.py :: _spawn_it`, a homonym of the
        ALIAS in a module `caller.py` never imports, does not spawn, so a resolution that still
        searched by the local binding could only have matched the decoy and would report
        nothing here.

    NEGATIVE SPEC. This test must exercise `find_unbatched_per_item_spawns` over a real corpus.
    Rewriting it to call `_import_resolves_to` (or any other predicate) directly re-creates the
    exact hole it exists to close: `test_import_resolves_to_prunes_a_homonym_of_the_alias` hands
    the predicate a `(def_relpath, def_name)` pair the real lookup never produces, so it stayed
    green through the whole defect."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "impl.py").write_text(
        "import subprocess\n"
        "\n"
        "def spawn_it(path):\n"
        "    subprocess.run(['git', 'status', path], cwd='/repo')\n",
        encoding="utf-8",
    )
    (tmp_path / "decoy.py").write_text(
        "def _spawn_it(path):\n    return path\n",
        encoding="utf-8",
    )
    (tmp_path / "caller.py").write_text(
        "from pkg.impl import spawn_it as _spawn_it\n"
        "\n"
        "def check(paths):\n"
        "    for p in paths:\n"
        "        _spawn_it(p)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    matches = [v for v in violations if v.route == "c-cross-module"]
    assert len(matches) == 1, (
        "route c did not reach the aliased import's true target -- the local binding "
        "`_spawn_it` was searched against definition names again, so `pkg.impl.spawn_it` is "
        f"unreachable and a real per-item spawn goes unreported. Got: {violations}"
    )
    assert matches[0].callee == "_spawn_it"


def test_resolve_callee_def_resolves_an_alias_to_its_original_name():
    """Unit-level companion to the goal probe above: the resolver must ANSWER with the true
    target, not merely decline the decoy. Pins the distinction the first delivery collapsed --
    filtering an alias-keyed candidate pool can only ever return a subset of homonyms OF THE
    ALIAS, so for an aliased import it returns nothing however correct the filter is."""
    fn_node = ast.parse("def spawn_it():\n    pass\n").body[0]
    index = _FuncIndex()
    index.func_defs[("pkg/impl.py", "spawn_it")] = fn_node
    index.func_defs[("decoy.py", "_spawn_it")] = fn_node
    index.funcs_by_name["spawn_it"] = [("pkg/impl.py", "spawn_it")]
    index.funcs_by_name["_spawn_it"] = [("decoy.py", "_spawn_it")]
    index.imported_names_by_file["caller.py"] = {"_spawn_it"}
    index.resolved_imports_by_file["caller.py"] = {"_spawn_it": {("spawn_it", "pkg.impl")}}

    assert _resolve_callee_def(index, "caller.py", "_spawn_it") == [("pkg/impl.py", "spawn_it")]


def test_resolve_callee_def_wide_keeps_the_homonym_narrow_prunes():
    """C2a asymmetry pin: `_resolve_callee_def_wide` (the two suppressor legs,
    `_argv0_varies_through_helper` and `_root_scoped_through_helper`) must return BOTH
    same-named candidates -- the real import target AND an unrelated homonym defined elsewhere
    -- while `_resolve_callee_def` (route g's fixed point, route c's
    `_is_direct_spawner_name`) must prune the homonym via `_import_resolves_to`, exactly as
    `test_import_resolves_to_prunes_a_homonym_of_the_alias` pins for that narrow path alone.

    This is the split the C2a EM resolution authorized: C1 had narrowed `_resolve_callee_def`
    itself, which both suppressor legs called, collapsing the deliberate asymmetry the
    2026-08-19 measurement settled (AC4b) -- narrowing the suppressor path took the reported
    baseline from 25 to 26 sites, surfacing exactly one false positive,
    `coordinator_core/ops/cascade_baton_rows.py :: _first_deliverable_commit_range_base`. A
    later 'consistency' cleanup collapsing `_resolve_callee_def_wide` back into
    `_resolve_callee_def` would silently reintroduce that false positive -- this test is the
    canary."""
    fn_node = ast.parse("def g():\n    pass\n").body[0]
    index = _FuncIndex()
    index.func_defs[("real_mod.py", "g")] = fn_node
    index.func_defs[("unrelated_mod.py", "g")] = fn_node
    index.funcs_by_name["g"] = [("real_mod.py", "g"), ("unrelated_mod.py", "g")]
    index.imported_names_by_file["caller.py"] = {"g"}
    index.resolved_imports_by_file["caller.py"] = {"g": {("g", "real_mod")}}

    narrow = _resolve_callee_def(index, "caller.py", "g")
    wide = _resolve_callee_def_wide(index, "caller.py", "g")

    assert narrow == [("real_mod.py", "g")]
    assert set(wide) == {("real_mod.py", "g"), ("unrelated_mod.py", "g")}


def test_route_d_injected_positive(tmp_path):
    """Matches `session_attribution.trailer_foreign_shas(..., run=_run)`'s real shape: the
    injected identifier passed AT THE CALL SITE must itself be named `_run` for this
    collector's by-name index resolution to find it -- resolving through an intermediate
    same-named local rebinding (a default-parameter alias) is out of scope; see module
    docstring's route-b/c/e "by function NAME only" blind spot, which applies identically here."""
    fixture = tmp_path / "route_d.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def _run(argv):\n"
        "    subprocess.run(argv, cwd='/repo')\n"
        "\n"
        "def trailer_foreign_shas(sha, session_id, run=_run):\n"
        "    return run(['git', 'log', sha])\n"
        "\n"
        "def check(shas):\n"
        "    for sha in shas:\n"
        "        trailer_foreign_shas(sha, 's1', run=_run)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    matches = [v for v in violations if v.route == "d-injected"]
    assert len(matches) == 1
    assert matches[0].lineno == 11


_RUNNER_MODULE_SRC = (
    "import subprocess\n"
    "\n"
    "def _run(argv):\n"
    "    return subprocess.run(argv, cwd='/repo')\n"
)


def test_route_e_generic_runner_positive_attribute_access(tmp_path):
    """Route e requires a CROSS-MODULE runner: `sites_in_source`'s own `_local_helpers`
    resolution already recognizes a same-module `_run(argv)` wrapper called with a literal argv
    and reports it as route a-direct -- that is spawn_policy's existing capability, reused, not
    re-derived. Route e exists for the shape `_local_helpers` cannot see: the runner defined in
    a DIFFERENT module, so this file's own `sites_in_source` pass has no local-helper visibility
    into it at all.

    Reached here by ATTRIBUTE access (`import mod` / `mod._run(...)`), not `from mod import
    _run`. Since AC11 dropped the git-argv0 requirement, a runner brought in by `from`-import is
    claimed by route c first -- c only ever skipped it before because `subprocess.run(argv,
    ...)` resolves argv0 to `<dynamic>`, never to `"git"`. Attribute access leaves the name out
    of `imported_names_by_file`, which is the remaining shape only route e resolves."""
    (tmp_path / "runner_mod.py").write_text(_RUNNER_MODULE_SRC, encoding="utf-8")
    (tmp_path / "route_e.py").write_text(
        "import runner_mod\n"
        "\n"
        "def check(shas):\n"
        "    for sha in shas:\n"
        "        runner_mod._run(['git', 'show', sha])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    matches = [v for v in violations if v.route == "e-generic-runner"]
    assert len(matches) == 1
    assert matches[0].callee == "_run"
    assert matches[0].path.endswith("route_e.py")


def test_route_e_generic_runner_positive_non_git_argv(tmp_path):
    """AC11 at route e. This fixture is the pre-AC11 `test_route_e_generic_runner_negative_
    non_git_argv`, INVERTED: a per-item `['ls', name]` spawn used to be a required silence,
    because route e read git-ness off the call site. A per-item process is a per-item process
    whatever it runs, so the same fixture is now a required violation. The assertion is kept
    in inverted form rather than deleted so the reversal is legible as a decision."""
    (tmp_path / "runner_mod.py").write_text(_RUNNER_MODULE_SRC, encoding="utf-8")
    (tmp_path / "route_e_non_git.py").write_text(
        "import runner_mod\n"
        "\n"
        "def check(names):\n"
        "    for name in names:\n"
        "        runner_mod._run(['ls', name])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    matches = [v for v in violations if v.route == "e-generic-runner"]
    assert len(matches) == 1
    assert matches[0].callee == "_run"


def test_route_e_negative_argument_not_argv_shaped(tmp_path):
    """Negative control that survives AC11: route e reads the call site's argument SHAPE, and a
    bare `Name` is not statically an argv. Conservative by construction -- see
    `_call_arg_is_argv_shaped`."""
    (tmp_path / "runner_mod.py").write_text(_RUNNER_MODULE_SRC, encoding="utf-8")
    (tmp_path / "route_e_unshaped.py").write_text(
        "import runner_mod\n"
        "\n"
        "def check(commands):\n"
        "    for command in commands:\n"
        "        runner_mod._run(command)\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_route_e_negative_runner_forwards_into_a_non_spawn(tmp_path):
    """A single-parameter function that forwards its parameter into an ordinary call is NOT a
    runner. Before AC11 this was free: route e also demanded a `"git"`-prefixed argv at the call
    site, so a non-spawning forwarder could never produce a violation on its own. Once every
    spawn verb counts, that looseness would make any one-argument pass-through function a
    runner -- `_generic_runner_param` requires the forwarding call to sit on a line
    `sites_in_source` independently detected as a spawn."""
    (tmp_path / "not_a_runner.py").write_text(
        "import subprocess\n"
        "\n"
        "def _forward(argv):\n"
        "    return ' '.join(argv)\n"
        "\n"
        "def _elsewhere():\n"
        "    return subprocess.run(['git', 'status'])\n",
        encoding="utf-8",
    )
    (tmp_path / "caller.py").write_text(
        "import not_a_runner\n"
        "\n"
        "def check(names):\n"
        "    for name in names:\n"
        "        not_a_runner._forward(['git', 'show', name])\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_ac11_per_item_non_git_spawn_is_flagged(tmp_path):
    """AC11's headline: "the regression guard fails on a new per-unit NON-GIT spawn in a
    composer path". The fixture is `cutover_gate`'s real shape -- a `sys.executable`-fronted
    pytest invocation per item -- which no argv0-keyed check can see: `_resolve_argv0` reports
    an `ast.Attribute` program name as `<dynamic>`, so this spawn has no resolvable name to put
    on an allowlist. It is caught because the argv0 filter was removed, not extended."""
    (tmp_path / "composer.py").write_text(
        "import subprocess\n"
        "import sys\n"
        "\n"
        "def reverify(node_ids):\n"
        "    for node_id in node_ids:\n"
        "        subprocess.run([sys.executable, '-m', 'pytest', node_id])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert len(violations) == 1
    assert violations[0].route == "a-direct"
    assert violations[0].enclosing == "reverify"


def test_route_a_does_not_report_a_helper_sharing_the_spawn_line(tmp_path):
    """Route a matches on `SpawnSite.lineno`, and a spawn line routinely carries a second call
    (`subprocess.call(argv, **no_console_passthrough_kwargs())`). The spawn is the violation;
    the kwargs helper is not. Without the `_SPAWN_API_NAMES` half of route a's test, widening
    past git reported both -- and `install_health_run._run_legs` was a live instance."""
    (tmp_path / "shared_line.py").write_text(
        "import subprocess\n"
        "\n"
        "def _flags():\n"
        "    return {}\n"
        "\n"
        "def run_all(argvs):\n"
        "    for argv in argvs:\n"
        "        subprocess.call(argv, **_flags())\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [v.callee for v in violations] == ["call"]


def test_route_f_parameter_default_runner_is_resolved(tmp_path):
    """Route f: the injectable-seam idiom, where the loop calls a PARAMETER and the runner is
    bound as that parameter's default a hop up. `_common.py`'s two index-resync sites are the
    live instance, and before AC11 they were visible only through an accidental collision with
    an unrelated same-named function in another module."""
    (tmp_path / "seam.py").write_text(
        "import asyncio\n"
        "\n"
        "async def _update_index_with_retry(argv, *, cwd):\n"
        "    return await asyncio.create_subprocess_exec(*argv, cwd=cwd)\n"
        "\n"
        "async def resync(paths, *, cwd, run_git=_update_index_with_retry):\n"
        "    for path in paths:\n"
        "        await run_git(['git', 'restore', '--staged', '--', path], cwd=cwd)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    matches = [v for v in violations if v.route == "f-default-runner"]
    assert len(matches) == 1
    assert matches[0].enclosing == "resync"
    assert matches[0].callee == "run_git"


def test_route_f_negative_default_is_not_a_spawner(tmp_path):
    """Negative control: a parameter default that resolves to a function which does not spawn
    is not a runner, so the loop is not amplification."""
    (tmp_path / "seam_negative.py").write_text(
        "def _record(item):\n"
        "    return item\n"
        "\n"
        "def walk(items, *, run=_record):\n"
        "    for item in items:\n"
        "        run(item)\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_route_e_generic_runner_positive_spawn_in_nested_closure(tmp_path):
    """Review: reviewer -- a runner candidate whose forwarding call sits inside a NESTED
    closure (`_run(argv): def _forward(): subprocess.run(argv); _forward()`) must still be
    recognized. `SpawnSite.enclosing` is a DOTTED scope path (`"_run._forward"`, not bare
    `"_run"`), so `_build_func_index`'s own-spawn-lineno lookup has to match the function's
    name AND any dotted scope nested under it -- a lookup keyed on the bare name alone finds
    nothing here and mis-reports a genuine runner as not-a-runner (a false negative, not the
    module docstring's already-documented opposite-direction over-inclusion blind spot)."""
    (tmp_path / "nested_runner_mod.py").write_text(
        "import subprocess\n"
        "\n"
        "def _run(argv):\n"
        "    def _forward():\n"
        "        subprocess.run(argv)\n"
        "    _forward()\n",
        encoding="utf-8",
    )
    (tmp_path / "route_e_nested.py").write_text(
        "import nested_runner_mod\n"
        "\n"
        "def check(shas):\n"
        "    for sha in shas:\n"
        "        nested_runner_mod._run(['git', 'show', sha])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    matches = [v for v in violations if v.route == "e-generic-runner"]
    assert len(matches) == 1
    assert matches[0].callee == "_run"


def test_route_f_negative_unscoped_default_name_collision(tmp_path):
    """Review: reviewer -- route f's `default_name in index.direct_spawn_funcs` fallback was
    unscoped by file, so a same-named, unrelated, UNIMPORTED spawning function in another file
    would false-positive route f purely off a bare-name repo-wide match. A parameter default
    can only bind a name resolvable in the defining module's own scope: same-module, or
    imported into that file -- neither holds here, so this must stay silent."""
    (tmp_path / "unrelated_spawner.py").write_text(
        "import subprocess\n"
        "\n"
        "def run_git():\n"
        "    return subprocess.run(['git', 'status'])\n",
        encoding="utf-8",
    )
    (tmp_path / "seam_collision.py").write_text(
        "def run_git(item):\n"
        "    return item\n"
        "\n"
        "def walk(items, *, run=run_git):\n"
        "    for item in items:\n"
        "        run(item)\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_spawn_api_names_track_spawn_policy():
    """Completeness pin for `_SPAWN_API_NAMES`, whose SSOT is `spawn_policy.detect._RECOGNIZED`.
    That name is private, so the collector holds a leaf-name projection rather than importing it
    (see the negative spec). This assertion is what stops the copy drifting: a spawn API added
    to `detect` but not here makes route a silently blind to it, which is the failure mode the
    whole AC11 widening exists to remove."""
    from coordinator_core.spawn_policy.detect import _RECOGNIZED

    assert {func for _module, func in _RECOGNIZED} == set(_SPAWN_API_NAMES)


def test_discriminator_loop_iterable_expression_not_flagged(tmp_path):
    """Discriminator 1: a call that IS the loop's own iterable expression (evaluated once,
    before the first iteration) must never be flagged, matching the measured #2/#11/#17/#28/#29
    FP class in gate-substrate.md."""
    fixture = tmp_path / "disc_iterable.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def _git_add(path):\n"
        "    subprocess.run(['git', 'add', path], cwd='/repo')\n"
        "\n"
        "def list_candidates():\n"
        "    return ['a', 'b']\n"
        "\n"
        "def check():\n"
        "    for p in list_candidates():\n"
        "        pass\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert violations == []


def test_discriminator_constant_literal_sequence_not_flagged(tmp_path):
    """Discriminator 2: `for x in (module-level literal tuple)` must never be flagged, matching
    the measured #12/#19 FP class."""
    fixture = tmp_path / "disc_literal.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "_BASES = ('origin/main', 'origin/dev')\n"
        "\n"
        "def _git_show(ref):\n"
        "    subprocess.run(['git', 'show', ref], cwd='/repo')\n"
        "\n"
        "def check():\n"
        "    for base in _BASES:\n"
        "        _git_show(base)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert violations == []


def test_discriminator_while_loop_not_flagged(tmp_path):
    """Discriminator 3: a `while` loop must never be flagged, matching the measured
    #8/#15/#16 FP class (retry loops, interactive prompts, calendar walks)."""
    fixture = tmp_path / "disc_while.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def _git_status(attempt):\n"
        "    subprocess.run(['git', 'status', str(attempt)], cwd='/repo')\n"
        "\n"
        "def check():\n"
        "    attempt = 0\n"
        "    while attempt <= 3:\n"
        "        _git_status(attempt)\n"
        "        attempt += 1\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert violations == []


def test_discriminator_chunking_stride_loop_not_flagged(tmp_path):
    """Discriminator 4: `for i in range(0, len(xs), CHUNK)` is the BATCHED shape this
    collector's own remedy asks for -- one spawn per chunk, not per item. Flagging it
    reported `coverage.py::_filter_shas_by_scope_paths` (documented as batched over
    stdin, explicitly "not one call per SHA") as an amplification site."""
    fixture = tmp_path / "disc_stride.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "CHUNK = 50\n"
        "\n"
        "def _git_batch(shas):\n"
        "    subprocess.run(['git', 'cat-file', '--batch-check'], cwd='/repo',\n"
        "                   input='\\n'.join(shas))\n"
        "\n"
        "def check(shas):\n"
        "    for i in range(0, len(shas), CHUNK):\n"
        "        _git_batch(shas[i : i + CHUNK])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert violations == []


def test_discriminator_unit_stride_range_still_flagged(tmp_path):
    """Negative control for discriminator 4: a 3-arg `range` with a LITERAL `1` step is a
    per-item walk wearing a stride's clothes, and must still be flagged -- the
    discriminator keys on a non-unit stride, never on the `range` call alone."""
    fixture = tmp_path / "disc_unit_stride.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def _git_show(sha):\n"
        "    subprocess.run(['git', 'show', sha], cwd='/repo')\n"
        "\n"
        "def check(shas):\n"
        "    for i in range(0, len(shas), 1):\n"
        "        _git_show(shas[i])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["check"]


def test_discriminator_argv_spliced_chunk_not_flagged(tmp_path):
    """Discriminator 7, direct-spawn arm: a runtime-built chunk list bounded by argv BYTES has
    no `range` for discriminator 4 to read, but splices its loop target into argv as a
    sequence -- one spawn per chunk. The real shape at `publish.py::_git_status_porcelain`."""
    fixture = tmp_path / "disc_splice.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(batches):\n"
        "    base = ['git', 'status', '--porcelain', '--']\n"
        "    for batch in batches:\n"
        "        subprocess.run(base + batch, cwd='/repo')\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert violations == []


def test_discriminator_argv_accumulated_over_chunk_not_flagged(tmp_path):
    """Discriminator 7, accumulation leg: argv is a bare prefix list grown by `.extend()` in a
    nested loop over the chunk, then spawned once. The real shape at `ops/fleet/_common.py::
    _resync_main_index_for_moves`, whose bounded chunking is itself the fix for a Windows argv
    overflow -- the splice leg cannot see it because the assignment RHS carries only the prefix."""
    fixture = tmp_path / "disc_accumulate.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def resync(groups):\n"
        "    for chunk in _chunks(groups):\n"
        "        argv = ['git', 'restore', '--staged', '--']\n"
        "        for _payload, tokens in chunk:\n"
        "            argv.extend(tokens)\n"
        "        subprocess.run(argv, cwd='/repo')\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert violations == []


def test_discriminator_argv_accumulation_declines_a_nested_loop_over_another_collection(tmp_path):
    """Negative pin for the accumulation leg. The nested loop iterates a collection that has
    nothing to do with the outer target, so argv carries loop-invariant flags and the outer walk
    is still one spawn per item -- the leg must key on the nested iterable NAMING the outer
    target, never on "there is a nested loop that grows argv"."""
    fixture = tmp_path / "disc_accumulate_unrelated.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def resync(paths, flags):\n"
        "    for path in paths:\n"
        "        argv = ['git', 'add', '--']\n"
        "        for flag in flags:\n"
        "            argv.extend(flag)\n"
        "        argv.append(path)\n"
        "        subprocess.run(argv, cwd='/repo')\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["resync"]


def test_discriminator_argv_accumulation_declines_an_invariant_payload(tmp_path):
    """Second negative pin: the nested loop DOES iterate the outer target, but what it pushes
    into argv is a constant, so no per-item payload is carried and the call is still one spawn
    per outer item. Without the "mutation arguments name the nested target" conjunct this shape
    would suppress, which is the widen-until-silent failure `_DISCRIMINATOR_PINS` exists for."""
    fixture = tmp_path / "disc_accumulate_invariant.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def resync(groups):\n"
        "    for chunk in groups:\n"
        "        argv = ['git', 'log']\n"
        "        for _item in chunk:\n"
        "            argv.append('--oneline')\n"
        "        subprocess.run(argv, cwd='/repo')\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["resync"]


def test_route_d_declines_a_runner_shaped_name_bound_to_a_loop_item(tmp_path):
    """Route-d resolution precision (`_name_is_locally_bound_data`), iteration-target leg. A
    comprehension variable spelled `run` because it holds a digit RUN collides by prefix with an
    unrelated module-level spawner, and route d resolves the collision repo-wide. The callee is
    the builtin `int`, which reaches no spawn by any route. Real shape:
    `block_subagent_commit._fold_template_is_bounded`."""
    _write_route_d_injected_runner_bare_name_collision(tmp_path)
    (tmp_path / "fold.py").write_text(
        "import re\n"
        "\n"
        "def bounded(template, width, items):\n"
        "    for item in items:\n"
        "        if not all(int(run) <= width for run in re.findall(r'\\\\d+', template)):\n"
        "            return False\n"
        "    return True\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert violations == []


def test_route_d_declines_a_runner_shaped_name_bound_to_a_literal(tmp_path):
    """Same predicate, constant-bearing-RHS leg. `run = m.group(1) if m else ''` can evaluate to
    a string, so the identifier does not denote the same-named spawner it collides with. Real
    shape: `fix_concrete_path_citations.fenced_line_numbers`."""
    _write_route_d_injected_runner_bare_name_collision(tmp_path)
    (tmp_path / "fences.py").write_text(
        "def fenced(lines, pattern):\n"
        "    inside = set()\n"
        "    for lineno, line in enumerate(lines, start=1):\n"
        "        m = pattern.match(line)\n"
        "        run = m.group(1) if m else ''\n"
        "        if run and len(run) >= 3:\n"
        "            inside.add(lineno)\n"
        "    return inside\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert violations == []


def test_route_d_still_resolves_an_or_default_runner_binding(tmp_path):
    """The pin that keeps `_name_is_locally_bound_data` from swallowing route d whole. The
    injectable-seam idiom rebinds its own parameter (`run_git = run_git or default_run_git`,
    `consolidate_assemble.brief`) -- a local assignment, but one with no literal leaf and no
    iteration target, so the site stays reported. A predicate that declined here would silence
    the sites two frozen inventory rows depend on."""
    _write_route_d_injected_runner_bare_name_collision(tmp_path)
    (tmp_path / "brief.py").write_text(
        "def default_runner(argv, cwd):\n"
        "    return None\n"
        "\n"
        "def worktree_is_dirty(run_git, path):\n"
        "    return run_git(['status', '--porcelain'], path)\n"
        "\n"
        "def brief(worktrees, run_git=None):\n"
        "    run_git = run_git or default_runner\n"
        "    for wt in worktrees:\n"
        "        worktree_is_dirty(run_git, wt)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["brief"]


def test_discriminator_argv_spliced_chunk_not_flagged_through_wrapper(tmp_path):
    """Discriminator 7, wrapper arm: the same shape reached through a local runner. Unlike
    discriminator 6 this is NOT restricted to a direct spawn call -- a wrapper handed a whole
    chunk in one argument spawns once for that chunk whatever it does with it. The real shape
    at `percolate-round.py::_dest_paths_exist`."""
    fixture = tmp_path / "disc_splice_wrapper.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def _run(argv):\n"
        "    return subprocess.run(argv, capture_output=True)\n"
        "\n"
        "def check(paths):\n"
        "    for chunk in _chunk_by_bytes(paths):\n"
        "        _run(['git', 'ls-files', '--error-unmatch', '--'] + chunk)\n"
        "\n"
        "def _chunk_by_bytes(paths):\n"
        "    return [paths]\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert violations == []


def test_discriminator_argv_element_loop_target_still_flagged(tmp_path):
    """Negative control for discriminator 7, and the whole point of it: a loop target placed in
    argv as ONE ELEMENT is the per-item shape this gate exists to report. Only splicing it as a
    SEQUENCE (`+ chunk`, `*chunk`) says one call carries the group."""
    fixture = tmp_path / "disc_splice_negative.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(paths):\n"
        "    base = ['git', 'add', '--']\n"
        "    for path in paths:\n"
        "        subprocess.run(base + [path], cwd='/repo')\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["check"]


def test_discriminator_argv_splice_declines_bare_name_argv(tmp_path):
    """Discriminator 7 declines a bare `Name` argv: `_run(argv_for(item))` and `_run(chunk)`
    are indistinguishable at this seam, so the site stays REPORTED. Declining rather than
    guessing is the safe direction for a discriminator that suppresses -- the same discipline
    `_argv0_expr` applies to its own extraction."""
    fixture = tmp_path / "disc_splice_bare.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(groups):\n"
        "    for group in groups:\n"
        "        subprocess.run(group, cwd='/repo')\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["check"]


def test_discriminator_argv_splice_declines_attribute_off_loop_target(tmp_path):
    """Discriminator 7 matches only a name the LOOP ITSELF binds. `item.argv` is an attribute
    off the target, not the group the iterable yielded -- a per-item spawn whose argv happens to
    be assembled from the item, which must stay reported."""
    fixture = tmp_path / "disc_splice_attr.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(items):\n"
        "    for item in items:\n"
        "        subprocess.run(['git'] + item.argv, cwd='/repo')\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["check"]


_VERB_GATED_CHOKEPOINT = (
    "import subprocess\n"
    "\n"
    "_SPAWN_VERBS = {'status', 'diff', 'add', 'commit'}\n"
    "\n"
    "def _run_git(args, cwd):\n"
    "    if args[0] in _SPAWN_VERBS:\n"
    "        return subprocess.run(['git', '-C', str(cwd), *args], capture_output=True)\n"
    "    return _read_model(args, cwd)\n"
    "\n"
    "def _read_model(args, cwd):\n"
    "    return None\n"
    "\n"
)


def test_discriminator_verb_gated_chokepoint_non_spawning_verb_not_flagged(tmp_path):
    """Discriminator 5: `pickup_assemble._run_git` spawns real git only for the verbs in its
    own module-level allowlist and serves every other verb from an in-process read model, so
    a loop calling it with `cat-file` creates ZERO processes. Reporting that as git
    amplification is a false claim about cost, and its stated remedy would replace a working
    in-process read with a `--batch` form the read model does not serve."""
    fixture = tmp_path / "disc_verb_gated.py"
    fixture.write_text(
        _VERB_GATED_CHOKEPOINT
        + "def check(shas, root):\n"
        "    for sha in shas:\n"
        "        _run_git(['cat-file', '-e', sha], root)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert violations == []


def test_discriminator_verb_gated_chokepoint_spawning_verb_still_flagged(tmp_path):
    """Negative control for discriminator 5: the SAME chokepoint called per-item with a verb
    that IS on its spawn allowlist really does spawn one process per item, and must stay
    flagged. The discriminator keys on the call site's own literal verb, never on the
    chokepoint being a dispatcher."""
    fixture = tmp_path / "disc_verb_gated_spawning.py"
    fixture.write_text(
        _VERB_GATED_CHOKEPOINT
        + "def check(paths, root):\n"
        "    for path in paths:\n"
        "        _run_git(['diff', '--quiet', path], root)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [(site.enclosing, site.callee) for site in violations] == [("check", "_run_git")]


def test_discriminator_verb_gated_requires_a_statically_known_verb(tmp_path):
    """Discriminator 5 stays false-negative-biased in the collector's own direction only:
    a call site whose verb is a VARIABLE cannot be decided statically, so the chokepoint is
    treated exactly as it was before this discriminator existed -- flagged."""
    fixture = tmp_path / "disc_verb_gated_dynamic.py"
    fixture.write_text(
        _VERB_GATED_CHOKEPOINT
        + "def check(verbs, root):\n"
        "    for verb in verbs:\n"
        "        _run_git([verb, '--quiet'], root)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [(site.enclosing, site.callee) for site in violations] == [("check", "_run_git")]


def test_discriminator_program_identity_behind_a_fixed_interpreter_not_flagged(tmp_path):
    """Discriminator 11: argv[0] is a constant `sys.executable` and the SCRIPT behind it varies,
    so the loop runs N different programs. Discriminator 6 reads argv[0] only and sees one fixed
    interpreter -- correct about the slot it reads, blind to the one that names the program."""
    fixture = tmp_path / "disc_identity.py"
    fixture.write_text(
        "import subprocess\n"
        "import sys\n"
        "\n"
        "def run_all(scripts):\n"
        "    for script in scripts:\n"
        "        subprocess.run([sys.executable, str(script), '--help'])\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_discriminator_program_identity_declines_behind_an_interpreter_flag(tmp_path):
    """Discriminator 11's load-bearing negative. With a flag between the interpreter and the
    rest, argv[1] stops naming the program -- `-m` makes argv[2] a MODULE, and what a flag
    consumes is not knowable from the AST. Declines rather than guessing at a position."""
    fixture = tmp_path / "disc_identity_flag.py"
    fixture.write_text(
        "import subprocess\n"
        "import sys\n"
        "\n"
        "def run_all(paths):\n"
        "    for path in paths:\n"
        "        subprocess.run([sys.executable, '-m', 'tool', str(path)])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [(site.enclosing, site.callee) for site in violations] == [("run_all", "run")]


def test_discriminator_program_identity_declines_a_varying_value_argument(tmp_path):
    """Discriminator 11 reads the PROGRAM slot, never a value slot. Here the interpreter and the
    script are both fixed and only an argument varies -- one program, N items, genuinely
    batchable, and the exact site this gate exists to catch."""
    fixture = tmp_path / "disc_identity_value.py"
    fixture.write_text(
        "import subprocess\n"
        "import sys\n"
        "\n"
        "SCRIPT = 'tool.py'\n"
        "\n"
        "def run_all(paths):\n"
        "    for path in paths:\n"
        "        subprocess.run([sys.executable, SCRIPT, str(path)])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [(site.enclosing, site.callee) for site in violations] == [("run_all", "run")]


def test_discriminator_argv_binding_resolves_through_shlex_split_not_flagged(tmp_path):
    """Binding-capture widening for discriminator 6/11 (`_shlex_split_subject_expr`, measured
    2026-08-19 against `workweek_reverse_drift_gate.run_gate`). `argv = shlex.split(cmd)` is a
    bare `Call` RHS -- `_argv0_expr` alone leaves it unresolved, so the call site passing the
    bare `Name` `argv` had nothing to resolve through and read as invariant. With `cmd`
    loop-tainted, argv[0] of the split result is provably tainted too."""
    fixture = tmp_path / "disc_shlex_split_binding.py"
    fixture.write_text(
        "import shlex\n"
        "import subprocess\n"
        "\n"
        "def run_all(rows):\n"
        "    for row in rows:\n"
        "        plugin, source_path, cmd = row.split('|', 2)\n"
        "        argv = shlex.split(cmd)\n"
        "        subprocess.run(argv, cwd=source_path)\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_discriminator_argv_binding_declines_shlex_split_of_a_non_tainted_command(tmp_path):
    """Declining half: the `shlex.split` SUBJECT is a constant, not loop-tainted, so argv[0]
    does not vary and the site is genuinely batchable -- stays flagged. Proves the widening does
    not treat every `shlex.split` result as immune regardless of what it splits."""
    fixture = tmp_path / "disc_shlex_split_binding_constant.py"
    fixture.write_text(
        "import shlex\n"
        "import subprocess\n"
        "\n"
        "def run_all(paths):\n"
        "    for path in paths:\n"
        "        argv = shlex.split('git status --porcelain')\n"
        "        subprocess.run(argv, cwd=path)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [(site.enclosing, site.callee) for site in violations] == [("run_all", "run")]


def test_discriminator_argv_binding_declines_a_non_shlex_call_rhs(tmp_path):
    """The other declining half, and the one the widening exists to guard against: an arbitrary
    function call assigned to the argv name (`build_argv(item)`) must NOT be treated as
    argv0-transparent even though `item` is loop-tainted -- a repo-defined resolver can return a
    fixed argv0 regardless of its input. Only `shlex.split` earns this treatment; any other
    `Call` RHS still declines to bind, and the site stays flagged."""
    fixture = tmp_path / "disc_shlex_split_binding_other_call.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def build_argv(item):\n"
        "    return ['git', 'show', item]\n"
        "\n"
        "def run_all(items):\n"
        "    for item in items:\n"
        "        argv = build_argv(item)\n"
        "        subprocess.run(argv)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [(site.enclosing, site.callee) for site in violations] == [("run_all", "run")]


def test_discriminator_argv_splice_resolves_a_one_hop_local_binding(tmp_path):
    """Discriminator 7's one-hop binding resolution: the chunker builds argv as its own
    statement and passes the local, which is how the idiom is actually spelled in this tree.
    Before this, `args[0]` was a bare `Name` and the matcher declined."""
    fixture = tmp_path / "disc_splice_binding.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def sweep(batches, base):\n"
        "    for batch in batches:\n"
        "        cmd = base + ['--'] + batch\n"
        "        subprocess.run(cmd)\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_discriminator_argv_splice_binding_declines_a_non_argv_shaped_local(tmp_path):
    """The hop resolves only argv-SHAPED right-hand sides. A local bound to a per-item value is
    not a spliced group, and resolving it would turn a suppressor loose on ordinary per-item
    spawns -- the bare-`Name` decline this discriminator has always made, preserved."""
    fixture = tmp_path / "disc_splice_binding_scalar.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def sweep(paths):\n"
        "    for path in paths:\n"
        "        cmd = argv_for(path)\n"
        "        subprocess.run(cmd)\n"
        "\n"
        "def argv_for(p):\n"
        "    return ['git', 'add', p]\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [(site.enclosing, site.callee) for site in violations] == [("sweep", "run")]


def test_discriminator_retry_loop_not_flagged(tmp_path):
    """Discriminator 10: a bounded retry with an early exit, whose target never reaches the
    spawn's arguments. Every iteration issues an identical spawn. Discriminator 3 already
    excludes the `while` spelling of this wholesale; that `for attempt in range(N)` was still
    flagged was an accident of spelling."""
    fixture = tmp_path / "disc_retry.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "_ATTEMPTS = 3\n"
        "\n"
        "def resync():\n"
        "    for attempt in range(_ATTEMPTS):\n"
        "        result = subprocess.run(['git', 'update-index', '--refresh'])\n"
        "        if result.returncode == 0:\n"
        "            return True\n"
        "    return False\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_discriminator_retry_loop_declines_when_the_target_reaches_argv(tmp_path):
    """Discriminator 10's load-bearing negative -- the call-level half of the test. The loop is
    retry-SHAPED (count-bounded range, early exit) but its target reaches the spawn's arguments,
    so the argv genuinely varies and the site is a fan-out, not a retry."""
    fixture = tmp_path / "disc_retry_varying.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "_ATTEMPTS = 3\n"
        "\n"
        "def sweep(paths):\n"
        "    for path in paths:\n"
        "        result = subprocess.run(['git', 'add', path])\n"
        "        if result.returncode == 0:\n"
        "            return True\n"
        "    return False\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [(site.enclosing, site.callee) for site in violations] == [("sweep", "run")]


def test_discriminator_retry_loop_declines_a_size_derived_bound(tmp_path):
    """Discriminator 10 inherits discriminator 9's safety line: a bound derived from a
    collection's SIZE is a fan-out however it is spelled, so `len(...)` anywhere in the range
    arguments declines even when the loop is otherwise retry-shaped."""
    fixture = tmp_path / "disc_retry_len.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def sweep(paths):\n"
        "    for i in range(len(paths)):\n"
        "        result = subprocess.run(['git', 'status'])\n"
        "        if result.returncode == 0:\n"
        "            return True\n"
        "    return False\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [(site.enclosing, site.callee) for site in violations] == [("sweep", "run")]


def test_discriminator_retry_loop_declines_without_an_early_exit(tmp_path):
    """Discriminator 10 requires the loop to be able to STOP on success. A count-bounded loop
    that runs all N iterations unconditionally is not retrying anything -- it is repeating work,
    and whether that is intentional is exactly what discriminator 9's discarded-target test
    decides instead. Here the target is read back, so neither discriminator applies."""
    fixture = tmp_path / "disc_retry_no_exit.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "_ROUNDS = 3\n"
        "\n"
        "def churn():\n"
        "    for round_no in range(_ROUNDS):\n"
        "        subprocess.run(['git', 'gc', '--quiet'])\n"
        "        print(round_no)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [(site.enclosing, site.callee) for site in violations] == [("churn", "run")]


def test_discriminator_repetition_loop_statement_form_not_flagged(tmp_path):
    """Discriminator 9, `ast.For` form: target discarded over a count-bounded `range`, so every
    iteration's argv is identical -- a repetition, not one spawn per item."""
    fixture = tmp_path / "disc_repetition_for.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def warm(warmup):\n"
        "    for _ in range(warmup):\n"
        "        subprocess.run(['git', 'status'])\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_discriminator_repetition_loop_comprehension_form_not_flagged(tmp_path):
    """Discriminator 9, comprehension form -- load-bearing, not a symmetry nicety. MEASURED
    2026-08-19: each retired benchmark key carried TWO call sites, a `for _ in range(warmup)`
    statement AND the sampling comprehension, so a statement-only matcher would have retired
    none of them."""
    fixture = tmp_path / "disc_repetition_comp.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def sample(n):\n"
        "    return [subprocess.run(['git', 'status']) for _ in range(n)]\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_discriminator_repetition_loop_declines_a_size_derived_count(tmp_path):
    """Discriminator 9's load-bearing negative and its entire safety argument. `range(len(x))`
    scales with INPUT SIZE -- that is the amplification this gate exists to catch, wearing a
    discarded target. Any `Call` in the `range` argument declines."""
    fixture = tmp_path / "disc_repetition_len.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def sweep(paths):\n"
        "    for _ in range(len(paths)):\n"
        "        subprocess.run(['git', 'status'])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [(site.enclosing, site.callee) for site in violations] == [("sweep", "run")]


def test_discriminator_repetition_loop_declines_a_target_read_back(tmp_path):
    """Discriminator 9 decides by USE, not by name: a target the body reads back is not
    discarded, whatever it is called. This is why the three MISCLASSIFIED `retry-loop` rows --
    all of which name their target `attempt` and read it for backoff -- are NOT retired here,
    and need the retry shape decided on its own terms instead."""
    fixture = tmp_path / "disc_repetition_used.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def attempts(tries):\n"
        "    for attempt in range(tries):\n"
        "        subprocess.run(['git', 'status', str(attempt)])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [(site.enclosing, site.callee) for site in violations] == [("attempts", "run")]


def test_discriminator_argv0_varies_through_a_local_helper_not_flagged(tmp_path):
    """Discriminator 8, positive: the loop calls a HELPER and the helper spawns whichever
    program its caller handed it. Same "different program each iteration" fact discriminator 6
    decides at route a, reached across one hop -- which is where this repo's population actually
    lives (41 of 65 exempt call sites measured route `b-local-helper`, only 16 route a)."""
    fixture = tmp_path / "disc_argv0_helper.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def _run_tool(exe, args):\n"
        "    return subprocess.run([exe, *args])\n"
        "\n"
        "def check(interpreters):\n"
        "    for interp in interpreters:\n"
        "        _run_tool(interp, ['-m', 'compileall'])\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_discriminator_operator_gated_spawn_not_flagged(tmp_path):
    """Discriminator 15. The spawn fires only when the operator types `d`, so its count is bounded
    by keypresses and the modal path spawns zero."""
    fixture = tmp_path / "disc_operator_gate.py"
    fixture.write_text(
        "import subprocess\n"
        "import sys\n"
        "\n"
        "def review(incoming, ref):\n"
        "    for f in incoming:\n"
        "        ans = sys.stdin.readline().strip()\n"
        "        if ans in ('d', 'D'):\n"
        "            subprocess.run(['git', 'diff', ref, '--', f])\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_discriminator_operator_gate_declines_a_read_before_the_loop(tmp_path):
    """Discriminator 15's load-bearing negative. A stdin read BEFORE the loop is the ITERABLE'S
    SOURCE, not a per-item gate -- "read a work list from stdin, then fan out over it" is ordinary
    amplification and common in this tree. Only a read inside the loop body gates per item."""
    fixture = tmp_path / "disc_operator_gate_outside.py"
    fixture.write_text(
        "import subprocess\n"
        "import sys\n"
        "\n"
        "def review(ref):\n"
        "    ans = sys.stdin.readline().strip()\n"
        "    for f in ans.split(','):\n"
        "        subprocess.run(['git', 'diff', ref, '--', f])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["review"]


def test_discriminator_lazy_memo_not_flagged(tmp_path):
    """Discriminator 16's positive control -- the shape at
    `promote_shipped_in_flight_stubs.py :: _run_promotions`. One resolution per scan behind a
    single-slot cache bound before the loop."""
    fixture = tmp_path / "disc_lazy_memo.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def promote(stubs, root):\n"
        "    common_dir = None\n"
        "    for stub in stubs:\n"
        "        if common_dir is None:\n"
        "            common_dir = subprocess.run(['git', 'rev-parse', '--git-common-dir'])\n"
        "        stamp(stub, common_dir)\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_discriminator_lazy_memo_declines_an_unguarded_assignment(tmp_path):
    """Discriminator 16's first load-bearing negative: assignment to a name is not memoization.
    Without the unset-test clause, every `x = subprocess.run(...)` in a loop goes silent."""
    fixture = tmp_path / "disc_lazy_memo_unguarded.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def promote(stubs, root):\n"
        "    common_dir = None\n"
        "    for stub in stubs:\n"
        "        common_dir = subprocess.run(['git', 'rev-parse', '--git-common-dir'])\n"
        "        stamp(stub, common_dir)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["promote"]


def test_discriminator_lazy_memo_declines_a_cache_bound_inside_the_loop(tmp_path):
    """Discriminator 16's second load-bearing negative. A name first bound INSIDE the loop is
    re-created every pass, so its `is None` test is true every pass and the call fires every
    pass. The guard is present and means nothing -- this is amplification wearing one."""
    fixture = tmp_path / "disc_lazy_memo_inner_binding.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def promote(stubs, root):\n"
        "    for stub in stubs:\n"
        "        common_dir = None\n"
        "        if common_dir is None:\n"
        "            common_dir = subprocess.run(['git', 'rev-parse', '--git-common-dir'])\n"
        "        stamp(stub, common_dir)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["promote"]


def test_discriminator_lazy_memo_declines_a_cache_rebound_inside_the_loop(tmp_path):
    """Discriminator 16's third load-bearing negative. A cache cleared mid-loop is resolved
    again on the next pass, so the site really does spawn per item -- N times, not once."""
    fixture = tmp_path / "disc_lazy_memo_rebound.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def promote(stubs, root):\n"
        "    common_dir = None\n"
        "    for stub in stubs:\n"
        "        if common_dir is None:\n"
        "            common_dir = subprocess.run(['git', 'rev-parse', '--git-common-dir'])\n"
        "        stamp(stub, common_dir)\n"
        "        common_dir = None\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["promote"]


def test_discriminator_lazy_memo_declines_a_keyed_cache(tmp_path):
    """Discriminator 16's scope boundary. A keyed cache resolves once per DISTINCT KEY, and the
    key set is a function of the iterable -- it may be N. Only a single-slot cache is provably
    once-per-scan, so the keyed shape stays reported."""
    fixture = tmp_path / "disc_lazy_memo_keyed.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def promote(stubs, root):\n"
        "    seen = {}\n"
        "    for stub in stubs:\n"
        "        if stub not in seen:\n"
        "            seen[stub] = subprocess.run(['git', 'log', '-1', stub])\n"
        "        stamp(stub, seen[stub])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["promote"]


def test_discriminator_attribution_search_not_flagged(tmp_path):
    """Discriminator 14. The loop runs one candidate, then asks OUT-OF-BAND state whether it was
    perturbed, and stops at the first that was. A batched run reports that the artifact appeared
    but not which candidate made it -- and that attribution is the entire output."""
    fixture = tmp_path / "disc_attribution_search.py"
    fixture.write_text(
        "import os\n"
        "import subprocess\n"
        "\n"
        "def main(test_files, marker):\n"
        "    for test_file in test_files:\n"
        "        subprocess.run(['npm', 'test', test_file])\n"
        "        if os.path.exists(marker):\n"
        "            return 1\n"
        "    return 0\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_discriminator_attribution_search_declines_ordinary_fail_fast(tmp_path):
    """Discriminator 14's load-bearing negative, and the clause that keeps it from silencing every
    early-exit loop in the tree. `if result.returncode: return` reads the SPAWN'S OWN result -- a
    per-item check on a genuinely batchable fan-out, not an out-of-band observation."""
    fixture = tmp_path / "disc_attribution_failfast.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def main(paths):\n"
        "    for path in paths:\n"
        "        result = subprocess.run(['git', 'log', path])\n"
        "        if result.returncode:\n"
        "            return 1\n"
        "    return 0\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["main"]


def test_discriminator_attribution_search_declines_a_loop_target_test(tmp_path):
    """The other half of clause 2: a test on the LOOP TARGET is a per-item filter, not an
    observation of state the spawn perturbed."""
    fixture = tmp_path / "disc_attribution_target_test.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def main(paths):\n"
        "    for path in paths:\n"
        "        subprocess.run(['git', 'log', path])\n"
        "        if path.endswith('.stop'):\n"
        "            return 1\n"
        "    return 0\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["main"]


def test_discriminator_function_local_append_only_literal_not_flagged(tmp_path):
    """Discriminator 2 at FUNCTION scope. The count is `len(literal) + number of append
    statements` -- both fixed at author time, which is the same safety argument the rule already
    made at module scope. `prereq_probe.probe_clone_auth`'s real shape."""
    fixture = tmp_path / "disc_local_literal.py"
    fixture.write_text(
        "import os\n"
        "import subprocess\n"
        "\n"
        "def probe():\n"
        "    hosts = ['git@github.com', 'git@gitlab.com']\n"
        "    extra = os.environ.get('PROBE', '')\n"
        "    if extra.startswith('git@'):\n"
        "        hosts.append(extra)\n"
        "    for host in hosts:\n"
        "        subprocess.run(['ssh', '-T', host])\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_discriminator_function_local_literal_declines_an_unbounded_grow(tmp_path):
    """The negative that keeps the function-local arm honest. `extend` grows the sequence by an
    amount nobody can read off the source, so the loop is input-sized again and the site must
    stay reported. Same for a rebind or a comprehension -- `append` is the only growth whose
    contribution is countable at author time."""
    fixture = tmp_path / "disc_local_literal_extend.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def probe(discovered):\n"
        "    hosts = ['git@github.com']\n"
        "    hosts.extend(discovered)\n"
        "    for host in hosts:\n"
        "        subprocess.run(['ssh', '-T', host])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["probe"]


def test_discriminator_batched_primary_fallback_except_shape_not_flagged(tmp_path):
    """Discriminator 13, shape 3a -- the per-item loop lives in the `except` of the batched
    primary, so it runs only when the batch spawn failed."""
    fixture = tmp_path / "disc_fallback_except.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(paths):\n"
        "    try:\n"
        "        result = subprocess.run(['git', 'ls-files', '--', *paths])\n"
        "    except OSError:\n"
        "        for p in paths:\n"
        "            subprocess.run(['git', 'ls-files', '--', p])\n"
        "        return None\n"
        "    return result\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_discriminator_batched_primary_fallback_early_return_shape_not_flagged(tmp_path):
    """Discriminator 13, shape 3c -- dominance by EARLY RETURN ON SUCCESS rather than by
    nesting. `coordinator-safe-commit._first_invalid_pathspec`'s real shape, and the one a
    nesting-only matcher misses entirely."""
    fixture = tmp_path / "disc_fallback_early_return.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(paths):\n"
        "    batch = subprocess.run(['git', 'ls-files', '--', *paths])\n"
        "    if batch is not None and batch.returncode == 0:\n"
        "        return None\n"
        "    for p in paths:\n"
        "        subprocess.run(['git', 'ls-files', '--', p])\n"
        "    return None\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_discriminator_batched_primary_fallback_declines_an_ungated_loop(tmp_path):
    """Discriminator 13's load-bearing negative. Clause 3 is the ONLY thing separating a real
    fallback from "calls a batch, ignores the result, fans out anyway" -- here the batch runs and
    its result is never consulted, so the per-item loop is unconditional amplification and must
    stay reported."""
    fixture = tmp_path / "disc_fallback_ungated.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(paths):\n"
        "    subprocess.run(['git', 'ls-files', '--', *paths])\n"
        "    for p in paths:\n"
        "        subprocess.run(['git', 'log', p])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["check"]


def test_discriminator_batched_primary_fallback_declines_without_a_primary(tmp_path):
    """Clause 1/2: a guard on some unrelated condition is not a batched primary. Without a call
    that CARRIES THE COLLECTION WHOLE, there is nothing for the fallback to be a fallback to."""
    fixture = tmp_path / "disc_fallback_no_primary.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(paths, flag):\n"
        "    if flag:\n"
        "        return None\n"
        "    for p in paths:\n"
        "        subprocess.run(['git', 'log', p])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["check"]


def test_discriminator_annotated_module_literal_not_flagged(tmp_path):
    """Discriminator 2 reads an ANNOTATED module-level literal too. The annotation is a fact
    about the author's typing discipline, never about how many times the loop runs."""
    fixture = tmp_path / "disc_annotated_literal.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "SCRIPTS: tuple[str, ...] = ('a.py', 'b.py')\n"
        "\n"
        "def check():\n"
        "    for script in SCRIPTS:\n"
        "        subprocess.run(['git', 'log', script])\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_discriminator_annotated_module_name_without_a_literal_still_flagged(tmp_path):
    """The negative that keeps the annotation arm honest: an annotated module-level name bound
    to a CALL is not a literal, its length is not fixed at author time, and the loop over it is
    a genuine input-sized fan-out."""
    fixture = tmp_path / "disc_annotated_non_literal.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "SCRIPTS: list[str] = sorted(open('manifest').read().split())\n"
        "\n"
        "def check():\n"
        "    for script in SCRIPTS:\n"
        "        subprocess.run(['git', 'log', script])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["check"]


def test_discriminator_argv_splice_accepts_an_elementwise_comprehension(tmp_path):
    """Discriminator 7, comprehension arm. `*[str(s) for s in batch]` puts the whole group into
    one argv exactly as `*batch` would -- the byte-budget chunking shape, where spawn count is
    O(argv_bytes) and never O(items)."""
    fixture = tmp_path / "disc_splice_comprehension.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(batches, dst):\n"
        "    for batch in batches:\n"
        "        subprocess.run(['git', 'mv', *[str(s) for s in batch], dst])\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_discriminator_argv_splice_declines_a_comprehension_not_over_the_target(tmp_path):
    """The comprehension arm must read the group the LOOP yielded, not any comprehension that
    happens to sit in argv. Here the comprehension iterates an unrelated collection while the
    loop target rides in as a single element -- a real per-item fan-out."""
    fixture = tmp_path / "disc_splice_comprehension_other.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(items, flags):\n"
        "    for item in items:\n"
        "        subprocess.run(['git', 'log', *[str(f) for f in flags], str(item)])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["check"]


def test_discriminator_root_scoped_direct_not_flagged(tmp_path):
    """Discriminator 12, leg A. argv0 is the constant `git` and the loop target reaches only the
    `-C` operand -- one process cannot serve two roots, so N roots is N spawns."""
    fixture = tmp_path / "disc_root_scoped_direct.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(roots):\n"
        "    for root in roots:\n"
        "        subprocess.run(['git', '-C', str(root), 'status', '--porcelain'])\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_discriminator_root_scoped_through_helper_not_flagged(tmp_path):
    """Discriminator 12, leg B -- the same fact one helper hop away, with the per-root group
    already spliced whole into the single spawn (`publish.py`'s real shape)."""
    fixture = tmp_path / "disc_root_scoped_helper.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def _batch(root, entries):\n"
        "    subprocess.run(['git', '-C', str(root), 'ls-tree', '--', *entries])\n"
        "\n"
        "def check(by_root):\n"
        "    for root, entries in by_root.items():\n"
        "        _batch(root, entries)\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_discriminator_root_scoped_declines_when_a_pathspec_also_varies(tmp_path):
    """Discriminator 12's load-bearing negative, and a REAL refutation rather than a synthetic
    one: this is `dispatch_checks.check_destructive_git_orphan`'s shape, which an earlier draft
    of leg B silenced. The root varies AND a per-item pathspec rides along in the same argv --
    that second dimension is genuinely batchable within one root, so the site must stay
    reported. Measured as collateral on 2026-08-19 and fixed by requiring the batch-dimension
    argument to be a bare Name rather than a per-iteration list literal."""
    fixture = tmp_path / "disc_root_scoped_pathspec.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def _run_git(args, cwd=None):\n"
        "    return subprocess.run(['git', *args], cwd=cwd)\n"
        "\n"
        "def check(targets, git_cwd):\n"
        "    for target in targets:\n"
        "        _run_git(['log', '%s..HEAD' % target], cwd=git_cwd)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [(site.enclosing, site.callee) for site in violations] == [("check", "_run_git")]


def test_discriminator_root_scoped_declines_a_non_git_program(tmp_path):
    """`-C` means "change directory" to git and something else entirely to another program. The
    `elts[0] == "git"` requirement is what makes the flag's semantics knowable; without it this
    suppresses on a coincidence of spelling."""
    fixture = tmp_path / "disc_root_scoped_non_git.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(roots):\n"
        "    for root in roots:\n"
        "        subprocess.run(['make', '-C', str(root), 'all'])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [site.enclosing for site in violations] == ["check"]


def _write_route_d_injected_runner_bare_name_collision(tmp_path):
    """Route d resolves `_find_injected_runner_name`'s result BY BARE NAME, repo-wide
    (`runner_name in index.direct_spawn_funcs`), mirroring route f's own documented imprecision
    -- never by tracing what the argument at this call site actually, eventually binds to. The
    real site (`consolidate_assemble.brief`'s `worktree_is_dirty(run_git, wt_path)`) is reached
    on route d-injected only because the UNRELATED `coordinator_core/ops/emit/sections/_shared.py
    :: run_git` -- a genuine direct git spawner sharing nothing but a name -- exists elsewhere in
    the same scanned tree. A single-file fixture with no such same-named function never resolves
    to route d at all: `route` stays `None` and the call is never a candidate violation, so a
    fixture asserting `== []` pins nothing (true whether or not the discriminator-12 leg
    suppresses it) and a fixture expecting a decline can never see one (nothing was ever flagged
    to decline). This companion file supplies that same-named, unrelated collision so route d
    actually fires here, matching the real tree's own resolution path."""
    (tmp_path / "_shared.py").write_text(
        "import subprocess\n"
        "\n"
        "def run_git(repo_root, *args):\n"
        "    return subprocess.run(['git', *args], cwd=str(repo_root))\n",
        encoding="utf-8",
    )


def test_discriminator_root_scoped_through_injected_runner_not_flagged(tmp_path):
    """Discriminator 12, leg B's route-d extension (`_root_scoped_through_injected_runner`,
    measured 2026-08-19 against `consolidate_assemble.brief`'s real
    `worktree_is_dirty(run_git, wt_path)` shape). `worktree_is_dirty` does not itself contain a
    recognized spawn call -- it calls its OWN parameter `run_git` -- and that parameter's real
    binding (`default_run_git`, resolved through the `run_git = run_git or default_run_git`
    seam in `brief`, the marked call's OWN enclosing function) DOES scope on a literal `git`
    argv0 via its `cwd` parameter. The `RunGit` calling convention's 2nd positional argument
    (`worktree_path`, filled at the marked call by `wt_path`) is the scope.

    `_write_route_d_injected_runner_bare_name_collision` supplies the unrelated same-named
    `run_git` spawner route d needs to resolve to before this leg is ever consulted -- without
    it, this call is never a candidate violation on route d and this assertion pins nothing."""
    _write_route_d_injected_runner_bare_name_collision(tmp_path)
    fixture = tmp_path / "disc_root_scoped_injected_runner.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def default_run_git(args, cwd):\n"
        "    return subprocess.run(['git', *args], cwd=str(cwd))\n"
        "\n"
        "def worktree_is_dirty(run_git, worktree_path):\n"
        "    return run_git(['status', '--porcelain'], worktree_path)\n"
        "\n"
        "def brief(worktree_paths, run_git=None):\n"
        "    run_git = run_git or default_run_git\n"
        "    for wt_path in worktree_paths:\n"
        "        worktree_is_dirty(run_git, wt_path)\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_discriminator_root_scoped_injected_runner_declines_a_non_git_program(tmp_path):
    """Declining half: the resolved runner (`default_run_program`) does not scope on a literal
    `git` argv0 -- `_resolve_named_git_scope_param` re-derives its scope params the normal way
    and finds none, so this leg must not guess from the runner-shaped NAME alone. Stays
    flagged. `_write_route_d_injected_runner_bare_name_collision` supplies the same-named
    `run_git` spawner elsewhere in the tree so the marked call actually reaches route d (and
    hence this leg) at all -- see that helper's docstring."""
    _write_route_d_injected_runner_bare_name_collision(tmp_path)
    fixture = tmp_path / "disc_root_scoped_injected_runner_non_git.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def default_run_program(args, cwd):\n"
        "    return subprocess.run(['make', *args], cwd=str(cwd))\n"
        "\n"
        "def worktree_is_dirty(run_git, worktree_path):\n"
        "    return run_git(['status', '--porcelain'], worktree_path)\n"
        "\n"
        "def brief(worktree_paths, run_git=None):\n"
        "    run_git = run_git or default_run_program\n"
        "    for wt_path in worktree_paths:\n"
        "        worktree_is_dirty(run_git, wt_path)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [(site.enclosing, site.callee) for site in violations] == [("brief", "worktree_is_dirty")]


def test_discriminator_root_scoped_injected_runner_declines_when_a_pathspec_also_varies(tmp_path):
    """Discriminator 12's precision constraint, applied to the injected-runner leg: the tainted
    name filling the scope slot (`wt_path`) must fill nothing else this call passes. Here a
    second tainted value (`pathspec`) co-varies with it and rides into the runner's own argv --
    a genuinely batchable-within-the-precision-sense dimension the suppressor must not silence.
    Stays flagged. `_write_route_d_injected_runner_bare_name_collision` supplies the same-named
    `run_git` spawner elsewhere in the tree so the marked call actually reaches route d (and
    hence this leg) at all -- see that helper's docstring."""
    _write_route_d_injected_runner_bare_name_collision(tmp_path)
    fixture = tmp_path / "disc_root_scoped_injected_runner_pathspec.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def default_run_git(args, cwd):\n"
        "    return subprocess.run(['git', *args], cwd=str(cwd))\n"
        "\n"
        "def worktree_is_dirty(run_git, worktree_path, pathspec):\n"
        "    return run_git(['status', pathspec], worktree_path)\n"
        "\n"
        "def brief(entries, run_git=None):\n"
        "    run_git = run_git or default_run_git\n"
        "    for wt_path, pathspec in entries:\n"
        "        worktree_is_dirty(run_git, wt_path, pathspec)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [(site.enclosing, site.callee) for site in violations] == [("brief", "worktree_is_dirty")]


def test_discriminator_argv0_through_helper_looks_past_a_fixed_interpreter(tmp_path):
    """The merge-order regression (2026-08-19). The helper's argv is bound to a local before the
    spawn, so BOTH binding maps hold the name `argv`: `_loop_argv0_bindings` holds the extracted
    head (`sys.executable`), `_loop_expr_bindings` holds the whole list. Merged with the head map
    last, the identity resolved to the interpreter and the script behind it was unreachable --
    which silently disabled discriminator 8 for the dominant spawn shape in this tree."""
    fixture = tmp_path / "disc_helper_past_interpreter.py"
    fixture.write_text(
        "import subprocess\n"
        "import sys\n"
        "\n"
        "def _spawn(root, rel):\n"
        "    script_path = root / rel\n"
        "    argv = [sys.executable, str(script_path), '--help']\n"
        "    subprocess.run(argv)\n"
        "\n"
        "def check(root, rels):\n"
        "    for rel in rels:\n"
        "        _spawn(root, rel)\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_discriminator_argv0_through_helper_declines_a_constant_program(tmp_path):
    """Discriminator 8's load-bearing negative, and the reason it is not the relaxation
    discriminator 6's route-a restriction exists to prevent. The helper's argv0 is the LITERAL
    `git`; only a git SUBCOMMAND varies with the loop. The program is identical every iteration,
    so the site is batchable and must stay flagged.

    This is the exact shape that produced a real false suppression when discriminator 6 was
    applied at a wrapper call site. 8 cannot repeat it: it requires the helper's own argv0 to be
    one of the helper's PARAMETERS before it ever looks at what the caller supplies, and a
    literal is not a parameter."""
    fixture = tmp_path / "disc_argv0_helper_constant.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def _run_git(args, root):\n"
        "    return subprocess.run(['git', '-C', root, *args])\n"
        "\n"
        "def check(verbs, root):\n"
        "    for verb in verbs:\n"
        "        _run_git([verb, '--quiet'], root)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [(site.enclosing, site.callee) for site in violations] == [("check", "_run_git")]


def test_discriminator_argv0_through_helper_declines_an_unfilled_slot(tmp_path):
    """Discriminator 8 declines when the caller does not supply the argv0 parameter at all --
    the helper falls back to its default, which is loop-invariant by construction, so the loop
    spawns ONE program N times and is genuinely batchable."""
    fixture = tmp_path / "disc_argv0_helper_default.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def _run_tool(args, exe='git'):\n"
        "    return subprocess.run([exe, *args])\n"
        "\n"
        "def check(paths):\n"
        "    for path in paths:\n"
        "        _run_tool(['add', path])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert [(site.enclosing, site.callee) for site in violations] == [("check", "_run_tool")]


def test_discriminator_varying_argv0_direct_loop_target_not_flagged(tmp_path):
    """Discriminator 6, AC5 first direction (direct): argv0 IS the loop target itself --
    each iteration spawns a different program by construction, so there is no single call to
    batch into."""
    fixture = tmp_path / "disc_argv0_direct.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(programs):\n"
        "    for program in programs:\n"
        "        subprocess.run([program, '--version'])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert violations == []


def test_discriminator_varying_argv0_via_assignment_hop_not_flagged(tmp_path):
    """Discriminator 6, AC5 first direction (assignment hop): argv0 is a local the loop body
    derives from the loop target in one assignment -- `resolved = _resolve_toolchain_tool(row)`
    -- matching the real `cruft_sweep.sweep_toolchain_caches` shape this discriminator retires
    from `_EXEMPT_SITES`."""
    fixture = tmp_path / "disc_argv0_hop.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def _resolve(row):\n"
        "    return row['path']\n"
        "\n"
        "def check(rows):\n"
        "    for row in rows:\n"
        "        resolved = _resolve(row)\n"
        "        subprocess.run([resolved, '--version'])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert violations == []


def test_discriminator_varying_argv0_invariant_argv0_still_flagged(tmp_path):
    """Discriminator 6, AC5 negative control: argv0 is LOOP-INVARIANT (always `sys.executable`
    in spirit, here a fixed literal) while a later argv element varies with the loop target --
    the batchable shape this whole module exists to flag. Proves the discriminator checks
    argv0 specifically and does not just disable route a wholesale."""
    fixture = tmp_path / "disc_argv0_invariant.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(names):\n"
        "    for name in names:\n"
        "        subprocess.run(['git', 'show', name])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert len(violations) == 1
    assert violations[0].route == "a-direct"


def test_discriminator_varying_argv0_unpacked_constant_argv0_still_flagged(tmp_path):
    """Discriminator 6, the negative control that actually bites (Review: code-reviewer
    2026-08-17, BLOCK + nit). The sibling control above uses a LITERAL argv0, so `_argv0_expr`
    returns a `Constant` with no names at all and the test passes whether or not the taint set
    is computed correctly -- it cannot catch an over-broad one.

    This one puts a NAME in argv0 that the taint pass must decline to taint: `b` is bound, in a
    tuple unpacking that also binds the loop target to `a`, to a constant. Before the
    element-wise correlation in `_paired_assign_elements`, the whole-RHS rule tainted both names
    and this real per-item spawn was silently suppressed. Regression test for exactly that."""
    fixture = tmp_path / "disc_argv0_unpacked_constant.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(items):\n"
        "    for item in items:\n"
        "        a, b = item, 'always-git'\n"
        "        subprocess.run([b, '--version', a])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert len(violations) == 1, (
        "argv0 `b` is bound to a constant in the same unpacking that binds the loop target to "
        "`a` -- suppressing this site means the taint set is over-broad again"
    )
    assert violations[0].route == "a-direct"


def test_deep_tail_not_flagged(tmp_path):
    """Negative control: a callee that only TRANSITIVELY reaches a git spawn (two hops) must
    not be flagged -- this collector is restricted to the high-precision stratum by design."""
    fixture = tmp_path / "deep_tail.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def _git_add(path):\n"
        "    subprocess.run(['git', 'add', path], cwd='/repo')\n"
        "\n"
        "def _stage_one(path):\n"
        "    _git_add(path)\n"
        "\n"
        "def check(paths):\n"
        "    for p in paths:\n"
        "        _stage_one(p)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert violations == []


def _index_transform_fixture(tmp_path):
    fixture = tmp_path / "transform_target.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(paths):\n"
        "    for p in paths:\n"
        "        subprocess.run(['git', 'add', p], cwd='/repo')\n",
        encoding="utf-8",
    )
    return fixture


def test_index_transform_none_is_byte_identical_to_today(tmp_path):
    """AC9.1: `index_transform=None` (the default) returns exactly what the untransformed call
    returns today -- the no-regression pin for this chunk's whole change."""
    _index_transform_fixture(tmp_path)
    baseline = find_unbatched_per_item_spawns((tmp_path,))
    explicit_none = find_unbatched_per_item_spawns((tmp_path,), index_transform=None)
    assert explicit_none == baseline
    assert len(baseline) == 1


def test_index_transform_identity_matches_untransformed_key_set(tmp_path):
    """AC9.2: an identity `index_transform` returns the identical key set to the untransformed
    call -- the positive leg this plan's later widened-index use builds on."""
    _index_transform_fixture(tmp_path)
    baseline = find_unbatched_per_item_spawns((tmp_path,))
    identity_transformed = find_unbatched_per_item_spawns(
        (tmp_path,), index_transform=lambda idx: idx
    )
    assert {(v.path, v.lineno) for v in identity_transformed} == {
        (v.path, v.lineno) for v in baseline
    }


def test_index_transform_forecloses_the_cross_parse_configuration(tmp_path):
    """AC9.3: pins that the cross-parse configuration this chunk replaces is UNREACHABLE
    through the public signature. The unsound predecessor shape let a caller hand in an
    `index` built from a DIFFERENT parse than the `ast.Call` nodes being visited -- measured
    on the live gate as 31 reported sites where the sound collector reports 26 (5 spurious,
    none lost). `index_transform` closes this by construction: it is always applied to the
    index this function builds from its OWN single parse of its OWN records (see the
    `elif index is None` branch in `find_unbatched_per_item_spawns`), never to a
    caller-supplied `index` -- so there is no argument through which a foreign-parse index
    can reach the discriminators. This test does not re-assert the 26-vs-31 divergence (the
    transform shape makes it unconstructible, so there is nothing left to measure) -- it pins
    that passing BOTH `index` and `index_transform` together still resolves against the
    self-built index, not the caller-supplied one, which is the property that keeps the
    configuration unreachable. A future signature change that lets `index_transform` see a
    caller-supplied `index` reopens exactly this cross-parse hole and must fail this test
    loudly."""
    _index_transform_fixture(tmp_path)

    foreign_index = _build_func_index([])  # built from a different (empty) parse

    seen_index_identity = []

    def _capturing_transform(idx):
        seen_index_identity.append(idx)
        return idx

    result_with_foreign_index_also_passed = find_unbatched_per_item_spawns(
        (tmp_path,), index=foreign_index, index_transform=_capturing_transform
    )

    assert len(seen_index_identity) == 1
    assert seen_index_identity[0] is not foreign_index, (
        "index_transform was applied to the caller-supplied `index` instead of a fresh "
        "self-built index -- this reopens the cross-parse configuration AC9.3 exists to "
        "foreclose: the visited ast.Call nodes and the index nodes would no longer share "
        "one parse."
    )
    baseline = find_unbatched_per_item_spawns((tmp_path,))
    assert {(v.path, v.lineno) for v in result_with_foreign_index_also_passed} == {
        (v.path, v.lineno) for v in baseline
    }


def test_gate_ignores_test_tree_paths(tmp_path):
    """Negative control: a planted per-item git spawn under a `tests/` directory (routed
    through the shared `is_test_tree_site` predicate) must not be flagged."""
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    fixture = test_dir / "test_something.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(paths):\n"
        "    for p in paths:\n"
        "        subprocess.run(['git', 'add', p], cwd='/repo')\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    assert violations == []


def test_gate_scope_includes_coordinator_bin():
    """AC4: coordinator/bin/ must be in the collector's scope constant."""
    assert "coordinator/bin" in _GATE_SCOPE_ROOTS


def test_reentrancy_sentinel_raises_loudly_if_self_scanned(tmp_path, monkeypatch):
    """Anti-scope 20: prove the sentinel actually fires, rather than trusting the filtering it
    double-checks. Simulates a discovery result that (wrongly) includes this gate's own file."""
    poisoned = [("coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py", _THIS_FILE)]
    with pytest.raises(RuntimeError, match="re-entrancy"):
        _assert_not_self_scanned(poisoned)


def test_gate_does_not_scan_its_own_file_in_a_real_pass(tmp_path):
    """Companion positive control: a real discovery pass over this file's own directory does
    NOT trip the sentinel, because `is_test_tree_site` correctly filters it first -- proving
    the sentinel and the filtering it double-checks agree in the ordinary case."""
    files = _discover_scope_files((_REPO_ROOT / "coordinator_core" / "tests",))
    assert all(f != _THIS_FILE for _rel, f in files)


def test_route_g_positive_two_hop_forwarded_runner(tmp_path):
    """Route g, `g-param-forwarded` shape: a runner forwarded through TWO parameter hops into
    a loop, where neither the parameter nor the callee is named `run`/`git`/`spawn` -- the
    real shape (`_collect_discharging_range_shas`'s `resolve_range_shas` parameter, forwarding
    into `_record_membership_shas`) that defeats route d twice over: the naming-convention
    filter never candidates it (root cause 1), and even past that, by-identifier resolution
    would still miss it because the identifier is renamed at each hop (root cause 2)."""
    (tmp_path / "hop.py").write_text(
        "import subprocess\n"
        "\n"
        "def _resolve_range_shas(sha):\n"
        "    subprocess.run(['git', 'rev-list', sha], cwd='/repo')\n"
        "\n"
        "def _record_membership_shas(sha, get_range):\n"
        "    get_range(sha)\n"
        "\n"
        "def _collect_discharging_range_shas(shas, resolve_range_shas):\n"
        "    for sha in shas:\n"
        "        _record_membership_shas(sha, resolve_range_shas)\n"
        "\n"
        "def chain_partition_verdict_discharged(shas):\n"
        "    _collect_discharging_range_shas(shas, resolve_range_shas=_resolve_range_shas)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    matches = [v for v in violations if v.route == "g-forwarded-runner"]
    assert len(matches) == 1
    assert matches[0].enclosing == "_collect_discharging_range_shas"
    assert matches[0].callee == "_record_membership_shas"


def test_route_g_positive_direct_call_of_spawn_bearing_param(tmp_path):
    """Route g, `g-param-called` shape: the loop body calls a spawn-bearing parameter of its
    enclosing function directly (leg 1's base case), tainted by a REAL call-site argument
    (leg 2) rather than a parameter default -- distinguishing this from route f, which
    resolves a runner bound as a DEFAULT, not one passed in at a call site."""
    (tmp_path / "direct.py").write_text(
        "import subprocess\n"
        "\n"
        "def _do_spawn(sha):\n"
        "    subprocess.run(['git', 'show', sha], cwd='/repo')\n"
        "\n"
        "def sweep(shas, injected):\n"
        "    for sha in shas:\n"
        "        injected(sha)\n"
        "\n"
        "def driver(shas):\n"
        "    sweep(shas, injected=_do_spawn)\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_spawns((tmp_path,))
    matches = [v for v in violations if v.route == "g-forwarded-runner"]
    assert len(matches) == 1
    assert matches[0].enclosing == "sweep"
    assert matches[0].callee == "injected"


def test_route_g_negative_invoked_without_taint(tmp_path):
    """Negative: leg 1 (invoked) without leg 2 (tainted) -- a parameter called directly inside
    the loop, but no real call site ever passes a spawner into it. Spawn-bearing requires BOTH
    legs; leg 1 alone would flag every dependency-injection seam regardless of what it is
    actually used for."""
    (tmp_path / "invoked_only.py").write_text(
        "def _record(item):\n"
        "    return item\n"
        "\n"
        "def sweep(items, injected):\n"
        "    for item in items:\n"
        "        injected(item)\n"
        "\n"
        "def driver(items):\n"
        "    sweep(items, injected=_record)\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_route_g_negative_tainted_without_invocation(tmp_path):
    """Negative: leg 2 (tainted) without leg 1 (invoked) -- a spawner flows into a parameter
    that the loop body never actually calls (referenced, not invoked). Not spawn-bearing."""
    (tmp_path / "tainted_only.py").write_text(
        "import subprocess\n"
        "\n"
        "def _do_spawn(sha):\n"
        "    subprocess.run(['git', 'show', sha], cwd='/repo')\n"
        "\n"
        "def sweep(shas, injected):\n"
        "    for sha in shas:\n"
        "        print(sha, injected)\n"
        "\n"
        "def driver(shas):\n"
        "    sweep(shas, injected=_do_spawn)\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_route_g_negative_constant_literal_loop_not_flagged(tmp_path):
    """Discriminator 2 still suppresses route g: a spawn-bearing parameter called inside a
    loop over a module-level literal sequence is not flagged -- the loop-qualification
    discriminators apply ahead of every route, route g included."""
    (tmp_path / "literal_loop.py").write_text(
        "import subprocess\n"
        "\n"
        "_BASES = ('origin/main', 'origin/dev')\n"
        "\n"
        "def _do_spawn(ref):\n"
        "    subprocess.run(['git', 'show', ref], cwd='/repo')\n"
        "\n"
        "def sweep(injected):\n"
        "    for base in _BASES:\n"
        "        injected(base)\n"
        "\n"
        "def driver():\n"
        "    sweep(injected=_do_spawn)\n",
        encoding="utf-8",
    )
    assert find_unbatched_per_item_spawns((tmp_path,)) == []


def test_route_g_pin_against_live_repo():
    """GRADUATED 2026-08-19, the visible deliberate edit the prior revision of this docstring
    demanded rather than silent drift. This pinned exactly two keys --
    `directives_review._collect_discharging_range_shas` and its sibling
    `chain_partition_execution_basis_report`, both forwarding a `_record_membership_shas`
    runner routes d/f cannot see. Both are now FIXED, not suppressed and not exempted:
    `97783e5d3` taught `_CARET_RANGE_RE` that `<A>~1..<B>` denotes the same commit as
    `<A>^..<B>`, so the 2 `~1`-spelled records in a 3785-record corpus resolve statically like
    the other 3783 and the last per-record `git rev-list` on the close path is gone.

    The assertion INVERTS rather than being deleted: route g must now find NOTHING repo-wide,
    which makes this a regrowth guard on a class that is currently at zero. Route g's own
    behaviour stays covered by the five planted-fixture tests above (two positive, three
    negative) -- an empty live pin proves the repo is clean, never that the route works, and
    those two claims must not be confused. A NEW key appearing here is a real two-hop forwarded
    runner and must be read as one, not absorbed by re-pinning it."""
    violations = find_unbatched_per_item_spawns(_gate_scope_paths())
    route_g_keys = {site.key for site in violations if site.route == "g-forwarded-runner"}
    assert route_g_keys == set()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
