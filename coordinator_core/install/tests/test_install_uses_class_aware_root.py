"""Guard: every `coordinator_core/install/*` module resolves the claude-klabauter
root through the class-AWARE resolver, never the class-less one.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C6

Purpose: `coordinator_engine_root()` is a class-less three-rung ladder;
the published-engine gate (live-working-tree vs. published-mirror) lives
only on its sibling `coordinator_engine_root_with_class()`. An install-chain
module that imports the class-less form is gate-blind by construction --
this test is the mechanical backstop that a future importer cannot silently
regress back onto the bypassing form.

Negative-spec: this test does NOT assert that every found call site uses
the two-tuple return value for gating logic -- only that no module under
`coordinator_core/install/` imports/uses the bare class-less name. Modules
outside `coordinator_core/install/` (e.g. `substrate.py`'s siblings under
other packages, or `coordinator/lib/resolve-claude-klabauter/`) are out of this
test's scope by design.
"""

from __future__ import annotations

import ast
from pathlib import Path

_INSTALL_DIR = Path(__file__).resolve().parent.parent

_CLASS_LESS_NAME = "coordinator_engine_root"
_CLASS_AWARE_NAME = "coordinator_engine_root_with_class"


def _install_py_files() -> list[Path]:
    return sorted(
        p
        for p in _INSTALL_DIR.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def _imports_class_less_form(path: Path) -> bool:
    """True iff `path` imports the class-less `coordinator_engine_root`
    name from `coordinator_core.engine_root` (as itself, not merely as a
    substring of the class-aware sibling's longer name)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "coordinator_core.engine_root":
            for alias in node.names:
                if alias.name == _CLASS_LESS_NAME:
                    return True
    return False


def test_no_install_module_imports_class_less_root_resolver():
    offenders = [
        str(p.relative_to(_INSTALL_DIR.parent.parent))
        for p in _install_py_files()
        if _imports_class_less_form(p)
    ]
    assert offenders == [], (
        "the following coordinator_core/install/ modules import the "
        f"class-less coordinator_engine_root(), gate-blind by construction: {offenders}"
    )


def test_class_aware_resolver_name_exists_and_differs():
    # Sanity check on the two literal names this test discriminates between
    # -- guards against a typo in this test itself silently no-op'ing.
    assert _CLASS_LESS_NAME != _CLASS_AWARE_NAME
    assert _CLASS_AWARE_NAME.startswith(_CLASS_LESS_NAME)
