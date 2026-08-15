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
    list/tuple literal -- return its first element) or an `os.system("...")`
    single-string call (return the string arg itself, so the string-literal
    check below can look for a leading real-binary token)."""
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, (ast.List, ast.Tuple)):
        if not first.elts:
            return None
        return first.elts[0]
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


def _decorator_names(decorators: list[ast.expr]) -> list[str]:
    """Best-effort dotted-name rendering of a decorator list, e.g.
    `pytest.mark.spawns_process` -> "pytest.mark.spawns_process"."""
    names: list[str] = []
    for dec in decorators:
        node = dec
        # `@pytest.mark.spawns_process` parses as an Attribute chain (no
        # call); `@pytest.mark.spawns_process()` would parse as a Call
        # wrapping the same Attribute chain -- unwrap it either way.
        if isinstance(node, ast.Call):
            node = node.func
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        if parts:
            names.append(".".join(reversed(parts)))
    return names


def _has_spawns_process_marker(decorators: list[ast.expr]) -> bool:
    return "pytest.mark.spawns_process" in _decorator_names(decorators)


def _has_module_level_pytestmark(
    tree: ast.Module, marker: str = "pytest.mark.spawns_process"
) -> bool:
    """True if the module declares `pytestmark = <marker>` or
    `pytestmark = [<marker>, ...]` at module level -- the file-wide
    equivalent of decorating every test individually."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            continue
        value = node.value
        candidates = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
        for candidate in candidates:
            target = candidate.func if isinstance(candidate, ast.Call) else candidate
            parts: list[str] = []
            while isinstance(target, ast.Attribute):
                parts.append(target.attr)
                target = target.value
            if isinstance(target, ast.Name):
                parts.append(target.id)
            if ".".join(reversed(parts)) == marker:
                return True
    return False


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
    module_level_linenos = sorted(set(module_level_linenos))

    file_wide_marker = _has_module_level_pytestmark(tree_for_marker)
    unmarked_funcs: list[str] = []
    spawning_func_names = set(scanner.func_spawns) | set(scanner.generic_calls)
    any_func_spawn = False
    for fname in spawning_func_names:
        direct = bool(scanner.func_spawns.get(fname))
        indirect = _func_reaches_spawn_indirectly(info, fname)
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
