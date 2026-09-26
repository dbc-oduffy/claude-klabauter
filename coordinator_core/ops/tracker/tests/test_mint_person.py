"""
Tests for coordinator_core.ops.tracker.mint_person — tracker.mint_person.

Coverage:
  (a) registration — tracker.mint_person lands in the live registry on import.
  (b) end-to-end mint + resolve through the registered op (AC1).
  (c) idempotence — a second call against an already-minted person resolves
      the SAME person_id via the lock-free compare-and-retry path.
  (d) empty resolved bundle mints nothing (DEC-41).
  (e) the write target is the LOCAL repo_root, never a different repo's root
      (WRITE BOUND) — no per-repo write-target crossing.
  (f) a genuine concurrent collision (simulated by pre-seeding a colliding
      alias before this op's own mint attempt) retries via
      `tracker_projection.resolve_alias` rather than raising to the caller.
  (g) no second lock acquisition anywhere in the retry path.
  (h) AC1 — a wire-level `dispatch_message` smoke case with `_origin_worktree`
      set, confirming `tracker.mint_person`'s `_OP_KEY_SCOPE` entry resolves
      `repo_root` correctly end-to-end over the real command-type dispatch
      wire. In-process handler-test evidence alone does not discharge AC1.

Import-hygiene note: this file deliberately never imports the underlying
sovereign-tracker event-store module (directly OR by its dotted name) and
never writes its module name as a literal anywhere in this file — doing so
would add a THIRD, unaffirmed referencer under `coordinator_core/ops/` to
the DR-241-affirmed allowlist scan (see
`coordinator_core/ops/tracker/mint_person.py`'s own module docstring for why
this op's handler code avoids that reference too). Where this file needs the
on-disk event-store shard layout to assert against, it duplicates the two
small path constants locally (`_EVENTS_DIR_RELPATH` / `_EVENTS_SHARD_GLOB`)
rather than importing them, mirroring `test_fold_observed_set.py`'s own
"avoid a third referencer" discipline applied one level further.

Harness: asyncio.run() in sync test fns for handler-level tests — no
pytest-asyncio dependency.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import coordinator_core.ops.tracker.mint_person  # noqa: F401

from coordinator_core.ipc import _REGISTRY, dispatch_message
from coordinator_core.op_scopes import _OP_KEY_SCOPE
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.ops import _EAGER_OP_MODULES
from coordinator_core.ops._registry_map import OP_MODULE_MAP
from coordinator_core.ops.tracker.mint_person import _handler, _mint_person_core
from coordinator_core.tracker_entities import emit_person_alias_added, emit_person_created, mint_person_id
from coordinator_core.tracker_projection import fold_person_registry, resolve_alias

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_EVENTS_DIR_RELPATH = "state/sovereign-tracker"
_EVENTS_SHARD_GLOB = "events.*.jsonl"


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
    _git("config", "user.email", "mint-person-test@claude-klabauter.test")
    _git("config", "user.name", "Mint Person Test")
    _git("config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "init")
    return root


def _shard_files(repo: Path):
    return sorted((repo / _EVENTS_DIR_RELPATH).glob(_EVENTS_SHARD_GLOB))


_FIXTURE_BUNDLE = {
    "github": "dbc-example-operator",
    "github_id": "240204332",
    "display": "Dónal example-operator",
    "email": "dan@example.com",
}


def test_tracker_mint_person_registered():
    assert "tracker.mint_person" in _REGISTRY


def test_mint_person_core_mints_and_resolves(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")

    result = _mint_person_core(bundle=dict(_FIXTURE_BUNDLE), repo_root=repo)

    assert result["minted"] is True
    assert result["reason"] == "created"
    assert result["person_id"]

    registry = fold_person_registry(repo_root=repo)
    resolved = resolve_alias("github", "dbc-example-operator", registry=registry)
    assert resolved == result["person_id"]
    resolved_id = resolve_alias("github_id", "240204332", registry=registry)
    assert resolved_id == result["person_id"]

    assert resolve_alias("github", "240204332", registry=registry) is None
    resolved_email = resolve_alias("email", "dan@example.com", registry=registry)
    assert resolved_email == result["person_id"]
    resolved_display = resolve_alias("display", "Dónal example-operator", registry=registry)
    assert resolved_display == result["person_id"]


def test_mint_person_core_second_call_is_idempotent(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")

    first = _mint_person_core(bundle=dict(_FIXTURE_BUNDLE), repo_root=repo)
    assert first["reason"] == "created"

    second = _mint_person_core(bundle=dict(_FIXTURE_BUNDLE), repo_root=repo)
    assert second["minted"] is True
    assert second["reason"] == "collision_resolved"
    assert second["person_id"] == first["person_id"]


def test_mint_person_core_empty_bundle_is_a_clean_no_op(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")

    result = _mint_person_core(bundle={}, repo_root=repo)

    assert result == {"minted": False, "reason": "empty_bundle", "person_id": None}
    assert not _shard_files(repo)


def test_mint_person_core_writes_only_the_local_repo_root(tmp_path):
    local_repo = _make_git_repo(tmp_path / "local")
    other_repo = _make_git_repo(tmp_path / "other")

    result = _mint_person_core(bundle=dict(_FIXTURE_BUNDLE), repo_root=local_repo)

    assert result["minted"] is True
    assert _shard_files(local_repo), "expected an event shard under the LOCAL repo"
    assert not (other_repo / _EVENTS_DIR_RELPATH).exists(), (
        "mint_person must never write into a different repo's own tree"
    )


def test_mint_person_core_collision_retries_via_resolve_alias(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")

    winner_id = mint_person_id()
    emit_person_created(winner_id, display_name="Winner", repo_root=repo)
    emit_person_alias_added(winner_id, "github", "dbc-example-operator", repo_root=repo)

    result = _mint_person_core(bundle=dict(_FIXTURE_BUNDLE), repo_root=repo)

    assert result["minted"] is True
    assert result["reason"] == "collision_resolved"
    assert result["person_id"] == winner_id


def test_mint_person_core_collision_on_non_github_alias_resolves_via_that_alias(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")

    winner_id = mint_person_id()
    emit_person_created(winner_id, display_name="Winner", repo_root=repo)
    emit_person_alias_added(winner_id, "email", "dan@example.com", repo_root=repo)

    bundle_without_github = {"email": "dan@example.com"}
    result = _mint_person_core(bundle=bundle_without_github, repo_root=repo)

    assert result["minted"] is True
    assert result["reason"] == "collision_resolved"
    assert result["person_id"] == winner_id


def test_mint_person_core_collision_on_later_alias_resolves_to_true_winner(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")

    winner_id = mint_person_id()
    emit_person_created(winner_id, display_name="Someone Else", repo_root=repo)
    emit_person_alias_added(winner_id, "github_id", "240204332", repo_root=repo)

    result = _mint_person_core(bundle=dict(_FIXTURE_BUNDLE), repo_root=repo)

    assert result["minted"] is True
    assert result["reason"] == "collision_resolved"
    assert result["person_id"] == winner_id

    registry = fold_person_registry(repo_root=repo)
    orphan_id = resolve_alias("github", "dbc-example-operator", registry=registry)
    assert orphan_id is not None
    assert orphan_id != winner_id


def test_mint_person_core_collision_on_person_created_reraises(tmp_path, monkeypatch):
    from coordinator_core.tracker_entities import TrackerEntityError

    repo = _make_git_repo(tmp_path / "repo")

    def _raise(*args, **kwargs):
        raise TrackerEntityError("simulated failure")

    monkeypatch.setattr(
        "coordinator_core.ops.tracker.mint_person.emit_person_created", _raise
    )

    with pytest.raises(TrackerEntityError):
        _mint_person_core(bundle=dict(_FIXTURE_BUNDLE), repo_root=repo)


def test_mint_person_core_retry_path_acquires_no_additional_lock(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")

    winner_id = mint_person_id()
    emit_person_created(winner_id, display_name="Winner", repo_root=repo)
    emit_person_alias_added(winner_id, "github", "dbc-example-operator", repo_root=repo)

    with mock.patch(
        "coordinator_core.ops.tracker.mint_person.fold_person_registry",
        side_effect=fold_person_registry,
    ) as spied_fold:
        result = _mint_person_core(bundle=dict(_FIXTURE_BUNDLE), repo_root=repo)

    assert result["reason"] == "collision_resolved"
    assert spied_fold.call_count == 1


def test_handler_repo_root_none_raises_runtime_error():
    with pytest.raises(RuntimeError):
        _run(_handler({}, repo_root=None))


def test_registered_in_registry_map():
    assert OP_MODULE_MAP.get("tracker.mint_person") == (
        "coordinator_core.ops.tracker.mint_person"
    )


def test_classified_mutating():
    assert OP_CLASSIFICATION.get("tracker.mint_person") is OpClass.MUTATING


def test_scoped_common_dir():
    assert _OP_KEY_SCOPE.get("tracker.mint_person") == "common_dir"


def test_eager_op_module_entry_present():
    eager_module_paths = [path for path, _note in _EAGER_OP_MODULES]
    assert "coordinator_core.ops.tracker.mint_person" in eager_module_paths


def test_command_type_smoke_resolves_non_none_repo_root(tmp_path, monkeypatch):
    """Full dispatch_message() round trip — proves the op resolves a non-None
    repo_root end to end, per docs/wiki/coordinator-core-engine.md:266's
    warning that an op missing from op_scopes._OP_KEY_SCOPE silently
    degrades to repo_root=None. AC1 is explicit that in-process handler-test
    evidence alone does not discharge it — only an actual dispatch does."""
    repo = _make_git_repo(tmp_path / "repo")

    monkeypatch.setattr(
        "coordinator_core.ops.tracker.mint_person.resolve_operating_person",
        lambda: dict(_FIXTURE_BUNDLE),
    )

    response = _run(
        dispatch_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tracker.mint_person",
                "params": {},
                "_origin_worktree": str(repo),
            }
        )
    )

    assert "error" not in response, f"unexpected dispatch error: {response}"
    result = response["result"]
    assert result["minted"] is True
    assert result["reason"] == "created"
    assert result["person_id"]
    assert _shard_files(repo), "expected the write to land under the dispatched repo"
