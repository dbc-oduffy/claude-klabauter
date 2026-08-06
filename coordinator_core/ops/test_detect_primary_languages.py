"""
coordinator_core.ops.test_detect_primary_languages

Characterization tests for the "detect.primary_languages" RPC wrapper in
coordinator_core.ops.detect_primary_languages — rank a target_root's file
extensions by count to pick primary language(s) for repomap generation.

Coverage:
  (a) registered under exactly "detect.primary_languages" on import
  (b) missing target_root raises a descriptive ValueError
  (c) invalid top_n (zero, negative, non-int, bool) raises a descriptive
      ValueError
  (d) happy path: counts ranked descending by count then ascending by
      extension name; primary capped at top_n
  (e) vendor/build/VCS directories (_SKIP_DIR_NAMES) are excluded from the
      tally
  (f) extensionless files contribute nothing
  (g) empty target_root yields empty counts/primary
  (h) idempotency (AC7): repeated invocation with identical inputs returns
      an identical result (read-only; no hazard per op-classification.tsv)

Spec backlink: docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-
inventory.md § Wave 2 "detect" cluster (detect-primary-languages-by-extension).
"""

from __future__ import annotations

import asyncio

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.detect_primary_languages  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.detect_primary_languages import (
    _detect_primary_languages,
    detect_primary_languages,
)

_OP_NAME = "detect.primary_languages"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.detect_primary_languages @register_op did not fire"
)


def _write(root, rel_path, content=""):
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_op_missing_target_root_raises_value_error():
    with pytest.raises(ValueError, match="target_root"):
        asyncio.run(_detect_primary_languages({}))


def test_op_invalid_top_n_raises_value_error(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for bad in (0, -1, "3", 3.5, True):
        with pytest.raises(ValueError, match="top_n"):
            asyncio.run(
                _detect_primary_languages({"target_root": str(root), "top_n": bad})
            )


def test_happy_path_ranks_by_count_then_name(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py")
    _write(root, "b.py")
    _write(root, "c.py")
    _write(root, "d.ts")
    _write(root, "e.ts")
    _write(root, "f.md")

    result = asyncio.run(
        _detect_primary_languages({"target_root": str(root), "top_n": 2})
    )

    assert result["counts"] == [
        {"extension": ".py", "count": 3},
        {"extension": ".ts", "count": 2},
        {"extension": ".md", "count": 1},
    ]
    assert result["primary"] == [".py", ".ts"]


def test_ties_broken_by_extension_name_ascending(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.ts")
    _write(root, "b.js")
    _write(root, "c.cpp")

    result = detect_primary_languages(root)

    assert result["counts"] == [
        {"extension": ".cpp", "count": 1},
        {"extension": ".js", "count": 1},
        {"extension": ".ts", "count": 1},
    ]


def test_skip_dirs_excluded_from_tally(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "src/a.py")
    _write(root, "node_modules/pkg/index.js")
    _write(root, ".venv/lib/site.py")
    _write(root, ".git/HEAD")

    result = detect_primary_languages(root)

    assert result["counts"] == [{"extension": ".py", "count": 1}]


def test_extensionless_files_contribute_nothing(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "Makefile")
    _write(root, "README")

    result = detect_primary_languages(root)

    assert result["counts"] == []
    assert result["primary"] == []


def test_empty_target_root_yields_empty_result(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    result = detect_primary_languages(root)

    assert result == {"counts": [], "primary": []}


def test_default_top_n_is_three(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py")
    _write(root, "b.ts")
    _write(root, "c.js")
    _write(root, "d.cpp")

    result = detect_primary_languages(root)

    assert result["primary"] == [".cpp", ".js", ".py"]


def test_idempotent_repeated_invocation_returns_identical_result(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py")
    _write(root, "b.ts")

    params = {"target_root": str(root), "top_n": 2}
    first = asyncio.run(_detect_primary_languages(dict(params)))
    second = asyncio.run(_detect_primary_languages(dict(params)))

    assert first == second
