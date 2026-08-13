"""
coordinator_core.ops.tests.test_roadmap_serve

Tests for the "roadmap.serve" op (C5).

Import guard: ``import coordinator_core.ops`` MUST precede all test functions so
that ALL op registrations fire (not just the single op under test).  This satisfies
the universal-registry-completeness-tests-ov lesson: asserting a non-empty registry
BEFORE per-op assertions prevents a silent false-positive over an empty registry.

Coverage:
  (a) registry-completeness    — registry is non-empty after coordinator_core.ops import
  (b) op-registered            — "roadmap.serve" is in the registry
  (c) missing_roadmap_id       — absent/empty param → empty payload without raising
  (d) unknown_roadmap_id       — zero-node guard: unknown id → well-formed empty payload (F3)
  (e) single_node_no_edges     — stub with no blocks → 1 node, 0 edges, correct roll_up
  (f) two_nodes_one_edge       — 2 stubs, blocks: [target] → 1 edge, correct critical_path
  (g) shipped_sha_passthrough  — shipped_in bare SHA → shipped_sha field as-is, no git call (F1)
  (h) roadmap_id_in_response   — top-level roadmap_id echoed in returned payload
  (i) no_repo_root_returns_empty — repo_root=None → empty payload without raising

Deferred verification (PM-gated daemon restart required):
  Live UDS socket-smoke: call ``roadmap.serve`` over the live Unix-domain socket after a
  daemon restart to confirm wire reachability.  In-process unit tests diverge from wire
  reachability — lesson: ``registering-a-new-op-in-a-resident-servi``.  This step
  cannot be run in CI without a running daemon and is deferred per the Dispatch Ledger.

Fixture shape: all fixture files are real ---fenced Markdown files with YAML frontmatter
matching the production stub handoff shape (lesson: test-fidelity-seed-fixtures-in-the-real).

Spec backlink: pln-claude-klabauter-served-initiative-roadm-8e0492 § C5 (AC5)
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Import guard — fires ALL @register_op(...) side-effects, including
# "roadmap.serve".  MUST precede all test functions.
# ---------------------------------------------------------------------------
import coordinator_core.ops  # noqa: F401 — populates _REGISTRY

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.roadmap_serve import _handler

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

_OP_NAME = "roadmap.serve"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.roadmap_serve @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Execute a coroutine synchronously (test helper).

    ``_handler`` is a plain ``def`` (no ``await`` in its body), so calls
    already resolve to a plain value by the time they reach here; pass
    those through unchanged.
    """
    if asyncio.iscoroutine(coro):
        return asyncio.run(coro)
    return coro


def _make_git_repo(root: Path) -> Path:
    """Create a minimal git repo at ``root`` and return its common_dir (.git path)."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(root),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "roadmap-serve-test@claude-klabauter.test"],
        cwd=str(root),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Roadmap Serve Test"],
        cwd=str(root),
        capture_output=True,
        check=True,
    )
    return (root / ".git").resolve()


def _write_stub_handoff(
    handoffs_dir: Path,
    filename: str,
    *,
    roadmap_id: str,
    stub_id: str,
    deployment_state: str = "in_flight",
    sprint: Optional[str] = None,
    wave: Optional[str] = None,
    shipped_in: Optional[str] = None,
    blocks: Optional[list] = None,
    blocked_by: Optional[list] = None,
) -> Path:
    """Write a ``state/handoffs/<filename>`` fixture with production-shaped YAML frontmatter.

    Produces a real ---fenced Markdown file matching the production stub handoff shape
    (lesson: test-fidelity-seed-fixtures-in-the-real).  All YAML fields are explicit
    strings; arrays use the inline YAML list syntax (e.g. ``blocks: [strang-01]``).
    """
    handoffs_dir.mkdir(parents=True, exist_ok=True)

    fm_lines = [
        "---",
        f"roadmap_id: {roadmap_id}",
        f"stub_id: {stub_id}",
        f"deployment_state: {deployment_state}",
    ]
    if sprint is not None:
        fm_lines.append(f"sprint: {sprint}")
    if wave is not None:
        fm_lines.append(f"wave: {wave}")
    if shipped_in is not None:
        fm_lines.append(f"shipped_in: {shipped_in}")
    if blocks:
        fm_lines.append(f"blocks: [{', '.join(blocks)}]")
    if blocked_by:
        fm_lines.append(f"blocked_by: [{', '.join(blocked_by)}]")
    fm_lines.append("---")
    fm_lines.append("")
    fm_lines.append(f"# Stub: {stub_id}")
    fm_lines.append("")

    content = "\n".join(fm_lines)
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
        """'roadmap.serve' is present in _REGISTRY."""
        assert "roadmap.serve" in _REGISTRY


class TestRoadmapServe:
    """Payload and resolution tests for roadmap.serve."""

    def test_missing_roadmap_id_returns_empty(self, tmp_path):
        """Absent roadmap_id param → empty well-formed payload without raising."""
        common_dir = _make_git_repo(tmp_path / "repo")
        result = _run(_handler({}, repo_root=common_dir))
        assert result["nodes"] == []
        assert result["edges"] == []
        assert result["critical_path"] == []
        assert result["roll_up"]["pct_shipped"] is None

    def test_empty_roadmap_id_returns_empty(self, tmp_path):
        """Empty string roadmap_id → empty well-formed payload without raising."""
        common_dir = _make_git_repo(tmp_path / "repo")
        result = _run(_handler({"roadmap_id": ""}, repo_root=common_dir))
        assert result["nodes"] == []

    def test_unknown_roadmap_id_zero_node_guard(self, tmp_path):
        """Unknown roadmap_id → well-formed empty payload (F3 zero-node guard)."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        # No stub handoffs exist at all
        result = _run(
            _handler({"roadmap_id": "no-such-roadmap"}, repo_root=common_dir)
        )
        assert result["roadmap_id"] == "no-such-roadmap"
        assert result["nodes"] == []
        assert result["edges"] == []
        assert result["critical_path"] == []
        assert result["roll_up"] == {"total": 0, "by_status": {}, "pct_shipped": None}

    def test_single_node_no_edges(self, tmp_path):
        """Single stub with no blocks/blocked_by → 1 node, 0 edges, roll_up correct."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        _write_stub_handoff(
            handoffs_dir,
            "2026-07-05-strang-01.md",
            roadmap_id="claude-klabauter-strangler",
            stub_id="strang-01",
            deployment_state="in_flight",
        )

        result = _run(
            _handler({"roadmap_id": "claude-klabauter-strangler"}, repo_root=common_dir)
        )

        assert result["roadmap_id"] == "claude-klabauter-strangler"
        assert len(result["nodes"]) == 1
        assert result["nodes"][0]["stub_id"] == "strang-01"
        assert result["nodes"][0]["status"] == "in_flight"
        assert result["nodes"][0]["roadmap_id"] == "claude-klabauter-strangler"
        assert result["edges"] == []
        assert result["roll_up"]["total"] == 1
        assert result["roll_up"]["by_status"].get("in_flight") == 1
        # Not shipped → pct_shipped is 0.0
        assert result["roll_up"]["pct_shipped"] == 0.0

    def test_two_nodes_one_edge_critical_path(self, tmp_path):
        """Two stubs with blocks relationship → 1 edge, correct critical_path."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        # strang-01 blocks strang-02
        _write_stub_handoff(
            handoffs_dir,
            "2026-07-05-strang-01.md",
            roadmap_id="claude-klabauter-strangler",
            stub_id="strang-01",
            deployment_state="shipped",
            blocks=["strang-02"],
        )
        _write_stub_handoff(
            handoffs_dir,
            "2026-07-05-strang-02.md",
            roadmap_id="claude-klabauter-strangler",
            stub_id="strang-02",
            deployment_state="in_flight",
        )

        result = _run(
            _handler({"roadmap_id": "claude-klabauter-strangler"}, repo_root=common_dir)
        )

        assert len(result["nodes"]) == 2
        assert len(result["edges"]) == 1
        edge = result["edges"][0]
        assert edge["from"] == "strang-01"
        assert edge["to"] == "strang-02"
        assert edge["type"] == "blocks"
        assert edge["roadmap_id"] == "claude-klabauter-strangler"
        # Critical path: strang-01 → strang-02 (chain of 2)
        assert result["critical_path"] == ["strang-01", "strang-02"]

    def test_shipped_sha_passthrough_no_git_call(self, tmp_path):
        """shipped_in bare SHA scalar → shipped_sha passed through as-is (F1 zero-spawn)."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        _write_stub_handoff(
            handoffs_dir,
            "2026-07-05-strang-shipped.md",
            roadmap_id="claude-klabauter-strangler",
            stub_id="strang-shipped",
            deployment_state="shipped",
            shipped_in="e7be8fe",  # realistic hex SHA — contains letters, parsed as str
        )

        result = _run(
            _handler({"roadmap_id": "claude-klabauter-strangler"}, repo_root=common_dir)
        )

        assert len(result["nodes"]) == 1
        node = result["nodes"][0]
        # shipped_in is carried through as-is — no git ancestor check, no dict wrapping
        assert node["shipped_sha"] == "e7be8fe"

    def test_node_without_shipped_in_has_null_shipped_sha(self, tmp_path):
        """Stub with no shipped_in frontmatter → shipped_sha is None."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        _write_stub_handoff(
            handoffs_dir,
            "2026-07-05-strang-03.md",
            roadmap_id="claude-klabauter-strangler",
            stub_id="strang-03",
            deployment_state="planned",
        )

        result = _run(
            _handler({"roadmap_id": "claude-klabauter-strangler"}, repo_root=common_dir)
        )

        assert len(result["nodes"]) == 1
        assert result["nodes"][0]["shipped_sha"] is None

    def test_roadmap_id_echoed_in_response(self, tmp_path):
        """top-level roadmap_id is echoed in the returned payload."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        _write_stub_handoff(
            handoffs_dir,
            "2026-07-05-strang-echo.md",
            roadmap_id="test-roadmap-echo",
            stub_id="strang-echo",
            deployment_state="in_flight",
        )

        result = _run(
            _handler(
                {"roadmap_id": "test-roadmap-echo"}, repo_root=common_dir
            )
        )

        assert result["roadmap_id"] == "test-roadmap-echo"

    def test_no_repo_root_returns_empty_without_raising(self):
        """repo_root=None → empty payload without raising."""
        result = _run(
            _handler({"roadmap_id": "some-roadmap"}, repo_root=None)
        )
        assert result["nodes"] == []
        assert result["edges"] == []
        assert result["critical_path"] == []
        assert result["roll_up"]["pct_shipped"] is None

    def test_only_matching_roadmap_id_returned(self, tmp_path):
        """Stubs with a different roadmap_id are excluded from the serve result."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoffs_dir = repo_root / "state" / "handoffs"
        _write_stub_handoff(
            handoffs_dir,
            "2026-07-05-roadmap-a.md",
            roadmap_id="roadmap-a",
            stub_id="stub-a-01",
            deployment_state="in_flight",
        )
        _write_stub_handoff(
            handoffs_dir,
            "2026-07-05-roadmap-b.md",
            roadmap_id="roadmap-b",
            stub_id="stub-b-01",
            deployment_state="in_flight",
        )

        result = _run(
            _handler({"roadmap_id": "roadmap-a"}, repo_root=common_dir)
        )

        stub_ids = [n["stub_id"] for n in result["nodes"]]
        assert stub_ids == ["stub-a-01"]
        assert "stub-b-01" not in stub_ids
