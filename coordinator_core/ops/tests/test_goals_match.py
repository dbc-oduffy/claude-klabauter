"""
coordinator_core.ops.tests.test_goals_match

Tests for the "goal.match_candidates" op.

Import guard: ``import coordinator_core.ops`` MUST precede all test functions so
that ALL op registrations fire (not just the single op under test).  This satisfies
the universal-registry-completeness-tests-ov lesson: asserting a non-empty registry
BEFORE per-op assertions prevents a silent false-positive over an empty registry.

Coverage:
  (a) registry-completeness — registry is non-empty after coordinator_core.ops import
  (b) op-registered — "goal.match_candidates" is in the registry
  (c) empty store — directory absent → returns empty candidates list
  (d) null repo_root — returns empty candidates without raising
  (e) well-formed goals → candidates carry {goal_id, title, score}; text closely
      matching one goal's objective/KR ranks it first (ordering asserted)
  (f) status != active goals are excluded
  (g) malformed YAML file → quarantined (skipped), well-formed siblings still returned
  (h) missing/empty text param → returns empty candidates
  (i) key_results list-of-mappings fixture: KR .text contributes to score
      (text matching only a KR term still surfaces the goal)
  (j) COMPUTE_ONLY classification assertion
  (k) absent status field → warned and skipped; active sibling still returned
  (l) missing id field → quarantined; active sibling still returned
  (m) missing title field → quarantined; active sibling still returned

Fixture approach: real on-disk YAML files in a tmp_path directory — matches the
production ``state/goals/<id>.yaml`` shape (lesson: test-fidelity-seed-fixtures-in-the-real).
key_results written as a proper YAML list-of-mappings block so the yaml.safe_load path
is exercised.

Spec backlink: DoE-claude:pln-per-repo-okr-goal-setting-syst-80bced § C3
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional

import pytest

import coordinator_core.ops  # noqa: F401 — populates _REGISTRY

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.goals_match import _handler

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


assert len(_REGISTRY) > 0, (
    "registry is empty after 'import coordinator_core.ops' — "
    "all @register_op decorators must have fired at module import time"
)

_OP_NAME = "goal.match_candidates"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.goals_match @register_op did not fire"
)


def _make_git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(root),
        capture_output=True,
        check=True,
        creationflags=_NO_WIN,
    )
    subprocess.run(
        ["git", "config", "user.email", "goals-test@claude-klabauter.test"],
        cwd=str(root),
        capture_output=True,
        check=True,
        creationflags=_NO_WIN,
    )
    subprocess.run(
        ["git", "config", "user.name", "Goals Test"],
        cwd=str(root),
        capture_output=True,
        check=True,
        creationflags=_NO_WIN,
    )
    return (root / ".git").resolve()


def _seed_goal(
    goals_dir: Path,
    filename: str,
    *,
    id_val: Optional[str] = None,
    title: Optional[str] = None,
    status: str = "active",
    objective: str = "",
    key_results: Optional[List[dict]] = None,
) -> Path:
    goals_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if id_val is not None:
        lines.append(f"id: {id_val}")
    if title is not None:
        lines.append(f"title: {title}")
    lines.append(f"status: {status}")
    if objective:
        lines.append(f"objective: {objective}")
    if key_results is not None:
        lines.append("key_results:")
        for kr in key_results:
            lines.append(f"  - id: {kr.get('id', 'kr-unknown')}")
            lines.append(f"    text: {kr.get('text', '')}")
            lines.append(f"    kind: {kr.get('kind', 'outcome')}")
            lines.append(f"    status: {kr.get('status', 'active')}")
    else:
        lines.append("key_results: []")
    lines.append("---")
    lines.append("")
    content = "\n".join(lines) + "\n"
    path = goals_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


class TestRegistryCompleteness:

    def test_registry_is_non_empty(self):
        assert len(_REGISTRY) > 0

    def test_op_name_registered(self):
        """'goal.match_candidates' is present in _REGISTRY."""
        assert "goal.match_candidates" in _REGISTRY


class TestGoalMatchCandidates:

    def test_empty_store_directory_absent(self, tmp_path):
        common_dir = _make_git_repo(tmp_path / "repo")
        result = _handler({"text": "legibility"}, repo_root=common_dir)
        assert result == {"candidates": []}

    def test_empty_store_directory_present_but_empty(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        (repo_root / "state" / "goals").mkdir(parents=True)
        result = _handler({"text": "legibility"}, repo_root=common_dir)
        assert result == {"candidates": []}

    def test_repo_root_none_returns_empty(self):
        result = _handler({"text": "legibility"}, repo_root=None)
        assert result == {"candidates": []}

    def test_missing_text_param_returns_empty(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_goal(
            repo_root / "state" / "goals",
            "g1.yaml",
            id_val="g1",
            title="Goal One",
            objective="improve legibility",
        )
        result = _handler({}, repo_root=common_dir)
        assert result == {"candidates": []}

    def test_empty_text_param_returns_empty(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_goal(
            repo_root / "state" / "goals",
            "g1.yaml",
            id_val="g1",
            title="Goal One",
            objective="improve legibility",
        )
        result = _handler({"text": ""}, repo_root=common_dir)
        assert result == {"candidates": []}

    def test_well_formed_goal_fields(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_goal(
            repo_root / "state" / "goals",
            "legibility.yaml",
            id_val="okr-legibility",
            title="OKR Legibility",
            objective="make OKR tracking legible across the team",
        )
        result = _handler({"text": "legibility"}, repo_root=common_dir)
        assert len(result["candidates"]) == 1
        entry = result["candidates"][0]
        assert entry["goal_id"] == "okr-legibility"
        assert entry["title"] == "OKR Legibility"
        assert isinstance(entry["score"], float)
        assert 0.0 <= entry["score"] <= 1.0

    def test_best_match_ranked_first(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        goals_dir = repo_root / "state" / "goals"
        _seed_goal(
            goals_dir,
            "unrelated.yaml",
            id_val="unrelated",
            title="Unrelated Goal",
            objective="completely different topic about infrastructure",
        )
        _seed_goal(
            goals_dir,
            "legibility.yaml",
            id_val="okr-legibility",
            title="OKR Legibility System",
            objective="make OKR tracking legible across the engineering team",
        )

        result = _handler({"text": "okr tracking legibility"}, repo_root=common_dir)

        assert len(result["candidates"]) == 2
        assert result["candidates"][0]["goal_id"] == "okr-legibility"
        assert result["candidates"][0]["score"] >= result["candidates"][1]["score"]
        assert result["candidates"][0]["score"] == 0.6
        assert result["candidates"][1]["score"] == 0.1318

    def test_non_active_goals_excluded(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        goals_dir = repo_root / "state" / "goals"
        _seed_goal(
            goals_dir,
            "achieved.yaml",
            id_val="g-achieved",
            title="Already Done",
            status="achieved",
            objective="this goal was achieved long ago",
        )
        _seed_goal(
            goals_dir,
            "abandoned.yaml",
            id_val="g-abandoned",
            title="Abandoned Goal",
            status="abandoned",
            objective="this goal was abandoned last quarter",
        )
        _seed_goal(
            goals_dir,
            "active.yaml",
            id_val="g-active",
            title="Active Goal",
            status="active",
            objective="this goal is actively in progress",
        )

        result = _handler({"text": "goal"}, repo_root=common_dir)

        ids = [c["goal_id"] for c in result["candidates"]]
        assert "g-active" in ids
        assert "g-achieved" not in ids
        assert "g-abandoned" not in ids

    def test_malformed_yaml_quarantined_sibling_still_returned(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        goals_dir = repo_root / "state" / "goals"
        bad_path = goals_dir / "bad.yaml"
        goals_dir.mkdir(parents=True, exist_ok=True)
        bad_path.write_text("---\nid: [unclosed bracket\ntitle: Bad\n---\n", encoding="utf-8")
        _seed_goal(
            goals_dir,
            "good.yaml",
            id_val="g-good",
            title="Good Goal",
            objective="a well-formed goal artifact",
        )

        result = _handler({"text": "goal"}, repo_root=common_dir)

        ids = [c["goal_id"] for c in result["candidates"]]
        assert "g-good" in ids
        assert len(ids) == 1

    def test_absent_status_warned_sibling_still_returned(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        goals_dir = repo_root / "state" / "goals"
        goals_dir.mkdir(parents=True, exist_ok=True)
        no_status_path = goals_dir / "no-status.yaml"
        no_status_path.write_text(
            "---\nid: g-nostatus\ntitle: No Status Goal\nobjective: missing status\nkey_results: []\n---\n",
            encoding="utf-8",
        )
        _seed_goal(
            goals_dir,
            "active.yaml",
            id_val="g-active",
            title="Active Sibling",
            objective="actively in progress",
        )

        result = _handler({"text": "goal"}, repo_root=common_dir)

        ids = [c["goal_id"] for c in result["candidates"]]
        assert "g-nostatus" not in ids
        assert "g-active" in ids
        assert len(ids) == 1

    def test_missing_id_field_quarantined(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        goals_dir = repo_root / "state" / "goals"
        _seed_goal(
            goals_dir,
            "no-id.yaml",
            id_val=None,
            title="Valid Title",
            objective="goal without id field",
        )
        _seed_goal(
            goals_dir,
            "good.yaml",
            id_val="g-good",
            title="Good Goal",
            objective="a well-formed active goal",
        )

        result = _handler({"text": "goal"}, repo_root=common_dir)

        ids = [c["goal_id"] for c in result["candidates"]]
        assert "g-good" in ids
        assert len(ids) == 1

    def test_missing_title_field_quarantined(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        goals_dir = repo_root / "state" / "goals"
        _seed_goal(
            goals_dir,
            "no-title.yaml",
            id_val="g-notitle",
            title=None,
            objective="goal without title field",
        )
        _seed_goal(
            goals_dir,
            "good.yaml",
            id_val="g-good",
            title="Good Goal",
            objective="a well-formed active goal",
        )

        result = _handler({"text": "goal"}, repo_root=common_dir)

        ids = [c["goal_id"] for c in result["candidates"]]
        assert "g-good" in ids
        assert len(ids) == 1

    def test_key_results_text_contributes_to_score(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        goals_dir = repo_root / "state" / "goals"
        _seed_goal(
            goals_dir,
            "g-kr.yaml",
            id_val="g-kr",
            title="Generic Goal",
            objective="improve overall performance",
            key_results=[
                {
                    "id": "kr-1",
                    "text": "reduce perceptual rendering latency below 100ms",
                    "kind": "outcome",
                    "status": "active",
                }
            ],
        )
        _seed_goal(
            goals_dir,
            "g-control.yaml",
            id_val="g-control",
            title="Unrelated Control",
            objective="expand marketing reach into new demographics",
        )

        result = _handler(
            {"text": "reduce perceptual rendering latency below 100ms"}, repo_root=common_dir
        )

        assert len(result["candidates"]) == 2
        assert result["candidates"][0]["goal_id"] == "g-kr"

    def test_compute_only_classification(self):
        """goal.match_candidates is classified as COMPUTE_ONLY."""
        from coordinator_core.authz.classification import classify, OpClass
        assert classify("goal.match_candidates") is OpClass.COMPUTE_ONLY
