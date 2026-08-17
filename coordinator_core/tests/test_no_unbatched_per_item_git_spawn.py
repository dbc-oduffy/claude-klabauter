"""Amplification collector (G1): sibling to `spawn_policy`, resolving generic runners and
injected runners -- the third state neither existing gate expresses.

Spec backlink: `pln-kill-the-n-1-git-spawn-class-a-88897a`,
`## Tasks` chunk G1 (this collector) and G2 (the two assertions this collector feeds, landed
in a later wave over this same file). Widened past git by
`docs/plans/2026-08-15-composition-invocation-budgets.md` chunk C11 (AC11).

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

STRUCTURAL DISCRIMINATORS -- SIX TOTAL, not the three this heading historically named (measured:
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
change does not take on fixing. Discriminator 6 (varying argv0, added 2026-08-17) is listed here
in full because it belongs to the same "does this loop even qualify" family as 1-3: it is NOT
covered by the 32.4%/4.2% figures above (those predate it by nine days) and its own FP rate has
never been measured -- state plainly rather than let it inherit a number it did not earn.

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

SIX DETECTION ROUTES (per gate-substrate.md Task C), restricted to the high-precision stratum:

  a-direct       -- the call itself is a recognized `subprocess`/`os`/`asyncio` spawn (via
                    `sites_in_source`). Matched on the detected spawn LINE *and* a recognized
                    spawn-API callee name (`_SPAWN_API_NAMES`), because a line routinely carries
                    a second call -- `subprocess.call(argv, **no_console_passthrough_kwargs())`
                    -- and matching on line alone reported the helper as the site.
  b-local-helper -- the callee is a function DEFINED IN THE SAME MODULE whose own body directly
                    contains a spawn site.
  c-cross-module -- the callee is imported (`from X import name`) and resolves, via a repo-wide
                    name index built over the same scope, to a function in another module whose
                    own body directly contains a spawn site.
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

  - Route b/c/e resolution is by function NAME only, not full import-graph resolution -- a
    same-named function in two unrelated modules can collide (the prototype's own `dict.get()`
    mis-resolution artifact, in the deep tail it excludes). Accepted here because routes b/c/e
    are restricted to the high-precision stratum, where this collector independently verifies the
    resolved function's body via `sites_in_source`/spawn-detection before counting a route, not
    by name alone.
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

_RUNNER_KWARG_NAMES: frozenset[str] = frozenset(
    {"run", "runner", "git", "git_runner", "run_git", "spawn"}
)
_RUNNER_NAME_PREFIXES: tuple[str, ...] = ("run", "git", "spawn")

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
_EXEMPT_SITES: set[tuple[str, str, str]] = {
    # 2026-08-17 -- the spawn loop IS the measurement. `_spawn_n_processes` times N sequential
    # `python -c "import <module>"` children as the fan-in arm's control; batching the N imports
    # into one child measures a different quantity and voids the comparison the module exists for.
    ('coordinator_core/benchmarks/shim_fanin_measure.py', '_spawn_n_processes', 'run'),
    # 2026-08-17 -- one FRESH login shell per entrypoint is the subject under test: the probe
    # reports how each entrypoint resolves on the PATH a login shell builds. The two spawns per
    # entrypoint were already folded into one combined `-lc` payload; folding ACROSS entrypoints
    # would report one shell's resolution N times. NOT decided by the varying-argv0 discriminator
    # -- `shell` (argv0) is loop-invariant here; only the script text varies, which the four
    # measured argv0 shapes do not reach.
    ('coordinator_core/install/path_resolution_report.py', '_check_posix', 'run'),
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
    #             | "e-generic-runner" | "f-default-runner"
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


def _loop_argv0_bindings(loop: ast.AST) -> dict[str, ast.expr]:
    """Name -> its resolved `argv0` expression, for every single-target `ast.Assign`/
    `ast.AnnAssign` in `loop`'s subtree whose RHS itself resolves via `_argv0_expr` -- the
    one-hop intermediate-variable idiom (`dry_argv = [resolved] + list(dry_run_argv)`) that a
    call site passing the bare `Name` (`subprocess.run(dry_argv, ...)`) needs resolved before
    `_argv0_expr` can see a List/BinOp shape at all. Threaded through as `bindings` so a chain
    of two such assignments resolves transitively; real sites only ever use one hop."""
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


def _build_func_index(records: list[_FileRecord]) -> _FuncIndex:
    """One pass over the scoped corpus, building the repo-wide name index routes b/c/d/e
    resolve against. Single-hop only -- no fixpoint, no recursion -- so there is no cycle for
    the re-entrancy sentinel above to guard beyond the self-scan check already applied to the
    files `records` was built from.

    Consumes pre-computed `_FileRecord`s (G3) rather than re-reading/re-parsing/re-detecting
    each file itself -- see `_load_file_records`'s docstring."""
    index = _FuncIndex()

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
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported.add(alias.asname or alias.name)
        index.imported_names_by_file[relpath] = imported

        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = node.name

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

    return index


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
        self._loop_taint_stack: list[frozenset[str]] = []
        self._loop_argv0_bindings_stack: list[dict[str, ast.expr]] = []

    def _scope_boundary(self, node: ast.AST) -> None:
        saved = self._in_qualifying_loop_depth
        self._in_qualifying_loop_depth = 0
        self.generic_visit(node)
        self._in_qualifying_loop_depth = saved

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope_boundary(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scope_boundary(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._scope_boundary(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope_boundary(node)

    def _visit_for(self, node: ast.For | ast.AsyncFor) -> None:
        # Discriminator 1: iter is evaluated outside any loop context this loop introduces.
        self.visit(node.iter)
        if _is_constant_literal_iterable(node.iter, self._literal_names) or _is_chunking_stride_iterable(node.iter):
            # Discriminators 2 and 4 (chunking-stride): excluded wholesale -- body still
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
        for stmt in node.body:
            self.visit(stmt)
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
        if _is_constant_literal_iterable(generators[0].iter, self._literal_names):
            # Discriminator 2, first generator only -- see module docstring blind spots.
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
        for gen in generators[1:]:
            self.visit(gen.iter)
            for if_clause in gen.ifs:
                self.visit(if_clause)
        for if_clause in generators[0].ifs:
            self.visit(if_clause)
        self._visit_comp_elt(node)
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


def _find_injected_runner_name(call: ast.Call) -> str | None:
    """Route d: a bare-`Name` argument in a runner-shaped position. Checked over both keyword
    and positional arguments -- see module docstring's route-d description.

    Resolution against `index.direct_spawn_funcs` is BY NAME: the identifier passed at
    THIS call site must literally match the target function's own defined name. A default-
    parameter alias one hop up the call chain (`def check(shas, run=_run): ...; g(run=run)`,
    where the passed identifier is `run`, not `_run`) is not traced and will be missed -- the
    same by-name-only limitation the module docstring's blind-spots section already states for
    routes b/c/e."""
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
    roots: tuple[pathlib.Path, ...], index: _FuncIndex | None = None
) -> list[AmpSite]:
    """Core collector. Walk `roots` (via the shared `discover_source_files` traversal),
    restricted to the high-precision stratum (callee directly contains a spawn, one hop),
    applying all three structural discriminators and all six detection routes described in
    the module docstring.

    `index`, when provided, lets a caller reuse one repo-wide `_FuncIndex` across multiple
    calls (e.g. G2's standing assertion and its `designed_red` worklist sharing one build) --
    when omitted, a fresh index is built over the same `roots`, which is what every self-test
    below does.
    """
    files = _discover_scope_files(roots)
    records = _load_file_records(files)
    if index is None:
        index = _build_func_index(records)

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

                # route c-cross-module: imported name resolves (repo-wide, by name) to a
                # function elsewhere that directly spawns.
                if (
                    route is None
                    and callee in imported_here
                    and callee in index.direct_spawn_funcs
                    and not any(r == relpath for r, _ in index.direct_spawn_funcs[callee])
                ):
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
                if runner_name is not None and runner_name in index.direct_spawn_funcs:
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
_KNOWN_SITES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ('coordinator/bin/age-sweep-lessons.py', 'main', 'run'),
        ('coordinator/bin/check-mcp-versions.py', 'main', '_npm_latest'),
        ('coordinator/bin/coordinator-doc-new.py', '_resolve_from_repo', '_machine_local_get'),
        ('coordinator/bin/coordinator-harvest-deferrals.py', '_harvest', '_run_lesson_promote'),
        ('coordinator/bin/coordinator-harvest-deferrals.py', '_harvest', '_run_queue_append'),
        ('coordinator/bin/coordinator-safe-commit.py', 'do_blanket', '_git_reset_unstage'),
        ('coordinator/bin/coordinator-safe-commit.py', 'do_scope_from', '_validate_pathspec'),
        ('coordinator/bin/coordinator-safe-commit.py', 'do_scoped', '_validate_pathspec'),
        ('coordinator/bin/cross-repo-memo.py', '_verify_delivery_landed', 'run'),
        ('coordinator/bin/emit-goal-from-artifact.py', 'main', 'run'),
        ('coordinator/bin/lib/cli_shared.py', 'resolve_from_repo', 'machine_local_get'),
        ('coordinator/bin/merge-release-notes-derive.py', '_contains_all', '_git'),
        (
            'coordinator/bin/merge-release-notes-derive.py',
            'cmd_reconcile_sweep',
            '_run_sibling_cli',
        ),
        ('coordinator/bin/migrate-archive-week-changelogs.py', 'run', 'git_mv'),
        ('coordinator/bin/migrate-bug-backlog.py', 'run_apply', 'run'),
        ('coordinator/bin/migrate-debt-backlog.py', 'run_apply', 'run'),
        ('coordinator/bin/migrate-improvement-queue-project.py', 'run_apply', 'run'),
        ('coordinator/bin/migrate-improvement-queue-universals.py', 'run_apply', 'run'),
        ('coordinator/bin/percolate-gate.py', '_git_log_batched', 'run'),
        ('coordinator/bin/percolate-round.py', '_filter_commit_pathspec', '_dest_path_exists'),
        ('coordinator/bin/publish.py', '_materialize_inject_srcs', '_git_materialize_ref'),
        (
            'coordinator/bin/publish.py',
            '_publish_relevant_allowlist_leg',
            '_git_ls_tree_entries_files',
        ),
        (
            'coordinator/bin/publish.py',
            'dispatch_end_of_run_argv_parity_gate',
            '_argv_parity_pairing_origin',
        ),
        ('coordinator/bin/publish.py', 'main', '_git_head'),
        ('coordinator/bin/publish.py', 'main', '_is_git_repo'),
        (
            'coordinator/bin/reap-orphaned-in-flight-handoffs.py',
            'main',
            '_has_live_children_exit_code',
        ),
        ('coordinator/bin/reap-orphaned-in-flight-handoffs.py', 'main', '_run_archive_stamp_cli'),
        ('coordinator/bin/reap-stale-subagent-sidecars.py', 'main', '_is_tracked'),
        ('coordinator/bin/refresh-plugin-live-install.py', '_handle_default', '_git'),
        ('coordinator/bin/refresh-plugin-live-install.py', '_interactive_gate', '_git'),
        (
            'coordinator/bin/workday-complete-close.py',
            'cmd_backfill_dispatch_rows',
            '_dispatch_step9_row',
        ),
        ('coordinator/bin/workday-complete-reconcile.py', 'run_completion_reconcile', '_git_add'),
        (
            'coordinator/bin/workday-complete-reconcile.py',
            'run_completion_reconcile',
            '_run_reconcile_append',
        ),
        ('coordinator/bin/workday-complete-step9-append-changelog.py', 'main', 'run'),
        ('coordinator/bin/workday-start-reconcile-sweep.py', 'run_sweep', '_run_reconcile_helper'),
        (
            'coordinator/bin/workweek-complete-close.py',
            'run_reconcile_sweep',
            '_run_reconcile_helper',
        ),
        (
            'coordinator_core/bash_guards/block_subagent_commit.py',
            '_fold_template_is_bounded',
            'int',
        ),
        (
            'coordinator_core/bash_guards/block_subagent_commit.py',
            '_git_commit_agent_may_commit',
            '_pathspec_element_is_sweeping',
        ),
        ('coordinator_core/bash_guards/commit_tripwires.py', 'check_bin_sh_polyglot', 'join'),
        (
            'coordinator_core/bash_guards/dispatch_checks.py',
            '_check_destructive_git_revert_full',
            '_check_destructive_git_revert_full',
        ),
        (
            'coordinator_core/bash_guards/dispatch_checks.py',
            '_check_destructive_git_revert_full',
            '_run_git',
        ),
        ('coordinator_core/bash_guards/dispatch_checks.py', '_is_hazard_repo', '_paths_match'),
        (
            'coordinator_core/bash_guards/dispatch_checks.py',
            '_no_verify_rescan_shell_c_and_heredoc',
            'check_no_verify',
        ),
        (
            'coordinator_core/bash_guards/dispatch_checks.py',
            'check_blanket_git_add',
            '_paths_match',
        ),
        (
            'coordinator_core/bash_guards/dispatch_checks.py',
            'check_destructive_git_clean',
            '_run_git',
        ),
        (
            'coordinator_core/bash_guards/dispatch_checks.py',
            'check_destructive_git_clean',
            'operator_override_note',
        ),
        (
            'coordinator_core/bash_guards/dispatch_checks.py',
            'check_destructive_git_orphan',
            '_run_git',
        ),
        ('coordinator_core/bash_guards/dispatch_checks.py', 'check_destructive_rm', '_run_git'),
        (
            'coordinator_core/bash_guards/dispatch_checks.py',
            'check_git_commit_safe_commit_advise',
            'operator_override_note',
        ),
        ('coordinator_core/bash_guards/dispatch_checks.py', 'check_validate_commit', '_run_git'),
        ('coordinator_core/bash_guards/dispatch_checks.py', 'check_validate_commit', 'join'),
        ('coordinator_core/benchmarks/floor.py', 'measure_floor', 'time_invocation'),
        ('coordinator_core/benchmarks/harness.py', '_collect_samples', 'time_invocation'),
        ('coordinator_core/benchmarks/measure_read_events.py', 'measure', '_time_probe'),
        ('coordinator_core/benchmarks/measure_render_status.py', 'measure', '_time_probe'),
        (
            'coordinator_core/benchmarks/measure_render_status.py',
            'measure_bare_import',
            '_time_bare_import',
        ),
        ('coordinator_core/consolidate_assemble/__init__.py', 'brief', 'branch_reachable'),
        ('coordinator_core/consolidate_assemble/__init__.py', 'brief', 'inspect_commits'),
        ('coordinator_core/consolidate_assemble/__init__.py', 'brief', 'tip_author'),
        ('coordinator_core/consolidate_assemble/__init__.py', 'brief', 'unique_commits'),
        ('coordinator_core/consolidate_assemble/__init__.py', 'brief', 'worktree_is_dirty'),
        (
            'coordinator_core/consolidate_assemble/apply.py',
            '_dispatch_cherry_pick_and_delete',
            '_run_git',
        ),
        (
            'coordinator_core/coverage.py',
            '_derive_dag_chain_set',
            '_add_commit_touched_file_count',
        ),
        ('coordinator_core/coverage.py', '_reviewed_via_graph_walk', '_run'),
        (
            'coordinator_core/distill/curation_status.py',
            'compute_curation_status',
            'active_reference_guard',
        ),
        ('coordinator_core/distill/sidecar_sweep.py', 'sweep_sidecars', 'active_reference_guard'),
        (
            'coordinator_core/execute_plan_assemble/close_out_and_stamp.py',
            '_first_deliverable_commit_range_base',
            '_run_git',
        ),
        (
            'coordinator_core/frontmatter/schema_drift_watch.py',
            '_scan',
            'check_schema_drift_advisory',
        ),
        (
            'coordinator_core/hooks/subagent_fabrication_check.py',
            '_targets_changed',
            '_git_porcelain_for_path',
        ),
        ('coordinator_core/install/first_run.py', '_seed_machine_local_registry', '_run'),
        ('coordinator_core/install/maximalist.py', '_defender_offer', '_run'),
        ('coordinator_core/install/maximalist.py', '_run_body', '_run_compileall'),
        ('coordinator_core/install/prereq_probe.py', 'probe_clone_auth', '_run'),
        (
            'coordinator_core/install/uninstall_legs.py',
            'uninstall_reverse_git_config_group',
            'config_unset',
        ),
        ('coordinator_core/ops/agent_worktree_sweep.py', '_sweep_one', '_cherry_pick_abort'),
        ('coordinator_core/ops/agent_worktree_sweep.py', '_sweep_one', '_cherry_pick_with_env'),
        ('coordinator_core/ops/backfill_initiative_fk.py', '_process_pairs', 'run'),
        ('coordinator_core/ops/bootstrap_orchestrate.py', 'main', '_git'),
        ('coordinator_core/ops/bootstrap_orchestrate.py', 'main', 'run'),
        ('coordinator_core/ops/central_run_due.py', 'main', '_count_universals'),
        (
            'coordinator_core/ops/ceremony/detached_render_commit.py',
            'commit_own_artifact',
            '_run_git',
        ),
        ('coordinator_core/ops/ceremony/scoped_git_commit.py', '_remote_sha_state', 'run'),
        (
            'coordinator_core/ops/ceremony/tail_ops.py',
            'fire_archive_sweeps_detached',
            'spawn_detached',
        ),
        (
            'coordinator_core/ops/ceremony/tail_ops.py',
            'fire_tracker_and_roadmap_detached',
            'spawn_detached',
        ),
        ('coordinator_core/ops/check_atlas_watch_drift.py', 'run', '_watch_line'),
        (
            'coordinator_core/ops/check_machine_local_regeneratability.py',
            'main',
            '_ladder_resolves',
        ),
        ('coordinator_core/ops/configure_git.py', 'main', '_git_config_get'),
        ('coordinator_core/ops/configure_git.py', 'main', '_git_config_set'),
        ('coordinator_core/ops/cruft_sweep.py', 'sweep_empty_toplevel_dirs', '_delete_path'),
        ('coordinator_core/ops/cruft_sweep.py', 'sweep_harness', '_delete_path'),
        ('coordinator_core/ops/cruft_sweep.py', 'sweep_orphans', '_delete_path'),
        ('coordinator_core/ops/cruft_sweep.py', 'sweep_scratch', '_delete_path'),
        ('coordinator_core/ops/cruft_sweep.py', 'sweep_subagent_sandbox_files', '_delete_path'),
        (
            'coordinator_core/ops/cutover_gate.py',
            '_reverify_test_node_ids_batch',
            '_run_pytest_batch',
        ),
        (
            'coordinator_core/ops/distill_apply_disposal.py',
            '_delete_tracked_and_append_log',
            '_run_git',
        ),
        (
            'coordinator_core/ops/distill_apply_disposal.py',
            '_write_denormalizations',
            '_is_tracked',
        ),
        (
            'coordinator_core/ops/distill_apply_disposal.py',
            'apply_disposal_manifest',
            '_is_tracked',
        ),
        ('coordinator_core/ops/emit/envelope.py', 'main', '_commit_age_label'),
        ('coordinator_core/ops/emit/envelope.py', 'main', 'resolve_ref'),
        ('coordinator_core/ops/find_polluter.py', 'main', '_existence_detail'),
        ('coordinator_core/ops/find_polluter.py', 'main', 'run'),
        # DELIBERATE, and not to be "fixed" by batching (docs/plans/
        # 2026-08-11-resync-leaves-a-bare-staged-deletion-whe.md, AC6): the resync annotates
        # `index_resync_failed` onto the individual acted[] / reaped[] item that failed. One
        # batched call cannot say WHICH path failed, which would trade a visible defect for an
        # invisible one. Re-keyed 2026-08-11 when the two post-commit main-index resyncs moved
        # out of archive_and_commit / rm_and_commit into their own module-level functions;
        # `run_git` is the extracted injectable seam, defaulting to _update_index_with_retry.
        #
        # Found by route f since 2026-08-16 (AC11). Until then these two rows were reported
        # only because `run_git` collided by name with an unrelated single-parameter function
        # in another module -- a true site standing on a false route, which is why widening
        # the collector had to add route f rather than simply drop them.
        ('coordinator_core/ops/fleet/_common.py', '_resync_main_index_for_moves', 'run_git'),
        ('coordinator_core/ops/fleet/_common.py', '_resync_main_index_for_reaps', 'run_git'),
        (
            'coordinator_core/ops/fleet/_common.py',
            '_update_index_with_retry',
            'create_subprocess_exec',
        ),
        ('coordinator_core/ops/fleet/_common.py', 'archive_and_commit', 'create_subprocess_exec'),
        ('coordinator_core/ops/fleet/_common.py', 'rm_and_commit', 'create_subprocess_exec'),
        ('coordinator_core/ops/fleet/_findings_reap.py', 'reap_findings', '_is_tracked'),
        ('coordinator_core/ops/fleet/archive_plans.py', '_handle_act', '_plan_worktree_dirty'),
        ('coordinator_core/ops/fleet/archive_plans.py', '_handle_preview', '_plan_worktree_dirty'),
        ('coordinator_core/ops/install_health_run.py', '_run_legs', 'call'),
        ('coordinator_core/ops/learn_lessons_config_update.py', 'main', '_machine_local_get'),
        ('coordinator_core/ops/learn_lessons_roots.py', '_registry_roots', '_machine_local_run'),
        ('coordinator_core/ops/migrate_branch_canonical_case.py', '_migrate', '_git'),
        ('coordinator_core/ops/migrate_completion_log_legacy.py', 'main', '_git_mv'),
        ('coordinator_core/ops/migrate_cross_repo_layout.py', 'main', '_move_one'),
        ('coordinator_core/ops/normalize_claimed_frontmatter.py', 'main', 'get_tracked_files'),
        ('coordinator_core/ops/orphan_branch_sweep.py', 'main', '_git'),
        ('coordinator_core/ops/orphan_branch_sweep.py', 'main', '_run'),
        (
            'coordinator_core/ops/percolate_preflight_scratch_publish.py',
            'check_allowlist_string',
            '_run_child',
        ),
        (
            'coordinator_core/ops/plan_suggest_completion_steps.py',
            '_plans_with_review_trail_coverage',
            '_resolve_range_shas',
        ),
        (
            'coordinator_core/ops/plan_suggest_completion_steps.py',
            'suggest_completion_steps',
            '_plan_touching_shas',
        ),
        (
            'coordinator_core/ops/promote_shipped_in_flight_stubs.py',
            '_run_promotions',
            '_git_common_dir',
        ),
        ('coordinator_core/ops/register_discovered_repos.py', 'main', 'run'),
        ('coordinator_core/ops/render_template_tree.py', 'main', 'run'),
        (
            'coordinator_core/ops/review_brightline_gate.py',
            '_compute_chain_oracle',
            '_derive_dag_chain_set',
        ),
        ('coordinator_core/ops/review_coverage_core.py', 'build_reviewed_set', '_run'),
        ('coordinator_core/ops/review_coverage_core.py', 'build_segments', '_run'),
        (
            'coordinator_core/ops/review_trail_readjudication_report.py',
            'compute_readjudication_report',
            '_full_range_shas',
        ),
        ('coordinator_core/ops/run_pre_ci_hooks.py', '_run_pre_ci_hooks', 'run'),
        ('coordinator_core/ops/run_shellcheck_sweep.py', 'run_shellcheck_sweep', '_lint_one_file'),
        (
            'coordinator_core/ops/session/boot_sweep.py',
            '_sweep_consumed_handoffs',
            '_shipped_in_resolvable',
        ),
        (
            'coordinator_core/ops/setup_chain_walker.py',
            '_sibling_fallback',
            '_functional_probe_ok',
        ),
        (
            'coordinator_core/ops/setup_chain_walker.py',
            'command_succeeds_native',
            '_run_probe_argv',
        ),
        ('coordinator_core/ops/setup_chain_walker.py', 'dep_probe_all', 'dep_probe'),
        ('coordinator_core/ops/staleness_git.py', 'commits_touching_since', '_touches_artifact'),
        ('coordinator_core/ops/updatedocs_gates.py', '_gate_queue_prune_sweep', '_run'),
        (
            'coordinator_core/ops/workday_complete_step2_5_dirty_tree.py',
            '_act_gitignore',
            '_run_git',
        ),
        ('coordinator_core/ops/workweek_reverse_drift_gate.py', 'run_gate', 'run'),
        ('coordinator_core/percolate/engine.py', 'run_entrypoint_gate', '_run_one_entrypoint'),
        (
            'coordinator_core/plan_assemble/predicates/composition_graph.py',
            'path_rename_or_move',
            '_run_git',
        ),
        ('coordinator_core/plugin_health/drift.py', '_check_copy_install', '_run_git'),
        (
            'coordinator_core/reconcile/ac27_differential_oracle.py',
            '_check_transitive_import_isolation',
            '_git_show_blob',
        ),
        ('coordinator_core/session_attribution.py', 'detect_foreign_commits', '_git_run'),
        ('coordinator_core/snippet_sync/registry.py', 'list_for', '_ml_get'),
        ('coordinator_core/snippet_sync/registry.py', 'resolve_conditional_consumers', '_ml_get'),
        (
            'coordinator_core/write_guards/block_consumed_handoff_edit.py',
            'check',
            '_normalize_and_gate',
        ),
        (
            'coordinator_core/write_guards/block_cutover_phase_hand_edit.py',
            'check',
            '_normalize_and_gate',
        ),
        (
            'coordinator_core/write_guards/block_memo_status_hand_edit.py',
            'check',
            '_normalize_and_gate',
        ),
        (
            'coordinator_core/write_guards/validate_frontmatter_schema_advisory.py',
            '_reviewed_range_offer',
            'Path',
        ),
        (
            'coordinator_core/write_guards/validate_frontmatter_schema_advisory.py',
            '_reviewed_range_offer',
            '_resolve_ref_to_sha',
        ),
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


@pytest.mark.designed_red
def test_burn_down_known_preexisting_amplification_sites():
    """Red by design, 2026-08-08 -- reported, deliberately not gated. Narrowed to its correct
    job (§ staff-eng review, finding 4): a non-gating worklist burning the 85 already-known sites
    (`_KNOWN_SITES`) toward zero, so graduating a site off the frozen inventory as it gets fixed
    is a one-constant edit, same shape as `test_widened_spawn_families_surface_known_preexisting_
    sites` in `test_no_bare_hot_path_spawn.py`. Full inventory: `state/audits/
    2026-08-08-git-amplification-gate-known-sites.md`.

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
        f"to `{site.callee}` inside a qualifying loop reaches a git spawn directly. Batch it "
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
