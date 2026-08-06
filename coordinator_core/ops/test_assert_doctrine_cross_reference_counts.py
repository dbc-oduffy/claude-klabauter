"""
Tests for coordinator_core.ops.assert_doctrine_cross_reference_counts —
"doctrine.assert_cross_reference_counts" handler.

Coverage:
  (a) all counts meet threshold -> {ok: True, mismatches: []}.
  (b) a count below threshold -> {ok: False, mismatches: [{file, expected, actual}]}.
  (c) grep -c LINE semantics: a line with the pattern twice counts once, not twice.
  (d) idempotent double-invocation (AC7): identical params, same result both times,
      no filesystem mutation (read-only op).
  (e) a path escaping skills/ and docs/wiki/ is rejected as a usage error.
  (f) repo_root is None -> setup error, never a worktree-derivation guess.
  (g) malformed 'expected' key/value shapes are rejected as usage errors.

Import guard (lesson: state/lessons/2026-07-04-universal-registry-completeness-tests-ov.yaml):
  Importing coordinator_core.ops.assert_doctrine_cross_reference_counts fires
  @register_op("doctrine.assert_cross_reference_counts") before any test that
  relies on registry state.

Harness: asyncio.run() in sync test fns — no pytest-asyncio dependency. Handler
called directly with repo_root=<tmp git common dir>. A throwaway git repo under
tmp_path stands in for the caller's own worktree — never the working repo.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

# ---- Import guard: fires @register_op side-effect for doctrine.assert_cross_reference_counts. ----
import coordinator_core.ops.assert_doctrine_cross_reference_counts  # noqa: F401

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.assert_doctrine_cross_reference_counts import _handler


def _run(coro):
    """Run async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _init_repo(tmp_path: Path) -> Path:
    """Init a throwaway git repo under tmp_path and return its common dir."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(root), check=True, capture_output=True)
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=str(root), capture_output=True, check=True,
    )
    return Path(result.stdout.decode().strip()).resolve()


def _write_skill(root: Path, rel: str, content: str) -> Path:
    path = root / "skills" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Import-guard floor assertion
# ---------------------------------------------------------------------------


def test_registered():
    assert "doctrine.assert_cross_reference_counts" in _REGISTRY, (
        f"doctrine.assert_cross_reference_counts must be registered; registered ops: {sorted(_REGISTRY.keys())}"
    )


# ---------------------------------------------------------------------------
# (a) all thresholds met
# ---------------------------------------------------------------------------


def test_all_thresholds_met(tmp_path):
    common_dir = _init_repo(tmp_path)
    worktree = common_dir.parent
    _write_skill(worktree, "plan/SKILL.md", "Eighth dimension\ntrampoline: true\nplan⇄spike\n")

    result = _run(_handler(
        {"expected": {"skills/plan/SKILL.md::Eighth dimension": 1}},
        repo_root=common_dir,
    ))

    assert result == {"ok": True, "mismatches": []}


# ---------------------------------------------------------------------------
# (b) a count below threshold is reported
# ---------------------------------------------------------------------------


def test_below_threshold_reported(tmp_path):
    common_dir = _init_repo(tmp_path)
    worktree = common_dir.parent
    _write_skill(worktree, "plan/SKILL.md", "no matching token here\n")

    result = _run(_handler(
        {"expected": {"skills/plan/SKILL.md::Eighth dimension": 1}},
        repo_root=common_dir,
    ))

    assert result["ok"] is False
    assert result["mismatches"] == [
        {"file": "skills/plan/SKILL.md::Eighth dimension", "expected": 1, "actual": 0}
    ]


# ---------------------------------------------------------------------------
# (c) grep -c LINE semantics — a line with the pattern twice counts once
# ---------------------------------------------------------------------------


def test_line_semantics_not_substring_count(tmp_path):
    common_dir = _init_repo(tmp_path)
    worktree = common_dir.parent
    # "tok" appears twice on line 1, once on line 2 -> grep -c == 2 lines, not 3 occurrences.
    _write_skill(worktree, "plan/SKILL.md", "tok tok\ntok\nno match\n")

    result = _run(_handler(
        {"expected": {"skills/plan/SKILL.md::tok": 3}},
        repo_root=common_dir,
    ))

    assert result["ok"] is False
    assert result["mismatches"] == [
        {"file": "skills/plan/SKILL.md::tok", "expected": 3, "actual": 2}
    ]


# ---------------------------------------------------------------------------
# (d) idempotent double-invocation (AC7)
# ---------------------------------------------------------------------------


def test_idempotent_double_invocation(tmp_path):
    common_dir = _init_repo(tmp_path)
    worktree = common_dir.parent
    _write_skill(worktree, "plan/SKILL.md", "Eighth dimension\n")

    params = {"expected": {"skills/plan/SKILL.md::Eighth dimension": 1}}

    first = _run(_handler(dict(params), repo_root=common_dir))
    second = _run(_handler(dict(params), repo_root=common_dir))

    assert first == second == {"ok": True, "mismatches": []}
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(worktree), capture_output=True, check=True,
    )
    # Untracked seed file only — the op itself must never stage/commit/mutate.
    assert status.stdout.decode().strip() == "?? skills/"


# ---------------------------------------------------------------------------
# (e) path escaping skills/ and docs/wiki/ is rejected
# ---------------------------------------------------------------------------


def test_escaping_path_rejected(tmp_path):
    common_dir = _init_repo(tmp_path)
    worktree = common_dir.parent
    outside = worktree / "elsewhere.md"
    outside.write_text("Eighth dimension\n", encoding="utf-8")

    result = _run(_handler(
        {"expected": {"../elsewhere.md::Eighth dimension": 1}},
        repo_root=common_dir,
    ))

    assert result["ok"] is False
    assert "escapes" in result["error"]
    assert result["mismatches"] == []


# ---------------------------------------------------------------------------
# (f) repo_root is None -> setup error, never a worktree-derivation guess
# ---------------------------------------------------------------------------


def test_repo_root_none_is_setup_error():
    result = _run(_handler({"expected": {"skills/plan/SKILL.md::x": 1}}, repo_root=None))

    assert result["ok"] is False
    assert "repo_root is None" in result["error"]
    assert result["mismatches"] == []


# ---------------------------------------------------------------------------
# (g) malformed 'expected' shapes are rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expected",
    [
        None,
        {},
        {"no-delimiter-here": 1},
        {"skills/plan/SKILL.md::x": "not-an-int"},
    ],
)
def test_malformed_expected_rejected(tmp_path, expected):
    common_dir = _init_repo(tmp_path)

    result = _run(_handler({"expected": expected}, repo_root=common_dir))

    assert result["ok"] is False
    assert result["error"]
    assert result["mismatches"] == []
