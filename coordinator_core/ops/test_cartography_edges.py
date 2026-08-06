"""
coordinator_core.ops.test_cartography_edges

Characterization tests for the ops-level RPC wrappers in
coordinator_core.ops.cartography_edges: "cartography.edges" (existing) and
"cartography.count_references" (C1f — wires build_edges' already-built
import/call graph up to a reference-count contract, retiring the fence's
`grep -rl <module> --include=*.py | wc -l` shape rather than porting it).

Coverage (cartography.count_references):
  (a) registered under exactly "cartography.count_references" on import
  (b) missing target_root / module_name / files each raise a descriptive
      ValueError naming the missing param
  (c) happy path: a single importing file is counted and named
  (d) a file that does NOT import module_name contributes nothing
  (e) multiple import statements of the same module in one file sum into
      reference_count (not just referencing_files' presence)
  (f) referencing_files is de-duplicated and sorted across multiple files
  (g) idempotency (AC7): repeated invocation with identical inputs returns
      an identical result (read-only; no hazard per op-classification.tsv)

Spec backlink: docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-
inventory.md § chunk C1f (count-module-cross-references).
"""

from __future__ import annotations

import asyncio

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.cartography_edges  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.cartography_edges import _cartography_count_references

_OP_NAME = "cartography.count_references"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.cartography_edges @register_op did not fire"
)


def _write(root, rel_path, content):
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_op_missing_target_root_raises_value_error():
    with pytest.raises(ValueError, match="target_root"):
        asyncio.run(
            _cartography_count_references(
                {"module_name": "os", "files": ["mod.py"]}
            )
        )


def test_op_missing_module_name_raises_value_error(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(ValueError, match="module_name"):
        asyncio.run(
            _cartography_count_references(
                {"target_root": str(root), "files": ["mod.py"]}
            )
        )


def test_op_missing_files_raises_value_error(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(ValueError, match="files"):
        asyncio.run(
            _cartography_count_references(
                {"target_root": str(root), "module_name": "os"}
            )
        )


def test_op_happy_path_single_reference(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py", "import target_mod\n")

    result = asyncio.run(
        _cartography_count_references(
            {
                "target_root": str(root),
                "module_name": "target_mod",
                "files": ["a.py"],
            }
        )
    )

    assert result == {
        "reference_count": 1,
        "referencing_files": ["a.py"],
    }


def test_op_non_referencing_file_contributes_nothing(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py", "import os\n")

    result = asyncio.run(
        _cartography_count_references(
            {
                "target_root": str(root),
                "module_name": "target_mod",
                "files": ["a.py"],
            }
        )
    )

    assert result == {"reference_count": 0, "referencing_files": []}


def test_op_multiple_files_dedup_and_sort_referencing_files(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "z.py", "import target_mod\n")
    _write(root, "a.py", "import target_mod\n")
    _write(root, "b.py", "import os\n")

    result = asyncio.run(
        _cartography_count_references(
            {
                "target_root": str(root),
                "module_name": "target_mod",
                "files": ["z.py", "a.py", "b.py"],
            }
        )
    )

    assert result["reference_count"] == 2
    assert result["referencing_files"] == ["a.py", "z.py"]


def test_op_repeated_import_in_one_file_sums_reference_count(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(
        root,
        "a.py",
        "import target_mod\n"
        "if True:\n"
        "    import target_mod\n",
    )

    result = asyncio.run(
        _cartography_count_references(
            {
                "target_root": str(root),
                "module_name": "target_mod",
                "files": ["a.py"],
            }
        )
    )

    assert result["reference_count"] == 2
    assert result["referencing_files"] == ["a.py"]


def test_op_from_import_edge_counts_as_reference(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py", "from target_mod import thing\n")

    result = asyncio.run(
        _cartography_count_references(
            {
                "target_root": str(root),
                "module_name": "target_mod",
                "files": ["a.py"],
            }
        )
    )

    assert result == {"reference_count": 1, "referencing_files": ["a.py"]}


def test_op_call_edge_is_not_counted_as_import_reference(tmp_path):
    """A same-named intra-module call ("kind": "call") must not be conflated
    with an import edge ("kind": "import") — count_references counts import
    references only."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(
        root,
        "a.py",
        "def target_mod():\n    return 1\n\n"
        "def caller():\n    return target_mod()\n",
    )

    result = asyncio.run(
        _cartography_count_references(
            {
                "target_root": str(root),
                "module_name": "target_mod",
                "files": ["a.py"],
            }
        )
    )

    assert result == {"reference_count": 0, "referencing_files": []}


def test_op_double_invocation_is_a_safe_no_op(tmp_path):
    """AC7 idempotency: identical inputs, invoked twice, return an identical
    result — read-only reference count over the already-built edge graph,
    no state mutated between calls (op-classification.tsv idempotency-hazard:
    "none")."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py", "import target_mod\n")

    params = {
        "target_root": str(root),
        "module_name": "target_mod",
        "files": ["a.py"],
    }

    result_1 = asyncio.run(_cartography_count_references(dict(params)))
    result_2 = asyncio.run(_cartography_count_references(dict(params)))

    assert result_1 == result_2 == {
        "reference_count": 1,
        "referencing_files": ["a.py"],
    }
