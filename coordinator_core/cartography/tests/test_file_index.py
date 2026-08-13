"""
coordinator_core.cartography.tests.test_file_index

Unit tests for coordinator_core.cartography.file_index (pure functions) and the
import-guard + registry assertions for the thin cartography.file_index op
wrapper.

Coverage:
  (a) system_for_path — top-level directory component, nested paths, root-level
      file -> REPO_ROOT_SYSTEM, dotdir top-level component
  (b) build_file_index — {path: system} + per-system counts over a tmp_path git
      fixture; unmapped_count is always 0
  (c) PROVEN-RUN REGRESSION — cartography.file_index over THIS repo maps every
      tracked file with 0 unmapped (the 2026-07-12-11h57 dogfood run proved
      149 files / 0 unmapped on an earlier tree shape; this asserts the
      zero-unmapped invariant holds on the current tree, not the stale count)
  (d) import-guard + registry — "cartography.file_index" registered after import
  (e) op handler — missing target_root raises ValueError; happy path delegates
      to build_file_index via the path_guard-contained root

Spec backlink: pln-claude-klabauter-cartography-substrate-a-26eb2e § C2
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.cartography_file_index  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.cartography_file_index import _cartography_file_index
from coordinator_core.cartography.file_index import (
    REPO_ROOT_SYSTEM,
    build_file_index,
    system_for_path,
)

_OP_NAME = "cartography.file_index"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.cartography_file_index @register_op did not fire"
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            check=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "cartography-test@claude-klabauter.test")
    _git("config", "user.name", "Cartography Test")
    _git("config", "commit.gpgsign", "false")

    (root / "README.md").write_text("root file\n", encoding="utf-8")
    (root / "state").mkdir()
    (root / "state" / "x.yaml").write_text("k: v\n", encoding="utf-8")
    (root / "coordinator_core").mkdir()
    (root / "coordinator_core" / "y.py").write_text("pass\n", encoding="utf-8")
    (root / "coordinator_core" / "ops").mkdir()
    (root / "coordinator_core" / "ops" / "z.py").write_text("pass\n", encoding="utf-8")
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "marketplace.json").write_text("{}\n", encoding="utf-8")

    _git("add", "-A")
    _git("commit", "-m", "seed")

    return root


# ---------------------------------------------------------------------------
# system_for_path
# ---------------------------------------------------------------------------


def test_system_for_path_top_level_directory():
    assert system_for_path("coordinator_core/ops/foo.py") == "coordinator_core"
    assert system_for_path("state/handoffs/foo.md") == "state"


def test_system_for_path_root_level_file():
    assert system_for_path("README.md") == REPO_ROOT_SYSTEM
    assert system_for_path("CLAUDE.md") == REPO_ROOT_SYSTEM


def test_system_for_path_dotdir_top_level():
    assert system_for_path(".claude-plugin/marketplace.json") == ".claude-plugin"


def test_system_for_path_deeply_nested():
    assert system_for_path("a/b/c/d/e.py") == "a"


# ---------------------------------------------------------------------------
# build_file_index
# ---------------------------------------------------------------------------


def test_build_file_index_shape(git_repo):
    result = build_file_index(git_repo)
    assert result["file_count"] == 5
    assert result["unmapped_count"] == 0
    assert result["index"]["README.md"] == REPO_ROOT_SYSTEM
    assert result["index"]["state/x.yaml"] == "state"
    assert result["index"]["coordinator_core/y.py"] == "coordinator_core"
    assert result["index"]["coordinator_core/ops/z.py"] == "coordinator_core"
    assert result["index"][".claude-plugin/marketplace.json"] == ".claude-plugin"


def test_build_file_index_systems_rollup(git_repo):
    result = build_file_index(git_repo)
    assert result["systems"]["coordinator_core"] == 2
    assert result["systems"]["state"] == 1
    assert result["systems"][REPO_ROOT_SYSTEM] == 1
    assert result["systems"][".claude-plugin"] == 1
    assert sum(result["systems"].values()) == result["file_count"]


def test_build_file_index_scope_narrows_result(git_repo):
    result = build_file_index(git_repo, scope=["coordinator_core"])
    assert result["file_count"] == 2
    assert set(result["index"]) == {"coordinator_core/y.py", "coordinator_core/ops/z.py"}
    assert result["systems"] == {"coordinator_core": 2}


def test_build_file_index_absent_scope_unchanged(git_repo):
    unscoped = build_file_index(git_repo)
    explicit_none = build_file_index(git_repo, scope=None)
    assert unscoped == explicit_none


def test_build_file_index_zero_unmapped_over_this_repo():
    result = build_file_index(_REPO_ROOT)
    assert result["file_count"] > 0
    assert result["unmapped_count"] == 0
    assert len(result["index"]) == result["file_count"]


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


def test_op_missing_target_root_raises_value_error():
    with pytest.raises(ValueError):
        _cartography_file_index({})


def test_op_happy_path(git_repo):
    result = _cartography_file_index({"target_root": str(git_repo)})
    assert result["file_count"] == 5
    assert result["unmapped_count"] == 0


def test_op_scope_narrows_result(git_repo):
    result = _cartography_file_index(
        {"target_root": str(git_repo), "scope": ["coordinator_core"]}
    )

    assert result["file_count"] == 2
    assert result["systems"] == {"coordinator_core": 2}
