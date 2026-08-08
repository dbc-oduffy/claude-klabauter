"""Amplification collector (G1): sibling to `spawn_policy`, resolving generic runners and
injected `GitRunner`s -- the third state neither existing gate expresses.

Spec backlink: `docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-amplification-gate.md`,
`## Tasks` chunk G1 (this collector) and G2 (the two assertions this collector feeds, landed
in a later wave over this same file).

THE GAP THIS COLLECTOR CLOSES. `test_no_bare_hot_path_spawn.py` asserts a property of each
individual call -- console suppression -- with no concept of call COUNT: a maximally-compliant
spawn inside a 3000-iteration loop is the defect and passes. `test_no_spawn_per_item_loop.py`
asserts an amplification-shaped property but the opposite discrimination: it fires only where
argv is INVARIANT with respect to the loop target (hoistable to one call), and by its own tested
acceptance criterion must stay SILENT on varying argv -- that silence is a negative control, not
an omission (see this module's own negative-spec block below; do NOT read this collector as
relaxing that silence). This collector's class is a third state neither expresses: *varying argv,
but batchable into a single call* -- one `git` spawn per loop item, where the callee itself
directly reaches a git spawn, reachable through a local helper, a cross-module import, a
dependency-injected runner, or a generic `_run(argv)` wrapper whose git-ness is only visible at
the call site.

REUSE FROM `spawn_policy`, UNMODIFIED (pinned API, `tasks/shell-spawn-regrowth-gate/
PINNED-API.md`): `discover_source_files`, `sites_in_source`, `is_test_tree_site`,
`DEFAULT_EXCLUDE`, `SpawnParseError`. This module does NOT extend `SpawnSite` -- it is a frozen
dataclass under that pinned API -- and instead defines a sibling, `GitAmpSite`, the same
precedent `LoopSpawnSite` (`test_no_spawn_per_item_loop.py`) and `BareSpawnSite`
(`test_no_bare_hot_path_spawn.py`) already set.

ONE collector, TWO assertions -- this wave ships only the collector plus its own planted-fixture
self-tests. G2 (next wave, same file) adds the standing frozen-inventory subset assertion (bites
on any NEW site) and the non-gating `designed_red` worklist over the known-114 high-precision
stratum, sharing this collector exactly as `_STANDING_GATE_FAMILIES` / `_ALL_FAMILIES` share
`find_bare_hot_path_spawns` in `test_no_bare_hot_path_spawn.py`. Do not add either assertion here.

SCOPE. `_GATE_SCOPE_ROOTS` names `coordinator_core` AND `coordinator/bin` -- AC4. Neither
existing gate scans `coordinator/bin/`, and that is where the worst site in this plan's audit
lives. Restricted to the HIGH-PRECISION STRATUM: the callee must DIRECTLY contain a git spawn
(one hop, by one of the five routes below), never a transitive/multi-hop reach. The prototype
measured the transitive deep tail at 32% TP with no static discriminator separating true from
false positives at any depth -- deliberately excluded here, tracked instead as G2's named
residual.

THREE STRUCTURAL DISCRIMINATORS (measured: 32.4% naive FP -> 4.2% with all three applied, zero
true positives lost):

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

FIVE DETECTION ROUTES (per gate-substrate.md Task C), restricted to the high-precision stratum:

  a-direct       -- the call itself is a recognized `subprocess`/`os`/`asyncio` spawn (via
                    `sites_in_source`) with a git-shaped argv0.
  b-local-helper -- the callee is a function DEFINED IN THE SAME MODULE whose own body directly
                    contains a git-argv0 spawn site.
  c-cross-module -- the callee is imported (`from X import name`) and resolves, via a repo-wide
                    name index built over the same scope, to a function in another module whose
                    own body directly contains a git-argv0 spawn site.
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
                    whose body forwards that parameter, unchanged, as the argv-bearing arg of
                    exactly one recognized spawn call (the `_run(argv)` wrapper idiom) -- and the
                    ACTUAL argument passed at THIS call site is git-shaped (a list/tuple literal
                    whose first element is the string `"git"`, or an f-string/concatenation whose
                    static prefix is `"git"`). Git-ness is read at the call site because the
                    wrapper's own body only ever sees a bare parameter name.

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
  - Does not extend `spawn_policy.detect.SpawnSite`; see `GitAmpSite` below.
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

_GIT_ARGV0 = "git"

#: AC4 -- coordinator/bin/ MUST be in scope; neither existing gate scans it.
_GATE_SCOPE_ROOTS: tuple[str, ...] = ("coordinator_core", "coordinator/bin")

_RUNNER_KWARG_NAMES: frozenset[str] = frozenset(
    {"run", "runner", "git", "git_runner", "run_git", "spawn"}
)
_RUNNER_NAME_PREFIXES: tuple[str, ...] = ("run", "git", "spawn")

#: Known, LIVE, outstanding exemptions -- keyed on (relpath, lineno), matching the sibling
#: gates' convention. Empty by construction here: G1 ships no gating assertion, so nothing is
#: exempted yet. G2 (next wave) is where a real exemption register, if any, would live.
_EXEMPT_SITES: set[tuple[str, int]] = set()


@dataclasses.dataclass(frozen=True)
class GitAmpSite:
    """Sibling to `spawn_policy.SpawnSite` -- carries the loop/route signal that frozen
    dataclass deliberately does not. NOT a subtype or extension of `SpawnSite`; see module
    docstring's "Reuse from spawn_policy" section for why a sibling, not an extension."""

    path: str
    lineno: int
    enclosing: str
    route: str  # "a-direct" | "b-local-helper" | "c-cross-module" | "d-injected" | "e-generic-runner"
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
# Repo-wide, one-hop function index (routes b/c/d/e)
# --------------------------------------------------------------------------


@dataclasses.dataclass
class _FuncIndex:
    #: top-level function name -> list of (relpath, func_name) whose body directly contains a
    #: git-argv0 spawn site (routes b/c)
    direct_git_funcs: dict[str, list[tuple[str, str]]] = dataclasses.field(default_factory=dict)
    #: top-level function name -> list of (relpath, func_name) whose body directly contains ANY
    #: recognized spawn site, regardless of argv0 (route d's "resolves to a direct spawner")
    direct_any_spawn_funcs: dict[str, list[tuple[str, str]]] = dataclasses.field(
        default_factory=dict
    )
    #: top-level function name -> forwarded parameter name, for a single-parameter function whose
    #: body forwards that parameter unchanged into exactly one recognized spawn call (route e)
    runner_shaped_funcs: dict[str, str] = dataclasses.field(default_factory=dict)
    #: (relpath, func_name) -> True, restricted to route-b's SAME-MODULE lookup
    same_module_direct_git: dict[tuple[str, str], bool] = dataclasses.field(default_factory=dict)
    #: relpath -> set of names imported via `from X import name` in that file (route c's gate)
    imported_names_by_file: dict[str, set[str]] = dataclasses.field(default_factory=dict)


def _generic_runner_param(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """A single-parameter function that forwards that parameter, unchanged, as the FIRST
    positional argument of at least one `ast.Call` anywhere in its body -- the `_run(argv)`
    wrapper idiom. See module docstring's blind-spots note: this does not exclude nested
    function scopes from the walk, a deliberate false-negative-biased looseness (it can only
    widen the candidate-runner set; route e still gates on the call SITE's own argv)."""
    params = [a.arg for a in func_node.args.args]
    if len(params) != 1:
        return None
    only_param = params[0]
    for node in ast.walk(func_node):
        if node is func_node:
            continue
        if isinstance(node, ast.Call) and node.args:
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id == only_param:
                return only_param
    return None


@dataclasses.dataclass(frozen=True)
class _FileRecord:
    """One file's read+parse+spawn-detect result, computed exactly ONCE and shared between
    `_build_func_index` and `find_unbatched_per_item_git_spawns`'s own violation-detection pass
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
    `find_unbatched_per_item_git_spawns`'s own loop each independently re-read, re-`ast.parse`,
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

        git_enclosing = {s.enclosing for s in spawn_sites if s.argv0 == _GIT_ARGV0}
        any_enclosing = {s.enclosing for s in spawn_sites}

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

            if name in git_enclosing:
                index.direct_git_funcs.setdefault(name, []).append((relpath, name))
                index.same_module_direct_git[(relpath, name)] = True

            if name in any_enclosing:
                index.direct_any_spawn_funcs.setdefault(name, []).append((relpath, name))

            runner_param = _generic_runner_param(node)
            if runner_param is not None and name not in index.runner_shaped_funcs:
                index.runner_shaped_funcs[name] = runner_param

    return index


# --------------------------------------------------------------------------
# Call-site argv-shape helpers (route e's "read git-ness at the call site")
# --------------------------------------------------------------------------


def _call_arg_is_git_shaped(call: ast.Call, param_index: int) -> bool:
    """True if the argument `call` passes at `param_index` looks like it could be a git argv:
    a list/tuple literal whose first element is the string literal `"git"`, or a string/
    f-string constant/prefix equal to `"git"`. Deliberately conservative (false-negative
    preferred): anything not statically resolvable is treated as NOT git-shaped."""
    if param_index >= len(call.args):
        return False
    arg = call.args[param_index]
    if isinstance(arg, (ast.List, ast.Tuple)) and arg.elts:
        first = arg.elts[0]
        return isinstance(first, ast.Constant) and first.value == _GIT_ARGV0
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value.strip().split()[:1] == [_GIT_ARGV0] if arg.value.strip() else False
    if isinstance(arg, ast.JoinedStr) and arg.values:
        first = arg.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value.strip().startswith(_GIT_ARGV0)
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
        if _is_constant_literal_iterable(node.iter, self._literal_names):
            # Discriminator 2: excluded wholesale -- body still visited (a nested qualifying
            # loop inside it may exist), but WITHOUT this loop's own context pushed.
            for stmt in node.body:
                self.visit(stmt)
            return
        self._in_qualifying_loop_depth += 1
        for stmt in node.body:
            self.visit(stmt)
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
        for gen in generators[1:]:
            self.visit(gen.iter)
            for if_clause in gen.ifs:
                self.visit(if_clause)
        for if_clause in generators[0].ifs:
            self.visit(if_clause)
        self._visit_comp_elt(node)
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
            self.marked_calls.add((node.lineno, node.col_offset))
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

    Resolution against `index.direct_any_spawn_funcs` is BY NAME: the identifier passed at
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


def _git_argv0_linenos(spawn_sites) -> set[int]:
    return {s.lineno for s in spawn_sites if s.argv0 == _GIT_ARGV0}


def find_unbatched_per_item_git_spawns(
    roots: tuple[pathlib.Path, ...], index: _FuncIndex | None = None
) -> list[GitAmpSite]:
    """Core collector. Walk `roots` (via the shared `discover_source_files` traversal),
    restricted to the high-precision stratum (callee directly contains a git spawn, one hop),
    applying all three structural discriminators and all five detection routes described in
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

    violations: list[GitAmpSite] = []

    for record in records:
        relpath = record.relpath
        tree = record.tree
        spawn_sites = record.spawn_sites

        git_linenos = _git_argv0_linenos(spawn_sites)
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
            if (relpath, node.lineno) in _EXEMPT_SITES:
                continue

            enclosing = enclosing_by_call.get(key, "<module>")
            callee = _call_callee_name(node)
            route: str | None = None

            # route a-direct: the call itself is a recognized git-argv0 spawn.
            if node.lineno in git_linenos:
                route = "a-direct"

            if route is None and callee is not None:
                # route b-local-helper: same-module function directly git-spawns.
                if (relpath, callee) in index.same_module_direct_git:
                    route = "b-local-helper"

                # route c-cross-module: imported name resolves (repo-wide, by name) to a
                # function elsewhere that directly git-spawns.
                if (
                    route is None
                    and callee in imported_here
                    and callee in index.direct_git_funcs
                    and not any(r == relpath for r, _ in index.direct_git_funcs[callee])
                ):
                    route = "c-cross-module"

                # route e-generic-runner: callee is runner-shaped (a single-parameter
                # `_run(argv)`-style wrapper -- `_generic_runner_param` always resolves to
                # that sole parameter, i.e. argument position 0) and THIS call's own argv
                # looks git-shaped.
                if (
                    route is None
                    and callee in index.runner_shaped_funcs
                    and _call_arg_is_git_shaped(node, 0)
                ):
                    route = "e-generic-runner"

            if route is None:
                # route d-injected: a runner-shaped argument resolves to ANY direct spawner.
                runner_name = _find_injected_runner_name(node)
                if runner_name is not None and runner_name in index.direct_any_spawn_funcs:
                    route = "d-injected"

            if route is not None:
                violations.append(
                    GitAmpSite(
                        path=relpath,
                        lineno=node.lineno,
                        enclosing=enclosing,
                        route=route,
                        callee=callee or "<unknown>",
                    )
                )

    return violations


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

#: Frozen inventory of already-known amplification sites (G2, dated 2026-08-08 -- repo-wide run
#: of `find_unbatched_per_item_git_spawns((coordinator_core, coordinator/bin))`: 116 violations /
#: 51 files, 85 distinct `GitAmpSite.key` identities after dedup by (path, enclosing, callee).
#: Full inventory, wall-clock cost, and the three deliberately-live sites this plan examined and
#: kept (none of which this collector's by-name/single-hop resolution can currently see):
#: `state/audits/2026-08-08-git-amplification-gate-known-sites.md`.
#:
#: The standing assertion below is a SUBSET check, not a bare `violations == []` (blocked on this
#: volume) -- it bites immediately on any NEW site outside this frozen set. Do NOT grow this
#: constant to silence a new violation; fix the site, or route a genuine deliberate exception
#: through the collector's own `_EXEMPT_SITES` with a dated reason (never a
#: `# amplification-ok:` pragma -- § Anti-scope 17, the discriminators are structural). Shrink
#: this constant as sites are fixed -- that is the designed_red worklist's job below.
_KNOWN_SITES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("coordinator/bin/age-sweep-lessons.py", "main", "run"),
        ("coordinator/bin/coordinator-safe-commit", "do_blanket", "_git_reset_unstage"),
        ("coordinator/bin/coordinator-safe-commit", "do_scope_from", "_validate_pathspec"),
        ("coordinator/bin/coordinator-safe-commit", "do_scoped", "_validate_pathspec"),
        ("coordinator/bin/emit-goal-from-artifact.py", "main", "run"),
        ("coordinator/bin/merge-release-notes-derive.py", "_contains_all", "_git"),
        ("coordinator/bin/migrate-archive-week-changelogs.py", "run", "git_mv"),
        ("coordinator/bin/publish.py", "_delta_row_source_sha", "_git_rev_parse"),
        ("coordinator/bin/publish.py", "_materialize_inject_srcs", "_git_materialize_ref"),
        (
            "coordinator/bin/publish.py",
            "_publish_relevant_allowlist_leg",
            "_git_ls_tree_entries_files",
        ),
        ("coordinator/bin/publish.py", "main", "_git_head"),
        ("coordinator/bin/publish.py", "main", "_is_git_repo"),
        ("coordinator/bin/publish.py", "run_pre_sync_gates", "_git_rev_parse"),
        ("coordinator/bin/reap-integrated-review-findings.py", "_reap_integrated_legacy", "_git"),
        ("coordinator/bin/reap-stale-subagent-sidecars.py", "main", "_is_tracked"),
        ("coordinator/bin/refresh-plugin-live-install.py", "_handle_default", "_git"),
        ("coordinator/bin/refresh-plugin-live-install.py", "_interactive_gate", "_git"),
        ("coordinator/bin/workday-complete-reconcile.py", "run_completion_reconcile", "_git_add"),
        ("coordinator/bin/workday-complete-step9-append-changelog.py", "main", "run"),
        ("coordinator_core/bash_guards/block_subagent_commit.py", "_fold_template_is_bounded", "int"),
        (
            "coordinator_core/bash_guards/block_subagent_commit.py",
            "_git_commit_agent_may_commit",
            "_pathspec_element_is_sweeping",
        ),
        ("coordinator_core/bash_guards/commit_tripwires.py", "check_bin_sh_polyglot", "join"),
        (
            "coordinator_core/bash_guards/dispatch_checks.py",
            "_check_destructive_git_revert_full",
            "_run_git",
        ),
        ("coordinator_core/bash_guards/dispatch_checks.py", "_is_hazard_repo", "_paths_match"),
        ("coordinator_core/bash_guards/dispatch_checks.py", "check_blanket_git_add", "_paths_match"),
        ("coordinator_core/bash_guards/dispatch_checks.py", "check_destructive_git_clean", "_run_git"),
        (
            "coordinator_core/bash_guards/dispatch_checks.py",
            "check_destructive_git_orphan",
            "_run_git",
        ),
        ("coordinator_core/bash_guards/dispatch_checks.py", "check_destructive_rm", "_run_git"),
        ("coordinator_core/bash_guards/dispatch_checks.py", "check_validate_commit", "_run_git"),
        ("coordinator_core/bash_guards/dispatch_checks.py", "check_validate_commit", "join"),
        ("coordinator_core/consolidate_assemble/__init__.py", "brief", "branch_reachable"),
        ("coordinator_core/consolidate_assemble/__init__.py", "brief", "inspect_commit"),
        ("coordinator_core/consolidate_assemble/__init__.py", "brief", "tip_author"),
        ("coordinator_core/consolidate_assemble/__init__.py", "brief", "unique_commits"),
        ("coordinator_core/consolidate_assemble/__init__.py", "brief", "worktree_is_dirty"),
        ("coordinator_core/consolidate_assemble/apply.py", "_dispatch_cherry_pick_and_delete", "_run_git"),
        ("coordinator_core/coverage.py", "_bulk_trailer_lookup", "_run"),
        ("coordinator_core/coverage.py", "_derive_dag_chain_set", "_run"),
        ("coordinator_core/coverage.py", "_reviewed_via_graph_walk", "_run"),
        (
            "coordinator_core/execute_plan_assemble/close_out_and_stamp.py",
            "_dispatch_ledger_delivered",
            "_run_git",
        ),
        (
            "coordinator_core/execute_plan_assemble/close_out_and_stamp.py",
            "_first_deliverable_commit_range_base",
            "_run_git",
        ),
        (
            "coordinator_core/frontmatter/schema_drift_watch.py",
            "_scan",
            "check_schema_drift_advisory",
        ),
        ("coordinator_core/ops/agent_worktree_sweep.py", "_sweep_one", "_cherry_pick_abort"),
        ("coordinator_core/ops/agent_worktree_sweep.py", "_sweep_one", "_cherry_pick_with_env"),
        ("coordinator_core/ops/bootstrap_orchestrate.py", "main", "_git"),
        ("coordinator_core/ops/bootstrap_orchestrate.py", "main", "run"),
        ("coordinator_core/ops/ceremony/detached_render_commit.py", "commit_own_artifact", "_run_git"),
        ("coordinator_core/ops/ceremony/scoped_git_commit.py", "_remote_sha_state", "run"),
        ("coordinator_core/ops/configure_git.py", "main", "_git_config_get"),
        ("coordinator_core/ops/configure_git.py", "main", "_git_config_set"),
        ("coordinator_core/ops/cutover_gate.py", "_reverify_commit_sha", "run"),
        ("coordinator_core/ops/dirty_tree_gate.py", "main", "run"),
        (
            "coordinator_core/ops/distill_apply_disposal.py",
            "_delete_tracked_and_append_log",
            "_run_git",
        ),
        ("coordinator_core/ops/distill_apply_disposal.py", "_write_denormalizations", "_is_tracked"),
        ("coordinator_core/ops/distill_apply_disposal.py", "apply_disposal_manifest", "_is_tracked"),
        ("coordinator_core/ops/emit/envelope.py", "main", "_commit_age_label"),
        ("coordinator_core/ops/emit/envelope.py", "main", "resolve_ref"),
        ("coordinator_core/ops/fleet/_common.py", "archive_and_commit", "_ls_tree_head_cacheinfo"),
        ("coordinator_core/ops/fleet/_common.py", "archive_and_commit", "create_subprocess_exec"),
        ("coordinator_core/ops/fleet/_common.py", "rm_and_commit", "create_subprocess_exec"),
        ("coordinator_core/ops/fleet/_findings_reap.py", "reap_findings", "_is_tracked"),
        ("coordinator_core/ops/fleet/archive_plans.py", "_handle_act", "_plan_worktree_dirty"),
        ("coordinator_core/ops/fleet/archive_plans.py", "_handle_preview", "_plan_worktree_dirty"),
        ("coordinator_core/ops/migrate_branch_canonical_case.py", "_migrate", "_git"),
        ("coordinator_core/ops/migrate_completion_log_legacy.py", "main", "_git_mv"),
        ("coordinator_core/ops/migrate_cross_repo_layout.py", "main", "_move_one"),
        ("coordinator_core/ops/normalize_claimed_frontmatter.py", "main", "get_tracked_files"),
        ("coordinator_core/ops/orphan_branch_sweep.py", "main", "_git"),
        (
            "coordinator_core/ops/promote_shipped_in_flight_stubs.py",
            "_run_promotions",
            "_git_common_dir",
        ),
        (
            "coordinator_core/ops/review_brightline_gate.py",
            "_compute_chain_oracle",
            "_derive_dag_chain_set",
        ),
        ("coordinator_core/ops/review_coverage_core.py", "build_reviewed_set", "_run"),
        ("coordinator_core/ops/review_coverage_core.py", "build_segments", "_run"),
        (
            "coordinator_core/ops/review_trail_readjudication_report.py",
            "compute_readjudication_report",
            "_full_range_shas",
        ),
        (
            "coordinator_core/ops/session/safe_commit_offer.py",
            "_render_report",
            "_commit_changed_count",
        ),
        (
            "coordinator_core/ops/workday_complete_step2_5_dirty_tree.py",
            "_act_gitignore",
            "_run_git",
        ),
        (
            "coordinator_core/ops/workday_complete_step2_5_dirty_tree.py",
            "_classify_main_pass",
            "_run_git",
        ),
        ("coordinator_core/pickup_assemble/__init__.py", "compute_premise_checks", "_run_git"),
        ("coordinator_core/plugin_health/drift.py", "_check_copy_install", "_run_git"),
        (
            "coordinator_core/reconcile/ac27_differential_oracle.py",
            "_check_transitive_import_isolation",
            "_git_show_blob",
        ),
        ("coordinator_core/session/scope.py", "compute_scope", "_dirty_files_under_batch"),
        ("coordinator_core/session_attribution.py", "detect_foreign_commits", "_git_run"),
        (
            "coordinator_core/workstream_complete/directives_commit_tail.py",
            "_peer_committed_paths",
            "_run_git_ok",
        ),
        (
            "coordinator_core/write_guards/block_consumed_handoff_edit.py",
            "check",
            "_normalize_and_gate",
        ),
        (
            "coordinator_core/write_guards/block_cutover_phase_hand_edit.py",
            "check",
            "_normalize_and_gate",
        ),
        (
            "coordinator_core/write_guards/block_memo_status_hand_edit.py",
            "check",
            "_normalize_and_gate",
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
    violations = find_unbatched_per_item_git_spawns(_gate_scope_paths())
    observed = {site.key for site in violations}
    new_site_keys = observed - _KNOWN_SITES
    new_violations = [site for site in violations if site.key in new_site_keys]
    assert not new_site_keys, "\n\n".join(_format_violation(site) for site in new_violations)


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
    violations = find_unbatched_per_item_git_spawns(_gate_scope_paths())
    assert violations == [], "\n\n".join(_format_violation(site) for site in violations)


def _format_violation(site: GitAmpSite) -> str:
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
# `find_unbatched_per_item_git_spawns` against the real tree.
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
    violations = find_unbatched_per_item_git_spawns((tmp_path,))
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
    violations = find_unbatched_per_item_git_spawns((tmp_path,))
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
    violations = find_unbatched_per_item_git_spawns((tmp_path,))
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
    violations = find_unbatched_per_item_git_spawns((tmp_path,))
    matches = [v for v in violations if v.route == "d-injected"]
    assert len(matches) == 1
    assert matches[0].lineno == 11


def test_route_e_generic_runner_positive_git_shaped(tmp_path):
    """Route e requires a CROSS-MODULE runner: `sites_in_source`'s own `_local_helpers`
    resolution already recognizes a same-module `_run(argv)` wrapper called with a literal
    argv (reads the git-ness straight off the call site) and reports it as route a-direct --
    that is spawn_policy's existing capability, reused, not re-derived. Route e exists for the
    shape `_local_helpers` cannot see: the runner defined in a DIFFERENT module, so this file's
    own `sites_in_source` pass has no local-helper visibility into it at all."""
    runner_mod = tmp_path / "git_runner.py"
    runner_mod.write_text(
        "import subprocess\n"
        "\n"
        "def _run(argv):\n"
        "    return subprocess.run(argv, cwd='/repo')\n",
        encoding="utf-8",
    )
    fixture = tmp_path / "route_e.py"
    fixture.write_text(
        "from git_runner import _run\n"
        "\n"
        "def check(shas):\n"
        "    for sha in shas:\n"
        "        _run(['git', 'show', sha])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_git_spawns((tmp_path,))
    matches = [v for v in violations if v.route == "e-generic-runner"]
    assert len(matches) == 1
    assert matches[0].callee == "_run"
    assert matches[0].path.endswith("route_e.py")


def test_route_e_generic_runner_negative_non_git_argv(tmp_path):
    """Negative control: the same cross-module runner shape, called with a non-git argv, must
    NOT fire -- route e's git-ness is read at the call site, not the definition site."""
    runner_mod = tmp_path / "git_runner.py"
    runner_mod.write_text(
        "import subprocess\n"
        "\n"
        "def _run(argv):\n"
        "    return subprocess.run(argv, cwd='/repo')\n",
        encoding="utf-8",
    )
    fixture = tmp_path / "route_e_negative.py"
    fixture.write_text(
        "from git_runner import _run\n"
        "\n"
        "def check(names):\n"
        "    for name in names:\n"
        "        _run(['ls', name])\n",
        encoding="utf-8",
    )
    violations = find_unbatched_per_item_git_spawns((tmp_path,))
    assert violations == []


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
    violations = find_unbatched_per_item_git_spawns((tmp_path,))
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
    violations = find_unbatched_per_item_git_spawns((tmp_path,))
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
    violations = find_unbatched_per_item_git_spawns((tmp_path,))
    assert violations == []


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
    violations = find_unbatched_per_item_git_spawns((tmp_path,))
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
    violations = find_unbatched_per_item_git_spawns((tmp_path,))
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
