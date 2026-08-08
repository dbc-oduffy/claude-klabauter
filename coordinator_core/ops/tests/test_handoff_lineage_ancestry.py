"""
coordinator_core.ops.tests.test_handoff_lineage_ancestry

Tests for the "handoff.lineage_ancestry" op.

Import guard: ``import coordinator_core.ops`` MUST precede all test functions so
that ALL op registrations fire (not just the single op under test).  This satisfies
the universal-registry-completeness-tests-ov lesson: asserting a non-empty registry
BEFORE per-op assertions prevents a silent false-positive over an empty registry.

Coverage:
  (a) registry-completeness — registry is non-empty after coordinator_core.ops import
  (b) op-registered — "handoff.lineage_ancestry" is in the registry
  (c) linear ancestry chain — fork -> parent -> grandparent, ordered fork-first
  (d) root handoff with no origin_handoff -> empty ancestry (list == [fork itself]
      is NOT the contract here; ancestry excludes the starting node's own entry
      only if it has no edge — see test docstrings for the exact contract)
  (e) sentinel/missing origin_handoff values (none/null/absent) -> excluded/root
  (f) cycle -> terminated_early == 'lineage-cycle', must not hang/crash
  (g) missing repo_root -> safe empty return
  (h) COMPUTE_ONLY classification assertion
  (i) handoff_id resolution (state/handoffs/<id>.md) and path resolution

Fixture approach: real on-disk Markdown files with YAML frontmatter in a tmp_path
directory — matches the production ``state/handoffs/*.md`` shape, same fixture
style as test_handoff_match.py.

Spec backlink: docs/plans/2026-07-07-claude-klabauter-fork-provenance-creation-path-tooling.md § C4
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Import guard — fires ALL @register_op(...) side-effects, including
# "handoff.lineage_ancestry".  MUST precede all test functions.
# ---------------------------------------------------------------------------
import coordinator_core.ops  # noqa: F401 — populates _REGISTRY

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_lineage_ancestry import _handler

assert len(_REGISTRY) > 0, (
    "registry is empty after 'import coordinator_core.ops' — "
    "all @register_op decorators must have fired at module import time"
)

_OP_NAME = "handoff.lineage_ancestry"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_lineage_ancestry @register_op did not fire"
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
        ["git", "config", "user.email", "handoff-lineage-test@claude-klabauter.test"],
        cwd=str(root),
        capture_output=True,
        check=True,
        creationflags=_NO_WIN,
    )
    subprocess.run(
        ["git", "config", "user.name", "Handoff Lineage Test"],
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
    origin_handoff: Optional[str] = None,
) -> Path:
    """Write a ``state/handoffs/<filename>.md`` fixture file with YAML frontmatter.

    ``origin_handoff`` is written verbatim as the frontmatter value when provided
    (including sentinel strings like "none"/"null" for negative-path tests). When
    omitted, no ``origin_handoff`` key is written at all (absent-field case).
    """
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if title is not None:
        lines.append(f'title: "{title}"')
    if origin_handoff is not None:
        lines.append(f"origin_handoff: {origin_handoff}")
    lines.append("status: open")
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
        assert len(_REGISTRY) > 0

    def test_op_name_registered(self):
        assert "handoff.lineage_ancestry" in _REGISTRY


class TestHandoffLineageAncestry:
    """Ancestry-walk tests for handoff.lineage_ancestry."""

    def test_repo_root_none_returns_empty(self):
        """repo_root=None -> empty ancestry without raising."""
        result = _handler({"handoff_id": "whatever"}, repo_root=None)
        assert result == {"ancestry": [], "terminated_early": ""}

    def test_missing_start_handoff_returns_empty(self, tmp_path):
        """handoff_id/path that resolves to no file on disk -> empty ancestry."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        (repo_root / "state" / "handoffs").mkdir(parents=True)
        result = _handler({"handoff_id": "does-not-exist"}, repo_root=common_dir)
        assert result == {"ancestry": [], "terminated_early": ""}

    def test_missing_params_returns_empty(self, tmp_path):
        """Neither handoff_id nor path supplied -> empty ancestry."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        result = _handler({}, repo_root=common_dir)
        assert result == {"ancestry": [], "terminated_early": ""}

    def test_root_handoff_no_origin_handoff_field(self, tmp_path):
        """A root handoff (no origin_handoff key at all) -> ancestry is just itself."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        _seed_handoff(handoffs_dir, "root.md", title="Root Handoff")

        result = _handler({"handoff_id": "root"}, repo_root=common_dir)

        assert result["terminated_early"] == ""
        assert len(result["ancestry"]) == 1
        assert result["ancestry"][0]["handoff_id"] == "root"
        assert result["ancestry"][0]["title"] == "Root Handoff"

    def test_sentinel_origin_handoff_none_string(self, tmp_path):
        """origin_handoff: none (sentinel) -> excluded from edges, ancestry is just itself."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        _seed_handoff(
            handoffs_dir, "sentinel-none.md", title="Sentinel None", origin_handoff="none"
        )

        result = _handler({"handoff_id": "sentinel-none"}, repo_root=common_dir)

        assert result["terminated_early"] == ""
        assert len(result["ancestry"]) == 1
        assert result["ancestry"][0]["handoff_id"] == "sentinel-none"

    def test_sentinel_origin_handoff_null_string(self, tmp_path):
        """origin_handoff: null (sentinel) -> excluded from edges, ancestry is just itself."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        _seed_handoff(
            handoffs_dir, "sentinel-null.md", title="Sentinel Null", origin_handoff="null"
        )

        result = _handler({"handoff_id": "sentinel-null"}, repo_root=common_dir)

        assert result["terminated_early"] == ""
        assert len(result["ancestry"]) == 1
        assert result["ancestry"][0]["handoff_id"] == "sentinel-null"

    def test_linear_ancestry_chain(self, tmp_path):
        """fork -> parent -> grandparent chain, ordered fork-first."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"

        _seed_handoff(handoffs_dir, "grandparent.md", title="Grandparent")
        _seed_handoff(
            handoffs_dir,
            "parent.md",
            title="Parent",
            origin_handoff="grandparent.md",
        )
        _seed_handoff(
            handoffs_dir,
            "fork.md",
            title="Fork",
            origin_handoff="parent.md",
        )

        result = _handler({"handoff_id": "fork"}, repo_root=common_dir)

        assert result["terminated_early"] == ""
        ids = [entry["handoff_id"] for entry in result["ancestry"]]
        assert ids == ["fork", "parent", "grandparent"]
        titles = [entry["title"] for entry in result["ancestry"]]
        assert titles == ["Fork", "Parent", "Grandparent"]

    def test_cycle_detected_does_not_hang(self, tmp_path):
        """A -> B -> A origin_handoff cycle: terminated_early == 'lineage-cycle', no hang/crash."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"

        _seed_handoff(handoffs_dir, "a.md", title="A", origin_handoff="b.md")
        _seed_handoff(handoffs_dir, "b.md", title="B", origin_handoff="a.md")

        result = _handler({"handoff_id": "a"}, repo_root=common_dir)

        assert result["terminated_early"] == "lineage-cycle"
        ids = [entry["handoff_id"] for entry in result["ancestry"]]
        # Both nodes are visited exactly once before the cycle is detected.
        assert ids == ["a", "b"]

    def test_path_param_resolution(self, tmp_path):
        """Explicit 'path' param (absolute) resolves the starting handoff."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        fork_path = _seed_handoff(handoffs_dir, "fork-by-path.md", title="Fork By Path")

        result = _handler({"path": str(fork_path)}, repo_root=common_dir)

        assert result["terminated_early"] == ""
        assert len(result["ancestry"]) == 1
        assert result["ancestry"][0]["handoff_id"] == "fork-by-path"

    def test_handoff_id_preferred_over_path(self, tmp_path):
        """When both handoff_id and path are given, handoff_id resolution is tried first."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        _seed_handoff(handoffs_dir, "by-id.md", title="By Id")

        result = _handler(
            {"handoff_id": "by-id", "path": "state/handoffs/does-not-exist.md"},
            repo_root=common_dir,
        )

        assert result["terminated_early"] == ""
        assert len(result["ancestry"]) == 1
        assert result["ancestry"][0]["handoff_id"] == "by-id"

    def test_compute_only_classification(self):
        """handoff.lineage_ancestry is classified as COMPUTE_ONLY."""
        from coordinator_core.authz.classification import classify, OpClass

        assert classify("handoff.lineage_ancestry") is OpClass.COMPUTE_ONLY

    def test_missing_link_terminated_early(self, tmp_path):
        """origin_handoff points at a file that does not exist -> terminated_early
        == 'missing-link', and the fork's own entry is still in ancestry (dag.py
        records a node before resolving its edges; accumulation continues past an
        unresolvable edge — dag.py:629-631 vs. 641-647)."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        _seed_handoff(
            handoffs_dir,
            "fork.md",
            title="Fork",
            origin_handoff="nonexistent-parent.md",
        )

        result = _handler({"handoff_id": "fork"}, repo_root=common_dir)

        assert result["terminated_early"] == "missing-link"
        ids = [entry["handoff_id"] for entry in result["ancestry"]]
        assert ids == ["fork"]

    def test_relative_path_param_resolution(self, tmp_path):
        """A worktree-relative 'path' (not absolute, not handoff_id) resolves via
        the worktree_root-join branch in _resolve_start_path."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        _seed_handoff(handoffs_dir, "fork-by-path.md", title="Fork By Path")

        result = _handler(
            {"path": "state/handoffs/fork-by-path.md"}, repo_root=common_dir
        )

        assert result["terminated_early"] == ""
        assert len(result["ancestry"]) == 1
        assert result["ancestry"][0]["handoff_id"] == "fork-by-path"

    def test_handoff_id_exclusive_not_fallback(self, tmp_path):
        """A supplied-but-unresolvable handoff_id returns None immediately —
        it does NOT fall through to a valid, resolvable 'path', proving
        handoff_id is exclusive rather than merely tried-first-with-fallback."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        _seed_handoff(handoffs_dir, "by-path.md", title="By Path")

        result = _handler(
            {
                "handoff_id": "does-not-exist",
                "path": "state/handoffs/by-path.md",
            },
            repo_root=common_dir,
        )

        # handoff_id was supplied but unresolvable -> None, never falls
        # through to the valid path param.
        assert result == {"ancestry": [], "terminated_early": ""}

    def test_handoff_id_traversal_rejected(self, tmp_path):
        """A handoff_id containing '../' traversal segments is rejected at
        parse time (allowlist regex) rather than resolved — must not escape
        state/handoffs/."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        _seed_handoff(handoffs_dir, "root.md", title="Root Handoff")

        # A secret file outside state/handoffs/ that a traversal could reach.
        secret = repo_root / "secret.md"
        secret.write_text("---\ntitle: \"Secret\"\n---\n", encoding="utf-8")

        result = _handler(
            {"handoff_id": "../secret"},
            repo_root=common_dir,
        )

        assert result == {"ancestry": [], "terminated_early": ""}

    def test_path_outside_tree_rejected(self, tmp_path):
        """An out-of-tree absolute 'path' (outside state/handoffs/ and
        archive/handoffs/) is rejected by the post-resolve containment check,
        not returned as the foreign file's contents."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        (repo_root / "state" / "handoffs").mkdir(parents=True)

        outside = tmp_path / "outside" / "secret.md"
        outside.parent.mkdir(parents=True)
        outside.write_text('---\ntitle: "Secret"\n---\n', encoding="utf-8")

        result = _handler(
            {"path": str(outside)},
            repo_root=common_dir,
        )

        assert result == {"ancestry": [], "terminated_early": ""}
