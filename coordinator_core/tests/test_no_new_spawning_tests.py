"""Recurrence guard: the spawn ratchet for git/bash/python-spawning tests.

WHY THIS GUARD EXISTS (empirical, not hypothetical)
    On 2026-08-07, tests that spawned real `git` crashed multiple live EM
    sessions on Windows and forced a standing PM prohibition on test runs.
    Commit `1d4e686a9` deleted the 76 worst chokepoints (1439 test
    functions) -- see `state/audits/2026-08-07-spawn-heavy-test-excision-
    ledger.md` for the full ledger, including what was DELIBERATELY left
    standing: ~297 files still spawn real git, plus ~100 duplicated
    module-local `def _git(...)` helpers with no shared chokepoint.

    That residue was too large to gate strictly on day one -- a strict "no
    spawning tests" rule would have failed against nearly 300 files
    immediately -- so this guard began life as a RATCHET over a frozen
    `_BASELINE` of tolerated files rather than a clean gate.

    THAT PHASE IS OVER. The baseline drained to empty on 2026-08-14 and was
    deleted; see "THE GRANDFATHER CLAUSE IS DISCHARGED" below. Every
    spawning test file now declares the spawn and is tiered onto the cadence
    suite, which is a rule that enforces itself rather than a list of
    exceptions nobody revisits.

WHAT THIS GUARD DETECTS
    A "spawn site" is a call to `subprocess.run`/`Popen`/`check_output`/
    `check_call`/`call`, or `os.system`, whose first positional argument's
    argv evidences a REAL external binary: either the first list element
    of a list/tuple literal is a string literal in
    {git, bash, sh, pwsh, powershell, node, npm, python, python3}, or that
    first element is the name `sys.executable` (an attribute access, not a
    literal, but a reliable "really launches a process" signal). This is
    intentionally an AST walk, not a text/regex scan: a `subprocess.run(...)`
    fixture written as a STRING LITERAL (e.g. test data fed to a *different*
    static detector, as in `coordinator_core/spawn_policy/tests/test_detect.py`)
    is not a `Call` node in the file's own AST and is correctly invisible
    here -- the false-positive-by-string-corpus failure mode the ledger and
    `state/audits/2026-08-06-windows-hostility-census/B-subprocess-shellouts.md`
    both warn inflated earlier hand-counts roughly 3x.

FOUR RULES
    Rule 1 -- import-time spawns: STRICT, no allowlist, no marker escape.
        A spawn site sitting at MODULE level (not nested in any function or
        method body) fails unconditionally, always, for every file. This is
        the shape that fires during pytest COLLECTION, before `-k`
        filtering or `--collect-only` ever get a chance to skip it -- the
        ledger names this as exactly why filtering "never bought relief"
        for the 17 modules that ran `git rev-parse --show-toplevel` at
        import time, all 17 deleted in `1d4e686a9`. Rule 1 therefore starts
        this guard's life at a clean zero and must stay there; there is no
        marker or baseline entry that excuses a NEW one. The fix is always
        `Path(__file__).resolve().parents[N]`, computed statically, never a
        spawned process.

    Rule 2 -- a spawning test file must DECLARE the spawn.
        A file containing a function-level (non-module-level) spawn site
        fails unless EVERY test function in it that contains a spawn site
        carries `@pytest.mark.spawns_process` (directly or via a stacked
        decorator list -- module-level `pytestmark = [pytest.mark.spawns_process]`
        also satisfies this for every test in the file). There is no
        allowlist and no grandfather path; the marker is the only route.

    Rule 3 -- REMOVED (2026-08-14), with the baseline it policed.
        It failed `_BASELINE` entries that had gone stale, keeping the list
        monotonically shrinking. The list is gone, so nothing is left to go
        stale. `test_no_grandfather_clause_is_reintroduced` now holds the
        line Rule 3 used to: no module-level exception list may come back.

    Rule 4 -- a spawning test file must be TIERED off the per-commit path.
        Declaring the spawn is not enough on its own: a file that spawns a
        real process must also carry `pytest.mark.cadence`, so it runs at
        cadence gates. Rule 2 without Rule 4 is what let this guard record
        the residue for six weeks while never moving any of it -- 555 files
        fully known, counted, and still running on the tier everyone runs.
        A `conftest.py` is exempt from Rule 4 and must instead hold NO spawn
        site at all: a marker only tiers the test that declares it, so a
        spawn in a conftest is untierable by construction.

THE ARGV0 TRI-STATE, STATED PLAINLY (so no reader misreads it)
    `_classify_spawn` answers REAL / NOT_A_SPAWN / UNKNOWN. UNKNOWN has TWO
    separate dispositions, at two different LAYERS, and they point in
    OPPOSITE directions on purpose -- collapsing them into one reading is
    wrong:
      - ARGV0 LAYER (does this call's own argv0 evidence a real binary):
        a `[<list-literal>] + <expr>` BinOp-concatenated argv now resolves
        to the left operand's first element, same as a plain list literal --
        this is the ONLY change to argv0-layer resolution in this file.
      - ROUTE LAYER (does a module-level call REACH a spawn transitively,
        Rule 1 only): a tracked spawn call, or a module-level call reaching
        one, whose argv0 is UNKNOWN counts as a spawn for Rule 1 -- but ONLY
        at module scope. At function scope, UNKNOWN remains UNKNOWN and
        Rules 2/4 are unchanged (they keep reading `func_spawns` alone,
        never the UNKNOWN buckets). `__main__`-guarded bodies never execute
        on import and are excluded from this promotion. Route-layer UNKNOWN
        elsewhere (e.g. a function-scope condition dominating an argv0, as
        in `test_kind_axis.py`'s `_RUN_GIT_SPAWN_VERBS` guard) is explicitly
        OUT OF SCOPE -- a documented limitation, not a fix; inter-procedural
        constant propagation through a ~10k-line module inside a pytest
        collection pass is not statically tractable, and a rule tuned until
        exactly that one file passes would be the same per-file exemption
        shape `test_no_grandfather_clause_is_reintroduced` exists to catch.
        `test_kind_axis.py` stays red.

MOCK-SEAM SUPPRESSION
    A file that `patch.object`/`monkeypatch.setattr`s a module's own
    `subprocess`/`os` attribute suppresses an indirect-spawn route into
    THAT SAME module, narrowly (same file, same target module) -- otherwise
    a test that mocks the only spawn in a helper it calls would still read
    as spawning. Computed per-file in this module's own scanner (never in
    `WrapperResolver`, whose `_wrapper_cache` is shared across the whole
    collection run and would make the suppression collection-order-
    dependent).

WHAT THIS COLLECTOR RESOLVES AND DOES NOT RESOLVE, AFTER C6 (three named
items -- do not compress into one "UNKNOWN is resolved now" sentence):
    - MOCK-SEAM PATCHING is now handled (C6a, "MOCK-SEAM SUPPRESSION"
      above): a file that patches its own target module's tracked
      `subprocess`/`os` attribute no longer reads as reaching a spawn
      through that route. Recorded, closed record of the prior gap this
      plan absorbed rather than deferred: `state/bug-backlog/2026-08-20-
      spawn-ratchet-collector-treats-a-binop-b-afe8334260b8.yaml`.
    - ARGV0-LAYER UNKNOWN at FUNCTION scope is resolved for the BinOp
      shape ONLY (C6c): `subprocess.run(["git"] + list(args), ...)` now
      classifies REAL, same as a plain list literal. This is not general
      ARGV0-layer resolution -- a `Name`/f-string/other unrecognised-
      literal argv0 at function scope (e.g. `shutil.which()`'s return, the
      shape both C14 files carried) remains UNKNOWN after this plan lands;
      it is not silently implied fixed.
    - MODULE-scope UNKNOWN now counts as a spawn for Rule 1 (C6d): a
      tracked spawn call, or a module-level call reaching one, whose argv0
      is UNKNOWN is promoted at module scope only, `__main__`-excluded.
      Measured blast radius: 0 direct module-level spawn sites, 2 routed
      (the two C14 files). Rules 2/4 are unaffected -- they still read
      `func_spawns` alone.
    - ROUTE-LAYER UNKNOWN (a function-scope condition dominating an argv0,
      as in `test_kind_axis.py`'s `_RUN_GIT_SPAWN_VERBS` guard) is
      explicitly NOT handled -- a documented limitation per staff-eng D3,
      not a narrow per-file rule tuned to clear one named file (that shape
      is the `_BASELINE`/`_ALLOWLIST` exemption
      `test_no_grandfather_clause_is_reintroduced` exists to catch).
      `test_kind_axis.py` stays red: a measured detector false positive
      pending a resolver decision, not a regression.

    Full union after C1-C14: 34 of 35 original arrears, plus 77 of 77
    C7-C12 measured-hidden files, plus C13's 4 containment-check files,
    plus C14's Rule 1 fixes, all clear; `test_kind_axis.py` is the one
    named, recorded exception -- the ratchet does not exit 0 on all four
    rules after this plan's chunks land.

    A SEPARATE, out-of-scope known-gap: `spawn_policy.detect.
    _is_python_source`'s extensionless-CLI blind spot is a different
    detector, a different gate, and a PM-side register ruling this plan
    does not take.

THE GUARD ITSELF MUST NOT SPAWN
    Pure AST parse + filesystem walk. No `git ls-files`, no shelling out to
    find spawners -- that would be self-defeating for a guard whose entire
    point is that spawning during test collection is what broke sessions.

Spec backlink: state/audits/2026-08-07-spawn-heavy-test-excision-ledger.md
Why the tiering is blanket rather than threshold-scoped:
    state/audits/2026-08-14-spawn-baseline-tier-threshold-evidence.md
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

from coordinator_core.spawn_policy.marker_check import (
    decorator_names as _decorator_names,
    has_marker_decorator as _has_spawns_process_marker,
    has_module_level_pytestmark as _has_module_level_pytestmark,
)
from coordinator_core.spawn_policy.wrapper_resolution import WrapperResolver

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# testpaths -- read from pyproject.toml, never hardcoded (CLAUDE.md: "Read
# testpaths and suite size off pyproject.toml, never off prose -- both drift
# on every edit to that config.")
# ---------------------------------------------------------------------------


def _read_testpaths() -> list[str]:
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover - stdlib always has it on 3.11+
        import tomli as tomllib  # type: ignore[no-redef]

    pyproject = REPO_ROOT / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return list(data["tool"]["pytest"]["ini_options"]["testpaths"])


# ---------------------------------------------------------------------------
# Spawn-site detection (pure AST, no execution).
# ---------------------------------------------------------------------------

_SPAWN_ATTRS = frozenset(
    {
        ("subprocess", "run"),
        ("subprocess", "Popen"),
        ("subprocess", "check_output"),
        ("subprocess", "check_call"),
        ("subprocess", "call"),
        ("os", "system"),
    }
)

_REAL_BINARIES = frozenset(
    {"git", "bash", "sh", "pwsh", "powershell", "node", "npm", "python", "python3"}
)


def _call_target(node: ast.Call) -> tuple[str, str] | None:
    """Return (module_alias, attr) for a `module.attr(...)` call, or None."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    if not isinstance(func.value, ast.Name):
        return None
    return (func.value.id, func.attr)


def _generic_call_target(node: ast.Call) -> tuple[str, str] | tuple[str, str, str] | None:
    """Return a coarse target descriptor for ANY call (spawn or not), used to
    resolve indirect spawns reached through a local or imported helper:
    `("name", "foo")` for a bare `foo(...)` call, or `("attr", "obj", "attr")`
    for `obj.attr(...)`. Anything else (chained calls, subscripted calls,
    etc.) returns None and is simply invisible to indirect resolution --
    consistent with the "skip, don't guess" honesty rule for this widening."""
    func = node.func
    if isinstance(func, ast.Name):
        return ("name", func.id)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return ("attr", func.value.id, func.attr)
    return None


def _first_argv_element(node: ast.Call) -> ast.expr | None:
    """Return the first element evidencing argv[0], for either a
    `subprocess.run([...])`-style call (first positional arg is a
    list/tuple literal -- return its first element), a `[<literal>, ...] +
    <expr>` BinOp-concatenated argv (return the LEFT operand's first element
    -- concatenating a list never changes its own first element, mirroring
    `coordinator_core/spawn_policy/detect.py::_resolve_argv0`'s identical
    `isinstance(node.left, (ast.List, ast.Tuple))` BinOp/Add branch, the
    source of this rule), or an `os.system("...")` single-string call
    (return the string arg itself, so the string-literal check below can
    look for a leading real-binary token)."""
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, (ast.List, ast.Tuple)):
        if not first.elts:
            return None
        return first.elts[0]
    if (
        isinstance(first, ast.BinOp)
        and isinstance(first.op, ast.Add)
        and isinstance(first.left, (ast.List, ast.Tuple))
    ):
        if not first.left.elts:
            return None
        return first.left.elts[0]
    return first


_SPAWN_ATTR_NAMES_BY_MODULE: dict[str, frozenset[str]] = {
    "subprocess": frozenset({"run", "Popen", "check_output", "check_call", "call"}),
    "os": frozenset({"system"}),
}

# Sentinel attr value marking a `spawn_aliases` entry as "this local name IS
# the module itself" (e.g. `import subprocess as sp` binds sp -> ("subprocess",
# _MODULE_ALIAS)), as opposed to "this local name IS one specific attr of the
# module" (e.g. `from subprocess import run` binds run -> ("subprocess",
# "run")). Empty string rather than a second dict or a tagged class: no attr
# name is ever the empty string, so it can't collide with a real binding, and
# the alias map stays a single `dict[str, tuple[str, str]]` -- one type for
# the whole `_alias_stack`, which is what the scoping/shadowing machinery
# (`_child_scope_aliases`, `_scope_shadowed_names`) is built to thread.
_MODULE_ALIAS = ""


def _collect_spawn_aliases(scope: ast.Module | list[ast.stmt]) -> dict[str, tuple[str, str]]:
    """Map a bare local name to its `(module, attr)` pair for `from
    subprocess import run` / `from os import system` style imports (aliased
    or not) -- `_call_target` only recognizes `module.attr(...)` call
    shapes, so without this a bare `run(...)` reached through such an
    import is invisible to spawn classification -- and for `import
    subprocess as sp` / `import os as o` style module aliases, recorded as
    `(module, _MODULE_ALIAS)` so `sp.run(...)` can be resolved back to
    `subprocess.run` before the `_SPAWN_ATTRS` lookup. Without the latter, a
    module-alias import escapes detection entirely: `_call_target` returns
    the alias name verbatim (`("sp", "run")`), which never matches
    `_SPAWN_ATTRS`' `("subprocess", "run")`, so the call reads as
    NOT_A_SPAWN instead of flowing through argv0 classification.

    An unaliased `import subprocess` needs no entry here -- the bare name
    already equals the module name, so `_call_target`'s literal
    `module.attr` read resolves it without any alias lookup. A dotted
    import (`import a.b as c`) is skipped: resolving it would mean tracking
    which of `a`/`a.b` carries the spawn attrs, not a free extension of the
    existing single-level alias map."""
    body = scope.body if isinstance(scope, ast.Module) else scope
    aliases: dict[str, tuple[str, str]] = {}
    for node in body:
        if isinstance(node, ast.ImportFrom):
            if node.level or node.module not in _SPAWN_ATTR_NAMES_BY_MODULE:
                continue
            valid_attrs = _SPAWN_ATTR_NAMES_BY_MODULE[node.module]
            for alias in node.names:
                if alias.name in valid_attrs:
                    local = alias.asname or alias.name
                    aliases[local] = (node.module, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname is None or "." in alias.name:
                    continue
                if alias.name in _SPAWN_ATTR_NAMES_BY_MODULE:
                    aliases[alias.asname] = (alias.name, _MODULE_ALIAS)
    return aliases


def _scope_shadowed_names(body: list[ast.stmt]) -> set[str]:
    """Names this scope rebinds by means OTHER than a spawn import -- a
    local `def run(...)`, `run = ...`, or a class of that name. An inherited
    alias for such a name must be dropped rather than resolved, or a helper
    innocently named `run` would be classified as a subprocess spawn."""
    shadowed: set[str] = set()
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            shadowed.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    shadowed.add(target.id)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name):
                shadowed.add(node.target.id)
    return shadowed


def _child_scope_aliases(
    inherited: dict[str, tuple[str, str]], body: list[ast.stmt]
) -> dict[str, tuple[str, str]]:
    """Lexical alias map for a nested scope: what it inherits, minus what it
    shadows, plus its own spawn imports. Deliberately whole-scope rather than
    statement-ordered -- an import late in a body still covers calls above it,
    which over-detects rather than under-detects, the only safe direction for
    a guard whose failure mode is reporting clean."""
    local = _collect_spawn_aliases(body)
    shadowed = _scope_shadowed_names(body)
    merged = {
        name: target for name, target in inherited.items() if name not in shadowed
    }
    merged.update(local)
    return merged


def _resolve_call_target(
    node: ast.Call, spawn_aliases: dict[str, tuple[str, str]]
) -> tuple[str, str] | None:
    """`_call_target`'s attribute-based resolution, widened to also resolve
    a bare `Name` callee through `spawn_aliases` (Gap 2: `from subprocess
    import run` then `run(...)`), and a module-aliased attribute callee
    (Gap 3: `import subprocess as sp` then `sp.run(...)`) by substituting
    the alias's real module name -- read off the `_MODULE_ALIAS` sentinel
    entry -- before `_call_target`'s literal `module.attr` read would
    otherwise return the alias name verbatim and miss `_SPAWN_ATTRS`."""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        alias = spawn_aliases.get(func.value.id)
        if alias is not None and alias[1] == _MODULE_ALIAS:
            return (alias[0], func.attr)
    target = _call_target(node)
    if target is not None:
        return target
    if isinstance(node.func, ast.Name):
        return spawn_aliases.get(node.func.id)
    return None


def _classify_spawn(
    node: ast.Call, spawn_aliases: dict[str, tuple[str, str]]
) -> str:
    """Tri-state classification: "REAL" (evidenced real-binary argv0),
    "NOT_A_SPAWN" (not a call to a tracked subprocess/os entry point at
    all), or "UNKNOWN" (the call target IS a tracked spawn entry point but
    argv0 cannot be resolved to a real binary by static inspection --
    non-literal argv0, absent argv0, or a string literal whose basename
    isn't in `_REAL_BINARIES`).

    UNKNOWN is deliberately NOT folded into either "real spawn" or "not a
    spawn": guessing either way would either inflate Rule 1/2 violations
    with false positives or silently under-detect real spawns (the exact
    argv0-recognition blind spot this tri-state exists to make visible
    rather than silent -- see `_UNKNOWN_ARGV0`)."""
    target = _resolve_call_target(node, spawn_aliases)
    if target is None or target not in _SPAWN_ATTRS:
        return "NOT_A_SPAWN"
    argv0 = _first_argv_element(node)
    if argv0 is None:
        return "UNKNOWN"

    # sys.executable -- attribute access, not a literal, but a reliable
    # "this really launches a process" signal per the spec.
    if isinstance(argv0, ast.Attribute) and argv0.attr == "executable":
        if isinstance(argv0.value, ast.Name) and argv0.value.id == "sys":
            return "REAL"

    if isinstance(argv0, ast.Constant) and isinstance(argv0.value, str):
        token = argv0.value.strip().split()[0] if argv0.value.strip() else ""
        # Strip a possible path prefix / extension so `python3.11`,
        # `/usr/bin/git`, `git.exe` etc. still evidence the real binary.
        base = Path(token).name
        if base.endswith(".exe"):
            base = base[: -len(".exe")]
        return "REAL" if base in _REAL_BINARIES else "UNKNOWN"

    return "UNKNOWN"


class _FunctionSpawnScanner(ast.NodeVisitor):
    """Walks a module's AST, classifying every real spawn `Call` node as
    either module-level (Rule 1) or belonging to some enclosing function
    (Rule 2), and recording which enclosing `def`/`async def` names contain
    at least one spawn site plus whatever decorators that `def` carries."""

    def __init__(self, relpath: str, spawn_aliases: dict[str, tuple[str, str]]) -> None:
        self.relpath = relpath
        self.spawn_aliases = spawn_aliases
        # Lexical alias scopes, innermost last. A spawn import inside a
        # function body binds only within it, so classification reads the
        # top of this stack rather than a single module-wide map.
        self._alias_stack: list[dict[str, tuple[str, str]]] = [spawn_aliases]
        self.module_level_spawns: list[ast.Call] = []
        # (d) MODULE-SCOPE UNKNOWN DISARMS RULE 1 -- a PARALLEL pair of
        # buckets to module_level_spawns/func_spawns, holding calls whose
        # target IS a tracked spawn entry point but whose argv0 classified
        # UNKNOWN. Rules 2/4 keep reading func_spawns alone, untouched;
        # `_analyze_file`'s Rule 1 walk reads the UNION for module scope
        # only. See module docstring "(d)".
        self.module_level_unknown_spawns: list[ast.Call] = []
        self.func_unknown_spawns: dict[str, list[ast.Call]] = {}
        # enclosing function name -> spawns found directly in its body
        # (nested defs get their own entry; a spawn inside a nested helper
        # is NOT attributed to the outer test function).
        self.func_spawns: dict[str, list[ast.Call]] = {}
        self.func_decorators: dict[str, list[ast.expr]] = {}
        self._func_stack: list[str] = []
        # Indirect-spawn support: every call target (spawn or not), bucketed
        # the same way as func_spawns/module_level_spawns, so an enclosing
        # function/module scope can be checked for "does it call a locally-
        # or import-resolved spawn wrapper" without re-walking the tree.
        self.module_level_calls: list[tuple[int, tuple]] = []
        self.generic_calls: dict[str, list[tuple]] = {}

    def _current_func(self) -> str | None:
        return self._func_stack[-1] if self._func_stack else None

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 (ast API name)
        fname = self._current_func()
        classification = _classify_spawn(node, self._alias_stack[-1])
        if classification == "REAL":
            if fname is None:
                self.module_level_spawns.append(node)
            else:
                self.func_spawns.setdefault(fname, []).append(node)
        elif classification == "UNKNOWN":
            _UNKNOWN_ARGV0.add(f"{self.relpath}:{node.lineno}")
            if fname is None:
                self.module_level_unknown_spawns.append(node)
            else:
                self.func_unknown_spawns.setdefault(fname, []).append(node)
        target = _generic_call_target(node)
        if target is not None:
            if fname is None:
                self.module_level_calls.append((node.lineno, target))
            else:
                self.generic_calls.setdefault(fname, []).append(target)
        self.generic_visit(node)

    def _visit_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.func_decorators.setdefault(node.name, [])
        self.func_decorators[node.name].extend(node.decorator_list)
        self._func_stack.append(node.name)
        self._alias_stack.append(_child_scope_aliases(self._alias_stack[-1], node.body))
        self.generic_visit(node)
        self._alias_stack.pop()
        self._func_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_def(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_def(node)


# `_decorator_names` / `_has_spawns_process_marker` / `_has_module_level_pytestmark`
# now live in `coordinator_core.spawn_policy.marker_check` (C4, staff-eng
# F7) and are imported above under their original names -- lifted, not
# duplicated, so this file and the new write-time nudge guard
# (`coordinator_core.write_guards.nudge_unmarked_spawning_test`) share one
# definition rather than two independently-maintained copies.


# ---------------------------------------------------------------------------
# Indirect-spawn resolution: a module-level `def foo(...)` whose body
# contains a direct spawn site is a "spawn wrapper" for that module; any
# call to `foo(...)` elsewhere (in that module, or in another module that
# imports it) is itself a spawn site. Resolved transitively (a wrapper
# calling a wrapper), and honestly bounded: a module that cannot be
# resolved on disk (external package, dynamic import, `importlib`) is
# SKIPPED, not guessed, and counted so under-detection stays visible rather
# than silent.
#
# The cross-file resolution machinery itself (`_ImportBinding`,
# `_resolve_absolute_module`, `_collect_imports`, `_func_is_wrapper`, ...)
# is hoisted into `coordinator_core.spawn_policy.wrapper_resolution` --
# generic over this file's own spawn-classification scanner via
# `_build_scanner` below -- so a vendoring refresh of `spawn_policy/`
# carries this widening. See that module's docstring for the "what this
# solves" framing this comment used to carry in full.
# ---------------------------------------------------------------------------


def _build_scanner(relpath: str, tree: ast.Module) -> _FunctionSpawnScanner:
    spawn_aliases = _collect_spawn_aliases(tree)
    scanner = _FunctionSpawnScanner(relpath, spawn_aliases)
    scanner.visit(tree)
    return scanner


_WRAPPER_RESOLVER = WrapperResolver(REPO_ROOT, _build_scanner)
_WRAPPER_CACHE = _WRAPPER_RESOLVER._wrapper_cache  # exposed for test introspection only
_UNRESOLVED_IMPORTS = _WRAPPER_RESOLVER.unresolved_imports
# Honesty bucket sibling to `_UNRESOLVED_IMPORTS`: every spawn-site call
# target (subprocess.run/Popen/etc, or os.system) whose argv0 could not be
# statically resolved to a real binary -- non-literal argv0 (a Name, an
# f-string, a call, a subscript...), absent argv0, or a string literal
# whose basename isn't in `_REAL_BINARIES`. Reported, never guessed either
# direction -- see `_classify_spawn`.
_UNKNOWN_ARGV0: set[str] = set()


def _get_module_info(path: Path):
    return _WRAPPER_RESOLVER.get_module_info(path)


def _target_reaches_spawn(info, target: tuple, visiting: frozenset) -> bool:
    return _WRAPPER_RESOLVER.target_reaches_spawn(info, target, visiting)


def _func_is_wrapper(module_path: Path, func_name: str, visiting: frozenset = frozenset()) -> bool:
    return _WRAPPER_RESOLVER.func_is_wrapper(module_path, func_name, visiting)


def _func_reaches_spawn_indirectly(info, func_name: str) -> bool:
    return _WRAPPER_RESOLVER.func_reaches_spawn_indirectly(info, func_name)


# ---------------------------------------------------------------------------
# (a) MOCK-SEAM BLINDNESS -- a same-file-scoped suppression, sited in THIS
# file (the caller), never in `WrapperResolver`: that resolver's
# `_wrapper_cache` is one dict for the whole collection run, so a per-file
# suppression computed inside `func_is_wrapper` would be first-file-wins
# and nondeterministic under `-n auto`. See module docstring "(a)".
# ---------------------------------------------------------------------------


def _patch_target_module_expr(call: ast.Call) -> ast.expr | None:
    """Return the target-module expr of a `patch.object(<mod>.subprocess,
    "run", ...)` / `monkeypatch.setattr(<mod>.subprocess, ...)`-shaped call,
    or None. Recognizes `mock.patch.object(...)`/`patch.object(...)` (any
    dotted prefix ending in `.patch.object`) and `<name>.setattr(...)` where
    `<name>` looks like a monkeypatch fixture parameter."""
    if not call.args:
        return None
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    first_arg = call.args[0]
    if func.attr == "object":
        base = func.value
        base_name = base.id if isinstance(base, ast.Name) else (
            base.attr if isinstance(base, ast.Attribute) else None
        )
        if base_name == "patch":
            return first_arg
        return None
    if func.attr == "setattr":
        base = func.value
        if isinstance(base, ast.Name) and "monkeypatch" in base.id.lower():
            return first_arg
    return None


def _collect_patched_module_aliases(tree: ast.Module) -> set[str]:
    """Every local alias name `<alias>` this FILE patches the tracked
    `subprocess`/`os` attribute of, anywhere in the file (a test body, a
    module-level helper it calls, a fixture) -- `patch.object(<alias>.
    subprocess, "run", ...)` / `monkeypatch.setattr(<alias>.subprocess,
    ...)`. Whole-file, not module-scope-statement-only: real fixtures patch
    inside test functions and helper functions, not at module level."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target_expr = _patch_target_module_expr(node)
        if target_expr is None:
            continue
        if (
            isinstance(target_expr, ast.Attribute)
            and isinstance(target_expr.value, ast.Name)
            and target_expr.attr in _SPAWN_ATTR_NAMES_BY_MODULE
        ):
            aliases.add(target_expr.value.id)
    return aliases


def _func_reaches_spawn_indirectly_excluding_patched(
    info, func_name: str, patched_module_paths: set[Path]
) -> bool:
    """Same one-hop walk as `WrapperResolver.func_reaches_spawn_indirectly`,
    except a route whose resolved target is a module-attribute access
    (`("attr", obj_name, attr)`) into a module THIS FILE itself patches is
    skipped entirely -- narrowly, same file + same target module, so an
    unpatched spawn in a file that merely patches something unrelated is
    not suppressed (the inverse false negative)."""
    for target in info.scanner.generic_calls.get(func_name, []):
        if target[0] == "attr":
            obj_name = target[1]
            binding = info.imports.get(obj_name)
            if (
                binding is not None
                and binding.kind == "module"
                and binding.module_path in patched_module_paths
            ):
                continue
        if _target_reaches_spawn(info, target, frozenset()):
            return True
    return False


# ---------------------------------------------------------------------------
# (d) MODULE-SCOPE UNKNOWN DISARMS RULE 1 -- reimplemented here, not
# threaded into `WrapperResolver` (out of this chunk's writes scope; see
# D1/(a)). A SEPARATE cache, keyed the same as `WrapperResolver.
# _wrapper_cache` but never sharing that dict, so a module-scope
# (promote-unknown) answer for `(module_path, func_name)` can never leak
# into a function-scope (Rules 2/4, unmodified `WrapperResolver`) query for
# the identical pair -- the collision D2 named for (a), re-emerging here if
# the caches were shared. See module docstring "(d)".
#
# Sited on `_WRAPPER_RESOLVER._promote_unknown_wrapper_cache` (staff-eng
# F4), not a module-level global -- `wrapper_resolution.py`'s own docstring
# ("NOT A SINGLETON -- no module-level mutable cache here") governs this
# caller too, and the disjointness above is unaffected: it is still a
# separate dict from `_wrapper_cache`, just owned by the same resolver
# instance rather than duplicated at module scope.
# ---------------------------------------------------------------------------


def _target_reaches_spawn_promoting_unknown(
    info, target: tuple, visiting: frozenset
) -> bool:
    result, _truncated = _target_reaches_spawn_promoting_unknown_ex(info, target, visiting)
    return result


def _target_reaches_spawn_promoting_unknown_ex(
    info, target: tuple, visiting: frozenset
) -> tuple[bool, bool]:
    """Like `_target_reaches_spawn_promoting_unknown`, plus a `truncated`
    bool -- see `_func_is_wrapper_promoting_unknown_ex` for the invariant."""
    if target[0] == "name":
        name = target[1]
        if name in info.top_level_funcs:
            return _func_is_wrapper_promoting_unknown_ex(info.path, name, visiting)
        binding = info.imports.get(name)
        if binding is not None and binding.kind == "func":
            return _func_is_wrapper_promoting_unknown_ex(
                binding.module_path, binding.orig_name, visiting
            )
    elif target[0] == "attr":
        obj_name, attr = target[1], target[2]
        binding = info.imports.get(obj_name)
        if binding is not None and binding.kind == "module":
            return _func_is_wrapper_promoting_unknown_ex(binding.module_path, attr, visiting)
    return False, False


def _func_is_wrapper_promoting_unknown(
    module_path: Path, func_name: str, visiting: frozenset = frozenset()
) -> bool:
    """Rule 1's module-scope widening: a module-level def counts as a
    spawn-wrapper, transitively, if it directly contains a REAL spawn OR one
    whose argv0 is UNKNOWN (`func_spawns` UNION `func_unknown_spawns`).
    Cycle-safe via `visiting`, exactly like `WrapperResolver.func_is_wrapper`
    -- reach is `_target_reaches_spawn`'s existing transitive walk, no new
    bound. REACH IS ALREADY DEFINED -- ADD NO NEW BOUND."""
    result, _truncated = _func_is_wrapper_promoting_unknown_ex(module_path, func_name, visiting)
    return result


def _func_is_wrapper_promoting_unknown_ex(
    module_path: Path, func_name: str, visiting: frozenset
) -> tuple[bool, bool]:
    """`_func_is_wrapper_promoting_unknown`'s real body, plus a `truncated`
    bool: True if this answer passed through a `key in visiting` cycle
    guard anywhere on the path (staff-eng F3, mirroring
    `WrapperResolver._func_is_wrapper_ex`, which this was copied from
    verbatim including the flaw). A cycle-truncated `False` is a
    placeholder to break the recursion, not a proven fact about `key`, and
    must never be written into the cache -- caching it lets a mutually-
    recursive function's answer depend on which function in the cycle was
    resolved first. A `True` result is never truncation-tainted."""
    cache = _WRAPPER_RESOLVER._promote_unknown_wrapper_cache
    key = (module_path, func_name)
    if key in cache:
        return cache[key], False
    if key in visiting:
        return False, True
    info = _get_module_info(module_path)
    if info is None or func_name not in info.top_level_funcs:
        cache[key] = False
        return False, False
    scanner = info.scanner
    if scanner.func_spawns.get(func_name) or scanner.func_unknown_spawns.get(func_name):
        cache[key] = True
        return True, False
    next_visiting = visiting | {key}
    result = False
    truncated = False
    for target in scanner.generic_calls.get(func_name, []):
        target_result, target_truncated = _target_reaches_spawn_promoting_unknown_ex(
            info, target, next_visiting
        )
        if target_truncated:
            truncated = True
        if target_result:
            result = True
            break
    if not truncated:
        cache[key] = result
    return result, truncated


def _is_main_guard_test(test: ast.expr) -> bool:
    """True for `__name__ == "__main__"` or `"__main__" == __name__`."""
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    left, right = test.left, test.comparators[0]

    def _is_dunder_name(n: ast.expr) -> bool:
        return isinstance(n, ast.Name) and n.id == "__name__"

    def _is_main_str(n: ast.expr) -> bool:
        return isinstance(n, ast.Constant) and n.value == "__main__"

    return (_is_dunder_name(left) and _is_main_str(right)) or (
        _is_main_str(left) and _is_dunder_name(right)
    )


def _main_guard_body_linenos(tree: ast.Module) -> set[int]:
    """Every lineno belonging to the BODY (never the `orelse`) of an
    `if __name__ == "__main__":` block anywhere in the module -- that code
    never executes on import, so it MUST be excluded from (d)'s promotion.
    Pre-pass over the whole tree rather than statement-context tracking in
    `_FunctionSpawnScanner`, which has none. Scoped to the newly-promoted
    UNKNOWN population only -- callers must NOT apply this to the existing
    REAL-spawn routing, which stays unfiltered/unchanged."""
    excluded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_main_guard_test(node.test):
            for stmt in node.body:
                for sub in ast.walk(stmt):
                    lineno = getattr(sub, "lineno", None)
                    if lineno is not None:
                        excluded.add(lineno)
    return excluded


class FileSpawnReport:
    __slots__ = ("relpath", "module_level_linenos", "unmarked_spawning_funcs", "has_any_spawn")

    def __init__(
        self,
        relpath: str,
        module_level_linenos: list[int],
        unmarked_spawning_funcs: list[str],
        has_any_spawn: bool,
    ) -> None:
        self.relpath = relpath
        self.module_level_linenos = module_level_linenos
        self.unmarked_spawning_funcs = unmarked_spawning_funcs
        self.has_any_spawn = has_any_spawn


def _analyze_file(path: Path, relpath: str) -> FileSpawnReport:
    info = _get_module_info(path)
    if info is None:
        return FileSpawnReport(relpath, [], [], False)
    tree_for_marker = ast.parse(
        path.read_text(encoding="utf-8", errors="replace"), filename=relpath
    )
    scanner = info.scanner

    module_level_linenos = sorted(node.lineno for node in scanner.module_level_spawns)
    # Rule 1 also covers an import-time CALL to a spawn wrapper -- e.g. a
    # module-level `_ensure_repo_synced()` that itself shells out.
    for lineno, target in scanner.module_level_calls:
        if _target_reaches_spawn(info, target, frozenset()):
            module_level_linenos.append(lineno)

    # (d) MODULE-SCOPE UNKNOWN DISARMS RULE 1: at module scope only, a
    # tracked spawn call (or a module-level call reaching one) whose argv0
    # is UNKNOWN also counts for Rule 1 -- Rules 2/4 below keep reading
    # `func_spawns` alone, unchanged. `__main__`-guarded bodies never
    # execute on import and are excluded from THIS promotion only; the
    # REAL-spawn routing above stays untouched. See module docstring "(d)".
    main_guard_linenos = _main_guard_body_linenos(tree_for_marker)
    for node in scanner.module_level_unknown_spawns:
        if node.lineno not in main_guard_linenos:
            module_level_linenos.append(node.lineno)
    for lineno, target in scanner.module_level_calls:
        if lineno in main_guard_linenos:
            continue
        if _target_reaches_spawn_promoting_unknown(info, target, frozenset()):
            module_level_linenos.append(lineno)

    module_level_linenos = sorted(set(module_level_linenos))

    # (a) MOCK-SEAM BLINDNESS: this file's own module-scope patch targets
    # (`patch.object(<mod>.subprocess, ...)` / `monkeypatch.setattr(<mod>.
    # subprocess, ...)`), resolved to on-disk module paths via THIS file's
    # own import bindings -- suppresses an indirect-spawn route into a
    # module the file itself patches. See module docstring "(a)".
    patched_aliases = _collect_patched_module_aliases(tree_for_marker)
    patched_module_paths = {
        info.imports[alias].module_path
        for alias in patched_aliases
        if alias in info.imports and info.imports[alias].kind == "module"
    }

    file_wide_marker = _has_module_level_pytestmark(tree_for_marker)
    unmarked_funcs: list[str] = []
    spawning_func_names = set(scanner.func_spawns) | set(scanner.generic_calls)
    any_func_spawn = False
    for fname in spawning_func_names:
        direct = bool(scanner.func_spawns.get(fname))
        indirect = _func_reaches_spawn_indirectly_excluding_patched(
            info, fname, patched_module_paths
        )
        if not (direct or indirect):
            continue
        any_func_spawn = True
        if file_wide_marker:
            continue
        decorators = scanner.func_decorators.get(fname, [])
        if _has_spawns_process_marker(decorators):
            continue
        unmarked_funcs.append(fname)

    has_any_spawn = bool(module_level_linenos) or any_func_spawn
    return FileSpawnReport(relpath, module_level_linenos, sorted(unmarked_funcs), has_any_spawn)


def _rel(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def _iter_test_files() -> list[Path]:
    """AST-target set: every `test_*.py` / `conftest.py` under the
    configured testpaths. Pure filesystem walk -- no subprocess involved."""
    out: list[Path] = []
    seen: set[Path] = set()
    for testpath in _read_testpaths():
        root = REPO_ROOT / testpath
        if root.is_file():
            candidates = [root]
        elif root.is_dir():
            candidates = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [
                    d for d in dirnames if d not in ("__pycache__", ".pytest_cache", "node_modules", ".git")
                ]
                for name in filenames:
                    if name == "conftest.py" or (name.startswith("test_") and name.endswith(".py")):
                        candidates.append(Path(dirpath) / name)
        else:
            continue
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                out.append(candidate)
    return sorted(out, key=lambda p: _rel(p))


def _all_reports() -> list[FileSpawnReport]:
    return [_analyze_file(path, _rel(path)) for path in _iter_test_files()]


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# THE GRANDFATHER CLAUSE IS DISCHARGED (2026-08-14).
#
# `_BASELINE` was a frozen list of ~600 pre-existing spawning test files,
# tolerated because a strict rule would have failed against all of them on day
# one. It is gone: every file it held now declares `spawns_process` AND is
# tiered onto `cadence` by Rule 4, so the tolerated-exception list has become a
# rule that enforces itself. There is no allowlist to add a file to any more --
# that is the point, and re-introducing one is what
# `test_no_grandfather_clause_is_reintroduced` exists to catch.
#
# Why blanket rather than a heaviness threshold: the per-commit tier does not
# run the test suite. This repo's pre-commit hook runs one gate
# (detect-staged-rollback) and no pytest at all, so moving a test onto the
# cadence suite reschedules it -- it does not stop it running and costs no
# coverage. There was no tradeoff to tune, and so no threshold to pick.
# ---------------------------------------------------------------------------


_ALTERNATIVE_MSG_RULE1 = (
    "Fix: compute the repo root statically, never spawn to find it -- "
    "`Path(__file__).resolve().parents[N]` (pick N for this file's depth "
    "under the repo root), not `git rev-parse --show-toplevel` or any other "
    "subprocess call at module level. Import-time spawns fire during pytest "
    "COLLECTION -- before -k filtering or --collect-only ever get a chance "
    "to skip them -- which is why this rule has no allowlist and no marker "
    "escape; see state/audits/2026-08-07-spawn-heavy-test-excision-ledger.md."
)

_ALTERNATIVE_MSG_RULE2 = (
    "Fix: either (a) decorate every spawning test in this file with "
    "@pytest.mark.spawns_process (or set module-level "
    "`pytestmark = [pytest.mark.spawns_process]` to cover the whole file), "
    "declaring the real-process spawn explicitly, or (b) rewrite the test "
    "against a mocked/faked git rather than a real spawned process. There is "
    "no allowlist to add the file to -- the grandfather list was discharged "
    "on 2026-08-14. A declared spawn must also carry `pytest.mark.cadence` "
    "(Rule 4)."
)


def test_rule1_no_import_time_spawns() -> None:
    """Rule 1 -- STRICT, no allowlist, no marker escape. A spawn site at
    module level fails unconditionally for every file, always. See module
    docstring 'Rule 1' section for why this has no escape hatch."""
    violations: list[str] = []
    for report in _all_reports():
        for lineno in report.module_level_linenos:
            violations.append(f"{report.relpath}:{lineno}")
    if violations:
        lines = "\n".join(f"  {v}" for v in sorted(violations))
        pytest.fail(
            f"SPAWN-RATCHET Rule 1: {len(violations)} import-time (module-level) "
            f"real-process spawn(s) found (direct or via a resolved spawn-wrapper "
            f"call; {len(_UNRESOLVED_IMPORTS)} import(s) and {len(_UNKNOWN_ARGV0)} "
            f"spawn-site argv0(s) were unresolvable and skipped rather than "
            f"guessed):\n{lines}\n\n{_ALTERNATIVE_MSG_RULE1}"
        )


def test_rule2_new_spawning_files_ratchet() -> None:
    """Rule 2 -- a file with a function-level spawn site must be fully
    marker-declared. See module docstring 'Rule 2' section."""
    violations: list[str] = []
    for report in _all_reports():
        if not report.unmarked_spawning_funcs:
            continue
        funcs = ", ".join(report.unmarked_spawning_funcs)
        violations.append(f"{report.relpath} (unmarked: {funcs})")
    if violations:
        lines = "\n".join(f"  {v}" for v in sorted(violations))
        pytest.fail(
            f"SPAWN-RATCHET Rule 2: {len(violations)} spawning test file(s) "
            f"that do not declare it (direct or via a "
            f"resolved spawn-wrapper call; {len(_UNRESOLVED_IMPORTS)} import(s) "
            f"and {len(_UNKNOWN_ARGV0)} spawn-site argv0(s) were unresolvable "
            f"and skipped rather than guessed):\n{lines}\n\n"
            f"{_ALTERNATIVE_MSG_RULE2}"
        )


def test_rule4_every_spawning_file_is_cadence_tiered() -> None:
    """Rule 4 -- a file that spawns a real process must ALSO carry
    `pytest.mark.cadence`, so it runs at cadence gates and not on whatever
    tier happens to sweep it up. See module docstring 'Rule 4'."""
    untiered: list[str] = []
    for path in _iter_test_files():
        relpath = _rel(path)
        if path.name == "conftest.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        report = _analyze_file(path, relpath)
        spawns = bool(report.module_level_linenos) or report.has_any_spawn
        if not spawns:
            continue
        if _has_module_level_pytestmark(tree, "pytest.mark.cadence"):
            continue
        untiered.append(relpath)
    assert not untiered, (
        f"SPAWN-RATCHET Rule 4: {len(untiered)} file(s) spawn a real process but are "
        "not tiered onto the cadence suite:\n"
        + "\n".join(f"  {u}" for u in sorted(untiered))
        + "\n\nFix: add `pytest.mark.cadence` to the module-level `pytestmark` list "
        "(alongside `pytest.mark.spawns_process`), or rewrite the test against a "
        "faked process so it stops spawning. A conftest.py cannot be tiered by a "
        "marker at all -- move any spawning helper out of it into a sibling module."
    )


def test_no_grandfather_clause_is_reintroduced() -> None:
    """The tolerated-exception list stays dead.

    NEGATIVE SPEC: `_BASELINE` was a shrink-only allowlist of spawning test
    files. It drained to empty on 2026-08-14 and was deleted. The failure mode
    this pins is the one that made the original necessary -- a future edit,
    facing a batch of new spawning files, re-adds a frozen list instead of
    marking them, and the guard goes back to producing the appearance of
    enforcement while the population stays where it is.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    banned = [
        name
        for name in ("_BASELINE", "_ALLOWLIST", "_GRANDFATHERED", "_EXEMPT")
        if re.search(rf"^{name}\b\s*[:=]", source, re.MULTILINE)
    ]
    assert not banned, (
        "SPAWN-RATCHET: a module-level exception list was reintroduced: "
        + ", ".join(banned)
        + ". The baseline was discharged on 2026-08-14 -- a spawning test file "
        "declares `pytest.mark.spawns_process` and tiers onto `pytest.mark.cadence`, "
        "or it stops spawning. There is no list to join."
    )


def test_known_string_corpus_files_produce_zero_hits() -> None:
    """The four known string-literal-corpus files must register ZERO
    function-level spawn sites -- proof the AST walk does not match spawn
    shapes embedded as string DATA fed to other static detectors (the
    3x-inflation failure mode named in the ledger)."""
    corpus_files = [
        "coordinator_core/spawn_policy/tests/test_detect.py",
        "coordinator_core/tests/test_no_unsanctioned_shell_spawn.py",
        "coordinator_core/test_no_bare_argv0_script_launch.py",
        "coordinator_core/bash_guards/tests/test_python_c_constant_folding.py",
    ]
    bad = []
    for relpath in corpus_files:
        full_path = REPO_ROOT / relpath
        assert full_path.is_file(), f"expected corpus file missing: {relpath}"
        report = _analyze_file(full_path, relpath)
        if report.module_level_linenos or report.unmarked_spawning_funcs:
            bad.append(relpath)
    assert not bad, (
        f"AST detector wrongly matched string-literal-corpus file(s) as containing "
        f"real spawn sites: {bad} -- this means the detector is matching text, not "
        "live Call nodes, and is unsound."
    )


def test_unknown_argv0_honesty_bucket_reports_variable_argv() -> None:
    """`_UNKNOWN_ARGV0` must be non-empty across the corpus -- proof the
    honesty mechanism is actually firing, not merely present in code -- and
    a representative variable-argv shape (`argv = resolved; subprocess.run
    (argv)`, argv0 is an `ast.Name`, not a literal) must classify as
    UNKNOWN rather than silently reading as "not a spawn". Pinned via an
    inline AST-parse fixture rather than a real in-repo site count, per the
    "do not pin a brittle exact count" instruction -- corpus-wide counts
    shift as files change."""
    _all_reports()
    assert _UNKNOWN_ARGV0, (
        "expected at least one spawn-site argv0 to be flagged as UNKNOWN "
        "across the corpus -- the honesty bucket should not be empty"
    )
    src = (
        "import subprocess\n"
        "def f(resolved):\n"
        "    argv = resolved\n"
        "    subprocess.run(argv)\n"
    )
    tree = ast.parse(src)
    call_node = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert _classify_spawn(call_node, {}) == "UNKNOWN"


def _scan_source(src: str) -> _FunctionSpawnScanner:
    tree = ast.parse(src)
    scanner = _FunctionSpawnScanner("<fixture>", _collect_spawn_aliases(tree))
    scanner.visit(tree)
    return scanner


def test_function_scoped_spawn_import_is_not_invisible() -> None:
    """A `from subprocess import run` inside a function body binds a bare
    `run(...)` just as a module-level one does. Resolving only module-level
    imports reproduced the argv0 blind spot one lexical scope down: the call
    never resolved to a spawn target, so it read as "not a spawn" rather than
    landing in `_UNKNOWN_ARGV0` -- silently clean, the exact outcome the
    tri-state exists to prevent."""
    scanner = _scan_source(
        "def test_thing():\n"
        "    from subprocess import run\n"
        "    run(['git', 'status'])\n"
    )
    assert scanner.func_spawns.get("test_thing"), (
        "a function-scoped `from subprocess import run` must still bind a bare "
        "`run(...)` call as a real spawn"
    )


def test_local_def_shadows_an_inherited_spawn_alias() -> None:
    """The inverse guard on the widening above: a helper innocently named
    `run`, defined locally, must NOT be classified as subprocess.run merely
    because an enclosing scope imported that name. Over-detection here would
    be a false Rule 1/2 violation, which is the other way to make the ratchet
    untrustworthy."""
    scanner = _scan_source(
        "from subprocess import run\n"
        "def test_thing():\n"
        "    def run(x):\n"
        "        return x\n"
        "    run(['git', 'status'])\n"
    )
    assert not scanner.func_spawns.get("test_thing"), (
        "a locally-defined `run` must shadow the inherited spawn alias"
    )


def test_module_level_aliased_import_spawn_is_not_invisible() -> None:
    """`import subprocess as sp` then `sp.run([...])` inside a function must
    classify as a real spawn -- the module-alias gap this change closes.
    Before this fix, `_call_target` returned `("sp", "run")` verbatim, which
    never matched `_SPAWN_ATTRS`' `("subprocess", "run")`, so the call read
    as NOT_A_SPAWN and never reached the `_UNKNOWN_ARGV0` honesty bucket
    either."""
    scanner = _scan_source(
        "import subprocess as sp\n"
        "def test_thing():\n"
        "    sp.run(['git', 'status'])\n"
    )
    assert scanner.func_spawns.get("test_thing"), (
        "a module-level `import subprocess as sp` must still bind `sp.run(...)` "
        "as a real spawn"
    )


def test_function_scoped_aliased_import_binds_only_that_scope() -> None:
    """A function-local `import subprocess as _sp` must bind the alias only
    inside that function's lexical scope, riding the same `_alias_stack` as
    `from`-import aliases -- not a second, module-wide map that bypasses
    scoping discipline."""
    scanner = _scan_source(
        "def test_thing():\n"
        "    import subprocess as _sp\n"
        "    _sp.run(['git', 'status'])\n"
        "def test_other():\n"
        "    _sp.run(['git', 'status'])\n"
    )
    assert scanner.func_spawns.get("test_thing"), (
        "a function-scoped `import subprocess as _sp` must bind `_sp.run(...)` "
        "as a real spawn within that function"
    )
    assert not scanner.func_spawns.get("test_other"), (
        "the alias must not leak into a sibling function that never imported it"
    )


def test_local_rebinding_shadows_an_inherited_module_alias() -> None:
    """The module-alias inverse of
    `test_local_def_shadows_an_inherited_spawn_alias`: a local rebinding of
    the alias name must drop the inherited module-alias binding rather than
    resolving as a spawn, or a helper innocently reusing the name `sp` would
    be misclassified."""
    scanner = _scan_source(
        "import subprocess as sp\n"
        "def test_thing():\n"
        "    sp = object()\n"
        "    sp.run(['git', 'status'])\n"
    )
    assert not scanner.func_spawns.get("test_thing"), (
        "a local rebinding of `sp` must shadow the inherited module alias"
    )


def test_module_level_os_alias_system_call_is_a_real_spawn() -> None:
    """`import os as _os` then `_os.system("git ...")` must resolve through
    the same module-alias path as the `subprocess` case -- proof the fix
    covers every module in `_SPAWN_ATTR_NAMES_BY_MODULE`, not just
    `subprocess`."""
    scanner = _scan_source(
        "import os as _os\n"
        "def test_thing():\n"
        "    _os.system('git status')\n"
    )
    assert scanner.func_spawns.get("test_thing"), (
        "a module-level `import os as _os` must still bind `_os.system(...)` "
        "as a real spawn"
    )


# ---------------------------------------------------------------------------
# (c) BinOp-ARGV GAP regression -- state/bug-backlog/2026-08-20-spawn-ratchet-
# collector-treats-a-binop-b-afe8334260b8.yaml. The probe shape is expressed
# as a STRING SOURCE via `_scan_source`, never a live call node (D6): a real
# `subprocess.run(["git", ...])` call in this file would itself be a Rule
# 1/2 violation of the guard it is testing.
# ---------------------------------------------------------------------------


def test_binop_concatenated_argv_resolves_the_left_lists_first_element() -> None:
    """The backlog's exact probe shape: `subprocess.run(["git"] + list(args),
    cwd=r)` inside a helper. Before the fix this returned the raw `ast.BinOp`
    node from `_first_argv_element`, which `_classify_spawn` cannot match,
    landing the site in `_UNKNOWN_ARGV0` and the file reading has_any_spawn
    without any func_spawns entry -- silently non-spawning."""
    scanner = _scan_source(
        "import subprocess\n"
        "def _git(args, r):\n"
        "    subprocess.run(['git'] + list(args), cwd=r)\n"
    )
    assert scanner.func_spawns.get("_git"), (
        "a `[<list-literal>] + <expr>` BinOp-concatenated argv must resolve "
        "the left operand's first element, same as a plain list literal"
    )
    assert not scanner.func_unknown_spawns.get("_git"), (
        "this shape is now REAL, not UNKNOWN -- it must not double-count in "
        "the honesty bucket"
    )


def test_binop_concatenated_argv_classifies_real_via_classify_spawn() -> None:
    """Direct `_classify_spawn` check (not routed through the scanner) on
    the identical BinOp shape, mirroring `test_unknown_argv0_honesty_bucket_
    reports_variable_argv`'s direct-classify style."""
    src = (
        "import subprocess\n"
        "def f(args, r):\n"
        "    subprocess.run(['git'] + list(args), cwd=r)\n"
    )
    tree = ast.parse(src)
    call_node = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert _classify_spawn(call_node, {}) == "REAL"


def test_binop_argv_with_empty_left_list_is_unknown_not_a_crash() -> None:
    """An empty left-list BinOp (`[] + extra`) has no first element to take
    -- must classify UNKNOWN (honest "cannot resolve"), never raise."""
    src = "import subprocess\ndef f(extra):\n    subprocess.run([] + extra)\n"
    tree = ast.parse(src)
    call_node = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert _classify_spawn(call_node, {}) == "UNKNOWN"


# ---------------------------------------------------------------------------
# (a) MOCK-SEAM BLINDNESS regression -- AC10: the two oracle files that
# patch a module's own spawn seam must report zero spawn sites, with no
# `spawns_process` marker needed.
# ---------------------------------------------------------------------------


def test_mock_seam_oracle_files_report_zero_spawn_with_no_marker() -> None:
    """`test_benchmark_fanin_spawn_count.py` and `test_dep_probe_varying_
    program.py` (C3's spike, zero real spawns, both mock-patched) must clear
    entirely after (a)'s suppression -- no module-level spawn, no unmarked
    function-level spawn -- without either file carrying
    `pytest.mark.spawns_process`."""
    oracle_files = [
        "coordinator_core/tests/oracles/test_benchmark_fanin_spawn_count.py",
        "coordinator_core/tests/oracles/test_dep_probe_varying_program.py",
    ]
    bad = []
    for relpath in oracle_files:
        full_path = REPO_ROOT / relpath
        assert full_path.is_file(), f"expected oracle file missing: {relpath}"
        report = _analyze_file(full_path, relpath)
        if report.module_level_linenos or report.unmarked_spawning_funcs:
            bad.append((relpath, report.module_level_linenos, report.unmarked_spawning_funcs))
    assert not bad, (
        f"mock-seam suppression did not clear the oracle file(s): {bad} -- "
        "a route into a module this same file patches must not read as a "
        "real spawn"
    )


def test_mock_seam_suppression_does_not_hide_an_unpatched_spawn_in_same_file() -> None:
    """The inverse false negative the plan names: a file that patches an
    UNRELATED module's `subprocess` must NOT suppress a genuine spawn
    reached through a DIFFERENT, unpatched module -- narrowly scoped to
    same file + same target module, never a whole-file amnesty."""
    aliases = _collect_patched_module_aliases(
        ast.parse(
            "from unittest.mock import patch\n"
            "import some_other_module\n"
            "def test_thing():\n"
            "    with patch.object(some_other_module.subprocess, 'run'):\n"
            "        pass\n"
        )
    )
    assert aliases == {"some_other_module"}, (
        "the alias collector must record exactly the module whose "
        "subprocess/os attribute is patched, nothing broader"
    )


# ---------------------------------------------------------------------------
# (d) MODULE-SCOPE UNKNOWN DISARMS RULE 1 regression.
# ---------------------------------------------------------------------------


def test_module_scope_unknown_argv0_promotes_a_routed_wrapper_for_rule1() -> None:
    """A module-level call to `_helper(...)`, where `_helper`'s only spawn
    site has an UNKNOWN argv0 (a non-literal), must count as reaching a
    spawn for Rule 1 -- the exact tz-file shape the plan traced
    (`func_is_wrapper` -> `func_spawns` miss -> False would be a null fix if
    only `func_spawns`, never `func_unknown_spawns`, were consulted)."""
    from coordinator_core.spawn_policy.wrapper_resolution import ModuleInfo

    src = (
        "import subprocess\n"
        "def _helper(argv):\n"
        "    subprocess.run(argv)\n"
        "_helper(['irrelevant'])\n"
    )
    tree = ast.parse(src)
    scanner = _build_scanner("<fixture-d>", tree)
    assert scanner.func_unknown_spawns.get("_helper"), (
        "the helper's UNKNOWN-argv0 spawn must land in func_unknown_spawns"
    )
    assert not scanner.func_spawns.get("_helper"), (
        "sanity: this fixture must not classify as REAL -- it exercises the "
        "UNKNOWN promotion path, not the REAL path"
    )
    fake_path = Path("<fixture-d-module>")
    info = ModuleInfo(fake_path, scanner, {}, {"_helper"})
    resolved = fake_path.resolve()
    cache_key = (fake_path, "_helper")
    # Instance-dict cache (staff-eng F4): pop only this fixture's own key,
    # not a wholesale `.clear()` -- the prior module-global shape discarded
    # every legitimately-computed entry process-wide on every test run;
    # this mirrors the targeted save/restore already used for
    # `_module_info_cache` immediately below.
    _WRAPPER_RESOLVER._promote_unknown_wrapper_cache.pop(cache_key, None)
    prior = _WRAPPER_RESOLVER._module_info_cache.get(resolved, "<absent>")
    _WRAPPER_RESOLVER._module_info_cache[resolved] = info
    try:
        assert _func_is_wrapper_promoting_unknown(fake_path, "_helper") is True, (
            "a module-level UNKNOWN-argv0 spawn wrapper must be promoted for Rule 1"
        )
    finally:
        if prior == "<absent>":
            del _WRAPPER_RESOLVER._module_info_cache[resolved]
        else:
            _WRAPPER_RESOLVER._module_info_cache[resolved] = prior
        _WRAPPER_RESOLVER._promote_unknown_wrapper_cache.pop(cache_key, None)


def test_module_scope_unknown_promotion_is_contained_to_module_scope() -> None:
    """AC11 containment: the SAME helper shape, called from FUNCTION scope
    (not module scope), must NOT be promoted -- `func_spawns` (what Rules
    2/4 read) stays empty; only the honesty bucket (`func_unknown_spawns`)
    sees it. The promotion machinery lives entirely outside `func_spawns`,
    so Rules 2/4 cannot observe it regardless of call site -- this pins that
    the UNION is Rule-1-exclusive by construction."""
    scanner = _scan_source(
        "import subprocess\n"
        "def _helper(argv):\n"
        "    subprocess.run(argv)\n"
        "def test_thing():\n"
        "    _helper(['irrelevant'])\n"
    )
    assert not scanner.func_spawns.get("test_thing"), (
        "Rules 2/4 must never see a promoted UNKNOWN route -- func_spawns "
        "stays empty regardless of module- or function-scope call site"
    )


def test_wrapper_resolver_agrees_on_a_cycle_regardless_of_query_order(
    tmp_path: Path,
) -> None:
    """staff-eng F3: mutually-recursive module-level wrappers `a`/`b`, where
    `a` reaches a real spawn (via `helper`) and `b` reaches a spawn ONLY
    through `a`, must resolve `b` to `True` regardless of which of `a`/`b`
    is queried first. Before the fix, querying `a` first cached `b: False`:
    `a` recurses into `b`, `b` recurses into `a`, hits the `key in visiting`
    cycle guard on `a` and returns False, so `b` has no other route and
    caches `False` -- permanently wrong, since calling `b` does reach
    `helper` by way of `a`. Querying `b` first happens to resolve both
    correctly, which is what makes the defect order-dependent. A fresh `WrapperResolver` per order, matching
    real usage (one resolver per test-collection run), not the shared
    module-level `_WRAPPER_RESOLVER` -- this fixture must not depend on nor
    pollute that singleton's cache."""
    module = tmp_path / "mutual_recursion.py"
    module.write_text(
        "import subprocess\n"
        "def a():\n"
        "    b()\n"
        "    helper()\n"
        "def b():\n"
        "    a()\n"
        "def helper():\n"
        "    subprocess.run(['git', 'status'])\n",
        encoding="utf-8",
    )

    resolver_a_first = WrapperResolver(tmp_path, _build_scanner)
    a_first_a = resolver_a_first.func_is_wrapper(module, "a")
    a_first_b = resolver_a_first.func_is_wrapper(module, "b")

    resolver_b_first = WrapperResolver(tmp_path, _build_scanner)
    b_first_b = resolver_b_first.func_is_wrapper(module, "b")
    b_first_a = resolver_b_first.func_is_wrapper(module, "a")

    assert (a_first_a, a_first_b) == (True, True), (
        "querying a-then-b: both reach helper's real spawn"
    )
    assert (b_first_a, b_first_b) == (True, True), (
        "querying b-then-a must agree with a-then-b -- b reaches a spawn "
        "through a's cycle regardless of query order"
    )


def test_module_scope_unknown_promotion_excludes_main_guard_body() -> None:
    """A call inside `if __name__ == '__main__':` never executes on import
    and MUST be excluded from (d)'s promotion -- the verified
    `test_harvest_idempotency_env_override.py` false-positive shape."""
    src = (
        "import sys, subprocess\n"
        "def main(argv):\n"
        "    subprocess.run(argv)\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main(['x']))\n"
    )
    tree = ast.parse(src)
    guarded_linenos = _main_guard_body_linenos(tree)
    call_lineno = next(
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "main"
    )
    assert call_lineno in guarded_linenos, (
        "a call inside `if __name__ == '__main__':` must be recognised as "
        "excluded from (d)'s promotion -- it never executes on import"
    )


def test_kind_axis_route_layer_unknown_stays_out_of_scope() -> None:
    """(b) is a documented limitation, not a fix: `test_kind_axis.py` must
    still be caught by the ratchet as an unmarked spawning file -- its one
    spawn is behind a function-scope `_RUN_GIT_SPAWN_VERBS` condition this
    chunk deliberately does not resolve. AC1/AC7/AC10."""
    relpath = "coordinator_core/baton_assemble/tests/test_kind_axis.py"
    full_path = REPO_ROOT / relpath
    assert full_path.is_file(), f"expected file missing: {relpath}"
    report = _analyze_file(full_path, relpath)
    assert report.has_any_spawn, (
        "test_kind_axis.py stays a documented, unfixed route-layer-UNKNOWN "
        "limitation per (b) -- it must still register as spawning"
    )


# ---------------------------------------------------------------------------
# AC12 -- the two live Rule 1 evasions C14 fixed must stay fixed, and the
# mechanism that fixes them (module-scope UNKNOWN promotion, C6d) must stay
# correctly contained to module scope regardless of query order.
# ---------------------------------------------------------------------------


def test_c14_fixed_files_report_clean_for_rule1() -> None:
    """AC12(i): the two files C14 fixed for Rule 1 -- both routed a
    module-level call into a helper whose only spawn site has a non-literal
    (`shutil.which()`-shaped) argv0, the exact shape module-scope UNKNOWN
    promotion (C6d) exists to catch -- must report CLEAN (no module-level
    linenos, no unmarked spawning funcs) after the fix. Modelled on
    `test_mock_seam_oracle_files_report_zero_spawn_with_no_marker`. Nothing
    else in this module pins that these two stay fixed; without this test a
    regression here is a silent Rule 1 reopening."""
    fixed_files = [
        "coordinator_core/tests/test_engine_root_conformance.py",
        "coordinator/tests/test_workday_evening_tz_coherence.py",
    ]
    bad = []
    for relpath in fixed_files:
        full_path = REPO_ROOT / relpath
        assert full_path.is_file(), f"expected C14-fixed file missing: {relpath}"
        report = _analyze_file(full_path, relpath)
        if report.module_level_linenos or report.unmarked_spawning_funcs:
            bad.append((relpath, report.module_level_linenos, report.unmarked_spawning_funcs))
    assert not bad, (
        f"a C14 Rule 1 fix regressed: {bad} -- these two files must report "
        "CLEAN (module-scope UNKNOWN promotion must still resolve their "
        "routed helper call)"
    )


def test_module_scope_unknown_promotion_reports_the_negative_direction(
    tmp_path: Path,
) -> None:
    """AC12(ii): the negative-direction counterpart to
    `test_c14_fixed_files_report_clean_for_rule1` -- a REAL file, not a
    `_scan_source` string, reproducing the pre-fix shape (a module-level call
    routed to a helper whose only spawn site has an UNKNOWN argv0) must be
    CAUGHT by Rule 1, proving the detector still fires rather than having
    been loosened into blanket silence. A `_scan_source`-only version would
    never touch `_get_module_info`/`_analyze_file`'s Path-based resolution
    path and would pass without exercising the mechanism at all -- worthless
    coverage. Hand-reverting either live C14 file is out of bounds (peer-
    hostile on a shared tree); this synthetic module is the only route."""
    module = tmp_path / "pre_fix_shape.py"
    module.write_text(
        "import subprocess\n"
        "def _helper(argv):\n"
        "    subprocess.run(argv)\n"
        "_helper(['irrelevant'])\n",
        encoding="utf-8",
    )
    # `_analyze_file` -> `_get_module_info` reads the module-level
    # `_WRAPPER_RESOLVER` singleton, which is pinned to `REPO_ROOT` -- a
    # `tmp_path` file is never a subpath of that root, so `get_module_info`
    # would raise on the `.relative_to(repo_root)` call. Swap in a resolver
    # rooted at `tmp_path` for the duration of this call only (targeted
    # save/restore, same discipline as the cache-key pops elsewhere in this
    # file), rather than duplicating `_analyze_file`'s logic against a
    # differently-scoped resolver.
    prior_resolver = globals()["_WRAPPER_RESOLVER"]
    try:
        globals()["_WRAPPER_RESOLVER"] = WrapperResolver(tmp_path, _build_scanner)
        report = _analyze_file(module, "pre_fix_shape.py")
    finally:
        globals()["_WRAPPER_RESOLVER"] = prior_resolver
    assert report.module_level_linenos, (
        "a module-level call routed to a helper with an UNKNOWN-argv0 spawn "
        "site must be reported by Rule 1 -- if this is empty, module-scope "
        "UNKNOWN promotion (C6d) has stopped firing"
    )


def test_function_scope_unknown_spawn_is_contained_regardless_of_query_order(
    tmp_path: Path,
) -> None:
    """AC12(iii): containment property, both call orders. A file whose ONLY
    UNKNOWN-argv0 spawn site sits at FUNCTION scope must never be reported
    by Rules 2/4 (`func_spawns` stays empty), and that answer must be
    unaffected by whether a module-scope (promote-UNKNOWN) query for the
    same `(module, func)` pair ran first.

    AC12(iii)'s written mechanism -- an extended `_wrapper_cache` key -- does
    not exist: the chunk that would have built it had a `writes:` scope that
    contradicted its own instruction, so the executor correctly built a
    SEPARATE cache instead (`_promote_unknown_wrapper_cache`, an instance
    dict on `WrapperResolver`, exercised via `_func_is_wrapper_promoting_
    unknown`) rather than extending the shared `_wrapper_cache` that
    `WrapperResolver.func_is_wrapper` (Rules 2/4's path) reads. This test
    pins the PROPERTY -- the two recursions stay disjoint and share only the
    deterministic module-info cache -- not the AC's stated (and unbuilt)
    mechanism. Modelled on `test_wrapper_resolver_agrees_on_a_cycle_
    regardless_of_query_order`'s fresh-resolver, both-orders shape."""
    module = tmp_path / "func_scope_only_unknown.py"
    module.write_text(
        "import subprocess\n"
        "def _helper(argv):\n"
        "    subprocess.run(argv)\n"
        "def test_thing():\n"
        "    _helper(['irrelevant'])\n",
        encoding="utf-8",
    )
    tree = ast.parse(module.read_text(encoding="utf-8"))
    scanner = _build_scanner("<fixture-iii>", tree)
    assert scanner.func_unknown_spawns.get("_helper"), (
        "sanity: this fixture must actually produce a function-scope "
        "UNKNOWN spawn site inside _helper, or the containment assertions "
        "below are vacuous"
    )
    assert not scanner.func_spawns.get("test_thing"), (
        "sanity: test_thing must reach no REAL spawn directly -- only the "
        "routed UNKNOWN one, via _helper"
    )

    # `_func_is_wrapper_promoting_unknown` (module-scope, promote-UNKNOWN)
    # reads the module-level `_WRAPPER_RESOLVER` singleton, which is pinned
    # to `REPO_ROOT` and would raise on a `tmp_path` file (see
    # `test_module_scope_unknown_promotion_reports_the_negative_direction`).
    # A fresh `WrapperResolver(tmp_path, ...)` is swapped into that global
    # ONCE PER ORDER and reused across both halves of that order: the
    # contamination this AC guards against is one query's cache write
    # changing a LATER query's answer, which only a shared instance can
    # exhibit. A fresh resolver per half would empty both caches between
    # the two calls and make the assertions below vacuously true.
    prior_resolver = globals()["_WRAPPER_RESOLVER"]
    try:
        # Order A: module-scope (promote-UNKNOWN) query first, then the
        # function-scope (Rules 2/4) query, same resolver.
        globals()["_WRAPPER_RESOLVER"] = WrapperResolver(tmp_path, _build_scanner)
        module_first_promote = _func_is_wrapper_promoting_unknown(module, "test_thing")
        module_first_rules24 = globals()["_WRAPPER_RESOLVER"].func_is_wrapper(
            module, "test_thing"
        )

        # Order B: function-scope query first, then module-scope, same
        # resolver.
        globals()["_WRAPPER_RESOLVER"] = WrapperResolver(tmp_path, _build_scanner)
        func_first_rules24 = globals()["_WRAPPER_RESOLVER"].func_is_wrapper(
            module, "test_thing"
        )
        func_first_promote = _func_is_wrapper_promoting_unknown(module, "test_thing")
    finally:
        globals()["_WRAPPER_RESOLVER"] = prior_resolver

    assert module_first_rules24 is False and func_first_rules24 is False, (
        "Rules 2/4 must never count a function-scope-only UNKNOWN spawn as a "
        "real wrapper, in either query order -- got "
        f"module-first={module_first_rules24}, func-first={func_first_rules24}"
    )
    assert module_first_promote is True and func_first_promote is True, (
        "the module-scope promote-UNKNOWN answer must also agree across "
        "both query orders (test_thing is itself a top-level def reaching "
        "an UNKNOWN spawn through _helper) -- got "
        f"module-first={module_first_promote}, func-first={func_first_promote}"
    )
