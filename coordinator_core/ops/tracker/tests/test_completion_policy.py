"""
Tests for coordinator_core.ops.tracker.completion_policy —
tracker.assert_code_complete.

Coverage:
  (a) registration — tracker.assert_code_complete lands in the live registry
      on import.
  (b) handler-level: repo_root=None raises RuntimeError; item_id/sha/actor
      missing/blank raise ValueError; a genuine D3 params.repo_root mismatch
      returns the degraded {"asserted": False, "reason": ...} envelope
      (mirrors tracker.assign's shape, not tracker.render_status's raise —
      this op has a write-side "did not happen" envelope to report).
  (c) end-to-end assert — a thin smoke proving the handler actually appends
      a transition event through tracker_completion_policy.
      emit_code_complete_assert, both auto- and suggest-tier.
  (d) C11 — the op is wired across all FIVE op-registration surfaces this
      chunk's own body names (registry, classification, scope, module_map,
      `_EAGER_OP_MODULES`), proven with a command-type smoke: a full
      `dispatch_message()` round trip through `coordinator_core.ipc` that
      resolves a non-None `repo_root` for this op (per
      `state/lessons/2026-07-06-compute-only-op-registration-needs-an-op.yaml`
      — an op missing from `op_scopes._OP_KEY_SCOPE` silently degrades to
      `repo_root=None` even with a fully green in-process handler test).
  (e) classification — this op is `OpClass.MUTATING` per C2's ruling
      (applied here per C11's own body), on the merits (a real write).

Import-hygiene note (AC11): this file never imports the underlying
sovereign-tracker append/read module (directly or by dotted name) and never
writes its module name as a literal anywhere in this file — the op module
itself reaches the write only through `tracker_completion_policy` ->
`tracker_transitions`, and this test file follows the same discipline so it
does not become a third, unaffirmed referencer under `coordinator_core/ops/`.

Harness: asyncio.run() in sync test fns for handler-level tests — no
pytest-asyncio dependency.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

# ---- Import guard: fires @register_op side-effect for tracker.assert_code_complete. ----
import coordinator_core.ops.tracker.completion_policy  # noqa: F401

from coordinator_core.ipc import _REGISTRY, dispatch_message
from coordinator_core.op_scopes import _OP_KEY_SCOPE
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.ops import _EAGER_OP_MODULES
from coordinator_core.ops._registry_map import OP_MODULE_MAP
from coordinator_core.ops.tracker.completion_policy import _handler
from coordinator_core.tracker_entities import emit_item_created, mint_item_id


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
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "completion-policy-test@claude-klabauter.test")
    _git("config", "user.name", "Completion Policy Test")
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


# ---------------------------------------------------------------------------
# (a) Import-guard floor assertion
# ---------------------------------------------------------------------------


def test_tracker_assert_code_complete_registered():
    assert "tracker.assert_code_complete" in _REGISTRY


# ---------------------------------------------------------------------------
# (b) handler-level
# ---------------------------------------------------------------------------


def test_handler_repo_root_none_raises_runtime_error():
    with pytest.raises(RuntimeError):
        _run(
            _handler(
                {"item_id": "item-x", "sha": "abc123", "actor": "a"},
                repo_root=None,
            )
        )


def test_handler_missing_item_id_raises_value_error(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    with pytest.raises(ValueError):
        _run(_handler({"sha": "abc123", "actor": "a"}, repo_root=repo))


def test_handler_missing_sha_raises_value_error(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)
    with pytest.raises(ValueError):
        _run(_handler({"item_id": item_id, "actor": "a"}, repo_root=repo))


def test_handler_missing_actor_raises_value_error(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)
    with pytest.raises(ValueError):
        _run(_handler({"item_id": item_id, "sha": "abc123"}, repo_root=repo))


def test_handler_returns_degraded_envelope_on_mismatched_params_repo_root(tmp_path):
    """D3 consistency check (contract §3.3 doctrine): a genuinely MISMATCHED
    params.repo_root must be caught and fail closed by returning the
    degraded {"asserted": False, ...} envelope, mirroring tracker.assign's
    shape — this is a write op with an honest "did not happen" result to
    report, unlike the read-only tracker.render_status."""
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)
    bogus_other = tmp_path / "some-other-path-that-does-not-exist"
    result = _run(
        _handler(
            {
                "item_id": item_id,
                "sha": "abc123",
                "actor": "a",
                "repo_root": str(bogus_other),
            },
            repo_root=repo,
        )
    )
    assert result["asserted"] is False


# ---------------------------------------------------------------------------
# (c) assert smoke — full classifier truth table lives in
#     coordinator_core/tests/test_tracker_completion_policy.py
# ---------------------------------------------------------------------------


def test_handler_asserts_auto_tier_when_trailer_bound_and_reachable(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)

    result = _run(
        _handler(
            {
                "item_id": item_id,
                "sha": "deadbeef",
                "trailer_bound": True,
                "reachable_on_default_branch": True,
                "actor": "a",
            },
            repo_root=repo,
        )
    )
    assert result["axis"] == "code_complete"
    assert result["to_state"] == "asserted"
    assert result["tier"] == "auto"


def test_handler_asserts_suggest_tier_when_not_reachable(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)

    result = _run(
        _handler(
            {
                "item_id": item_id,
                "sha": "deadbeef",
                "trailer_bound": True,
                "reachable_on_default_branch": False,
                "actor": "a",
            },
            repo_root=repo,
        )
    )
    assert result["axis"] == "code_complete"
    assert result["to_state"] == "asserted"
    assert result["tier"] == "suggest"


# ---------------------------------------------------------------------------
# (d) five-surface wiring + command-type smoke (C11's own body: registry,
#     classification, scope, module_map, _EAGER_OP_MODULES)
# ---------------------------------------------------------------------------


def test_registered_in_registry_map():
    assert OP_MODULE_MAP.get("tracker.assert_code_complete") == (
        "coordinator_core.ops.tracker.completion_policy"
    )


def test_classified_mutating():
    assert OP_CLASSIFICATION.get("tracker.assert_code_complete") is OpClass.MUTATING


def test_scoped_common_dir():
    assert _OP_KEY_SCOPE.get("tracker.assert_code_complete") == "common_dir"


def test_eager_op_module_entry_present():
    eager_module_paths = [path for path, _note in _EAGER_OP_MODULES]
    assert "coordinator_core.ops.tracker.completion_policy" in eager_module_paths


def test_command_type_smoke_resolves_non_none_repo_root(tmp_path):
    """Full dispatch_message() round trip — proves the op resolves a non-None
    repo_root end to end, per docs/wiki/coordinator-core-engine.md:266's
    warning that an op missing from op_scopes._OP_KEY_SCOPE silently degrades
    to repo_root=None. A string-literal check across four files would NOT
    catch that degradation; only an actual dispatch does. AC5."""
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)

    response = _run(
        dispatch_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tracker.assert_code_complete",
                "params": {
                    "item_id": item_id,
                    "sha": "deadbeef",
                    "trailer_bound": True,
                    "reachable_on_default_branch": True,
                    "actor": "a",
                },
                "_origin_worktree": str(repo),
            }
        )
    )

    assert "error" not in response, f"unexpected dispatch error: {response}"
    result = response["result"]
    assert result["axis"] == "code_complete"
    assert result["to_state"] == "asserted"
    assert result["tier"] == "auto"
