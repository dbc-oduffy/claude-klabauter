"""Tests for coordinator_core.ops.plan_suggest_completion_steps —
`plan.suggest_completion_steps`, the plan-completion assist surface for the
vanilla-plan-mode safety net (Part 3, state/handoffs/2026-08-13-vanilla-
plan-mode-capture-safety-net.md). Co-located per current house convention
(mirrors coordinator_core/ops/test_draft_plan_aging.py, the sibling this
module's own docstring cites as the shape to study).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ipc import get_op_handler
from coordinator_core.ops.plan_suggest_completion_steps import (
    _EXECUTION_AUTHORIZATION_ELEMENT,
    _REVIEW_TRAIL_ELEMENT,
    _plan_suggest_completion_steps,
    _plan_touching_shas_batch,
    suggest_completion_steps,
)

# Declared, not excused: every test in this file drives real `git log`/`git
# rev-list` queries (candidate-touching-commit resolution, sha_range
# resolution) — no mock stands in for real commit history. Mirrors
# test_draft_plan_aging.py's identical declaration for the same reason.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")


def _write_plan(repo: Path, name: str, status: str, extra_fm: str = "") -> Path:
    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / name
    path.write_text(
        "---\n"
        'title: "fixture"\n'
        f"status: {status}\n"
        f"{extra_fm}"
        "---\n\nbody\n",
        encoding="utf-8",
    )
    return path


def _commit_plan(repo: Path, path: Path, message: str) -> str:
    _run_git(repo, "add", str(path.relative_to(repo)))
    _run_git(repo, "commit", "-q", "-m", message)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _write_trail_record(
    repo: Path,
    name: str,
    sha_range: str,
    scope_kind: str = "plan",
    verdict: str = "ok",
) -> None:
    trail_dir = repo / "state" / "review-trail"
    trail_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "sha_range": sha_range,
        "reviewer": "code-reviewer",
        "scope": "plan review",
        "scope_kind": scope_kind,
        "verdict": verdict,
        "diff_loc": 10,
        "session_id": "sess1234",
        "workstream": None,
    }
    (trail_dir / name).write_text(json.dumps(record), encoding="utf-8")


_EXPECTED_MISSING = [dict(_EXECUTION_AUTHORIZATION_ELEMENT), dict(_REVIEW_TRAIL_ELEMENT)]


def test_no_docs_plans_dir_returns_empty(tmp_path):
    _init_repo(tmp_path)
    assert suggest_completion_steps(tmp_path) == []


def test_draft_status_is_not_a_candidate(tmp_path):
    _init_repo(tmp_path)
    path = _write_plan(tmp_path, "a.md", "draft")
    _commit_plan(tmp_path, path, "add a")
    assert suggest_completion_steps(tmp_path) == []


def test_approved_with_authorization_stamp_has_nothing_to_suggest(tmp_path):
    _init_repo(tmp_path)
    path = _write_plan(
        tmp_path, "a.md", "approved", extra_fm="execution_authorized_at: 2026-08-13T10:00:00Z\n"
    )
    _commit_plan(tmp_path, path, "add a")
    assert suggest_completion_steps(tmp_path) == []


def test_executing_with_review_trail_coverage_has_nothing_to_suggest(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    base_sha = _commit_plan(tmp_path, tmp_path / "README.md", "seed")
    path = _write_plan(tmp_path, "a.md", "executing")
    sha = _commit_plan(tmp_path, path, "add a")
    _write_trail_record(tmp_path, "2026-08-13-000000-sess1234.json", f"{base_sha}..{sha}")

    assert suggest_completion_steps(tmp_path) == []


def test_executing_missing_both_elements_reports_both_completion_steps(tmp_path):
    _init_repo(tmp_path)
    path = _write_plan(tmp_path, "a.md", "executing")
    _commit_plan(tmp_path, path, "add a")

    result = suggest_completion_steps(tmp_path)

    assert result == [
        {
            "path": "docs/plans/a.md",
            "status": "executing",
            "missing": _EXPECTED_MISSING,
        }
    ]


def test_pending_verdict_review_trail_record_does_not_count_as_a_completion_step(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    base_sha = _commit_plan(tmp_path, tmp_path / "README.md", "seed")
    path = _write_plan(tmp_path, "a.md", "approved")
    sha = _commit_plan(tmp_path, path, "add a")
    _write_trail_record(
        tmp_path, "2026-08-13-000000-sess1234.json", f"{base_sha}..{sha}", verdict="pending"
    )

    result = suggest_completion_steps(tmp_path)
    assert result and result[0]["path"] == "docs/plans/a.md"


def test_diff_scope_kind_review_trail_record_does_not_count_as_a_completion_step(tmp_path):
    """A scope_kind:diff record over the plan's own commit is NOT a plan
    review — scope_kind:plan is the only kind this module credits (mirrors
    coverage.py's kind-aware plan-crediting rule)."""
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    base_sha = _commit_plan(tmp_path, tmp_path / "README.md", "seed")
    path = _write_plan(tmp_path, "a.md", "approved")
    sha = _commit_plan(tmp_path, path, "add a")
    _write_trail_record(
        tmp_path, "2026-08-13-000000-sess1234.json", f"{base_sha}..{sha}", scope_kind="diff"
    )

    result = suggest_completion_steps(tmp_path)
    assert result and result[0]["path"] == "docs/plans/a.md"


def test_sidecar_file_excluded(tmp_path):
    _init_repo(tmp_path)
    path = _write_plan(tmp_path, "a.review.md", "executing")
    _commit_plan(tmp_path, path, "add sidecar")
    assert suggest_completion_steps(tmp_path) == []


def test_plan_touching_shas_batch_attributes_commits_to_the_right_path(tmp_path):
    """Multi-item coverage for the batched `git log -- pathA pathB ...`
    replacement of the former per-path `_plan_touching_shas` loop (W8/C8
    amplification disposition) — a single-item call would pass identically
    whether or not cross-path attribution worked; this pins that a commit
    touching plan A's file is never attributed to plan B's, and vice versa,
    including a commit that touches BOTH in one go."""
    _init_repo(tmp_path)
    path_a = _write_plan(tmp_path, "a.md", "executing")
    sha_a = _commit_plan(tmp_path, path_a, "add a")
    path_b = _write_plan(tmp_path, "b.md", "executing")
    sha_b = _commit_plan(tmp_path, path_b, "add b")

    # A third commit touches BOTH a.md and b.md at once.
    path_a.write_text(path_a.read_text(encoding="utf-8") + "more\n", encoding="utf-8")
    path_b.write_text(path_b.read_text(encoding="utf-8") + "more\n", encoding="utf-8")
    _run_git(tmp_path, "add", "docs/plans/a.md", "docs/plans/b.md")
    _run_git(tmp_path, "commit", "-q", "-m", "touch both")
    sha_both = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()

    result = _plan_touching_shas_batch(tmp_path, ["docs/plans/a.md", "docs/plans/b.md"])

    assert result["docs/plans/a.md"] == frozenset({sha_a, sha_both})
    assert result["docs/plans/b.md"] == frozenset({sha_b, sha_both})


def test_plan_touching_shas_batch_empty_paths_returns_empty_dict(tmp_path):
    _init_repo(tmp_path)
    assert _plan_touching_shas_batch(tmp_path, []) == {}


def test_op_registered_under_contractual_key():
    handler = get_op_handler("plan.suggest_completion_steps")
    assert handler is _plan_suggest_completion_steps


def test_handler_fails_loud_when_repo_root_is_none():
    with pytest.raises(ValueError, match="repo_root is None"):
        _plan_suggest_completion_steps({}, repo_root=None)


def test_handler_derives_worktree_root_from_common_dir(tmp_path):
    _init_repo(tmp_path)
    path = _write_plan(tmp_path, "a.md", "executing")
    _commit_plan(tmp_path, path, "add a")
    common_dir = tmp_path / ".git"

    result = _plan_suggest_completion_steps({}, repo_root=common_dir)

    assert result == {
        "plans": [
            {
                "path": "docs/plans/a.md",
                "status": "executing",
                "missing": _EXPECTED_MISSING,
            }
        ]
    }
