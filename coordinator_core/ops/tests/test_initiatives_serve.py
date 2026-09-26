
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import pytest

import coordinator_core.ops  # noqa: F401 — populates _REGISTRY

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.initiatives_serve import _handler
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


assert len(_REGISTRY) > 0, (
    "registry is empty after 'import coordinator_core.ops' — "
    "all @register_op decorators must have fired at module import time"
)

_OP_NAME = "initiative.serve_set"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.initiatives_serve @register_op did not fire"
)


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
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(root),
        capture_output=True,
        check=True, **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "config", "user.email", "serve-test@claude-klabauter.test"],
        cwd=str(root),
        capture_output=True,
        check=True, **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "config", "user.name", "Serve Test"],
        cwd=str(root),
        capture_output=True,
        check=True, **no_console_creationflags(),
    )
    return (root / ".git").resolve()


class TestRegistryCompleteness:

    def test_registry_is_non_empty(self):
        assert len(_REGISTRY) > 0

    def test_op_name_registered(self):
        """'initiative.serve_set' is present in _REGISTRY."""
        assert "initiative.serve_set" in _REGISTRY


class TestInitiativeServeSet:

    def test_empty_store_directory_absent(self, tmp_path):
        common_dir = _make_git_repo(tmp_path / "repo")
        result = _handler({}, repo_root=common_dir)
        assert result == {"initiatives": []}

    def test_empty_store_directory_present_but_empty(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        (repo_root / "state" / "initiatives").mkdir(parents=True)
        result = _handler({}, repo_root=common_dir)
        assert result == {"initiatives": []}

    def test_single_active_ongoing_initiative(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        ini_dir = repo_root / "state" / "initiatives"
        _seed_initiative(ini_dir, "python-core.yaml", id_val="python-core", label="Python Core")

        result = _handler({}, repo_root=common_dir)

        assert len(result["initiatives"]) == 1
        entry = result["initiatives"][0]
        assert entry["id"] == "python-core"
        assert entry["label"] == "Python Core"
        assert entry["status"] == "active"
        assert entry["target_date"] is None
        assert entry["shape"] == "ongoing"

    def test_completion_shape_when_target_date_set(self, tmp_path):
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

        result = _handler({}, repo_root=common_dir)

        assert len(result["initiatives"]) == 1
        entry = result["initiatives"][0]
        assert entry["id"] == "fleet-deliverable-spine"
        assert entry["target_date"] == "2026-09-30"
        assert entry["shape"] == "completion"

    def test_ongoing_shape_when_target_date_null(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        ini_dir = repo_root / "state" / "initiatives"
        _seed_initiative(
            ini_dir,
            "claude-klabauter-strangler.yaml",
            id_val="claude-klabauter-strangler",
            label="Claude-Klabauter Strangler",
            target_date=None,
        )

        result = _handler({}, repo_root=common_dir)

        entry = result["initiatives"][0]
        assert entry["target_date"] is None
        assert entry["shape"] == "ongoing"

    def test_multiple_initiatives_sorted_by_filename(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        ini_dir = repo_root / "state" / "initiatives"
        _seed_initiative(ini_dir, "b-second.yaml", id_val="second", label="Second")
        _seed_initiative(ini_dir, "a-first.yaml", id_val="first", label="First")

        result = _handler({}, repo_root=common_dir)

        ids = [e["id"] for e in result["initiatives"]]
        assert ids == ["first", "second"]

    def test_malformed_missing_id_skipped(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        ini_dir = repo_root / "state" / "initiatives"
        _seed_initiative(ini_dir, "bad.yaml", id_val=None, label="Has Label")
        _seed_initiative(ini_dir, "good.yaml", id_val="good", label="Good")

        result = _handler({}, repo_root=common_dir)

        ids = [e["id"] for e in result["initiatives"]]
        assert ids == ["good"]

    def test_malformed_missing_label_skipped(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        ini_dir = repo_root / "state" / "initiatives"
        _seed_initiative(ini_dir, "nolabel.yaml", id_val="no-label", label=None)
        _seed_initiative(ini_dir, "valid.yaml", id_val="valid", label="Valid")

        result = _handler({}, repo_root=common_dir)

        ids = [e["id"] for e in result["initiatives"]]
        assert ids == ["valid"]

    def test_invalid_status_coerced_to_null(self, tmp_path):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        ini_dir = repo_root / "state" / "initiatives"
        _seed_initiative(
            ini_dir,
            "weird-status.yaml",
            id_val="weird",
            label="Weird Status",
            status="complete",
        )

        result = _handler({}, repo_root=common_dir)

        entry = result["initiatives"][0]
        assert entry["status"] is None

    def test_valid_status_passed_through(self, tmp_path):
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

        result = _handler({}, repo_root=common_dir)

        returned_statuses = {e["id"]: e["status"] for e in result["initiatives"]}
        for status in ("active", "paused", "shipped", "abandoned"):
            assert returned_statuses[status] == status

    def test_repo_root_none_returns_empty(self):
        result = _handler({}, repo_root=None)
        assert result == {"initiatives": []}

