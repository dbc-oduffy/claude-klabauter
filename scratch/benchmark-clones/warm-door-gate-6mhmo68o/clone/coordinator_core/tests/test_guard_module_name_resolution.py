"""Every name a guard module references at runtime must actually resolve.

Why this exists as its own gate rather than riding on the guard suites:
a guard module in a HALF-LANDED state does not fail neutral, it fails
**closed**, and it fails closed for every session sharing the tree -- not
just the one doing the editing. Ordinary code breaks its own caller; a
PreToolUse guard breaks everyone else's unrelated commands. On 2026-07-30
the window between "the import block was stripped" and "the call sites were
removed" was live-fire for a peer repo's session: `guard_grep_via_bash`
raised `NameError: name 'operator_override_note' is not defined`, the
dispatcher routed the crash to `_crash_deny`, and a DoE-claude session had
its `grep` invocations denied by a defect in an uncommitted file it did not
own (`cross-repo/inbox/2026-07-30-doe-claude-em-guard-grep-via-bash-
nameerror.md`).

**A plain import -- or `compileall` -- cannot catch this.** The module
imports fine; `NameError` is raised when the function body finally executes
a global load that has no binding, which is precisely why it reached a live
session instead of a test. So this gate does not merely import each module:
it imports it AND statically resolves every global name each of its scopes
references against the imported module's namespace plus builtins. A stripped
import fails here instead of in a peer's Bash call.

Scope is both guard packages, for the same reason in two different
directions: a `NameError` in `bash_guards` fails CLOSED and wedges peers,
while one in `write_guards` is swallowed by the engine's per-guard
containment and silently DISARMS that guard. Loud-wrong and quiet-wrong are
both worth a test.

Negative-spec -- this is not a linter and must not grow into one. It checks
resolvability of referenced globals, nothing about style, unused imports, or
shadowing. It is stdlib-only (`symtable`) on purpose: `pyproject.toml`'s
test dependencies are deliberately confined to pytest/pytest-xdist, and
pulling in pyflakes to catch one bug class would trade that for a
third-party gate.
"""
from __future__ import annotations

import builtins
import importlib
import pathlib
import symtable
from types import ModuleType
from typing import Iterator, List, Set, Tuple

import pytest

GUARD_PACKAGES = (
    "coordinator_core.bash_guards",
    "coordinator_core.write_guards",
)

# Names the interpreter injects into a module namespace, which `vars(mod)`
# may or may not carry depending on how the module was loaded.
_IMPLICIT_MODULE_NAMES = frozenset(
    {"__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__", "__builtins__"}
)


def _walk_scopes(table: symtable.SymbolTable) -> Iterator[symtable.SymbolTable]:
    yield table
    for child in table.get_children():
        yield from _walk_scopes(child)


def _guard_modules() -> List[Tuple[str, pathlib.Path]]:
    """Every module directly inside each guard package -- guards, their
    shared private helpers, and the package `__init__` alike. Subpackages
    (`tests/`) are deliberately excluded: a test module's names are resolved
    by running it."""
    found: List[Tuple[str, pathlib.Path]] = []
    for package in GUARD_PACKAGES:
        pkg = importlib.import_module(package)
        pkg_dir = pathlib.Path(next(iter(pkg.__path__)))
        for path in sorted(pkg_dir.glob("*.py")):
            modname = package if path.stem == "__init__" else f"{package}.{path.stem}"
            found.append((modname, path))
    return found


def _resolvable_names(module: ModuleType, source: str) -> Set[str]:
    """The bindings a global load in `module` can legitimately find.

    `vars(module)` covers module-level definitions and imports; builtins
    cover the rest. A name assigned under a `global` declaration inside some
    function is added too -- it is absent from the namespace until that
    function first runs, so treating it as undefined would be a false
    positive."""
    declared_global: Set[str] = set()
    for scope in _walk_scopes(symtable.symtable(source, module.__name__, "exec")):
        for symbol in scope.get_symbols():
            if symbol.is_declared_global() and symbol.is_assigned():
                declared_global.add(symbol.get_name())
    return set(vars(module)) | set(dir(builtins)) | declared_global | set(_IMPLICIT_MODULE_NAMES)


def unresolved_global_references(module: ModuleType, source: str) -> List[Tuple[str, str]]:
    """`(scope_name, symbol_name)` for every referenced global with no
    binding -- i.e. every `NameError` waiting for its code path to run.

    Module scope and inner scopes are read differently on purpose. At module
    scope, symtable reports no `is_global()`, so the test is "referenced but
    never assigned or imported here". Inside a function or class, a name
    resolved neither locally nor as a closure comes back `is_global()`, and
    that is the set to check.
    """
    resolvable = _resolvable_names(module, source)
    unresolved: List[Tuple[str, str]] = []
    for scope in _walk_scopes(symtable.symtable(source, module.__name__, "exec")):
        at_module_scope = scope.get_type() == "module"
        for symbol in scope.get_symbols():
            if not symbol.is_referenced():
                continue
            if at_module_scope:
                if symbol.is_assigned() or symbol.is_imported():
                    continue
            elif not symbol.is_global():
                continue
            if symbol.get_name() not in resolvable:
                unresolved.append((scope.get_name(), symbol.get_name()))
    return unresolved


_GUARD_MODULES = _guard_modules()


def test_guard_module_discovery_is_non_trivial():
    """Guard the guard: if discovery silently returns nothing -- a renamed
    package, a changed `__path__` shape -- every parametrized case below
    would vanish rather than fail, and this gate would pass while checking
    nothing."""
    assert len(_GUARD_MODULES) > 20, _GUARD_MODULES
    names = {modname for modname, _ in _GUARD_MODULES}
    assert "coordinator_core.bash_guards.guard_grep_via_bash" in names
    assert "coordinator_core.write_guards.engine" in names


@pytest.mark.parametrize("modname,path", _GUARD_MODULES, ids=[m for m, _ in _GUARD_MODULES])
def test_every_referenced_global_resolves(modname: str, path: pathlib.Path):
    module = importlib.import_module(modname)
    unresolved = unresolved_global_references(module, path.read_text(encoding="utf-8"))
    assert not unresolved, (
        "%s references names that resolve to nothing at runtime -- a call reaching "
        "any of them raises NameError, and in `bash_guards` that crash fails CLOSED "
        "for every session on this tree, not just this repo's. Most likely an import "
        "block was stripped ahead of its call sites: %r" % (modname, unresolved)
    )


def test_the_detector_catches_a_stripped_import():
    """The 2026-07-30 shape, reconstructed: two helpers called, neither
    imported. Without this case the gate above could silently degrade to a
    no-op (returning `[]` for everything) and still look green."""
    source = (
        '"""doc"""\n'
        "from os import path\n"
        "\n"
        "def check(payload):\n"
        "    if operator_override_note(payload):\n"
        "        return platform_verdict_for_shape(payload)\n"
        "    return path.sep\n"
    )
    module = ModuleType("synthetic_half_landed_guard")
    module.path = __import__("os").path

    unresolved = unresolved_global_references(module, source)

    assert sorted(name for _, name in unresolved) == [
        "operator_override_note",
        "platform_verdict_for_shape",
    ], unresolved


def test_the_detector_does_not_flag_a_well_formed_module():
    """The other half of the calibration -- imports, module-level constants,
    a `global` binding assigned only inside a function, and a genuine
    closure must all read as resolvable."""
    source = (
        "import os\n"
        "\n"
        "LIMIT = 3\n"
        "_cache = None\n"
        "\n"
        "def outer(seed):\n"
        "    def inner():\n"
        "        return seed + LIMIT\n"
        "    return inner()\n"
        "\n"
        "def prime():\n"
        "    global _cache\n"
        "    _cache = os.sep\n"
    )
    module = ModuleType("synthetic_well_formed_guard")
    module.os = os = __import__("os")
    module.LIMIT = 3
    del os

    assert unresolved_global_references(module, source) == []
