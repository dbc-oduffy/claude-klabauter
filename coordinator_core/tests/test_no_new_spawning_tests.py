"""Recurrence guard: the spawn ratchet for git/bash/python-spawning tests.

WHY THIS GUARD EXISTS (empirical, not hypothetical)
    On 2026-08-07, tests that spawned real `git` crashed multiple live EM
    sessions on Windows and forced a standing PM prohibition on test runs.
    Commit `1d4e686a9` deleted the 76 worst chokepoints (1439 test
    functions) -- see `state/audits/2026-08-07-spawn-heavy-test-excision-
    ledger.md` for the full ledger, including what was DELIBERATELY left
    standing: ~297 files still spawn real git, plus ~100 duplicated
    module-local `def _git(...)` helpers with no shared chokepoint.

    That residue is too large to gate strictly on day one -- a strict "no
    spawning tests" rule would fail against nearly 300 files immediately.
    So this guard is a RATCHET, not a clean gate: it freezes the residue
    into a shrink-only `_BASELINE`, blocks NEW spawn-heavy test files from
    joining it unmarked, and separately keeps import-time spawns (the
    actual mechanism that broke `--collect-only`/`-k` filtering, per the
    ledger) at a permanently strict zero.

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

THREE RULES
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

    Rule 2 -- new spawning test files: RATCHET.
        A file containing a function-level (non-module-level) spawn site
        fails UNLESS either (a) its repo-relative POSIX path is in the
        frozen `_BASELINE` below, or (b) EVERY test function in the file
        that itself contains a spawn site carries `@pytest.mark.spawns_process`
        (directly or via a stacked decorator list -- module-level
        `pytestmark = [pytest.mark.spawns_process]` also satisfies this for
        every test in the file). The marker is the declaration a NEW file
        must make; the baseline is the one-time grandfather clause for the
        pre-existing residue, not a route around the marker for new work.

    Rule 3 -- stale baseline entries fail.
        A `_BASELINE` entry that no longer exists on disk, or exists but no
        longer contains any function-level spawn site, fails as stale. This
        keeps `_BASELINE` monotonically shrinking -- entries may only be
        REMOVED, never added -- rather than becoming a permanent parking
        lot nobody revisits. Modeled directly on
        `coordinator/tests/test_dr084_single_accessor_guard.py`'s
        `test_allowlist_entries_still_exist_on_disk` staleness check.

THE GUARD ITSELF MUST NOT SPAWN
    Pure AST parse + filesystem walk. No `git ls-files`, no shelling out to
    find spawners -- that would be self-defeating for a guard whose entire
    point is that spawning during test collection is what broke sessions.

Spec backlink: state/audits/2026-08-07-spawn-heavy-test-excision-ledger.md
Restoration workstream that drains `_BASELINE`:
    state/handoffs/2026-08-07-restore-excised-spawn-heavy-tests.md
"""

from __future__ import annotations

import ast
import os
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


def _has_module_level_pytestmark(tree: ast.Module) -> bool:
    """True if the module declares `pytestmark = pytest.mark.spawns_process`
    or `pytestmark = [pytest.mark.spawns_process, ...]` at module level --
    the file-wide equivalent of decorating every test individually."""
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
            if ".".join(reversed(parts)) == "pytest.mark.spawns_process":
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
# Rule 3 baseline. MACHINE-GENERATED 2026-08-07 by running this guard's own
# detector over the tree at HEAD. Entries may only be REMOVED, never added
# -- this is the grandfather clause for the ~297-file residue named in the
# excision ledger, and it must shrink monotonically as the restoration
# workstream (state/handoffs/2026-08-07-restore-excised-spawn-heavy-tests.md)
# converts files to real mocking or adds the `spawns_process` marker.
# Count at generation time: see the count asserted in
# `test_baseline_count_matches_header` below (kept in lockstep with this
# frozenset rather than duplicated as a bare literal here).
#
# RE-FROZEN 2026-08-07 (widening): the detector was extended to see spawns
# reached indirectly through a local or imported helper function (e.g. a
# module-local `def _git(...)` wrapper, or `_run_cli()` calling
# `subprocess.run`), not just a literal `subprocess.run(...)` call inline in
# the test body. That widening surfaced 64 previously-invisible files (563 ->
# 627), then MINUS one file intentionally left off per this widening's own
# dispatch brief -- coordinator_core/ops/session/tests/
# test_resolve_chain_terminal_disposition.py belongs to a live peer session
# and must not be marked/baselined by this change -- giving 626, then further
# reduced (626 -> 623 -> 622) as entries were removed for files deleted from
# disk (most recently: test_ceremony_lock.py, removed when ceremony_lock.py
# became a no-op shim). No stale entries remain (Rule 3 held clean).
# ---------------------------------------------------------------------------
_BASELINE: frozenset[str] = frozenset(
    {
        'bin/tests/test_commit_anchors_failopen.py',
        'bin/tests/test_claude_klabauter_revendor.py',
        'bin/tests/test_claude_klabauter_revendor_schema.py',
        'bin/tests/test_shell_init_guard.py',
        'coordinator/bin/lib/test_emit_lesson_summaries.py',
        'coordinator/bin/lib/test_git_hook_install.py',
        'coordinator/bin/test_coordinator_queue_append.py',
        'coordinator/bin/test_coordinator_tasks_mirror.py',
        'coordinator/bin/test_cross_repo_memo_c6.py',
        'coordinator/bin/test_cross_repo_memo_draft.py',
        'coordinator/bin/test_cross_repo_memo_roundtrip.py',
        'coordinator/bin/test_emit_goal_from_artifact.py',
        'coordinator/bin/test_gen_launcher_shim.py',
        'coordinator/bin/test_generate_tested_platforms.py',
        'coordinator/bin/test_lint_frontmatter.py',
        'coordinator/bin/test_migrate_bug_backlog.py',
        'coordinator/bin/test_migrate_improvement_queue_project.py',
        'coordinator/bin/test_probe_memory_headroom.py',
        'coordinator/bin/test_spawn_census.py',
        'coordinator/bin/test_sweep_actioned_memos.py',
        'coordinator/bin/test_sweep_argv_shared.py',
        'coordinator/bin/test_sweep_shipped_handoffs.py',
        'coordinator/bin/test_sync_cockpit_contract.py',
        'coordinator/bin/tests/file-attribution/test_query_file_attribution.py',
        'coordinator/bin/tests/test_assert_cwd.py',
        'coordinator/bin/tests/test_bug_sweep_probes.py',
        'coordinator/bin/tests/test_cartography_cli.py',
        'coordinator/bin/tests/test_cc_invoke_py.py',
        'coordinator/bin/tests/test_check_install_divergence.py',
        'coordinator/bin/tests/test_check_machine_path_leak.py',
        'coordinator/bin/tests/test_check_multi_event_hook_hardcoded_event.py',
        'coordinator/bin/tests/test_check_schema_version_bump.py',
        'coordinator/bin/tests/test_claude_machine_local.py',
        'coordinator/bin/tests/test_cmd_rem_line_metacharacters.py',
        'coordinator/bin/tests/test_coordinator_doc_new_roadmap_baton_self_validation.py',
        'coordinator/bin/tests/test_coordinator_doc_new_sizing_object_gate.py',
        'coordinator/bin/tests/test_coordinator_lesson_add_meta_routing.py',
        'coordinator/bin/tests/test_handoff_deliverable_carry.py',
        'coordinator/bin/tests/test_handoff_has_live_children.py',
        'coordinator/bin/tests/test_learn_lessons_age_sweep.py',
        'coordinator/bin/tests/test_machine_local_ladder_parity.py',
        'coordinator/bin/tests/test_merge_recovery_and_tag_cut.py',
        'coordinator/bin/tests/test_merge_release_notes_derive.py',
        'coordinator/bin/tests/test_no_bin_docstring_command_substitution.py',
        'coordinator/bin/tests/test_no_bin_polyglot_invariant.py',
        'coordinator/bin/tests/test_parallel_review_gate_decision.py',
        'coordinator/bin/tests/test_parallel_review_orthogonality_guard.py',
        'coordinator/bin/tests/test_percolate_gate.py',
        'coordinator/bin/tests/test_plan_tasks_spine_and_harvest.py',
        'coordinator/bin/tests/test_publish_batch_delta_honest_reporting.py',
        'coordinator/bin/tests/test_reap_integrated_review_findings_native.py',
        'coordinator/bin/tests/test_record_platform_outcome.py',
        'coordinator/bin/tests/test_repo_census.py',
        'coordinator/bin/tests/test_repo_setup_args_and_register.py',
        'coordinator/bin/tests/test_scoped_git_commit_cli.py',
        'coordinator/bin/tests/test_shebang_removal_ordering_ratchet.py',
        'coordinator/bin/tests/test_spinoff_deliverable_and_commit.py',
        'coordinator/bin/tests/test_testpaths_location_guard.py',
        'coordinator/bin/tests/test_update_docs_probes.py',
        'coordinator/bin/tests/test_validate_fast_and_packageability.py',
        'coordinator/bin/tests/test_workday_start_advisory_counters.py',
        'coordinator/bin/tests/test_workweek_complete_drift_guards.py',
        'coordinator/bin/tests/test_wsc_coverage_gate_runner.py',
        'coordinator/bin/tests/test_wsc_session_disposition.py',
        'coordinator/bin/tests/test_zero_test_module_ratchet.py',
        'coordinator/tests/conftest.py',
        'coordinator/tests/test_agent_sessions_schema.py',
        'coordinator/tests/test_aggregate_chain_loe.py',
        'coordinator/tests/test_aggregate_chain_loe_diamond.py',
        'coordinator/tests/test_append_goal_event_facade.py',
        'coordinator/tests/test_append_plan_session.py',
        'coordinator/tests/test_atlas_watch_drift.py',
        'coordinator/tests/test_audit_roadmap_dependency_order.py',
        'coordinator/tests/test_audit_roadmap_verdict_regex.py',
        'coordinator/tests/test_b4_query_records_caller_parity.py',
        'coordinator/tests/test_backfill_initiative_fk.py',
        'coordinator/tests/test_backfill_week_changelog_gaps_facade.py',
        'coordinator/tests/test_bash_exclusion_gate.py',
        'coordinator/tests/test_bootstrap_orchestrate.py',
        'coordinator/tests/test_bootstrap_repo.py',
        'coordinator/tests/test_capture_fan_out_threshold.py',
        'coordinator/tests/test_chain_preinstall_phase.py',
        'coordinator/tests/test_chain_walk_setup_rename_compat.py',
        'coordinator/tests/test_check_auto_reconcile.py',
        'coordinator/tests/test_check_deferral_orphan_memo.py',
        'coordinator/tests/test_check_deferral_partial_strangle.py',
        'coordinator/tests/test_check_engine_drift.py',
        'coordinator/tests/test_check_harvest_debt.py',
        'coordinator/tests/test_check_machine_local_regeneratability.py',
        'coordinator/tests/test_check_no_monolith_completion_append.py',
        'coordinator/tests/test_check_plugin_drift_copy_install.py',
        'coordinator/tests/test_check_registry_codename_leak_keepset.py',
        'coordinator/tests/test_check_version_consistency.py',
        'coordinator/tests/test_check_wsc_inline_budget.py',
        'coordinator/tests/test_chunk6_onboarding_setup_doctor.py',
        'coordinator/tests/test_classify_dispatch_shape.py',
        'coordinator/tests/test_claude_doe.py',
        'coordinator/tests/test_complete_entry_rollup.py',
        'coordinator/tests/test_conformance_chain_preinstall_gate.py',
        'coordinator/tests/test_coordinator_auto_push.py',
        'coordinator/tests/test_coordinator_ceremony_hook.py',
        'coordinator/tests/test_coordinator_complete_entry.py',
        'coordinator/tests/test_coordinator_data_root.py',
        'coordinator/tests/test_coordinator_doc_new.py',
        'coordinator/tests/test_coordinator_doc_new_handoff_archived_twin_guard.py',
        'coordinator/tests/test_coordinator_ensure_post_commit_hook.py',
        'coordinator/tests/test_coordinator_initiative_cli.py',
        'coordinator/tests/test_coordinator_initiative_list_unattached.py',
        'coordinator/tests/test_coordinator_reap_stale_locks.py',
        'coordinator/tests/test_coordinator_render_rollup_cli.py',
        'coordinator/tests/test_coordinator_renormalize_index.py',
        'coordinator/tests/test_coordinator_root_warn_guard.py',
        'coordinator/tests/test_coordinator_safe_commit.py',
        'coordinator/tests/test_coordinator_session_self_claim.py',
        'coordinator/tests/test_coordinator_setup_state.py',
        'coordinator/tests/test_coordinator_uninstall_trampoline.py',
        'coordinator/tests/test_coordinator_workflow_scaffold.py',
        'coordinator/tests/test_coordinator_write_review_trail_facade.py',
        'coordinator/tests/test_count_distill_backlog.py',
        'coordinator/tests/test_cruft_sweep_collapse.py',
        'coordinator/tests/test_cruft_sweep_concurrency.py',
        'coordinator/tests/test_cruft_sweep_jsonl.py',
        'coordinator/tests/test_cruft_sweep_log.py',
        'coordinator/tests/test_cruft_sweep_mtime_floor.py',
        'coordinator/tests/test_cruft_sweep_phase_a.py',
        'coordinator/tests/test_cruft_sweep_phase_b.py',
        'coordinator/tests/test_deep_research_record_roundtrip.py',
        'coordinator/tests/test_detect_initiative_candidates_port.py',
        'coordinator/tests/test_detect_onboarding_offer.py',
        'coordinator/tests/test_dirty_tree_gate.py',
        'coordinator/tests/test_distill_cli_relocation.py',
        'coordinator/tests/test_doctor_manifest_ssot.py',
        'coordinator/tests/test_doctor_selection_grammar.py',
        'coordinator/tests/test_doe_root_durable_resolution.py',
        'coordinator/tests/test_dr084_migrate_live_duplicate_guard.py',
        'coordinator/tests/test_dr_allocator.py',
        'coordinator/tests/test_draft_plan_aging.py',
        'coordinator/tests/test_emit_cockpit_snapshot_facade.py',
        'coordinator/tests/test_ensure_vscode_readonly.py',
        'coordinator/tests/test_extract_scope_paths.py',
        'coordinator/tests/test_extract_verify_gate.py',
        'coordinator/tests/test_fan_out_dispatch_plan_doc_oos.py',
        'coordinator/tests/test_fan_out_integrator.py',
        'coordinator/tests/test_flight_recorder_scaffolder.py',
        'coordinator/tests/test_freeze_review_diff.py',
        'coordinator/tests/test_generate_exec_summary.py',
        'coordinator/tests/test_git_hook_install_foreign_hook_preservation.py',
        'coordinator/tests/test_goal_coverage_scan.py',
        'coordinator/tests/test_handoff_gate_aging.py',
        'coordinator/tests/test_hardware_audit_ssot.py',
        'coordinator/tests/test_hook_shims_portable.py',
        'coordinator/tests/test_install_health_run.py',
        'coordinator/tests/test_install_maximalist_trampoline.py',
        'coordinator/tests/test_learn_lessons_roots.py',
        'coordinator/tests/test_lesson_promote_node_enum.py',
        'coordinator/tests/test_lessons_add_concurrency.py',
        'coordinator/tests/test_lessons_closure.py',
        'coordinator/tests/test_lessons_consumer_sweep.py',
        'coordinator/tests/test_lessons_migration_parity.py',
        'coordinator/tests/test_lessons_query.py',
        'coordinator/tests/test_lessons_schema_roundtrip.py',
        'coordinator/tests/test_list_reverse_drift_scoping.py',
        'coordinator/tests/test_list_review_trail_records.py',
        'coordinator/tests/test_live_machine_local_registry_isolation.py',
        'coordinator/tests/test_migrate_completion_log_legacy.py',
        'coordinator/tests/test_migrate_cross_repo_layout.py',
        'coordinator/tests/test_mint_deliverable_id.py',
        'coordinator/tests/test_new_project_scaffold.py',
        'coordinator/tests/test_normalize_consumed_frontmatter.py',
        'coordinator/tests/test_orientation_auto_push_health.py',
        'coordinator/tests/test_parse_resolves_trailer.py',
        'coordinator/tests/test_percolate_engine_cutover.py',
        'coordinator/tests/test_percolate_resolve_target.py',
        'coordinator/tests/test_percolate_transform.py',
        'coordinator/tests/test_posture_default_preservation.py',
        'coordinator/tests/test_prepare_commit_msg_session_id.py',
        'coordinator/tests/test_probe_onboarding_currency.py',
        'coordinator/tests/test_promote_shipped_in_flight_stubs.py',
        'coordinator/tests/test_prune_resolved_queue_entries.py',
        'coordinator/tests/test_publish_guard_before_mutate.py',
        'coordinator/tests/test_publish_honest_update_report.py',
        'coordinator/tests/test_publish_materialize_inject_srcs.py',
        'coordinator/tests/test_queue_append_node_validation.py',
        'coordinator/tests/test_queue_append_workstream_native_transport.py',
        'coordinator/tests/test_read_frontmatter_field.py',
        'coordinator/tests/test_reap_integrated_review_findings.py',
        'coordinator/tests/test_reap_stale_locks.py',
        'coordinator/tests/test_reconcile_completion_commits.py',
        'coordinator/tests/test_reconcile_completion_commits_facade.py',
        'coordinator/tests/test_records_query_filters.py',
        'coordinator/tests/test_records_query_limit_and_unattached.py',
        'coordinator/tests/test_records_query_py.py',
        'coordinator/tests/test_records_query_repo_root.py',
        'coordinator/tests/test_refresh_plugin_live_install_integration.py',
        'coordinator/tests/test_refresh_queries_files_scope.py',
        'coordinator/tests/test_refresh_roadmap_callout.py',
        'coordinator/tests/test_refresh_source_is_live_venv.py',
        'coordinator/tests/test_release_currency.py',
        'coordinator/tests/test_render_posture_overlay.py',
        'coordinator/tests/test_render_template.py',
        'coordinator/tests/test_render_template_multiline.py',
        'coordinator/tests/test_render_template_tree.py',
        'coordinator/tests/test_repo_setup_phase3j_integration.py',
        'coordinator/tests/test_repomap_wrapper_resolution.py',
        'coordinator/tests/test_resolve_coordinator_clone.py',
        'coordinator/tests/test_resolve_coordinator_clone_source_mode.py',
        'coordinator/tests/test_resolve_repo_path.py',
        'coordinator/tests/test_resolve_session_id.py',
        'coordinator/tests/test_review_brightline_gate_session_filter.py',
        'coordinator/tests/test_review_brightline_two_oracle.py',
        'coordinator/tests/test_review_coverage_core.py',
        'coordinator/tests/test_review_findings_scaffold.py',
        'coordinator/tests/test_rollup_derive.py',
        'coordinator/tests/test_round_trip_t3.py',
        'coordinator/tests/test_run_plan_tasks_spine_suites.py',
        'coordinator/tests/test_run_report_fold_cli_roundtrip.py',
        'coordinator/tests/test_safe_commit_offer_outcome_signal.py',
        'coordinator/tests/test_safe_commit_offer_sigterm_handler.py',
        'coordinator/tests/test_scaffold_canonical_structure.py',
        'coordinator/tests/test_scan_addon_health_hookprobe.py',
        'coordinator/tests/test_schema_cli.py',
        'coordinator/tests/test_self_claim_refresh_queries.py',
        'coordinator/tests/test_self_claim_snippet_sync.py',
        'coordinator/tests/test_setup_detect_test_cmd.py',
        'coordinator/tests/test_setup_health_ledger_seed.py',
        'coordinator/tests/test_setup_rag_decision.py',
        'coordinator/tests/test_snippet_registry.py',
        'coordinator/tests/test_snippet_registry_conditional.py',
        'coordinator/tests/test_snippet_registry_malformed.py',
        'coordinator/tests/test_spawn_hidden.py',
        'coordinator/tests/test_stamp_plan_status_on_ship.py',
        'coordinator/tests/test_sync_plugin_wiki_mirror_guard.py',
        'coordinator/tests/test_verify_doe_root_seam_sync.py',
        'coordinator/tests/test_verify_no_console_flash.py',
        'coordinator/tests/test_verify_no_console_flash_file_allow.py',
        'coordinator/tests/test_workday_complete_backfill_anchor.py',
        'coordinator/tests/test_workday_complete_backfill_inject_anchor.py',
        'coordinator/tests/test_workday_complete_backfill_scan.py',
        'coordinator/tests/test_workday_complete_step1_validate.py',
        'coordinator/tests/test_workday_complete_step2_5_dirty_tree_rename_fold.py',
        'coordinator/tests/test_workday_complete_step2_5_dirty_tree_smoke.py',
        'coordinator/tests/test_workday_complete_step3_consolidate.py',
        'coordinator/tests/test_workday_complete_step9_append_changelog_facade.py',
        'coordinator/tests/test_workday_evening_tz_coherence.py',
        'coordinator/tests/test_workday_start_cross_repo_memo_surface.py',
        'coordinator/tests/test_workday_start_inbox_blitz_assemble.py',
        'coordinator/tests/test_workday_start_outbox_stale_nudge.py',
        'coordinator/tests/test_workday_start_step0_slug_selfheal.py',
        'coordinator/tests/test_workstream_store_collision.py',
        'coordinator/tests/test_workweek_trail_scope.py',
        'coordinator_core/bash_guards/tests/test_block_subagent_commit.py',
        'coordinator_core/bash_guards/tests/test_bump_foreign_repo_write.py',
        'coordinator_core/bash_guards/tests/test_bump_foreign_repo_write_c7_findings.py',
        'coordinator_core/bash_guards/tests/test_bump_outside_repo_write.py',
        'coordinator_core/bash_guards/tests/test_cd_prefix_bypass.py',
        'coordinator_core/bash_guards/tests/test_check_destructive_git_orphan_check2.py',
        'coordinator_core/bash_guards/tests/test_check_destructive_git_revert_stash.py',
        'coordinator_core/bash_guards/tests/test_check_destructive_rm_repo_root.py',
        'coordinator_core/bash_guards/tests/test_check_seven_claude_md_budget.py',
        'coordinator_core/bash_guards/tests/test_check_test_suite_invocation.py',
        'coordinator_core/bash_guards/tests/test_check_validate_commit.py',
        'coordinator_core/bash_guards/tests/test_command_tokenizer_length_ceiling.py',
        'coordinator_core/bash_guards/tests/test_commit_scope_events.py',
        'coordinator_core/bash_guards/tests/test_commit_tripwires.py',
        'coordinator_core/bash_guards/tests/test_confinement_attack_corpus.py',
        'coordinator_core/bash_guards/tests/test_dispatch_checks_git_fact_memoization.py',
        'coordinator_core/bash_guards/tests/test_dispatch_crash_deny.py',
        'coordinator_core/bash_guards/tests/test_dispatch_latency_bound.py',
        'coordinator_core/bash_guards/tests/test_firing_shape_gate.py',
        'coordinator_core/bash_guards/tests/test_git_commit_safe_commit_deny_escalation.py',
        'coordinator_core/bash_guards/tests/test_guard_offer_invoke_params_stdin.py',
        'coordinator_core/bash_guards/tests/test_platform_conditioned_deny_reachability.py',
        'coordinator_core/bash_guards/tests/test_write_bump_applicability.py',
        'coordinator_core/bash_guards/tests/test_write_bump_marker.py',
        'coordinator_core/bash_guards/tests/test_write_bump_message.py',
        'coordinator_core/bash_guards/tests/test_write_bump_session_start.py',
        'coordinator_core/bash_guards/tests/test_write_bump_surface_parity.py',
        'coordinator_core/baton_assemble/tests/test_deliverable_collision_warn.py',
        'coordinator_core/baton_assemble/tests/test_j_continuation_vs_fork_excise.py',
        'coordinator_core/benchmarks/tests/test_harness.py',
        'coordinator_core/cartography/tests/test_chunk_table.py',
        'coordinator_core/cartography/tests/test_churn.py',
        'coordinator_core/cartography/tests/test_file_index.py',
        'coordinator_core/cartography/tests/test_tree.py',
        'coordinator_core/contract/memo_conformance/test_doe_round_trip_conformance.py',
        'coordinator_core/dispatch/tests/test_provision.py',
        'coordinator_core/distill/tests/test_delete_guard.py',
        'coordinator_core/distill/tests/test_log_append.py',
        'coordinator_core/distill/tests/test_ripe_filter.py',
        'coordinator_core/execute_plan_assemble/tests/test_close_out_and_stamp.py',
        'coordinator_core/frontmatter/tests/test_handoff_lineage_corpus_dangling_refs.py',
        'coordinator_core/frontmatter/tests/test_parity_handoff_ops.py',
        'coordinator_core/frontmatter/tests/test_parity_memo_ops.py',
        'coordinator_core/frontmatter/tests/test_plan_sizing_object_field.py',
        'coordinator_core/frontmatter/tests/test_schema_drift_watch.py',
        'coordinator_core/frontmatter/tests/test_schema_validate.py',
        'coordinator_core/git/test_commit_trailers.py',
        'coordinator_core/git/test_repo_root.py',
        'coordinator_core/hooks/test_auto_push.py',
        'coordinator_core/hooks/test_nudge_harness_directive_dispatch.py',
        'coordinator_core/hooks/test_nudge_unrouted_sizing.py',
        'coordinator_core/hooks/tests/test_c4_ownership_inherited_at_dispatch_tripwires.py',
        'coordinator_core/hooks/tests/test_lazy_hooks_channel.py',
        'coordinator_core/hooks/tests/test_track_touched_files_normalize.py',
        'coordinator_core/install/test_clone_sibling_repo.py',
        'coordinator_core/install/test_first_run.py',
        'coordinator_core/install/test_resolve_claude_klabauter.py',
        'coordinator_core/install/test_resolve_claude_klabauter_exec_cli.py',
        'coordinator_core/install/test_substrate.py',
        'coordinator_core/install/test_substrate_resolution_journal.py',
        'coordinator_core/install/test_wrap_hook_command_guarded.py',
        'coordinator_core/install/test_wrapper_onto_path.py',
        'coordinator_core/install/tests/test_observed_set_fold_install_surface.py',
        'coordinator_core/install/tests/test_scaffold_structure.py',
        'coordinator_core/ops/ceremony/tests/test_branch_resolution.py',
        'coordinator_core/ops/ceremony/tests/test_ceremony_claim_readers.py',
        'coordinator_core/ops/ceremony/tests/test_commit_exec_bit.py',
        'coordinator_core/ops/ceremony/tests/test_commit_gates.py',
        'coordinator_core/ops/ceremony/tests/test_commit_gates_known_scope.py',
        'coordinator_core/ops/ceremony/tests/test_commit_pipeline.py',
        'coordinator_core/ops/ceremony/tests/test_commit_scoped.py',
        'coordinator_core/ops/ceremony/tests/test_commit_scoped_trailer_replay.py',
        'coordinator_core/ops/ceremony/tests/test_consumed_handoff_stamp.py',
        'coordinator_core/ops/ceremony/tests/test_detached_render_commit.py',
        'coordinator_core/ops/ceremony/tests/test_git_native.py',
        'coordinator_core/ops/ceremony/tests/test_post_commit_tail.py',
        'coordinator_core/ops/ceremony/tests/test_receipt_emit.py',
        'coordinator_core/ops/ceremony/tests/test_render_handoff_tracker.py',
        'coordinator_core/ops/ceremony/tests/test_renderers_handoff_lineage.py',
        'coordinator_core/ops/ceremony/tests/test_resolver_git_provenance.py',
        'coordinator_core/ops/ceremony/tests/test_scoped_git_commit.py',
        'coordinator_core/ops/ceremony/tests/test_snapshot_diff_and_head.py',
        'coordinator_core/ops/ceremony/tests/test_update_docs_scan.py',
        'coordinator_core/ops/ceremony/tests/test_wsc_tail_chain_ancestry_waiver_bookkeeping.py',
        'coordinator_core/ops/ceremony/tests/test_wsc_tail_parity.py',
        'coordinator_core/ops/ceremony/tests/test_wsc_tail_trailer_divergence.py',
        'coordinator_core/ops/docgen/tests/test_c6_conformance.py',
        'coordinator_core/ops/emit/tests/test_backlogs_section.py',
        'coordinator_core/ops/emit/tests/test_check_shipped_on_main.py',
        'coordinator_core/ops/emit/tests/test_classify_shas_on_origin_main.py',
        'coordinator_core/ops/emit/tests/test_docs_staleness_stamp.py',
        'coordinator_core/ops/emit/tests/test_doe_drift.py',
        'coordinator_core/ops/emit/tests/test_emission_scope_e2e.py',
        'coordinator_core/ops/emit/tests/test_emit_context_resolve.py',
        'coordinator_core/ops/emit/tests/test_emit_default_path.py',
        'coordinator_core/ops/emit/tests/test_emit_graceful_absent.py',
        'coordinator_core/ops/emit/tests/test_emit_parity.py',
        'coordinator_core/ops/emit/tests/test_enrich.py',
        'coordinator_core/ops/emit/tests/test_file_attribution_section.py',
        'coordinator_core/ops/emit/tests/test_lessons_provenance_ref.py',
        'coordinator_core/ops/emit/tests/test_lma_cache.py',
        'coordinator_core/ops/emit/tests/test_market_intel_arrays_empty.py',
        'coordinator_core/ops/emit/tests/test_per_repo_emission_integration.py',
        'coordinator_core/ops/emit/tests/test_routine_signals_native_ports.py',
        'coordinator_core/ops/fleet/tests/test_memo_check_addressee.py',
        'coordinator_core/ops/fleet/tests/test_memo_send.py',
        'coordinator_core/ops/fleet/tests/test_setup_error_stderr_channel.py',
        'coordinator_core/ops/introspect/tests/test_verify_shipped.py',
        'coordinator_core/ops/memo/tests/test_memo_transition_commit_ownership.py',
        'coordinator_core/ops/memo/tests/test_memo_transition_distill_fate.py',
        'coordinator_core/ops/memo/tests/test_memo_transition_unit.py',
        'coordinator_core/ops/session/tests/test_boot_sweep.py',
        'coordinator_core/ops/session/tests/test_end_to_end_wrap_all_four_classes.py',
        'coordinator_core/ops/session/tests/test_guard_settings_integrity.py',
        'coordinator_core/ops/session/tests/test_guard_settings_integrity_restore_rungs.py',
        'coordinator_core/ops/session/tests/test_in_process_writer_claim_path.py',
        'coordinator_core/ops/session/tests/test_safe_commit_offer.py',
        'coordinator_core/ops/session/tests/test_scope_report.py',
        'coordinator_core/ops/session/tests/test_stranded_baton_drain.py',
        'coordinator_core/ops/session/tests/test_sweep_consumed_handoffs.py',
        'coordinator_core/ops/test_agent_worktree_sweep.py',
        'coordinator_core/ops/test_assert_doctrine_cross_reference_counts.py',
        'coordinator_core/ops/test_assert_no_dangling_plan_backlinks.py',
        'coordinator_core/ops/test_backfill_initiative_fk.py',
        'coordinator_core/ops/test_blocked.py',
        'coordinator_core/ops/test_bootstrap_orchestrate.py',
        'coordinator_core/ops/test_bootstrap_repo.py',
        'coordinator_core/ops/test_central_run_due.py',
        'coordinator_core/ops/test_changelog_compute_day_fields.py',
        'coordinator_core/ops/test_changelog_ops.py',
        'coordinator_core/ops/test_check_atlas_watch_drift.py',
        'coordinator_core/ops/test_check_auto_memory_drained.py',
        'coordinator_core/ops/test_check_auto_reconcile.py',
        'coordinator_core/ops/test_check_harvest_debt.py',
        'coordinator_core/ops/test_check_posix_exec_assumptions.py',
        'coordinator_core/ops/test_check_version_consistency.py',
        'coordinator_core/ops/test_check_windows_ssh_binary.py',
        'coordinator_core/ops/test_completion_ops.py',
        'coordinator_core/ops/test_configure_git.py',
        'coordinator_core/ops/test_coordinator_complete_entry.py',
        'coordinator_core/ops/test_coordinator_postsync_marker_resync_check.py',
        'coordinator_core/ops/test_coordinator_precommit_settings_tracking_check.py',
        'coordinator_core/ops/test_create_github_remote.py',
        'coordinator_core/ops/test_detect_changed_dependency_manifests.py',
        'coordinator_core/ops/test_detect_onboarding_offer.py',
        'coordinator_core/ops/test_detect_project_runtime.py',
        'coordinator_core/ops/test_detect_staged_rollback.py',
        'coordinator_core/ops/test_dirty_tree_gate.py',
        'coordinator_core/ops/test_discover_working_repos.py',
        'coordinator_core/ops/test_dispatch_shape_classify.py',
        'coordinator_core/ops/test_doc_content_verify.py',
        'coordinator_core/ops/test_doc_staleness.py',
        'coordinator_core/ops/test_draft_plan_aging.py',
        'coordinator_core/ops/test_ensure_doe_clone.py',
        'coordinator_core/ops/test_fan_out_integrator.py',
        'coordinator_core/ops/test_find_polluter.py',
        'coordinator_core/ops/test_fold_execution_record.py',
        'coordinator_core/ops/test_generate_exec_summary.py',
        'coordinator_core/ops/test_install_claude_klabauter_precommit_hook.py',
        'coordinator_core/ops/test_install_meta_repo_precommit_hook.py',
        'coordinator_core/ops/test_install_meta_repo_precommit_hook_install_all.py',
        'coordinator_core/ops/test_install_post_sync_hooks.py',
        'coordinator_core/ops/test_install_publish_repo_precommit_hook.py',
        'coordinator_core/ops/test_merge_branch_into_workstream.py',
        'coordinator_core/ops/test_merge_quiet_activity_gate.py',
        'coordinator_core/ops/test_migrate_branch_canonical_case.py',
        'coordinator_core/ops/test_migrate_completion_log_legacy.py',
        'coordinator_core/ops/test_migrate_cross_repo_layout.py',
        'coordinator_core/ops/test_mirror_ci_run_tests_require_tests.py',
        'coordinator_core/ops/test_new_project_scaffold.py',
        'coordinator_core/ops/test_normalize_claimed_frontmatter.py',
        'coordinator_core/ops/test_orphan_branch_sweep.py',
        'coordinator_core/ops/test_parse_resolves_trailer.py',
        'coordinator_core/ops/test_percolate_check_inverse_drift.py',
        'coordinator_core/ops/test_promote_shipped_in_flight_stubs.py',
        'coordinator_core/ops/test_push_failure_verdict.py',
        'coordinator_core/ops/test_reap_stale_locks.py',
        'coordinator_core/ops/test_refresh_roadmap_callout.py',
        'coordinator_core/ops/test_release_tagging.py',
        'coordinator_core/ops/test_renormalize_index.py',
        'coordinator_core/ops/test_repo_bootstrap.py',
        'coordinator_core/ops/test_research_archive_workdir.py',
        'coordinator_core/ops/test_research_dir_restructure.py',
        'coordinator_core/ops/test_resolve_baton_path.py',
        'coordinator_core/ops/test_resolve_swept_baton.py',
        'coordinator_core/ops/test_review_coverage_core.py',
        'coordinator_core/ops/test_review_freeze_diff.py',
        'coordinator_core/ops/test_review_trail_write.py',
        'coordinator_core/ops/test_rollup_derive.py',
        'coordinator_core/ops/test_run_pre_ci_hooks.py',
        'coordinator_core/ops/test_run_semgrep_scan.py',
        'coordinator_core/ops/test_run_shellcheck_sweep.py',
        'coordinator_core/ops/test_scan_unresolved_ubt_records.py',
        'coordinator_core/ops/test_schema_drift_gate.py',
        'coordinator_core/ops/test_session_hierarchy_query.py',
        'coordinator_core/ops/test_setup_chain_walker.py',
        'coordinator_core/ops/test_sync_main.py',
        'coordinator_core/ops/test_verify_arch_audit_atlas_refresh.py',
        'coordinator_core/ops/test_verify_fix_files_changed.py',
        'coordinator_core/ops/test_verify_orientation_cache_sync.py',
        'coordinator_core/ops/test_verify_scout_inventory_completeness.py',
        'coordinator_core/ops/test_whoami_run_tests.py',
        'coordinator_core/ops/test_workday_complete_backfill_scan.py',
        'coordinator_core/ops/test_workday_complete_step2_5_dirty_tree.py',
        'coordinator_core/ops/test_workday_start_cross_repo_memo_outbox_surface.py',
        'coordinator_core/ops/test_workday_start_cross_repo_memo_surface.py',
        'coordinator_core/ops/test_workday_start_step0_reconcile.py',
        'coordinator_core/ops/test_workday_surface_stale_stash_entries.py',
        'coordinator_core/ops/test_workweek_complete_reverse_drift_gate.py',
        'coordinator_core/ops/test_workweek_trail_scope.py',
        'coordinator_core/ops/test_write_identity_file.py',
        'coordinator_core/ops/tests/test_ac_priority_drain_durability.py',
        'coordinator_core/ops/tests/test_artifact_emit_scope_touch.py',
        'coordinator_core/ops/tests/test_assert_plan_sizing_citation.py',
        'coordinator_core/ops/tests/test_audience_mismatch_scan.py',
        'coordinator_core/ops/tests/test_cartography_chunk_table.py',
        'coordinator_core/ops/tests/test_cascade_backstop_sweep.py',
        'coordinator_core/ops/tests/test_cascade_baton_rows.py',
        'coordinator_core/ops/tests/test_cascade_retract.py',
        'coordinator_core/ops/tests/test_changelog_parity.py',
        'coordinator_core/ops/tests/test_completion_ops_claim_state.py',
        'coordinator_core/ops/tests/test_completion_ops_concurrency.py',
        'coordinator_core/ops/tests/test_completion_ops_reconcile.py',
        'coordinator_core/ops/tests/test_completion_parity.py',
        'coordinator_core/ops/tests/test_coverage_gate_mint_chain_waivers.py',
        'coordinator_core/ops/tests/test_crossrepo_closure_status.py',
        'coordinator_core/ops/tests/test_cutover_advance.py',
        'coordinator_core/ops/tests/test_cutover_gate_schema_resolution.py',
        'coordinator_core/ops/tests/test_deliverable_cascade_claim_state.py',
        'coordinator_core/ops/tests/test_deliverable_rollup.py',
        'coordinator_core/ops/tests/test_distill_apply_disposal.py',
        'coordinator_core/ops/tests/test_distill_curation_status.py',
        'coordinator_core/ops/tests/test_distill_disposal_manifest.py',
        'coordinator_core/ops/tests/test_distill_scope.py',
        'coordinator_core/ops/tests/test_distill_stamp_disposal.py',
        'coordinator_core/ops/tests/test_doctor.py',
        'coordinator_core/ops/tests/test_engine_drift.py',
        'coordinator_core/ops/tests/test_goal_kr_status.py',
        'coordinator_core/ops/tests/test_goals_match.py',
        'coordinator_core/ops/tests/test_handoff_author_fork.py',
        'coordinator_core/ops/tests/test_handoff_author_fork_claim_state.py',
        'coordinator_core/ops/tests/test_handoff_correct_body.py',
        'coordinator_core/ops/tests/test_handoff_lineage_ancestry.py',
        'coordinator_core/ops/tests/test_handoff_match.py',
        'coordinator_core/ops/tests/test_handoff_repair_archived_shipped_in.py',
        'coordinator_core/ops/tests/test_initiatives_serve.py',
        'coordinator_core/ops/tests/test_ipc_get_op_handler_lazy_resolution.py',
        'coordinator_core/ops/tests/test_lazy_ops_channel.py',
        'coordinator_core/ops/tests/test_memo_fate_backfill.py',
        'coordinator_core/ops/tests/test_memo_triage.py',
        'coordinator_core/ops/tests/test_normalize_claimed_frontmatter.py',
        'coordinator_core/ops/tests/test_op_registration.py',
        'coordinator_core/ops/tests/test_ownership_index.py',
        'coordinator_core/ops/tests/test_plan_match.py',
        'coordinator_core/ops/tests/test_plan_status_transition.py',
        'coordinator_core/ops/tests/test_plan_status_transition_commit_ownership.py',
        'coordinator_core/ops/tests/test_plan_tasks_grouping_digest.py',
        'coordinator_core/ops/tests/test_plan_tasks_mutate.py',
        'coordinator_core/ops/tests/test_priority_drain.py',
        'coordinator_core/ops/tests/test_priority_set.py',
        'coordinator_core/ops/tests/test_queue_age_ping.py',
        'coordinator_core/ops/tests/test_queue_append_concurrency.py',
        'coordinator_core/ops/tests/test_queue_close.py',
        'coordinator_core/ops/tests/test_queue_cluster.py',
        'coordinator_core/ops/tests/test_queue_family.py',
        'coordinator_core/ops/tests/test_queue_parity.py',
        'coordinator_core/ops/tests/test_queue_scaffold_baton.py',
        'coordinator_core/ops/tests/test_records_query.py',
        'coordinator_core/ops/tests/test_registry_map_sync.py',
        'coordinator_core/ops/tests/test_review_brightline_gate_claim_state.py',
        'coordinator_core/ops/tests/test_review_trail_readjudication_report.py',
        'coordinator_core/ops/tests/test_review_trail_write.py',
        'coordinator_core/ops/tests/test_roadmap_serve.py',
        'coordinator_core/ops/tests/test_strang10_invoke_smoke.py',
        'coordinator_core/orientation/test_regenerate_cache.py',
        'coordinator_core/percolate/tests/test_engine.py',
        'coordinator_core/percolate/tests/test_rewrite.py',
        'coordinator_core/percolate/tests/test_runtime_root.py',
        'coordinator_core/pickup_assemble/tests/test_claim_state_reads.py',
        'coordinator_core/plugin_health/tests/test_drift.py',
        'coordinator_core/reconcile/tests/test_ac27_differential_oracle.py',
        'coordinator_core/reconcile/tests/test_commit_reality.py',
        'coordinator_core/reconcile/tests/test_commitments_recheck.py',
        'coordinator_core/review_assemble/test_exec_auth_stamp.py',
        'coordinator_core/review_assemble/test_residue.py',
        'coordinator_core/roadmap/tests/test_audit.py',
        'coordinator_core/session/tests/test_claims.py',
        'coordinator_core/session/tests/test_claude_md_grant.py',
        'coordinator_core/session/tests/test_cli_transport.py',
        'coordinator_core/session/tests/test_core.py',
        'coordinator_core/session/tests/test_grant.py',
        'coordinator_core/session/tests/test_js_bridge_cli.py',
        'coordinator_core/session/tests/test_liveness.py',
        'coordinator_core/session/tests/test_scope.py',
        'coordinator_core/session/tests/test_shape.py',
        'coordinator_core/session/tests/test_stale_claims.py',
        'coordinator_core/session/tests/test_stale_claims_claim_state.py',
        'coordinator_core/session/tests/test_tier_u_gate.py',
        'coordinator_core/session/tests/test_worktree_safety.py',
        'coordinator_core/session_ledger/test_aggregate_chain_loe.py',
        'coordinator_core/subagent_sandbox/tests/test_engine.py',
        'coordinator_core/subagent_sandbox/tests/test_provision_report.py',
        'coordinator_core/subagent_sandbox/tests/test_provision_report_touch_claim.py',
        'coordinator_core/test_archive_stamp.py',
        'coordinator_core/test_backlog_grind_assemble.py',
        'coordinator_core/test_baton_assemble.py',
        'coordinator_core/test_ceremony_common_tail.py',
        'coordinator_core/test_cockpit_contract_freshness.py',
        'coordinator_core/test_diff_scoped_tests.py',
        'coordinator_core/test_lazy_reexports.py',
        'coordinator_core/test_machine_resolver.py',
        'coordinator_core/test_pickup_apply.py',
        'coordinator_core/test_pickup_assemble.py',
        'coordinator_core/test_pickup_assemble_reply_closure.py',
        'coordinator_core/test_pickup_assemble_stamp_check.py',
        'coordinator_core/test_resolve_validation_cmd.py',
        'coordinator_core/testing/test_full_runner.py',
        'coordinator_core/testing/test_suite_mutex.py',
        'coordinator_core/tests/test_bash_guards_avoid_hooks_package.py',
        'coordinator_core/tests/test_commit_anchors.py',
        'coordinator_core/tests/test_coverage_reviewed_set.py',
        'coordinator_core/tests/test_cross_repo_probe_git_scoping.py',
        'coordinator_core/tests/test_dag_git_path_ever_tracked_cache.py',
        'coordinator_core/tests/test_dag_handoff_id_index.py',
        'coordinator_core/tests/test_dag_resolve_target_tier3_dedup.py',
        'coordinator_core/tests/test_decision_object_envelope.py',
        'coordinator_core/tests/test_detect_existing_claude_home.py',
        'coordinator_core/tests/test_draft_plan_aging.py',
        'coordinator_core/tests/test_engine_version.py',
        'coordinator_core/tests/test_git_ancestry.py',
        'coordinator_core/tests/test_git_scope.py',
        'coordinator_core/tests/test_gitattributes_eol_coverage.py',
        'coordinator_core/tests/test_handoff_gate_aging.py',
        'coordinator_core/tests/test_hooks_bookkeeping.py',
        'coordinator_core/tests/test_hot_path_hook_import_budget.py',
        'coordinator_core/tests/test_install_chain_driven_leaf_seed_sweep.py',
        'coordinator_core/tests/test_install_publish_repo_precommit_hook.py',
        'coordinator_core/tests/test_invoke_main.py',
        'coordinator_core/tests/test_ipc_scope_touch_self_report.py',
        'coordinator_core/tests/test_locked_write.py',
        'coordinator_core/tests/test_claude_klabauter_doctor_probe_selectors.py',
        'coordinator_core/tests/test_no_optional_locks_read_sites.py',
        'coordinator_core/tests/test_normalize_snippet.py',
        'coordinator_core/tests/test_package_installable.py',
        'coordinator_core/tests/test_pickup_assemble_git_readmodel_parity.py',
        'coordinator_core/tests/test_pickup_assemble_import_perf.py',
        'coordinator_core/tests/test_pickup_assemble_scoped_commit.py',
        'coordinator_core/tests/test_review_brightline_gate.py',
        'coordinator_core/tests/test_scope_soak_enable.py',
        'coordinator_core/tests/test_scope_warning_resolve.py',
        'coordinator_core/tests/test_session_attribution.py',
        'coordinator_core/tests/test_sibling_fact.py',
        'coordinator_core/tests/test_strategic_emit.py',
        'coordinator_core/tests/test_strategic_generate.py',
        'coordinator_core/tests/test_tracker_projection.py',
        'coordinator_core/tests/test_tracker_store.py',
        'coordinator_core/tests/test_verify_orientation_cache_sync.py',
        'coordinator_core/workday_complete/test_brief_goal_close_day.py',
        'coordinator_core/workstream_complete/test_apply.py',
        'coordinator_core/workstream_complete/test_chain_partition_verdict_store_claim_path.py',
        'coordinator_core/workstream_complete/test_workstream_complete.py',
        'coordinator_core/workstream_complete/test_workstream_complete_contract.py',
        'coordinator_core/write_guards/tests/test_ac6_keep_hard_sweep.py',
        'coordinator_core/write_guards/tests/test_bump_out_of_repo_tool_write.py',
        'coordinator_core/write_guards/tests/test_casefold_bypass_lint.py',
        'coordinator_core/write_guards/tests/test_guard_concrete_path_citations.py',
        'coordinator_core/write_guards/tests/test_guard_doctrine_surface_edits.py',
        'coordinator_core/write_guards/tests/test_repo_root_shared_resolver.py',
    }
)

_BASELINE_COUNT = 618


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
    "against a mocked/faked git rather than a real spawned process. This is "
    "a RATCHET, not a permanent block -- new files must declare via the "
    "marker rather than growing the frozen _BASELINE grandfather list."
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
    """Rule 2 -- RATCHET. A file with a function-level spawn site must be
    either in the frozen _BASELINE or fully marker-declared. See module
    docstring 'Rule 2' section."""
    violations: list[str] = []
    for report in _all_reports():
        if not report.unmarked_spawning_funcs:
            continue
        if report.relpath in _BASELINE:
            continue
        funcs = ", ".join(report.unmarked_spawning_funcs)
        violations.append(f"{report.relpath} (unmarked: {funcs})")
    if violations:
        lines = "\n".join(f"  {v}" for v in sorted(violations))
        pytest.fail(
            f"SPAWN-RATCHET Rule 2: {len(violations)} NEW spawning test file(s) "
            f"not in the frozen baseline and not marked (direct or via a "
            f"resolved spawn-wrapper call; {len(_UNRESOLVED_IMPORTS)} import(s) "
            f"and {len(_UNKNOWN_ARGV0)} spawn-site argv0(s) were unresolvable "
            f"and skipped rather than guessed):\n{lines}\n\n"
            f"{_ALTERNATIVE_MSG_RULE2}"
        )


def test_rule3_baseline_entries_are_not_stale() -> None:
    """Rule 3 -- a _BASELINE entry that no longer exists, or no longer
    contains a function-level spawn site, fails as stale. Keeps the
    baseline monotonically shrinking rather than a permanent parking lot.
    Mirrors coordinator/tests/test_dr084_single_accessor_guard.py's
    test_allowlist_entries_still_exist_on_disk shape."""
    reports_by_path = {report.relpath: report for report in _all_reports()}
    stale: list[str] = []
    for relpath in sorted(_BASELINE):
        full_path = REPO_ROOT / relpath
        if not full_path.is_file():
            stale.append(f"{relpath} (file no longer exists on disk)")
            continue
        report = reports_by_path.get(relpath)
        if report is None or not report.unmarked_spawning_funcs:
            stale.append(f"{relpath} (no longer contains a function-level spawn site)")
    assert not stale, (
        f"SPAWN-RATCHET Rule 3: {len(stale)} stale _BASELINE entry(ies) -- remove them, "
        "the baseline shrinks monotonically as files are fixed or excised:\n"
        + "\n".join(f"  {s}" for s in stale)
    )


def test_baseline_count_matches_header() -> None:
    """Sanity anchor: the frozen baseline's size, asserted here rather than
    only in a comment, so a hand-edit to _BASELINE that silently drifts
    from its own documented count fails loudly."""
    assert len(_BASELINE) == _BASELINE_COUNT


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
