"""
coordinator_core.ops.tests.test_handoff_match

Tests for the "handoff.match_candidates" op.

Import guard: ``import coordinator_core.ops`` MUST precede all test functions so
that ALL op registrations fire (not just the single op under test).  This satisfies
the universal-registry-completeness-tests-ov lesson: asserting a non-empty registry
BEFORE per-op assertions prevents a silent false-positive over an empty registry.

Coverage:
  (a) registry-completeness — registry is non-empty after coordinator_core.ops import
  (b) op-registered — "handoff.match_candidates" is in the registry
  (c) empty store — directory absent → returns empty candidates list
  (d) null repo_root — returns empty candidates without raising
  (e) well-formed handoffs → candidates carry {handoff_id, title, score}; text closely
      matching one handoff's title ranks it first (ordering asserted)
  (f) malformed YAML frontmatter → quarantined (skipped), well-formed siblings returned
  (g) missing/empty text param → returns empty candidates
  (h) handoff_id is the filename stem (timestamp+slug convention)
  (i) missing title field → quarantined; valid sibling still returned
  (j) COMPUTE_ONLY classification assertion
  (k) id→handoff_id wire-key remap — "handoff_id" present in output, "id" absent
  (l) no-frontmatter file → quarantined; valid sibling still returned

Fixture approach: real on-disk Markdown files with YAML frontmatter in a tmp_path
directory — matches the production ``state/handoffs/*.md`` shape.

Spec backlink: pln-claude-klabauter-fork-provenance-creatio-01c09f § C2
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Import guard — fires ALL @register_op(...) side-effects, including
# "handoff.match_candidates".  MUST precede all test functions.
# ---------------------------------------------------------------------------
import coordinator_core.ops  # noqa: F401 — populates _REGISTRY

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_match import _handler

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

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

_OP_NAME = "handoff.match_candidates"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_match @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------



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
        ["git", "config", "user.email", "handoff-match-test@claude-klabauter.test"],
        cwd=str(root),
        capture_output=True,
        check=True,
        creationflags=_NO_WIN,
    )
    subprocess.run(
        ["git", "config", "user.name", "Handoff Match Test"],
        cwd=str(root),
        capture_output=True,
        check=True,
        creationflags=_NO_WIN,
    )
    return (root / ".git").resolve()


def _seed_handoff(
    handoffs_dir: Path,
    filename: str,
    *,
    title: Optional[str] = None,
    status: str = "open",
    no_frontmatter: bool = False,
) -> Path:
    """Write a ``state/handoffs/<filename>.md`` fixture file with YAML frontmatter.

    Omitting ``title`` produces a file that should be quarantined.
    Setting ``no_frontmatter=True`` produces a file with no frontmatter block.
    """
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    if no_frontmatter:
        content = "# Handoff body with no frontmatter\n"
    else:
        lines = ["---"]
        if title is not None:
            lines.append(f'title: "{title}"')
        lines.append(f"status: {status}")
        lines.append("---")
        lines.append("")
        lines.append("# Handoff body")
        content = "\n".join(lines) + "\n"
    path = handoffs_dir / filename
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
        """'handoff.match_candidates' is present in _REGISTRY."""
        assert "handoff.match_candidates" in _REGISTRY


class TestHandoffMatchCandidates:
    """Payload and ranking tests for handoff.match_candidates."""

    def test_empty_store_directory_absent(self, tmp_path):
        """No state/handoffs/ directory → empty candidates list."""
        common_dir = _make_git_repo(tmp_path / "repo")
        result = _handler({"text": "roadmap"}, repo_root=common_dir)
        assert result == {"candidates": []}

    def test_empty_store_directory_present_but_empty(self, tmp_path):
        """state/handoffs/ exists but has no .md files → empty candidates list."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        (repo_root / "state" / "handoffs").mkdir(parents=True)
        result = _handler({"text": "roadmap"}, repo_root=common_dir)
        assert result == {"candidates": []}

    def test_repo_root_none_returns_empty(self):
        """repo_root=None → empty candidates without raising."""
        result = _handler({"text": "roadmap"}, repo_root=None)
        assert result == {"candidates": []}

    def test_missing_text_param_returns_empty(self, tmp_path):
        """params missing 'text' key → empty candidates list."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_handoff(
            repo_root / "state" / "handoffs",
            "2026-07-02_230112_roadmap-pcore-12.md",
            title="Edifice-wide migration ordering",
        )
        result = _handler({}, repo_root=common_dir)
        assert result == {"candidates": []}

    def test_empty_text_param_returns_empty(self, tmp_path):
        """params text='' → empty candidates list."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_handoff(
            repo_root / "state" / "handoffs",
            "2026-07-02_230112_roadmap-pcore-12.md",
            title="Edifice-wide migration ordering",
        )
        result = _handler({"text": ""}, repo_root=common_dir)
        assert result == {"candidates": []}

    def test_well_formed_handoff_fields(self, tmp_path):
        """Well-formed handoff → candidate carries {handoff_id, title, score}."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_handoff(
            repo_root / "state" / "handoffs",
            "2026-07-02_230112_roadmap-pcore-12.md",
            title="Edifice-wide migration ordering",
        )
        result = _handler({"text": "migration ordering"}, repo_root=common_dir)
        assert len(result["candidates"]) == 1
        entry = result["candidates"][0]
        assert entry["handoff_id"] == "2026-07-02_230112_roadmap-pcore-12"
        assert entry["title"] == "Edifice-wide migration ordering"
        assert isinstance(entry["score"], float)
        assert 0.0 <= entry["score"] <= 1.0

    def test_id_wire_key_is_handoff_id_not_id(self, tmp_path):
        """Candidates carry 'handoff_id' wire key, never the generic 'id' key."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        _seed_handoff(
            repo_root / "state" / "handoffs",
            "2026-07-02_230112_roadmap-pcore-12.md",
            title="Edifice-wide migration ordering",
        )
        result = _handler({"text": "migration"}, repo_root=common_dir)
        assert len(result["candidates"]) == 1
        entry = result["candidates"][0]
        assert "handoff_id" in entry
        assert "id" not in entry

    def test_handoff_id_is_filename_stem(self, tmp_path):
        """handoff_id in output is the filename stem (timestamp+slug)."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        stem = "2026-07-05_120000_fork-authoring-tooling"
        _seed_handoff(
            repo_root / "state" / "handoffs",
            f"{stem}.md",
            title="Fork Authoring Tooling",
        )
        result = _handler({"text": "fork authoring"}, repo_root=common_dir)
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["handoff_id"] == stem

    def test_best_match_ranked_first(self, tmp_path):
        """Text closely matching one handoff's title ranks it first."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        _seed_handoff(
            handoffs_dir,
            "2026-07-02_230112_unrelated.md",
            title="Database Configuration Review",
        )
        _seed_handoff(
            handoffs_dir,
            "2026-07-05_120000_fork-authoring.md",
            title="Fork Authoring Provenance Tooling",
        )

        result = _handler({"text": "fork authoring provenance"}, repo_root=common_dir)

        assert len(result["candidates"]) == 2
        # The fork authoring handoff must rank first.
        assert result["candidates"][0]["handoff_id"] == "2026-07-05_120000_fork-authoring"
        assert result["candidates"][0]["score"] >= result["candidates"][1]["score"]

    def test_malformed_yaml_quarantined_sibling_still_returned(self, tmp_path):
        """Malformed YAML frontmatter is skipped; well-formed siblings are returned."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        handoffs_dir.mkdir(parents=True)
        # Write a file with invalid YAML frontmatter.
        bad_path = handoffs_dir / "2026-07-01_000000_bad.md"
        bad_path.write_text("---\ntitle: [unclosed bracket\n---\n", encoding="utf-8")
        _seed_handoff(
            handoffs_dir,
            "2026-07-02_000000_good.md",
            title="Good Handoff",
        )

        result = _handler({"text": "handoff"}, repo_root=common_dir)

        ids = [c["handoff_id"] for c in result["candidates"]]
        assert "2026-07-02_000000_good" in ids
        assert len(ids) == 1

    def test_missing_title_quarantined_sibling_still_returned(self, tmp_path):
        """Handoff with missing title field is quarantined; valid sibling is returned."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        _seed_handoff(
            handoffs_dir,
            "2026-07-01_000000_no-title.md",
            title=None,  # no title — should be quarantined
        )
        _seed_handoff(
            handoffs_dir,
            "2026-07-02_000000_good.md",
            title="Good Handoff",
        )

        result = _handler({"text": "handoff"}, repo_root=common_dir)

        ids = [c["handoff_id"] for c in result["candidates"]]
        assert "2026-07-02_000000_good" in ids
        assert len(ids) == 1

    def test_no_frontmatter_quarantined_sibling_still_returned(self, tmp_path):
        """Handoff without frontmatter block is quarantined; valid sibling returned."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        _seed_handoff(
            handoffs_dir,
            "2026-07-01_000000_no-fm.md",
            no_frontmatter=True,
        )
        _seed_handoff(
            handoffs_dir,
            "2026-07-02_000000_good.md",
            title="Good Handoff",
        )

        result = _handler({"text": "handoff"}, repo_root=common_dir)

        ids = [c["handoff_id"] for c in result["candidates"]]
        assert "2026-07-02_000000_good" in ids
        assert len(ids) == 1

    def test_compute_only_classification(self):
        """handoff.match_candidates is classified as COMPUTE_ONLY."""
        from coordinator_core.authz.classification import classify, OpClass
        assert classify("handoff.match_candidates") is OpClass.COMPUTE_ONLY
