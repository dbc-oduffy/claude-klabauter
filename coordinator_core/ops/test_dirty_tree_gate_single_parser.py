"""Pins `parse_porcelain_paths` as the single porcelain-parsing loop.

`coordinator_core/ops/dirty_tree_gate.py`'s `parse_porcelain_paths` docstring
declares the invariant: the porcelain-parsing loop exists exactly ONCE, here;
a second copy anywhere else is a bug, not a shortcut. This module makes that
claim checkable rather than merely stated, and asserts both named external
importers (`ops/session/safe_commit_offer.py`,
`baton_assemble/__init__.py`) still resolve the symbol from this one module.

Negative spec: this is a pin on the fact (single definition + both importers
resolve), not a shape-fingerprinting detector over parser bodies generally —
a bespoke source scanner for parser *shape* was rejected as overbuilt for a
population of one known site (see plan
docs/plans/2026-09-11-the-dirty-tree-classifier-half-converges.md, row C3).
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEF_PATTERN = re.compile(r"^\s*def parse_porcelain_paths\b")


def _is_test_path(path: Path) -> bool:
    if "tests" in path.parts:
        return True
    return path.name.startswith("test_")


def _non_test_python_files():
    for path in (REPO_ROOT / "coordinator_core").rglob("*.py"):
        if _is_test_path(path.relative_to(REPO_ROOT / "coordinator_core")):
            continue
        yield path


def test_parse_porcelain_paths_defined_exactly_once_in_non_test_code():
    defining_files = []
    for path in _non_test_python_files():
        text = path.read_text(encoding="utf-8")
        if any(DEF_PATTERN.match(line) for line in text.splitlines()):
            defining_files.append(path)

    assert defining_files == [
        REPO_ROOT / "coordinator_core" / "ops" / "dirty_tree_gate.py"
    ], (
        "parse_porcelain_paths must be defined exactly once, in "
        "coordinator_core/ops/dirty_tree_gate.py; found in: "
        f"{[str(p) for p in defining_files]}"
    )


def test_external_importers_resolve_parse_porcelain_paths_from_dirty_tree_gate():
    importer_paths = [
        REPO_ROOT / "coordinator_core" / "ops" / "session" / "safe_commit_offer.py",
        REPO_ROOT / "coordinator_core" / "baton_assemble" / "__init__.py",
    ]
    expected_import = (
        "from coordinator_core.ops.dirty_tree_gate import parse_porcelain_paths"
    )
    for path in importer_paths:
        text = path.read_text(encoding="utf-8")
        assert expected_import in text, (
            f"{path} must import parse_porcelain_paths from "
            "coordinator_core.ops.dirty_tree_gate unchanged"
        )
