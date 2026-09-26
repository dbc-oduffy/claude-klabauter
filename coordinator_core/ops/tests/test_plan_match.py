"""
coordinator_core.ops.tests.test_plan_match

Tests for the "plan.match_candidates" op.

Import guard: ``import coordinator_core.ops`` MUST precede all test functions so
that ALL op registrations fire (not just the single op under test).  This satisfies
the universal-registry-completeness-tests-ov lesson: asserting a non-empty registry
BEFORE per-op assertions prevents a silent false-positive over an empty registry.

Coverage:
  (a) registry-completeness — registry is non-empty after coordinator_core.ops import
  (b) op-registered — "plan.match_candidates" is in the registry
  (c) empty store — directory absent → returns empty candidates list
  (d) null repo_root — returns empty candidates without raising
  (e) well-formed plans → candidates carry {plan_id, title, score}; text closely
      matching one plan's title ranks it first (ordering asserted)
  (f) malformed YAML frontmatter → quarantined (skipped), well-formed siblings returned
  (g) missing/empty text param → returns empty candidates
  (h) plan_id frontmatter used when present; filename stem used as fallback
  (i) missing title field → quarantined; valid sibling still returned
  (j) COMPUTE_ONLY classification assertion
  (k) id→plan_id wire-key remap — "plan_id" present in output, "id" absent

Fixture approach: real on-disk Markdown files with YAML frontmatter in a tmp_path
directory — matches the production ``docs/plans/*.md`` shape.

Spec backlink: pln-claude-klabauter-fork-provenance-creatio-01c09f § C2
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import pytest

import coordinator_core.ops  # noqa: F401 — populates _REGISTRY

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.plan_match import _handler

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


assert len(_REGISTRY) > 0, (
    "registry is empty after 'import coordinator_core.ops' — "
    "all @register_op decorators must have fired at module import time"
)

_OP_NAME = "plan.match_candidates"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.plan_match @register_op did not fire"
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
        ["git", "config", "user.email", "plan-match-test@claude-klabauter.test"],
        cwd=str(root),
        capture_output=True,
        check=True,
        creationflags=_NO_WIN,
    )
    subprocess.run(
        ["git", "config", "user.name", "Plan Match Test"],
        cwd=str(root),
        capture_output=True,
        check=True,
        creationflags=_NO_WIN,
    )
    return (root / ".git").resolve()


def _seed_plan(
    plans_dir: Path,
    filename: str,
    *,
    title: Optional[str] = None,
    plan_id: Optional[str] = None,
    status: str = "draft",
) -> Path:
    plans_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if title is not None:
        lines.append(f'title: "{title}"')
    if plan_id is not None:
        lines.append(f'plan_id: "{plan_id}"')
    lines.append(f"status: {status}")
    lines.append("---")
    lines.append("")
    lines.append("# Plan body")
    content = "\n".join(lines) + "\n"
    path = plans_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


class TestRegistryCompleteness:

    def test_registry_is_non_empty(self):
        assert len(_REGISTRY) > 0

    def test_op_name_registered(self):
        """'plan.match_candidates' is present in _REGISTRY."""
        assert "plan.match_candidates" in _REGISTRY


class TestPlanMatchCandidates:

    def test_empty_store_directory_absent(self, tmp_path):
        common_dir = _make_git_repo(tmp_path / "repo")
        result = _handler({"text": "provenance"}, repo_root=common_dir)
        assert result == {"candidates": []}

    def test_empty_store_directory_present_but_empty(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        (repo_root / "docs" / "plans").mkdir(parents=True)
        result = _handler({"text": "provenance"}, repo_root=common_dir)
        assert result == {"candidates": []}

    def test_repo_root_none_returns_empty(self):
        result = _handler({"text": "provenance"}, repo_root=None)
        assert result == {"candidates": []}

    def test_missing_text_param_returns_empty(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-test-plan.md",
            title="Test Plan",
            plan_id="pln-test-01",
        )
        result = _handler({}, repo_root=common_dir)
        assert result == {"candidates": []}

    def test_empty_text_param_returns_empty(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-test-plan.md",
            title="Test Plan",
            plan_id="pln-test-01",
        )
        result = _handler({"text": ""}, repo_root=common_dir)
        assert result == {"candidates": []}

    def test_well_formed_plan_fields(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-fork-provenance.md",
            title="Fork Provenance Tooling",
            plan_id="pln-fork-provenance-01",
        )
        result = _handler({"text": "provenance"}, repo_root=common_dir)
        assert len(result["candidates"]) == 1
        entry = result["candidates"][0]
        assert entry["plan_id"] == "pln-fork-provenance-01"
        assert entry["title"] == "Fork Provenance Tooling"
        assert isinstance(entry["score"], float)
        assert 0.0 <= entry["score"] <= 1.0

    def test_id_wire_key_is_plan_id_not_id(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-fork-provenance.md",
            title="Fork Provenance Tooling",
            plan_id="pln-fork-provenance-01",
        )
        result = _handler({"text": "provenance"}, repo_root=common_dir)
        assert len(result["candidates"]) == 1
        entry = result["candidates"][0]
        assert "plan_id" in entry
        assert "id" not in entry

    def test_best_match_ranked_first(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        plans_dir = repo_root / "docs" / "plans"
        _seed_plan(
            plans_dir,
            "2026-07-07-unrelated.md",
            title="Database Schema Migration",
            plan_id="pln-db-migration-01",
        )
        _seed_plan(
            plans_dir,
            "2026-07-07-fork-provenance.md",
            title="Fork Provenance Creation Path Tooling",
            plan_id="pln-fork-provenance-01",
        )

        result = _handler({"text": "fork provenance tooling"}, repo_root=common_dir)

        assert len(result["candidates"]) == 2
        assert result["candidates"][0]["plan_id"] == "pln-fork-provenance-01"
        assert result["candidates"][0]["score"] >= result["candidates"][1]["score"]

    def test_malformed_yaml_quarantined_sibling_still_returned(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        plans_dir = repo_root / "docs" / "plans"
        plans_dir.mkdir(parents=True)
        bad_path = plans_dir / "2026-07-07-bad.md"
        bad_path.write_text("---\ntitle: [unclosed bracket\n---\n", encoding="utf-8")
        _seed_plan(
            plans_dir,
            "2026-07-07-good.md",
            title="Good Plan",
            plan_id="pln-good-01",
        )

        result = _handler({"text": "plan"}, repo_root=common_dir)

        ids = [c["plan_id"] for c in result["candidates"]]
        assert "pln-good-01" in ids
        assert len(ids) == 1

    def test_missing_title_quarantined_sibling_still_returned(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        plans_dir = repo_root / "docs" / "plans"
        _seed_plan(
            plans_dir,
            "2026-07-07-no-title.md",
            title=None,
            plan_id="pln-no-title-01",
        )
        _seed_plan(
            plans_dir,
            "2026-07-07-good.md",
            title="Good Plan",
            plan_id="pln-good-01",
        )

        result = _handler({"text": "plan"}, repo_root=common_dir)

        ids = [c["plan_id"] for c in result["candidates"]]
        assert "pln-good-01" in ids
        assert len(ids) == 1

    def test_frontmatterless_markdown_table_skipped_sibling_still_returned(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        plans_dir = repo_root / "docs" / "plans"
        plans_dir.mkdir(parents=True)
        index_path = plans_dir / "INDEX.md"
        index_path.write_text(
            "# Plans Index\n\n"
            "| Plan | Status |\n"
            "| --- | --- |\n"
            "| Fork Provenance | open |\n",
            encoding="utf-8",
        )
        _seed_plan(
            plans_dir,
            "2026-07-07-good.md",
            title="Good Plan",
            plan_id="pln-good-01",
        )

        result = _handler({"text": "plan"}, repo_root=common_dir)

        ids = [c["plan_id"] for c in result["candidates"]]
        assert "pln-good-01" in ids
        assert len(ids) == 1

    def test_plan_id_frontmatter_preferred_stem_fallback(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        plans_dir = repo_root / "docs" / "plans"
        _seed_plan(
            plans_dir,
            "2026-07-07-with-id.md",
            title="Plan With Explicit ID",
            plan_id="pln-explicit-id-01",
        )
        _seed_plan(
            plans_dir,
            "2026-07-07-no-plan-id.md",
            title="Plan Without Explicit ID",
            plan_id=None,
        )

        result = _handler({"text": "plan"}, repo_root=common_dir)
        assert len(result["candidates"]) == 2
        ids = {c["plan_id"] for c in result["candidates"]}
        assert "pln-explicit-id-01" in ids
        assert "2026-07-07-no-plan-id" in ids

    def test_compute_only_classification(self):
        """plan.match_candidates is classified as COMPUTE_ONLY."""
        from coordinator_core.authz.classification import classify, OpClass
        assert classify("plan.match_candidates") is OpClass.COMPUTE_ONLY
