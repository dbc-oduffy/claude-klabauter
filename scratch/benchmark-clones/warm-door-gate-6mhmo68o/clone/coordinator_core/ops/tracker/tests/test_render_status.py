"""
Tests for coordinator_core.ops.tracker.render_status — tracker.render_status.

Coverage:
  (a) registration — tracker.render_status lands in the live registry on import.
  (b) handler-level: repo_root=None raises RuntimeError; item_id missing/blank
      raises ValueError; a genuine D3 params.repo_root mismatch raises
      ValueError (no honest "skipped" envelope exists for a read).
  (c) end-to-end open/closed round trip through the registered handler — a
      thin smoke over the same truth table `coordinator_core/tests/
      test_tracker_projection.py` already covers exhaustively; this file does
      not re-derive that table.
  (d) C3 — the op is wired across all FIVE op-registration surfaces this
      chunk's own body names (registry, classification, scope, module_map,
      `_EAGER_OP_MODULES`), proven with a command-type smoke: a full
      `dispatch_message()` round trip through `coordinator_core.ipc` that
      resolves a non-None `repo_root` for this op (per
      `state/lessons/2026-07-06-compute-only-op-registration-needs-an-op.yaml`
      — an op missing from `op_scopes._OP_KEY_SCOPE` silently degrades to
      `repo_root=None` even with a fully green in-process handler test).
  (e) classification — this op is `OpClass.MUTATING` per C2's ruling, not
      `COMPUTE_ONLY` (see `render_status.py`'s own module docstring).

Import-hygiene note: this file never imports the underlying sovereign-tracker
append/read module (directly or by dotted name) and never writes its module
name as a literal anywhere in this file — the op module itself reaches the
fold only through `tracker_projection`, and this test file follows the same
discipline so it does not become a third, unaffirmed referencer under
`coordinator_core/ops/` (see `render_status.py`'s own module docstring, and
`test_mint_person.py`'s identical "Import-hygiene note").

Harness: asyncio.run() in sync test fns for handler-level tests — no
pytest-asyncio dependency.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

# ---- Import guard: fires @register_op side-effect for tracker.render_status. ----
import coordinator_core.ops.tracker.render_status  # noqa: F401

from coordinator_core.ipc import _REGISTRY, dispatch_message
from coordinator_core.op_scopes import _OP_KEY_SCOPE
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.ops import _EAGER_OP_MODULES
from coordinator_core.ops._registry_map import OP_MODULE_MAP
from coordinator_core.ops.tracker.render_status import _handler
from coordinator_core.tracker_entities import emit_item_created, mint_item_id
from coordinator_core import tracker_transitions as tt

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


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
    _git("config", "user.email", "render-status-test@claude-klabauter.test")
    _git("config", "user.name", "Render Status Test")
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


def test_tracker_render_status_registered():
    assert "tracker.render_status" in _REGISTRY


# ---------------------------------------------------------------------------
# (b) handler-level
# ---------------------------------------------------------------------------


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
    """D3 consistency check (contract §3.3 doctrine): a genuinely MISMATCHED
    params.repo_root must be caught and fail closed — never silently
    proceed. There is no honest degraded envelope for a read op, so this
    raises rather than returning a "skipped" result (unlike the sibling
    mutating ops in this package)."""
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)
    bogus_other = tmp_path / "some-other-path-that-does-not-exist"
    with pytest.raises(ValueError):
        _run(_handler({"item_id": item_id, "repo_root": str(bogus_other)}, repo_root=repo))


def test_handler_derives_worktree_from_common_dir_arg_not_params(tmp_path):
    """Per module docstring negative-spec: params.repo_root must NEVER be used
    as the path source — only the repo_root handler arg (via
    main_worktree_root). A CONSISTENT params.repo_root (resolving to the same
    common dir) must not block the read — it is a D3 consistency check, not a
    path source."""
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)

    result = _run(
        _handler({"item_id": item_id, "repo_root": str(repo)}, repo_root=repo / ".git")
    )
    assert result == {"item_id": item_id, "status": "open"}


# ---------------------------------------------------------------------------
# (c) open/closed thin smoke — full truth table lives in
#     coordinator_core/tests/test_tracker_projection.py
# ---------------------------------------------------------------------------


def test_handler_no_events_reads_open(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)

    result = _run(_handler({"item_id": item_id}, repo_root=repo))
    assert result == {"item_id": item_id, "status": "open"}


def test_handler_manual_close_reads_closed(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)
    tt.emit_transition(
        item_id, "manual_close", "closed", actor="a", tier="direct", repo_root=repo
    )

    result = _run(_handler({"item_id": item_id}, repo_root=repo))
    assert result == {"item_id": item_id, "status": "closed"}


# ---------------------------------------------------------------------------
# (d) five-surface wiring + command-type smoke (C3's own body: registry,
#     classification, scope, module_map, _EAGER_OP_MODULES)
# ---------------------------------------------------------------------------


def test_registered_in_registry_map():
    assert OP_MODULE_MAP.get("tracker.render_status") == (
        "coordinator_core.ops.tracker.render_status"
    )


def test_classified_mutating():
    assert OP_CLASSIFICATION.get("tracker.render_status") is OpClass.MUTATING


def test_scoped_common_dir():
    assert _OP_KEY_SCOPE.get("tracker.render_status") == "common_dir"


def test_eager_op_module_entry_present():
    eager_module_paths = [path for path, _note in _EAGER_OP_MODULES]
    assert "coordinator_core.ops.tracker.render_status" in eager_module_paths


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
                "method": "tracker.render_status",
                "params": {"item_id": item_id},
                "_origin_worktree": str(repo),
            }
        )
    )

    assert "error" not in response, f"unexpected dispatch error: {response}"
    result = response["result"]
    assert result == {"item_id": item_id, "status": "open"}
