"""A published CLI's imports must resolve against the UNTRANSFORMED live modules.

THE FAILURE THIS EXISTS FOR, because it is not obvious and it has now bitten
three times in one night. The published engine and its CLIs are transformed on
the way out -- every `claude-klabauter` identifier becomes `claude_klabauter`. But a
published CLI puts the LIVE tree's `coordinator/bin/lib` on `sys.path` and
imports its helpers from there, and the live tree is not transformed. So the
moment a `bin/lib` symbol carrying a repo token is renamed, the published side
asks for the new spelling and the live side offers only the old one.

WHY NO EXISTING TEST CATCHES IT. Both trees are internally consistent. The live
suite runs live-against-live and passes; the mirror is consistent with itself.
The break only exists at the seam between them, and it surfaces as some other
session's ceremony dying on ImportError -- attributed to whoever ran the ceremony,
not to whoever did the rename. That is the worst possible failure signal.

Fix a failure here by exporting BOTH spellings from the live module, the way
`cc_invoke._resolve_claude_klabauter_root` does: an alias assignment costs
nothing, transforms into a harmless self-assignment in the mirror, and closes
the window until no published CLI references the old name.

Skips when no published mirror is present -- this asserts a property of a
two-tree box and must not fail a single-tree checkout.
"""

from __future__ import annotations

import ast
import os
import pathlib

import pytest

_LIVE = pathlib.Path(__file__).resolve().parents[3]

_LIB_DIRS = ("coordinator/bin/lib", "coordinator/lib")
_CLI_PREFIXES = ("coordinator/bin/", "coordinator/lib/", "bin/", "scripts/")


def _mirror_root() -> pathlib.Path | None:
    for key in ("CLAUDE_KLABAUTER_ROOT", "COORDINATOR_ENGINE_ROOT", "CLAUDE_KLABAUTER_ROOT"):
        val = os.environ.get(key)
        if val and (pathlib.Path(val) / "coordinator_core").is_dir():
            p = pathlib.Path(val)
            if p.resolve() != _LIVE.resolve():
                return p
    sibling = _LIVE.parent / "claude-klabauter"
    return sibling if (sibling / "coordinator_core").is_dir() else None


def _exported_names(path: pathlib.Path) -> set[str]:
    """Every name importable from a module: defs, classes, assignments,
    annotated assignments, and RE-EXPORTS via its own module-level imports.

    The re-export leg is load-bearing: `records_query` offers `route_mutation`
    only by importing it from `cc_invoke`, and a scan without this leg reports a
    working import as broken.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
    return names


@pytest.fixture(scope="module")
def mirror():
    root = _mirror_root()
    if root is None:
        pytest.skip("no published mirror on this box — single-tree checkout")
    return root


@pytest.fixture(scope="module")
def live_lib_exports() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for d in _LIB_DIRS:
        p = _LIVE / d
        if p.is_dir():
            for f in p.glob("*.py"):
                out.setdefault(f.stem, _exported_names(f))
    return out


@pytest.fixture(scope="module")
def findings(mirror, live_lib_exports):
    resolved, missing = 0, []
    for f in sorted(mirror.rglob("*.py")):
        rel = f.relative_to(mirror).as_posix()
        if not rel.startswith(_CLI_PREFIXES):
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level:
                continue
            if node.module not in live_lib_exports:
                continue
            for alias in node.names:
                if alias.name in live_lib_exports[node.module]:
                    resolved += 1
                else:
                    missing.append((rel, node.module, alias.name))
    return resolved, missing


def test_the_scan_reaches_real_imports(findings):
    """Non-vacuity: a wrong mirror path would make this pass by scanning nothing."""
    resolved, _ = findings
    assert resolved > 50, (
        f"only {resolved} cross-tree imports found — the mirror path or the lib "
        "inventory is wrong, so a green result here proves nothing"
    )


def test_every_published_cli_import_resolves_against_the_live_lib(findings):
    _resolved, missing = findings
    if not missing:
        return
    lines = sorted({f"from {mod} import {name}  (e.g. {rel})" for rel, mod, name in missing})
    pytest.fail(
        "published CLIs import names the LIVE bin/lib does not offer. Each will "
        "ImportError at runtime in whatever ceremony calls it, and will be blamed on "
        "the session that ran the ceremony rather than on the rename.\n"
        "Fix by exporting BOTH spellings from the live module (see this module's "
        "docstring), not by editing the mirror.\n  " + "\n  ".join(lines)
    )


# THE SECOND, DISTINCT FAILURE THIS MODULE ALSO COVERS (2026-09-24 klabauter
# publish bug, 265 collection errors): the cross-tree seam above is not the
# only way a published import can dangle. A percolate content-transform pass
# can rewrite `from coordinator_core._claude_klabauter_root import x` to
# `from coordinator_core._claude_klabauter_root import x` (substitute /
# stem-rewrite) while the FILE `coordinator_core/_claude_klabauter_root.py` itself has
# no matching `basename_rename` entry and keeps shipping under its old name --
# a purely INTRA-mirror inconsistency, entirely within one tree, that the
# cross-tree check above cannot see: it only ever compares a mirror's imports
# against the LIVE checkout's `bin/lib`/`coordinator/lib` exports, never a
# mirror's own `coordinator_core` package against itself. This is why that
# check stayed green through the whole incident.
#
# `test_module_rename_and_import_rewrite_agree_after_a_real_sweep`
# (coordinator_core/percolate/tests/test_klabauter_scrub_gap_exercised.py)
# pins the store-level mechanism (substitute/stem-rewrite vs basename_rename)
# pre-publish, on a fixture. This check is its end-to-end twin: it walks a
# REAL published mirror's own `coordinator_core` tree and asserts every
# absolute `coordinator_core.<name>` import it contains resolves to a module
# that actually exists in that SAME mirror -- catching this bug class
# regardless of which content-transform mechanism produced the mismatch.
def _is_import_error_handler(handler: ast.ExceptHandler) -> bool:
    """True when an `except` clause catches `ImportError`/`ModuleNotFoundError`
    (directly, via a tuple, a bare `except:`, or a blanket `Exception`/
    `BaseException`) -- a deliberately-tolerant guarded import, the documented
    pattern for a caller of a module another chunk killed on purpose (e.g.
    `handoff_ship_archive.py`'s `archive_shipped_handoffs` import, C1b) rather
    than an accidental dangling reference."""
    if handler.type is None:
        return True
    elts = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    names = {e.attr if isinstance(e, ast.Attribute) else getattr(e, "id", None) for e in elts}
    return bool(names & {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"})


def _is_pytest_raises_import_error(context_expr: ast.expr) -> bool:
    """True for `with pytest.raises(ModuleNotFoundError)` / `raises(ImportError)`
    -- a test deliberately asserting a module is ABSENT (e.g.
    `test_claude_klabauter_root_module_is_retired`'s `with pytest.raises(ModuleNotFoundError):
    import coordinator_core.claude_klabauter_root`, pinning C1's "move, don't re-export"),
    never a real dangling reference."""
    if not isinstance(context_expr, ast.Call):
        return False
    func = context_expr.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name != "raises":
        return False
    for arg in context_expr.args:
        argname = arg.attr if isinstance(arg, ast.Attribute) else getattr(arg, "id", None)
        if argname in ("ImportError", "ModuleNotFoundError"):
            return True
    return False


class _GuardAwareImportFinder(ast.NodeVisitor):
    """Collects every ABSOLUTE `coordinator_core.<...>` dotted module named by
    an `import`/`from ... import` in a file, skipping one that sits inside a
    guarded context (§ `_is_import_error_handler` / `_is_pytest_raises_import_error`)
    -- those are deliberately expected to fail, not a defect this check exists
    to find. `node.level` (a relative `from . import x`) is skipped
    unconditionally: this check is scoped to ABSOLUTE `coordinator_core.*`
    references, the shape a content-transform's substitute/stem-rewrite pass
    actually rewrites the text of.
    """

    def __init__(self) -> None:
        self.dotted_modules: set[str] = set()
        self._guard_depth = 0

    def visit_Try(self, node: ast.Try) -> None:
        if any(_is_import_error_handler(h) for h in node.handlers):
            self._guard_depth += 1
            for child in node.body:
                self.visit(child)
            self._guard_depth -= 1
            for child in (*node.handlers, *node.orelse, *node.finalbody):
                self.visit(child)
        else:
            self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        if any(_is_pytest_raises_import_error(item.context_expr) for item in node.items):
            self._guard_depth += 1
            for child in node.body:
                self.visit(child)
            self._guard_depth -= 1
        else:
            self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        if self._guard_depth:
            return
        for alias in node.names:
            if alias.name == "coordinator_core" or alias.name.startswith("coordinator_core."):
                self.dotted_modules.add(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self._guard_depth or node.level:
            return
        if node.module and (
            node.module == "coordinator_core" or node.module.startswith("coordinator_core.")
        ):
            self.dotted_modules.add(node.module)


def _coordinator_core_import_findings(mirror: pathlib.Path) -> list[tuple[str, str]]:
    """`(rel_path, dotted_module)` for every UNGUARDED `coordinator_core.<...>`
    import in `mirror` whose named module does not exist on disk in `mirror`.

    Resolves `import coordinator_core.a.b` / `from coordinator_core.a.b import x`
    against `coordinator_core/a/b.py`, `coordinator_core/a/b/__init__.py` (a
    regular package), OR a bare `coordinator_core/a/b/` directory (a PEP 420
    IMPLICIT namespace package -- `__init__.py` is not required for one, and
    requiring it here produced a false positive against `coordinator_core/
    warm/door/`, a real namespace package with no `__init__.py` that imports
    correctly in both the live tree and the published mirror).
    """
    root = mirror / "coordinator_core"
    if not root.is_dir():
        return []
    missing: list[tuple[str, str]] = []
    for f in sorted(root.rglob("*.py")):
        rel = f.relative_to(mirror).as_posix()
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        finder = _GuardAwareImportFinder()
        finder.visit(tree)
        for dotted in finder.dotted_modules:
            if dotted == "coordinator_core":
                continue
            rel_parts = dotted.split(".")[1:]
            candidate = mirror.joinpath("coordinator_core", *rel_parts)
            if candidate.with_suffix(".py").is_file():
                continue
            if candidate.is_dir():
                continue
            missing.append((rel, dotted))
    return missing


def test_mirrors_own_coordinator_core_imports_resolve_within_itself(mirror):
    missing = _coordinator_core_import_findings(mirror)
    if not missing:
        return
    lines = sorted({f"{dotted}  (e.g. {rel})" for rel, dotted in missing})
    pytest.fail(
        "the published mirror's own coordinator_core imports a module that does "
        "not exist anywhere in that SAME mirror. A content-transform pass "
        "rewrote the import TEXT (substitute/stem-rewrite) without a matching "
        "basename_rename entry renaming the FILE -- ModuleNotFoundError at "
        "collection time for every consumer (the 2026-09-24 klabauter publish "
        "bug: coordinator-safe-commit, session-claim-cli, 265 collection "
        "errors). Fix in setup/percolate-hooks/percolate-store.yaml: add the "
        "missing basename_rename entry (or entries) so the renamed file and "
        "the rewritten import agree.\n  " + "\n  ".join(lines)
    )
