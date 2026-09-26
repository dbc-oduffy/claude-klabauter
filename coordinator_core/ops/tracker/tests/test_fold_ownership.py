
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.tracker.fold_ownership  # noqa: F401

from coordinator_core.ipc import _REGISTRY, dispatch_message
from coordinator_core.op_scopes import _OP_KEY_SCOPE
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.ops import _EAGER_OP_MODULES
from coordinator_core.ops._registry_map import OP_MODULE_MAP
from coordinator_core.ops.tracker.fold_ownership import _handler
from coordinator_core.tracker_entities import (
    emit_item_created,
    emit_item_person_added,
    emit_item_person_retracted,
    emit_person_alias_added,
    emit_person_created,
    emit_person_merged,
    mint_item_id,
    mint_person_id,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _run(coro):
    return asyncio.run(coro)


def _make_git_repo(root: Path) -> Path:
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
    _git("config", "user.email", "fold-ownership-test@claude-klabauter.test")
    _git("config", "user.name", "Fold Ownership Test")
    _git("config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "init")
    return root


def _make_item(repo_root: Path, *, title: str = "Widget", body: str = "Do the thing") -> str:
    item_id = mint_item_id(title, body, "2026-08-18T10:00:00.000000Z")
    emit_item_created(
        item_id,
        title=title,
        body=body,
        created_at="2026-08-18T10:00:00.000000Z",
        repo_root=repo_root,
    )
    return item_id


def test_tracker_fold_ownership_registered():
    assert "tracker.fold_ownership" in _REGISTRY


def test_handler_repo_root_none_raises_runtime_error():
    with pytest.raises(RuntimeError):
        _run(_handler({"item_id": "item-x"}, repo_root=None))


def test_handler_missing_item_id_raises_value_error(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    with pytest.raises(ValueError):
        _run(_handler({}, repo_root=repo))


def test_handler_blank_item_id_raises_value_error(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    with pytest.raises(ValueError):
        _run(_handler({"item_id": "   "}, repo_root=repo))


def test_handler_fails_closed_on_mismatched_params_repo_root(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)
    bogus_other = tmp_path / "some-other-path-that-does-not-exist"
    with pytest.raises(ValueError):
        _run(_handler({"item_id": item_id, "repo_root": str(bogus_other)}, repo_root=repo))


def test_handler_derives_worktree_from_common_dir_arg_not_params(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)

    result = _run(
        _handler({"item_id": item_id, "repo_root": str(repo)}, repo_root=repo / ".git")
    )
    assert result == {"item_id": item_id, "owners": []}


def test_no_edge_returns_explicit_unowned_empty_answer(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)

    result = _run(_handler({"item_id": item_id}, repo_root=repo))
    assert result == {"item_id": item_id, "owners": []}


def test_retraction_removes_the_edge(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)

    emit_item_person_added(item_id, "person-1", "assignee", repo_root=repo)
    emit_item_person_retracted(item_id, "person-1", "assignee", repo_root=repo)

    result = _run(_handler({"item_id": item_id}, repo_root=repo))
    assert result == {"item_id": item_id, "owners": []}


def test_added_edge_surfaces_person_id_and_role(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)

    emit_item_person_added(item_id, "person-1", "assignee", repo_root=repo)

    result = _run(_handler({"item_id": item_id}, repo_root=repo))
    assert result == {
        "item_id": item_id,
        "owners": [
            {"person_id": "person-1", "role": "assignee", "contributor_slug": None},
        ],
    }


def test_person_merged_resolves_losing_id_to_winner(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)

    from_id = mint_person_id()
    into_id = mint_person_id()
    emit_person_created(from_id, display_name="Losing", repo_root=repo)
    emit_person_created(into_id, display_name="Winning", repo_root=repo)
    emit_item_person_added(item_id, from_id, "assignee", repo_root=repo)
    emit_person_merged(from_id, into_id, "actor", repo_root=repo)

    result = _run(_handler({"item_id": item_id}, repo_root=repo))
    assert result == {
        "item_id": item_id,
        "owners": [
            {"person_id": into_id, "role": "assignee", "contributor_slug": None},
        ],
    }


def test_null_person_id_edge_folds_with_null_slug(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)

    emit_item_person_added(item_id, None, "watcher", repo_root=repo)

    result = _run(_handler({"item_id": item_id}, repo_root=repo))
    assert result == {
        "item_id": item_id,
        "owners": [
            {"person_id": None, "role": "watcher", "contributor_slug": None},
        ],
    }


def test_contributor_slug_resolved_from_github_id_alias(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)

    person_id = mint_person_id()
    emit_person_created(person_id, display_name="Owner", repo_root=repo)
    emit_person_alias_added(person_id, "github_id", "12345", repo_root=repo)
    emit_item_person_added(item_id, person_id, "assignee", repo_root=repo)

    result = _run(_handler({"item_id": item_id}, repo_root=repo))
    owners = result["owners"]
    assert len(owners) == 1
    assert owners[0]["person_id"] == person_id
    assert owners[0]["role"] == "assignee"
    assert owners[0]["contributor_slug"] is not None
    assert isinstance(owners[0]["contributor_slug"], str)


def test_contributor_slug_null_when_no_github_id_alias_edge_still_returned(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)

    person_id = mint_person_id()
    emit_person_created(person_id, display_name="Owner", repo_root=repo)
    emit_item_person_added(item_id, person_id, "assignee", repo_root=repo)

    result = _run(_handler({"item_id": item_id}, repo_root=repo))
    assert result == {
        "item_id": item_id,
        "owners": [
            {"person_id": person_id, "role": "assignee", "contributor_slug": None},
        ],
    }


def test_registered_in_registry_map():
    assert OP_MODULE_MAP.get("tracker.fold_ownership") == (
        "coordinator_core.ops.tracker.fold_ownership"
    )


def test_classified_mutating():
    assert OP_CLASSIFICATION.get("tracker.fold_ownership") is OpClass.MUTATING


def test_scoped_common_dir():
    assert _OP_KEY_SCOPE.get("tracker.fold_ownership") == "common_dir"


def test_eager_op_module_entry_present():
    eager_module_paths = [path for path, _note in _EAGER_OP_MODULES]
    assert "coordinator_core.ops.tracker.fold_ownership" in eager_module_paths


def test_command_type_smoke_resolves_non_none_repo_root(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)
    emit_item_person_added(item_id, "person-1", "assignee", repo_root=repo)

    response = _run(
        dispatch_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tracker.fold_ownership",
                "params": {"item_id": item_id},
                "_origin_worktree": str(repo),
            }
        )
    )

    assert "error" not in response, f"unexpected dispatch error: {response}"
    result = response["result"]
    assert result["item_id"] == item_id
    assert result["owners"] == [
        {"person_id": "person-1", "role": "assignee", "contributor_slug": None}
    ]
