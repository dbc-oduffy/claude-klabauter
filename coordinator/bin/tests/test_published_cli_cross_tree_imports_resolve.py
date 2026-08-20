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
