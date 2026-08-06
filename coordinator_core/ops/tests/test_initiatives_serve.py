"""
coordinator_core.ops.tests.test_initiatives_serve

Tests for the "initiative.serve_set" op.

Import guard: ``import coordinator_core.ops`` MUST precede all test functions so
that ALL op registrations fire (not just the single op under test).  This satisfies
the universal-registry-completeness-tests-ov lesson: asserting a non-empty registry
BEFORE per-op assertions prevents a silent false-positive over an empty registry.

Coverage:
  (a) registry-completeness — registry is non-empty after coordinator_core.ops import
  (b) op-registered — "initiative.serve_set" is in the registry
  (c) empty store — directory absent → returns empty initiatives list
  (d) well-formed files — payload contains {id, label, status, target_date, shape}
  (e) shape derivation — target_date=null → "ongoing"; ISO-date → "completion"
  (f) malformed files — missing id/label → quarantined (skipped), others still served
  (g) status coercion — invalid status → null; valid status → passed through
  (h) null repo_root — returns empty initiatives list without raising

Fixture approach: real on-disk YAML files in a tmp_path directory — matches the
production ``state/initiatives/<id>.yaml`` shape (lesson: test-fidelity-seed-fixtures-
in-the-real).

Spec backlink: docs/plans/2026-07-05-claude-klabauter-served-initiative-roadmap-read-model.md § C2 (AC2)
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Import guard — fires ALL @register_op(...) side-effects, including
# "initiative.serve_set".  MUST precede all test functions.
# ---------------------------------------------------------------------------
import coordinator_core.ops  # noqa: F401 — populates _REGISTRY

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.initiatives_serve import _handler

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

_OP_NAME = "initiative.serve_set"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.initiatives_serve @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Execute a coroutine synchronously (test helper)."""
    return asyncio.run(coro)


def _seed_initiative(
    initiatives_dir: Path,
    filename: str,
    *,
    id_val: Optional[str] = None,
    label: Optional[str] = None,
    status: str = "active",
    target_date: Optional[str] = None,
    description: Optional[str] = None,
) -> Path:
    """Write a ``state/initiatives/<filename>.yaml`` fixture file.

    Omitting ``id_val`` or ``label`` produces a file that should be quarantined
    (used by malformed-file tests).  ``target_date=None`` writes ``target_date: null``
    (the claude-klabauter schema's ongoing/burn-down convention).
    """
    initiatives_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    if id_val is not None:
        lines.append(f"id: {id_val}")
    if label is not None:
        lines.append(f"label: {label}")
    lines.append(f"status: {status}")
    if target_date is not None:
        lines.append(f"target_date: {target_date}")
    else:
        lines.append("target_date: null")
    if description is not None:
        lines.append(f"description: {description}")
    content = "\n".join(lines) + "\n"
    path = initiatives_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


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
        ["git", "config", "user.email", "serve-test@claude-klabauter.test"],
        cwd=str(root),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Serve Test"],
        cwd=str(root),
        capture_output=True,
        check=True,
    )
    return (root / ".git").resolve()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRegistryCompleteness:
    """Positive-floor registry checks (must pass before any per-op test)."""

    def test_registry_is_non_empty(self):
        """Registry populated after coordinator_core.ops import (positive floor)."""
        assert len(_REGISTRY) > 0

    def test_op_name_registered(self):
        """'initiative.serve_set' is present in _REGISTRY."""
        assert "initiative.serve_set" in _REGISTRY


class TestInitiativeServeSet:
    """Payload and shape-derivation tests for initiative.serve_set."""

    def test_empty_store_directory_absent(self, tmp_path):
        """No state/initiatives/ directory → empty initiatives list."""
        common_dir = _make_git_repo(tmp_path / "repo")
        result = _run(_handler({}, repo_root=common_dir))
        assert result == {"initiatives": []}

    def test_empty_store_directory_present_but_empty(self, tmp_path):
        """state/initiatives/ exists but has no YAML files → empty list."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        (repo_root / "state" / "initiatives").mkdir(parents=True)
        result = _run(_handler({}, repo_root=common_dir))
        assert result == {"initiatives": []}

    def test_single_active_ongoing_initiative(self, tmp_path):
        """Well-formed active/ongoing initiative → correct payload fields."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        ini_dir = repo_root / "state" / "initiatives"
        _seed_initiative(ini_dir, "python-core.yaml", id_val="python-core", label="Python Core")

        result = _run(_handler({}, repo_root=common_dir))

        assert len(result["initiatives"]) == 1
        entry = result["initiatives"][0]
        assert entry["id"] == "python-core"
        assert entry["label"] == "Python Core"
        assert entry["status"] == "active"
        assert entry["target_date"] is None
        assert entry["shape"] == "ongoing"

    def test_completion_shape_when_target_date_set(self, tmp_path):
        """Initiative with ISO target_date → shape='completion'."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        ini_dir = repo_root / "state" / "initiatives"
        _seed_initiative(
            ini_dir,
            "fleet-spine.yaml",
            id_val="fleet-deliverable-spine",
            label="Fleet Deliverable Spine",
            status="active",
            target_date="2026-09-30",
        )

        result = _run(_handler({}, repo_root=common_dir))

        assert len(result["initiatives"]) == 1
        entry = result["initiatives"][0]
        assert entry["id"] == "fleet-deliverable-spine"
        assert entry["target_date"] == "2026-09-30"
        assert entry["shape"] == "completion"

    def test_ongoing_shape_when_target_date_null(self, tmp_path):
        """Initiative with target_date: null → shape='ongoing' (burn-down convention)."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        ini_dir = repo_root / "state" / "initiatives"
        # target_date=None writes "target_date: null"
        _seed_initiative(
            ini_dir,
            "claude-klabauter-strangler.yaml",
            id_val="claude-klabauter-strangler",
            label="Claude-Klabauter Strangler",
            target_date=None,
        )

        result = _run(_handler({}, repo_root=common_dir))

        entry = result["initiatives"][0]
        assert entry["target_date"] is None
        assert entry["shape"] == "ongoing"

    def test_multiple_initiatives_sorted_by_filename(self, tmp_path):
        """Multiple YAML files → all returned (sorted by filename)."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        ini_dir = repo_root / "state" / "initiatives"
        _seed_initiative(ini_dir, "b-second.yaml", id_val="second", label="Second")
        _seed_initiative(ini_dir, "a-first.yaml", id_val="first", label="First")

        result = _run(_handler({}, repo_root=common_dir))

        ids = [e["id"] for e in result["initiatives"]]
        assert ids == ["first", "second"]  # sorted by filename a-first < b-second

    def test_malformed_missing_id_skipped(self, tmp_path):
        """File without 'id' field is skipped; other files still served."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        ini_dir = repo_root / "state" / "initiatives"
        _seed_initiative(ini_dir, "bad.yaml", id_val=None, label="Has Label")
        _seed_initiative(ini_dir, "good.yaml", id_val="good", label="Good")

        result = _run(_handler({}, repo_root=common_dir))

        ids = [e["id"] for e in result["initiatives"]]
        assert ids == ["good"]

    def test_malformed_missing_label_skipped(self, tmp_path):
        """File without 'label' field is skipped; other files still served."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        ini_dir = repo_root / "state" / "initiatives"
        _seed_initiative(ini_dir, "nolabel.yaml", id_val="no-label", label=None)
        _seed_initiative(ini_dir, "valid.yaml", id_val="valid", label="Valid")

        result = _run(_handler({}, repo_root=common_dir))

        ids = [e["id"] for e in result["initiatives"]]
        assert ids == ["valid"]

    def test_invalid_status_coerced_to_null(self, tmp_path):
        """File with unrecognised status → status field is None in payload."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        ini_dir = repo_root / "state" / "initiatives"
        _seed_initiative(
            ini_dir,
            "weird-status.yaml",
            id_val="weird",
            label="Weird Status",
            status="complete",  # rejected canonical-3 value (not in canonical-4)
        )

        result = _run(_handler({}, repo_root=common_dir))

        entry = result["initiatives"][0]
        assert entry["status"] is None

    def test_valid_status_passed_through(self, tmp_path):
        """Each canonical-4 status value passes through unmodified."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        ini_dir = repo_root / "state" / "initiatives"

        for status in ("active", "paused", "shipped", "abandoned"):
            _seed_initiative(
                ini_dir,
                f"{status}.yaml",
                id_val=status,
                label=f"Status {status}",
                status=status,
            )

        result = _run(_handler({}, repo_root=common_dir))

        returned_statuses = {e["id"]: e["status"] for e in result["initiatives"]}
        for status in ("active", "paused", "shipped", "abandoned"):
            assert returned_statuses[status] == status

    def test_repo_root_none_returns_empty(self):  # Review: code-reviewer — no_ctx_repo_root fragment is a dead transition-time artifact; ctx fully stripped
        """repo_root=None → empty list without raising."""
        result = _run(_handler({}, repo_root=None))
        assert result == {"initiatives": []}

