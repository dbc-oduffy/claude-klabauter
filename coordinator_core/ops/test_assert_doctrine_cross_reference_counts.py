
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.assert_doctrine_cross_reference_counts  # noqa: F401

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.assert_doctrine_cross_reference_counts import _handler
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(root), check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(root), check=True, capture_output=True, **no_console_creationflags())
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=str(root), capture_output=True, check=True,
        **no_console_creationflags(),
    )
    return Path(result.stdout.decode().strip()).resolve()


def _write_skill(root: Path, rel: str, content: str) -> Path:
    path = root / "skills" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_registered():
    assert "doctrine.assert_cross_reference_counts" in _REGISTRY, (
        f"doctrine.assert_cross_reference_counts must be registered; registered ops: {sorted(_REGISTRY.keys())}"
    )


def test_all_thresholds_met(tmp_path):
    common_dir = _init_repo(tmp_path)
    worktree = common_dir.parent
    _write_skill(worktree, "plan/SKILL.md", "Eighth dimension\ntrampoline: true\nplan⇄spike\n")

    result = _handler(
        {"expected": {"skills/plan/SKILL.md::Eighth dimension": 1}},
        repo_root=common_dir,
    )

    assert result == {"ok": True, "mismatches": []}


def test_below_threshold_reported(tmp_path):
    common_dir = _init_repo(tmp_path)
    worktree = common_dir.parent
    _write_skill(worktree, "plan/SKILL.md", "no matching token here\n")

    result = _handler(
        {"expected": {"skills/plan/SKILL.md::Eighth dimension": 1}},
        repo_root=common_dir,
    )

    assert result["ok"] is False
    assert result["mismatches"] == [
        {"file": "skills/plan/SKILL.md::Eighth dimension", "expected": 1, "actual": 0}
    ]


def test_line_semantics_not_substring_count(tmp_path):
    common_dir = _init_repo(tmp_path)
    worktree = common_dir.parent
    _write_skill(worktree, "plan/SKILL.md", "tok tok\ntok\nno match\n")

    result = _handler(
        {"expected": {"skills/plan/SKILL.md::tok": 3}},
        repo_root=common_dir,
    )

    assert result["ok"] is False
    assert result["mismatches"] == [
        {"file": "skills/plan/SKILL.md::tok", "expected": 3, "actual": 2}
    ]


def test_idempotent_double_invocation(tmp_path):
    common_dir = _init_repo(tmp_path)
    worktree = common_dir.parent
    _write_skill(worktree, "plan/SKILL.md", "Eighth dimension\n")

    params = {"expected": {"skills/plan/SKILL.md::Eighth dimension": 1}}

    first = _handler(dict(params), repo_root=common_dir)
    second = _handler(dict(params), repo_root=common_dir)

    assert first == second == {"ok": True, "mismatches": []}
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(worktree), capture_output=True, check=True,
        **no_console_creationflags(),
    )
    assert status.stdout.decode().strip() == "?? skills/"


def test_escaping_path_rejected(tmp_path):
    common_dir = _init_repo(tmp_path)
    worktree = common_dir.parent
    outside = worktree / "elsewhere.md"
    outside.write_text("Eighth dimension\n", encoding="utf-8")

    result = _handler(
        {"expected": {"../elsewhere.md::Eighth dimension": 1}},
        repo_root=common_dir,
    )

    assert result["ok"] is False
    assert "escapes" in result["error"]
    assert result["mismatches"] == []


def test_repo_root_none_is_setup_error():
    result = _handler({"expected": {"skills/plan/SKILL.md::x": 1}}, repo_root=None)

    assert result["ok"] is False
    assert "repo_root is None" in result["error"]
    assert result["mismatches"] == []


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

    result = _handler({"expected": expected}, repo_root=common_dir)

    assert result["ok"] is False
    assert result["error"]
    assert result["mismatches"] == []
