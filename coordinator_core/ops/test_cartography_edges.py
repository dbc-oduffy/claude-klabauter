
from __future__ import annotations

import pytest

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
        _cartography_count_references(
            {"module_name": "os", "files": ["mod.py"]}
        )


def test_op_missing_module_name_raises_value_error(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(ValueError, match="module_name"):
        _cartography_count_references(
            {"target_root": str(root), "files": ["mod.py"]}
        )


def test_op_missing_files_raises_value_error(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(ValueError, match="files"):
        _cartography_count_references(
            {"target_root": str(root), "module_name": "os"}
        )


def test_op_happy_path_single_reference(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py", "import target_mod\n")

    result = _cartography_count_references(
        {
            "target_root": str(root),
            "module_name": "target_mod",
            "files": ["a.py"],
        }
    )

    assert result == {
        "reference_count": 1,
        "referencing_files": ["a.py"],
    }


def test_op_non_referencing_file_contributes_nothing(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py", "import os\n")

    result = _cartography_count_references(
        {
            "target_root": str(root),
            "module_name": "target_mod",
            "files": ["a.py"],
        }
    )

    assert result == {"reference_count": 0, "referencing_files": []}


def test_op_multiple_files_dedup_and_sort_referencing_files(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "z.py", "import target_mod\n")
    _write(root, "a.py", "import target_mod\n")
    _write(root, "b.py", "import os\n")

    result = _cartography_count_references(
        {
            "target_root": str(root),
            "module_name": "target_mod",
            "files": ["z.py", "a.py", "b.py"],
        }
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

    result = _cartography_count_references(
        {
            "target_root": str(root),
            "module_name": "target_mod",
            "files": ["a.py"],
        }
    )

    assert result["reference_count"] == 2
    assert result["referencing_files"] == ["a.py"]


def test_op_from_import_edge_counts_as_reference(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py", "from target_mod import thing\n")

    result = _cartography_count_references(
        {
            "target_root": str(root),
            "module_name": "target_mod",
            "files": ["a.py"],
        }
    )

    assert result == {"reference_count": 1, "referencing_files": ["a.py"]}


def test_op_call_edge_is_not_counted_as_import_reference(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(
        root,
        "a.py",
        "def target_mod():\n    return 1\n\n"
        "def caller():\n    return target_mod()\n",
    )

    result = _cartography_count_references(
        {
            "target_root": str(root),
            "module_name": "target_mod",
            "files": ["a.py"],
        }
    )

    assert result == {"reference_count": 0, "referencing_files": []}


def test_op_double_invocation_is_a_safe_no_op(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py", "import target_mod\n")

    params = {
        "target_root": str(root),
        "module_name": "target_mod",
        "files": ["a.py"],
    }

    result_1 = _cartography_count_references(dict(params))
    result_2 = _cartography_count_references(dict(params))

    assert result_1 == result_2 == {
        "reference_count": 1,
        "referencing_files": ["a.py"],
    }
