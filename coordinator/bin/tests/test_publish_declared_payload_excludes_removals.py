"""test_publish_declared_payload_excludes_removals -- pins the disjointness
of `RoundManifest.declared_payload` and `RoundManifest.removed` at the site
that builds the round's per-root union (`publish.py :: _assemble_root_union`).

`declared_payload` is the cold union's file set and `removed` is the union's
deletions, so a path in both would be written and deleted by the same round.
The union keeps them disjoint by subtracting every entry it writes from its
deletions.

Negative-spec: this file runs no publish round and does not test
`RoundManifest` (de)serialization (`coordinator_core/percolate/tests/test_manifest.py`).

Run: python -m pytest coordinator/bin/tests/test_publish_declared_payload_excludes_removals.py -q
"""
from __future__ import annotations

import ast
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _union_assembly_source() -> str:
    """Read the function out of the module's AST rather than asserting on a
    substring of the whole file (which would pass on a comment mentioning the name)."""
    tree = ast.parse((_BIN_DIR / "publish.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_assemble_root_union":
            return ast.unparse(node)
    raise AssertionError("publish.py no longer defines `_assemble_root_union`")


def test_union_deletions_exclude_every_path_the_union_writes():
    source = _union_assembly_source()
    assert "deletions -= set(entries)" in source, (
        "the union must drop from its deletions every path it writes; got: " + source
    )
