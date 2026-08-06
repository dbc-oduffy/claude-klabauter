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

Spec backlink: docs/plans/2026-07-07-claude-klabauter-fork-provenance-creation-path-tooling.md § C2
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Import guard — fires ALL @register_op(...) side-effects, including
# "plan.match_candidates".  MUST precede all test functions.
# ---------------------------------------------------------------------------
import coordinator_core.ops  # noqa: F401 — populates _REGISTRY

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.plan_match import _handler

# ---------------------------------------------------------------------------
# Registry completeness assertion (universal positive floor)
#
# Lesson: universal-registry-completeness-tests-ov — import coordinator_core.ops
# FIRST, then assert non-empty registry BEFORE any per-op assertion.  An empty
# registry would make all per-op assertions vacuously pass (false-positive).
# ---------------------------------------------------------------------------

assert len(_REGISTRY) > 0, (
    "registry is empty after 'import coordinator_core.ops' — "
    "all @register_op decorators must have fired at module import time"
)

_OP_NAME = "plan.match_candidates"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.plan_match @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Execute a coroutine synchronously (test helper)."""
    return asyncio.run(coro)


def _make_git_repo(root: Path) -> Path:
    """Create a minimal git repo at ``root`` and return its common_dir (.git path)."""
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
    """Write a ``docs/plans/<filename>.md`` fixture file with YAML frontmatter.

    Omitting ``title`` produces a file that should be quarantined.
    Omitting ``plan_id`` produces a file where the filename stem is used as id.
    """
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRegistryCompleteness:
    """Positive-floor registry checks (must pass before any per-op test)."""

    def test_registry_is_non_empty(self):
        """Registry populated after coordinator_core.ops import (positive floor)."""
        assert len(_REGISTRY) > 0

    def test_op_name_registered(self):
        """'plan.match_candidates' is present in _REGISTRY."""
        assert "plan.match_candidates" in _REGISTRY


class TestPlanMatchCandidates:
    """Payload and ranking tests for plan.match_candidates."""

    def test_empty_store_directory_absent(self, tmp_path):
        """No docs/plans/ directory → empty candidates list."""
        common_dir = _make_git_repo(tmp_path / "repo")
        result = _run(_handler({"text": "provenance"}, repo_root=common_dir))
        assert result == {"candidates": []}

    def test_empty_store_directory_present_but_empty(self, tmp_path):
        """docs/plans/ exists but has no .md files → empty candidates list."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        (repo_root / "docs" / "plans").mkdir(parents=True)
        result = _run(_handler({"text": "provenance"}, repo_root=common_dir))
        assert result == {"candidates": []}

    def test_repo_root_none_returns_empty(self):
        """repo_root=None → empty candidates without raising."""
        result = _run(_handler({"text": "provenance"}, repo_root=None))
        assert result == {"candidates": []}

    def test_missing_text_param_returns_empty(self, tmp_path):
        """params missing 'text' key → empty candidates list."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-test-plan.md",
            title="Test Plan",
            plan_id="pln-test-01",
        )
        result = _run(_handler({}, repo_root=common_dir))
        assert result == {"candidates": []}

    def test_empty_text_param_returns_empty(self, tmp_path):
        """params text='' → empty candidates list."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-test-plan.md",
            title="Test Plan",
            plan_id="pln-test-01",
        )
        result = _run(_handler({"text": ""}, repo_root=common_dir))
        assert result == {"candidates": []}

    def test_well_formed_plan_fields(self, tmp_path):
        """Well-formed plan → candidate carries {plan_id, title, score}."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-fork-provenance.md",
            title="Fork Provenance Tooling",
            plan_id="pln-fork-provenance-01",
        )
        result = _run(_handler({"text": "provenance"}, repo_root=common_dir))
        assert len(result["candidates"]) == 1
        entry = result["candidates"][0]
        assert entry["plan_id"] == "pln-fork-provenance-01"
        assert entry["title"] == "Fork Provenance Tooling"
        assert isinstance(entry["score"], float)
        assert 0.0 <= entry["score"] <= 1.0

    def test_id_wire_key_is_plan_id_not_id(self, tmp_path):
        """Candidates carry 'plan_id' wire key, never the generic 'id' key."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_plan(
            repo_root / "docs" / "plans",
            "2026-07-07-fork-provenance.md",
            title="Fork Provenance Tooling",
            plan_id="pln-fork-provenance-01",
        )
        result = _run(_handler({"text": "provenance"}, repo_root=common_dir))
        assert len(result["candidates"]) == 1
        entry = result["candidates"][0]
        assert "plan_id" in entry
        assert "id" not in entry

    def test_best_match_ranked_first(self, tmp_path):
        """Text closely matching one plan's title ranks it first."""
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

        result = _run(_handler({"text": "fork provenance tooling"}, repo_root=common_dir))

        assert len(result["candidates"]) == 2
        # The fork provenance plan must rank first.
        assert result["candidates"][0]["plan_id"] == "pln-fork-provenance-01"
        assert result["candidates"][0]["score"] >= result["candidates"][1]["score"]

    def test_malformed_yaml_quarantined_sibling_still_returned(self, tmp_path):
        """Malformed YAML frontmatter is skipped; well-formed siblings are returned."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        plans_dir = repo_root / "docs" / "plans"
        plans_dir.mkdir(parents=True)
        # Write a file with invalid YAML frontmatter.
        bad_path = plans_dir / "2026-07-07-bad.md"
        bad_path.write_text("---\ntitle: [unclosed bracket\n---\n", encoding="utf-8")
        _seed_plan(
            plans_dir,
            "2026-07-07-good.md",
            title="Good Plan",
            plan_id="pln-good-01",
        )

        result = _run(_handler({"text": "plan"}, repo_root=common_dir))

        ids = [c["plan_id"] for c in result["candidates"]]
        assert "pln-good-01" in ids
        assert len(ids) == 1

    def test_missing_title_quarantined_sibling_still_returned(self, tmp_path):
        """Plan with missing title field is quarantined; valid sibling is returned."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        plans_dir = repo_root / "docs" / "plans"
        _seed_plan(
            plans_dir,
            "2026-07-07-no-title.md",
            title=None,  # no title — should be quarantined
            plan_id="pln-no-title-01",
        )
        _seed_plan(
            plans_dir,
            "2026-07-07-good.md",
            title="Good Plan",
            plan_id="pln-good-01",
        )

        result = _run(_handler({"text": "plan"}, repo_root=common_dir))

        ids = [c["plan_id"] for c in result["candidates"]]
        assert "pln-good-01" in ids
        assert len(ids) == 1

    def test_frontmatterless_markdown_table_skipped_sibling_still_returned(self, tmp_path):
        """A frontmatter-less .md (e.g. a generated INDEX.md) is skipped cleanly —
        no exception from feeding a markdown table to the YAML scanner — while a
        valid sibling plan in the same directory still enumerates."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        plans_dir = repo_root / "docs" / "plans"
        plans_dir.mkdir(parents=True)
        # Write a frontmatter-less markdown file containing a markdown table —
        # mirrors renderers.py::render_plans_index_markdown output.
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

        result = _run(_handler({"text": "plan"}, repo_root=common_dir))

        ids = [c["plan_id"] for c in result["candidates"]]
        assert "pln-good-01" in ids
        assert len(ids) == 1

    def test_plan_id_frontmatter_preferred_stem_fallback(self, tmp_path):
        """plan_id frontmatter is used when present; filename stem is the fallback."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        plans_dir = repo_root / "docs" / "plans"
        # Plan with explicit plan_id frontmatter.
        _seed_plan(
            plans_dir,
            "2026-07-07-with-id.md",
            title="Plan With Explicit ID",
            plan_id="pln-explicit-id-01",
        )
        # Plan without plan_id frontmatter — stem should be used.
        _seed_plan(
            plans_dir,
            "2026-07-07-no-plan-id.md",
            title="Plan Without Explicit ID",
            plan_id=None,
        )

        result = _run(_handler({"text": "plan"}, repo_root=common_dir))
        assert len(result["candidates"]) == 2
        ids = {c["plan_id"] for c in result["candidates"]}
        assert "pln-explicit-id-01" in ids
        assert "2026-07-07-no-plan-id" in ids

    def test_compute_only_classification(self):
        """plan.match_candidates is classified as COMPUTE_ONLY."""
        from coordinator_core.authz.classification import classify, OpClass
        assert classify("plan.match_candidates") is OpClass.COMPUTE_ONLY
