"""
Tests for coordinator_core.ops.tracker.fold_observed_set — tracker.fold_observed_set.

Coverage:
  (a) registration — tracker.fold_observed_set lands in _REGISTRY on import.
  (b) run_fold_observed_set: opt-in-by-existence gate (no store -> ran:False,
      reason:"no_store"; store present -> delegates to the underlying
      per-machine fold).
  (c) handler-level: repo_root=None raises RuntimeError; worktree derivation via
      main_worktree_root (never params.repo_root).
  (d) AC14 — the op is wired across all FOUR op-registration surfaces, proven with
      a command-type smoke: a full dispatch_message() round trip through
      coordinator_core.ipc that resolves a non-None repo_root for this op (per
      docs/wiki/coordinator-core-engine.md:266's degradation warning — an op
      missing from op_scopes._OP_KEY_SCOPE silently resolves repo_root=None).

Import-hygiene note: this file deliberately imports `EVENTS_DIR_RELPATH` from
the `fold_observed_set` OP module (which legitimately re-exports it) rather
than from the underlying store module directly, and never patches the
underlying store module's machine-identity helper by name — both would add a
third referencer of the underlying store module's own dotted import path to
`coordinator_core/ops/`, which the sat-01 substrate's DR-241-affirmed
allowlist (see `coordinator_core/tests/`) forbids (exactly two referencers
are sanctioned: `fold_observed_set.py` itself and `session/boot_sweep.py`; a
test file is not exempt from that scan). Per-test machine-shard identity is
instead discovered dynamically by globbing the store directory after a fold,
never assumed or injected.

Harness: asyncio.run() in sync test fns for handler-level tests — no
pytest-asyncio dependency.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

# ---- Import guard: fires @register_op side-effect for tracker.fold_observed_set. ----
import coordinator_core.ops.tracker.fold_observed_set  # noqa: F401

from coordinator_core.ipc import _REGISTRY, dispatch_message
from coordinator_core.op_scopes import _OP_KEY_SCOPE
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.ops import _EAGER_OP_MODULES
from coordinator_core.ops._registry_map import OP_MODULE_MAP
from coordinator_core.ops.tracker.fold_observed_set import (
    EVENTS_DIR_RELPATH,
    EVENTS_SHARD_GLOB,
    _handler,
    run_fold_observed_set,
)


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
    _git("config", "user.email", "fold-observed-set-test@claude-klabauter.test")
    _git("config", "user.name", "Fold Observed Set Test")
    _git("config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "init")
    return root


def _shard_files(repo: Path):
    """Every per-machine shard file currently present under the store dir."""
    return sorted((repo / EVENTS_DIR_RELPATH).glob(EVENTS_SHARD_GLOB))


# ---------------------------------------------------------------------------
# (a) Import-guard floor assertion
# ---------------------------------------------------------------------------


def test_tracker_fold_observed_set_registered():
    assert "tracker.fold_observed_set" in _REGISTRY


# ---------------------------------------------------------------------------
# (b) run_fold_observed_set — opt-in-by-existence gate
# ---------------------------------------------------------------------------


def test_run_fold_observed_set_no_store_is_a_clean_skip(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    assert not (repo / EVENTS_DIR_RELPATH).is_dir()

    result = run_fold_observed_set(repo_root=repo)

    assert result == {"ran": False, "reason": "no_store", "marker": None}
    assert not (repo / EVENTS_DIR_RELPATH).exists(), (
        "run_fold_observed_set must never mint the store directory itself"
    )


def test_run_fold_observed_set_store_present_appends_marker(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    (repo / EVENTS_DIR_RELPATH).mkdir(parents=True)

    result = run_fold_observed_set(repo_root=repo)

    assert result["ran"] is True
    assert result["reason"] == "appended"
    assert result["marker"]["kind"] == "observed_set_fold"
    assert len(_shard_files(repo)) == 1, "exactly one own-shard file expected"


def test_run_fold_observed_set_idempotent_second_call_is_no_op(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    (repo / EVENTS_DIR_RELPATH).mkdir(parents=True)

    first = run_fold_observed_set(repo_root=repo)
    assert first["reason"] == "appended"

    second = run_fold_observed_set(repo_root=repo)
    assert second == {"ran": True, "reason": "no_op", "marker": None}

    shards = _shard_files(repo)
    assert len(shards) == 1
    lines = [l for l in shards[0].read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1, f"expected exactly ONE marker line, got {len(lines)}: {lines}"


# ---------------------------------------------------------------------------
# (c) handler-level
# ---------------------------------------------------------------------------


def test_handler_repo_root_none_raises_runtime_error():
    with pytest.raises(RuntimeError):
        _run(_handler({}, repo_root=None))


def test_handler_derives_worktree_from_common_dir_arg_not_params(tmp_path):
    """Per module docstring negative-spec: params.repo_root must NEVER be used as
    the path source — only the repo_root handler arg (via main_worktree_root).
    A CONSISTENT params.repo_root (resolving to the same common dir) must not
    block the fold — it is a D3 consistency check, not a path source."""
    repo = _make_git_repo(tmp_path / "repo")
    (repo / EVENTS_DIR_RELPATH).mkdir(parents=True)

    result = _run(
        _handler({"repo_root": str(repo)}, repo_root=repo / ".git")
    )
    assert result["ran"] is True
    assert result["reason"] == "appended"


def test_handler_fails_closed_on_mismatched_params_repo_root(tmp_path):
    """D3 consistency check (contract §3.3 doctrine): a genuinely MISMATCHED
    params.repo_root must be caught and fail closed — never silently
    proceed. Mirrors session.boot_sweep's and every fleet op's own
    check_repo_root guard (coordinator_core/ops/fleet/_common.py)."""
    repo = _make_git_repo(tmp_path / "repo")
    (repo / EVENTS_DIR_RELPATH).mkdir(parents=True)

    bogus_other = tmp_path / "some-other-path-that-does-not-exist"
    result = _run(
        _handler({"repo_root": str(bogus_other)}, repo_root=repo / ".git")
    )
    assert result["ran"] is False
    assert result["marker"] is None
    assert "repo_root-mismatch" in result["reason"]
    assert not _shard_files(repo), (
        "a rejected D3 mismatch must not append a marker"
    )


# ---------------------------------------------------------------------------
# (d) AC14 — four-surface wiring + command-type smoke
# ---------------------------------------------------------------------------


def test_registered_in_registry_map():
    assert OP_MODULE_MAP.get("tracker.fold_observed_set") == (
        "coordinator_core.ops.tracker.fold_observed_set"
    )


def test_classified_mutating():
    assert OP_CLASSIFICATION.get("tracker.fold_observed_set") is OpClass.MUTATING


def test_scoped_common_dir():
    assert _OP_KEY_SCOPE.get("tracker.fold_observed_set") == "common_dir"


def test_eager_op_module_entry_present():
    eager_module_paths = [path for path, _note in _EAGER_OP_MODULES]
    assert "coordinator_core.ops.tracker.fold_observed_set" in eager_module_paths


def test_command_type_smoke_resolves_non_none_repo_root(tmp_path):
    """Full dispatch_message() round trip — proves the op resolves a non-None
    repo_root end to end, per docs/wiki/coordinator-core-engine.md:266's warning
    that an op missing from op_scopes._OP_KEY_SCOPE silently degrades to
    repo_root=None. A string-literal check across four files would NOT catch
    that degradation; only an actual dispatch does."""
    repo = _make_git_repo(tmp_path / "repo")
    (repo / EVENTS_DIR_RELPATH).mkdir(parents=True)

    response = _run(
        dispatch_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tracker.fold_observed_set",
                "params": {},
                "_origin_worktree": str(repo),
            }
        )
    )

    assert "error" not in response, f"unexpected dispatch error: {response}"
    result = response["result"]
    assert result["ran"] is True
    assert result["reason"] == "appended"
