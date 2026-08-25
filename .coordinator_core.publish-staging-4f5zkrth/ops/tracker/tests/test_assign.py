"""
Tests for coordinator_core.ops.tracker.assign — tracker.assign.

Coverage:
  (a) registration — tracker.assign lands in the live registry on import.
  (b) end-to-end add through the handler — writes an item_person_added
      event via tracker_entities.emit_item_person_added.
  (c) retract — the same triple, `retract: true`, writes an
      item_person_retracted event via emit_item_person_retracted.
  (d) invalid role — TrackerEntityError propagates, never swallowed.
  (e) duplicate-triple refusal (AC9) propagates from tracker_entities.
  (f) the write target is the LOCAL repo_root only (WRITE BOUND, DEC-11).
  (g) four-surface registry wiring + a command-type dispatch_message smoke.

Harness: asyncio.run() in sync test fns for handler-level tests — no
pytest-asyncio dependency.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

# ---- Import guard: fires @register_op side-effect for tracker.assign. ----
import coordinator_core.ops.tracker.assign  # noqa: F401

from coordinator_core.ipc import _REGISTRY, dispatch_message
from coordinator_core.op_scopes import _OP_KEY_SCOPE
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.ops import _EAGER_OP_MODULES
from coordinator_core.ops._registry_map import OP_MODULE_MAP
from coordinator_core.ops.tracker.assign import _handler
from coordinator_core.tracker_entities import TrackerEntityError

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

# Duplicated locally rather than imported — mirrors test_mint_person.py's
# "avoid a third referencer" discipline.
_EVENTS_DIR_RELPATH = "state/sovereign-tracker"
_EVENTS_SHARD_GLOB = "events.*.jsonl"


def _run(coro):
    return asyncio.run(coro)


def _make_git_repo(root: Path) -> Path:
    """Init a minimal git repository under *root* and return the repo root."""
    root.mkdir(parents=True, exist_ok=True)

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(root),
            capture_output=True,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "tracker-assign-test@claude-klabauter.test")
    _git("config", "user.name", "Tracker Assign Test")
    _git("config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "init")
    return root


def _shard_files(repo: Path):
    return sorted((repo / _EVENTS_DIR_RELPATH).glob(_EVENTS_SHARD_GLOB))


def _make_item(repo: Path, item_id: str = "itm-20260820-item-abc123-def456789012") -> str:
    """Seed a local item_created event so DEC-24's local-item check passes."""
    from coordinator_core.tracker_entities import emit_item_created

    emit_item_created(
        item_id,
        title="Item",
        body="body",
        created_at="2026-08-20T00:00:00+00:00",
        repo_root=repo,
    )
    return item_id


# ---------------------------------------------------------------------------
# (a) Import-guard floor assertion
# ---------------------------------------------------------------------------


def test_tracker_assign_registered():
    assert "tracker.assign" in _REGISTRY


# ---------------------------------------------------------------------------
# (b) End-to-end add
# ---------------------------------------------------------------------------


def test_handler_adds_item_person_edge(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)

    result = _run(
        _handler(
            {"item_id": item_id, "person_id": "person-1", "role": "assignee"},
            repo_root=repo,
        )
    )

    assert result == {
        "assigned": True,
        "reason": "added",
        "item_id": item_id,
        "person_id": "person-1",
        "role": "assignee",
    }
    assert _shard_files(repo)


# ---------------------------------------------------------------------------
# (c) Retract
# ---------------------------------------------------------------------------


def test_handler_retracts_item_person_edge(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)

    _run(
        _handler(
            {"item_id": item_id, "person_id": "person-1", "role": "assignee"},
            repo_root=repo,
        )
    )
    result = _run(
        _handler(
            {
                "item_id": item_id,
                "person_id": "person-1",
                "role": "assignee",
                "retract": True,
            },
            repo_root=repo,
        )
    )

    assert result == {
        "assigned": False,
        "reason": "retracted",
        "item_id": item_id,
        "person_id": "person-1",
        "role": "assignee",
    }


# ---------------------------------------------------------------------------
# (d) Invalid role propagates
# ---------------------------------------------------------------------------


def test_handler_invalid_role_raises(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)

    with pytest.raises(TrackerEntityError):
        _run(
            _handler(
                {"item_id": item_id, "person_id": "person-1", "role": "nope"},
                repo_root=repo,
            )
        )


# ---------------------------------------------------------------------------
# (e) Duplicate-triple refusal propagates (AC9)
# ---------------------------------------------------------------------------


def test_handler_duplicate_triple_raises(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)

    _run(
        _handler(
            {"item_id": item_id, "person_id": "person-1", "role": "assignee"},
            repo_root=repo,
        )
    )
    with pytest.raises(TrackerEntityError):
        _run(
            _handler(
                {"item_id": item_id, "person_id": "person-1", "role": "assignee"},
                repo_root=repo,
            )
        )


# ---------------------------------------------------------------------------
# (f) Write target is the LOCAL repo_root only (WRITE BOUND)
# ---------------------------------------------------------------------------


def test_handler_writes_only_the_local_repo_root(tmp_path):
    local_repo = _make_git_repo(tmp_path / "local")
    other_repo = _make_git_repo(tmp_path / "other")
    item_id = _make_item(local_repo)

    _run(
        _handler(
            {"item_id": item_id, "person_id": "person-1", "role": "assignee"},
            repo_root=local_repo,
        )
    )

    assert _shard_files(local_repo), "expected an event shard under the LOCAL repo"
    assert not (other_repo / _EVENTS_DIR_RELPATH).exists(), (
        "tracker.assign must never write into a different repo's own tree"
    )


# ---------------------------------------------------------------------------
# (g) Four-surface wiring + command-type smoke
# ---------------------------------------------------------------------------


def test_handler_repo_root_none_raises_runtime_error():
    with pytest.raises(RuntimeError):
        _run(_handler({}, repo_root=None))


def test_registered_in_registry_map():
    assert OP_MODULE_MAP.get("tracker.assign") == "coordinator_core.ops.tracker.assign"


def test_classified_mutating():
    assert OP_CLASSIFICATION.get("tracker.assign") is OpClass.MUTATING


def test_scoped_common_dir():
    assert _OP_KEY_SCOPE.get("tracker.assign") == "common_dir"


def test_eager_op_module_entry_present():
    eager_module_paths = [path for path, _note in _EAGER_OP_MODULES]
    assert "coordinator_core.ops.tracker.assign" in eager_module_paths


def test_command_type_smoke_resolves_non_none_repo_root(tmp_path):
    """Full dispatch_message() round trip — proves the op resolves a
    non-None repo_root end to end, per docs/wiki/coordinator-core-engine.md
    :266's warning that an op missing from op_scopes._OP_KEY_SCOPE silently
    degrades to repo_root=None."""
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)

    response = _run(
        dispatch_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tracker.assign",
                "params": {"item_id": item_id, "person_id": "person-1", "role": "watcher"},
                "_origin_worktree": str(repo),
            }
        )
    )

    assert "error" not in response, f"unexpected dispatch error: {response}"
    result = response["result"]
    assert result["assigned"] is True
    assert result["item_id"] == item_id
    assert _shard_files(repo), "expected the write to land under the dispatched repo"
