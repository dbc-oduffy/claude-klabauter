"""Standing gate (test-only): no hot-path spawn sits directly under a `for`/comprehension
loop with an INVARIANT argv -- one that could be hoisted out of the loop and run once
instead of N times.

Context: `docs/plans/2026-08-07-spawn-storm-culprit-taxonomy-and-detectors.md` chunk D5
(AC4, AC5). D1's taxonomy names two live instances of exactly this shape in
`coordinator_core/bash_guards/dispatch_checks.py`:
`_check_destructive_git_revert_full` ran `git status --porcelain` once per loop
segment against the same `git_cwd`, and `check_destructive_git_orphan` resolved the
current branch unmemoized while sitting next to two memoized sibling calls. D6 fixed
both (commit `b7318bc48`) by moving the spawn behind a cwd-keyed memo closure defined
OUTSIDE the loop, called (not spawned-from) inside it. This gate is the prevention: it
fails the moment a new hot-path loop reintroduces the same unhoisted shape.

THE DISCRIMINATION THIS GATE EXISTS FOR (AC5): an argv that VARIES with the loop item
is a legitimate per-item spawn and must NOT fire. `check_destructive_rm` spawns per
rm-target inside a loop, and that argv genuinely depends on the loop variable (each
target needs its own `git rev-parse --show-toplevel`-shaped resolution) -- it is
memoized per (cwd, args) already and D1 rates it WEAK for exactly this reason. A gate
that cannot tell "invariant argv, hoist it" from "per-item argv, cannot batch it" would
fire there, which this plan's anti-scope forbids outright. See
`test_gate_ignores_a_per_item_spawn_using_the_loop_variable` below for the proof.

REUSE FROM `spawn_policy`, UNMODIFIED: `discover_source_files` (traversal -- its own
docstring names two prior census/gate drift incidents caused by re-walking instead of
reusing this), `sites_in_source` (the single source of truth for "is this call a
recognized spawn shape", used here only to obtain the set of spawn-call line numbers
per file -- never re-derived), `site_key`, `SpawnParseError`, `DEFAULT_EXCLUDE`, and
D2's hoisted `is_test_tree_site` predicate (test-tree spawn sites never run in a live
session and are never culprits).

DO NOT EXTEND `SpawnSite` -- it is a frozen dataclass under a pinned API
(`tasks/shell-spawn-regrowth-gate/PINNED-API.md`) whose `lineno` field is documented
"informational only -- never part of identity". This gate needs the loop-context
signal `SpawnSite` deliberately does not carry, so it defines a SIBLING dataclass,
`LoopSpawnSite`, built by cross-referencing `sites_in_source`'s output (which call
lines are real spawns, per the shared enumeration) against this module's own
loop-context AST pass (which of those lines sit under a `For`/comprehension, and
whether the call's argv references that loop's own target names). Using `lineno` here
is a same-parse positional cross-reference within one file, not a `site_key`-shaped
identity match -- `site_key` deliberately excludes `lineno` for THAT reason, and this
gate does not use `site_key` at all.

SCOPE IT HOT-PATH-ONLY -- `_HOT_PATH_ROOTS` below is the ONE list that needs to change
to widen this gate's reach; it mirrors chunk D3's fire-set
(`coordinator_core/write_guards/nudge_private_git_fact_resolver.py`), the modules that
actually run on the PreToolUse/write-time hook hot path per this plan's Problem
section. A repo-wide assertion would fail today against 907 non-test `plain-spawn`
sites outside the hot path -- that is the sibling plan's C10 shape (see this plan's Out
of scope section), not this gate's, and is deliberately not attempted here.

Modeled directly on
`coordinator_core/tests/test_no_cwd_unguarded_coordinator_core_subprocess_spawn.py`
(structure, ast-over-regex rationale, planted-fixture self-tests proving the gate
actually detects rather than passing by absence, and a named-blind-spots section).

Named blind spots -- deliberately biased toward a false NEGATIVE (a missed hoistable
spawn) over a false POSITIVE (flagging a genuinely per-item spawn), because a gate that
cries wolf on `check_destructive_rm`-shaped code gets disabled, which is the specific
failure this plan's anti-scope forbids:

  - "Nearest enclosing loop" is scoped to the INNERMOST `For`/`AsyncFor`/comprehension
    that lexically contains the call without crossing a `FunctionDef`/`AsyncFunctionDef`/
    `Lambda`/`ClassDef` boundary. A spawn inside a nested closure defined inside a loop
    (the exact D6 fix shape -- `_memo_status_porcelain` is a `def` nested inside the
    loop's enclosing function, called FROM inside the loop) is NOT considered "under
    the loop" by this gate, because the spawn call itself is lexically inside the
    closure's own function scope, not the loop body. This is the intended discrimination,
    not an evasion: a spawn hidden behind a memoizing closure has already been hoisted
    in the sense this gate cares about (it runs once per distinct memo key, not once
    per iteration), and re-flagging it would make the D6 fix shape itself the thing
    that re-trips the gate it exists to satisfy.
  - "Bound by the loop's target" collects every `Name` id appearing anywhere in the
    `For`/comprehension target (handles tuple/list unpacking) but does NOT trace
    one-hop-assigned locals the way `spawn_policy`'s own `_single_static_bindings`
    does for argv0 resolution. A call whose argv is a variable assigned FROM the loop
    variable one statement earlier (`item_str = str(item); spawn([item_str])`) is
    invisible to this check and will be treated as invariant -- a genuine per-item
    spawn could be missed as a false negative in that shape. Accepted per the
    false-negative-over-false-positive preference above.
  - Only the recognized-spawn LINE NUMBERS from `sites_in_source` are trusted as "this
    is a real spawn call"; the loop-context AST pass is a SEPARATE walk over the same
    parse matching by `(lineno, col_offset)` of `ast.Call` nodes. Two distinct
    recognized spawn calls sharing one line (e.g. `run(a); run(b)`) both count fully
    since `col_offset` still disambiguates; a spawn call reported by `sites_in_source`
    at a line this module's own walk cannot find an `ast.Call` at (which should not
    happen for the same parse of the same source, but is not statically provable
    identical between two independent walks) is silently skipped rather than crashing
    the gate -- another false-negative-biased choice.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from coordinator_core.spawn_policy import (
    SpawnParseError,
    is_test_tree_site,
    sites_in_source,
)
from coordinator_core.spawn_policy.detect import DEFAULT_EXCLUDE, discover_source_files

_REPO_ROOT = Path(__file__).resolve().parents[2]

# HOT-PATH SCOPE -- the ONE obvious place to widen this gate. Mirrors D3's fire-set:
# the modules that run on the PreToolUse/write-time hook hot path.
_HOT_PATH_ROOTS: tuple[str, ...] = (
    "coordinator_core/write_guards",
    "coordinator_core/bash_guards",
    "coordinator_core/hooks",
    "coordinator_core/ops/session",
)

# Known, LIVE, outstanding exemptions -- keyed on (relpath, lineno). Empty by
# construction: D6 (commit b7318bc48) fixed the two real instances this gate would
# otherwise catch. A live entry here means either the sweep missed a site or this
# gate found a genuine new one -- per this gate's own spec, that is a reason to
# report it (see this plan's D5 dispatch instructions), not to populate this set.
_EXEMPT_SITES: set[tuple[str, int]] = set()


@dataclasses.dataclass(frozen=True)
class LoopSpawnSite:
    """Sibling to `spawn_policy.SpawnSite` -- carries the loop-context signal that
    frozen dataclass deliberately does not. Not part of `spawn_policy`'s pinned API;
    defined here because it is specific to this gate's property, not a general
    spawn-identity concept.
    """

    path: str
    lineno: int
    invariant: bool  # True: argv references no Name bound by the nearest enclosing loop's target.


def _relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.resolve().relative_to(root.resolve()).as_posix()


def _loop_target_names(target: ast.expr) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(target):
        if isinstance(node, ast.Name):
            names.add(node.id)
    return names


def _names_in_call_args(call: ast.Call) -> set[str]:
    names: set[str] = set()
    for arg in call.args:
        for node in ast.walk(arg):
            if isinstance(node, ast.Name):
                names.add(node.id)
    for kw in call.keywords:
        if kw.value is None:
            continue
        for node in ast.walk(kw.value):
            if isinstance(node, ast.Name):
                names.add(node.id)
    return names


class _LoopContextVisitor(ast.NodeVisitor):
    """Walks one module, tracking the nearest enclosing `For`/`AsyncFor`/comprehension
    (without crossing a function/lambda/class boundary) for every `ast.Call` node.
    Records `(lineno, col_offset) -> invariant`.
    """

    def __init__(self) -> None:
        self._loop_target_stack: list[set[str]] = []
        self.calls: dict[tuple[int, int], bool] = {}

    def _visit_scope_boundary(self, node: ast.AST) -> None:
        saved = self._loop_target_stack
        self._loop_target_stack = []
        self.generic_visit(node)
        self._loop_target_stack = saved

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope_boundary(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope_boundary(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_scope_boundary(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope_boundary(node)

    def _visit_loop(self, node: ast.For | ast.AsyncFor) -> None:
        self._loop_target_stack.append(_loop_target_names(node.target))
        self.generic_visit(node)
        self._loop_target_stack.pop()

    def visit_For(self, node: ast.For) -> None:
        self._visit_loop(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_loop(node)

    def _visit_comprehension_container(
        self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
    ) -> None:
        pushed = 0
        for generator in node.generators:
            self._loop_target_stack.append(_loop_target_names(generator.target))
            pushed += 1
        self.generic_visit(node)
        for _ in range(pushed):
            self._loop_target_stack.pop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension_container(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension_container(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension_container(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension_container(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self._loop_target_stack:
            nearest_targets = self._loop_target_stack[-1]
            referenced = _names_in_call_args(node)
            invariant = referenced.isdisjoint(nearest_targets)
            self.calls[(node.lineno, node.col_offset)] = invariant
        self.generic_visit(node)


def _find_call_nodes_by_line(tree: ast.Module) -> dict[int, list[ast.Call]]:
    by_line: dict[int, list[ast.Call]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            by_line.setdefault(node.lineno, []).append(node)
    return by_line


def find_invariant_loop_spawns(roots: tuple[Path, ...]) -> list[LoopSpawnSite]:
    """Walk `roots` (via the shared `discover_source_files` traversal), and for every
    non-test-tree file, cross-reference `sites_in_source`'s recognized spawn lines
    against a loop-context AST pass to find spawns under a loop whose argv is
    invariant with respect to that loop's own target.

    Hot-path scoping happens by the CALLER passing hot-path roots (see
    `_HOT_PATH_ROOTS`) -- this function itself does not know about hot-path
    vocabulary, matching `sites_in_source`'s own negative-spec (report call-sites,
    never reachability/cost/hot-path judgments). Used both against the real
    hot-path modules (the standing gate) and against an isolated tmp_path fixture
    (the gate's own self-tests).
    """
    violations: list[LoopSpawnSite] = []
    for root in roots:
        if not root.exists():
            continue
        discovered, _excluded = discover_source_files(root, exclude=DEFAULT_EXCLUDE)
        for rel_posix, file_path in discovered:
            relpath = _relpath(file_path, root)
            if is_test_tree_site(relpath):
                continue

            text = file_path.read_text(encoding="utf-8")
            try:
                spawn_sites = sites_in_source(text, rel_posix)
            except SpawnParseError:
                continue
            if not spawn_sites:
                continue

            try:
                tree = ast.parse(text, filename=str(file_path))
            except SyntaxError:
                continue

            loop_visitor = _LoopContextVisitor()
            loop_visitor.visit(tree)
            if not loop_visitor.calls:
                continue

            call_nodes_by_line = _find_call_nodes_by_line(tree)
            spawn_linenos = {site.lineno for site in spawn_sites}

            for lineno in spawn_linenos:
                for call_node in call_nodes_by_line.get(lineno, []):
                    key = (call_node.lineno, call_node.col_offset)
                    if key not in loop_visitor.calls:
                        continue
                    invariant = loop_visitor.calls[key]
                    if invariant:
                        if (relpath, lineno) in _EXEMPT_SITES:
                            continue
                        violations.append(
                            LoopSpawnSite(path=relpath, lineno=lineno, invariant=True)
                        )
    return violations


def _format_violation(site: LoopSpawnSite) -> str:
    return (
        f"{site.path}:{site.lineno} -- this spawn sits under a loop but its argv "
        f"references none of the loop's own target names, meaning it runs once per "
        f"iteration for a value that never changes. Hoist it out of the loop (a "
        f"single call before the loop) or route it through a cwd/args-keyed memo "
        f"closure defined OUTSIDE the loop body, the way "
        f"`_check_destructive_git_revert_full`'s `_memo_status_porcelain` closure "
        f"does in `coordinator_core/bash_guards/dispatch_checks.py` (commit "
        f"b7318bc48)."
    )


def test_no_spawn_per_item_loop():
    """Standing gate: no hot-path spawn under a loop may have an invariant argv,
    except the explicit, narrow, dated exemption(s) in _EXEMPT_SITES -- empty by
    construction, since D6 (b7318bc48) already fixed every known site.
    """
    hot_path_roots = tuple(_REPO_ROOT / prefix for prefix in _HOT_PATH_ROOTS)
    violations = find_invariant_loop_spawns(hot_path_roots)
    assert violations == [], "\n\n".join(_format_violation(v) for v in violations)


def test_gate_detects_a_planted_invariant_argv_spawn_inside_a_loop(tmp_path):
    """Proves the gate has teeth: a planted spawn under a loop whose argv never
    references the loop variable must fire -- without this, the gate's soundness
    would be passing-by-absence against the real tree."""
    fixture = tmp_path / "planted_invariant_loop_spawn.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check_segments(segments):\n"
        "    for seg in segments:\n"
        "        subprocess.run(['git', 'status', '--porcelain'], cwd='/repo')\n"
        "        print(seg)\n",
        encoding="utf-8",
    )

    violations = find_invariant_loop_spawns((tmp_path,))

    assert len(violations) == 1
    assert violations[0].path.endswith("planted_invariant_loop_spawn.py")
    assert violations[0].lineno == 5
    assert violations[0].invariant is True


def test_gate_ignores_a_per_item_spawn_using_the_loop_variable(tmp_path):
    """Negative control matching `check_destructive_rm`'s real shape: a spawn under
    a loop whose argv DOES reference the loop's own target must NEVER fire -- this is
    the entire discrimination AC5 demands, and getting it wrong here is the one
    outcome that would make this gate worthless."""
    fixture = tmp_path / "planted_per_item_loop_spawn.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check_destructive_rm(targets):\n"
        "    for target in targets:\n"
        "        subprocess.run(['git', 'rev-parse', '--show-toplevel', target], cwd=target)\n",
        encoding="utf-8",
    )

    violations = find_invariant_loop_spawns((tmp_path,))

    assert violations == []


def test_gate_ignores_a_spawn_outside_any_loop(tmp_path):
    """Negative control: a spawn with no enclosing loop at all must not be flagged,
    regardless of its argv shape."""
    fixture = tmp_path / "planted_no_loop_spawn.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def resolve_root():\n"
        "    subprocess.run(['git', 'rev-parse', '--show-toplevel'])\n",
        encoding="utf-8",
    )

    violations = find_invariant_loop_spawns((tmp_path,))

    assert violations == []


def test_gate_ignores_test_tree_paths(tmp_path):
    """Negative control: a planted invariant-argv-under-loop spawn in a test-tree
    path (routed through D2's shared `is_test_tree_site` predicate) must not be
    flagged -- test-tree spawn sites never run in a live session."""
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    fixture = test_dir / "test_something.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check_segments(segments):\n"
        "    for seg in segments:\n"
        "        subprocess.run(['git', 'status', '--porcelain'], cwd='/repo')\n",
        encoding="utf-8",
    )

    violations = find_invariant_loop_spawns((tmp_path,))

    assert violations == []


def test_gate_hoisted_memo_closure_shape_is_not_flagged(tmp_path):
    """Proves the D6 fix shape itself does not re-trip this gate: the real spawn is
    lexically inside a nested closure's own function scope, called (not spawned)
    from inside the loop -- named explicitly in this module's blind-spots section."""
    fixture = tmp_path / "hoisted_memo_closure.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check_revert(segments, git_cwd):\n"
        "    cache = {}\n"
        "\n"
        "    def _memo_status_porcelain(cwd):\n"
        "        if cwd not in cache:\n"
        "            cache[cwd] = subprocess.run(['git', 'status', '--porcelain'], cwd=cwd)\n"
        "        return cache[cwd]\n"
        "\n"
        "    for seg in segments:\n"
        "        _memo_status_porcelain(git_cwd)\n"
        "        print(seg)\n",
        encoding="utf-8",
    )

    violations = find_invariant_loop_spawns((tmp_path,))

    assert violations == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
