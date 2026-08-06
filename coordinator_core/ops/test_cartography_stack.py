"""
coordinator_core.ops.test_cartography_stack

Characterization tests for coordinator_core.ops.cartography_stack:
"cartography.stack" — the language / test-framework / config-file
fingerprint op (ports the fence-inventory `detect-project-stack` payload
by intent, not transliteration; see module docstring).

Coverage:
  (a) registered under exactly "cartography.stack" on import
  (b) missing target_root raises a descriptive ValueError
  (c) happy path: languages / test_frameworks / configs each detected
      independently and in fence order
  (d) empty tree: no markers -> all three lists empty, no error
  (e) vendor/build dirs (_SKIP_DIR_NAMES) do not contribute language hits
  (f) jest.config.* glob match inserted in fence order among configs
  (g) idempotency (AC7): repeated invocation with identical inputs returns
      a byte-identical result (read-only; hazard "none" per
      op-classification.tsv)
  (h) a target_root that does not exist on disk degrades to empty results
      rather than raising (matches _walk_extensions' OSError-tolerant walk)

Spec backlink: docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-
inventory.md § Wave 2 "detect" cluster (detect-project-stack).
"""

from __future__ import annotations

import asyncio

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.cartography_stack  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.cartography_stack import (
    _cartography_stack,
    detect_project_stack,
)

_OP_NAME = "cartography.stack"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.cartography_stack @register_op did not fire"
)


def test_op_registered_under_exact_key():
    assert _REGISTRY[_OP_NAME] is _cartography_stack


def test_op_missing_target_root_raises_value_error():
    with pytest.raises(ValueError, match="target_root"):
        asyncio.run(_cartography_stack({}))


def test_op_empty_tree_returns_empty_lists(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    result = asyncio.run(_cartography_stack({"target_root": str(root)}))
    assert result["languages"] == []
    assert result["test_frameworks"] == []
    assert result["configs"] == []
    assert result["target_root"] == str(root.resolve())


def test_detect_languages_present_only_and_fence_order(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.cpp").write_text("", encoding="utf-8")
    (root / "app.tsx").write_text("", encoding="utf-8")
    (root / "script.py").write_text("", encoding="utf-8")
    result = detect_project_stack(root)
    # Python, TypeScript, C++ present; JavaScript absent — fence order preserved.
    assert result["languages"] == ["Python", "TypeScript", "C++"]


def test_typescript_dedupes_ts_and_tsx(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.ts").write_text("", encoding="utf-8")
    (root / "b.tsx").write_text("", encoding="utf-8")
    result = detect_project_stack(root)
    assert result["languages"] == ["TypeScript"]


def test_language_hit_found_in_nested_directory(tmp_path):
    root = tmp_path / "repo"
    nested = root / "src" / "pkg"
    nested.mkdir(parents=True)
    (nested / "mod.py").write_text("", encoding="utf-8")
    result = detect_project_stack(root)
    assert result["languages"] == ["Python"]


def test_skip_dirs_do_not_contribute_language_hits(tmp_path):
    root = tmp_path / "repo"
    vendored = root / "node_modules" / "pkg"
    vendored.mkdir(parents=True)
    (vendored / "index.js").write_text("", encoding="utf-8")
    result = detect_project_stack(root)
    assert result["languages"] == []


def test_detect_test_frameworks_present_only(tmp_path):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "spec").mkdir(parents=True)
    result = detect_project_stack(root)
    assert result["test_frameworks"] == ["tests", "spec"]


def test_detect_configs_present_only(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    (root / "CMakeLists.txt").write_text("", encoding="utf-8")
    result = detect_project_stack(root)
    assert result["configs"] == ["pyproject.toml", "CMakeLists.txt"]


def test_jest_config_glob_inserted_in_fence_order(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    (root / "jest.config.js").write_text("", encoding="utf-8")
    (root / "tsconfig.json").write_text("", encoding="utf-8")
    result = detect_project_stack(root)
    assert result["configs"] == ["pyproject.toml", "jest.config.*", "tsconfig.json"]


def test_jest_config_glob_inserted_before_cmakelists_when_tsconfig_absent(tmp_path):
    # Review: code-reviewer — Finding 2. tsconfig.json absent, CMakeLists.txt
    # present: jest.config.* must still land BEFORE CMakeLists.txt (fence
    # order), not appended after it.
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    (root / "jest.config.js").write_text("", encoding="utf-8")
    (root / "CMakeLists.txt").write_text("", encoding="utf-8")
    result = detect_project_stack(root)
    assert result["configs"] == ["pyproject.toml", "jest.config.*", "CMakeLists.txt"]


def test_top_level_config_not_matched_when_nested(tmp_path):
    root = tmp_path / "repo"
    nested = root / "sub"
    nested.mkdir(parents=True)
    (nested / "pytest.ini").write_text("", encoding="utf-8")
    result = detect_project_stack(root)
    assert result["configs"] == []


def test_idempotent_second_invocation_is_byte_identical(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "script.py").write_text("", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "pytest.ini").write_text("", encoding="utf-8")

    first = asyncio.run(_cartography_stack({"target_root": str(root)}))
    second = asyncio.run(_cartography_stack({"target_root": str(root)}))
    assert first == second


def test_nonexistent_target_root_degrades_to_empty_results(tmp_path):
    missing = tmp_path / "does-not-exist"
    result = asyncio.run(_cartography_stack({"target_root": str(missing)}))
    assert result["languages"] == []
    assert result["test_frameworks"] == []
    assert result["configs"] == []
