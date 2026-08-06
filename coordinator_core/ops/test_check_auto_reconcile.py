"""Tests for coordinator_core.ops.check_auto_reconcile.

Golden oracle (Port of: check-auto-reconcile.sh, example-doctrine-repo b5a4192c, 2026-07-20),
snapshotted 2026-07-16 -- this module owns only the dispatch slice (repo-root
resolution + in-process handoff.reconcile_open call); the envelope-parsing and
rendering slice stays example-doctrine-repo-side (see that repo's own bin test), so these tests
cover get_response()'s infrastructure-failure silent-skip contract and main()'s
stdout passthrough, not rendering.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops import check_auto_reconcile


def test_get_response_returns_none_when_repo_root_unresolvable(monkeypatch):
    monkeypatch.setattr(
        check_auto_reconcile, "_resolve_own_repo_root", lambda: None
    )
    assert check_auto_reconcile.get_response() is None


def test_get_response_returns_none_on_dispatch_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(
        check_auto_reconcile, "_resolve_own_repo_root", lambda: tmp_path
    )

    async def _boom(msg):
        raise RuntimeError("dispatch exploded")

    monkeypatch.setattr(
        "coordinator_core.invoke.dispatch.dispatch_message", _boom
    )
    assert check_auto_reconcile.get_response() is None


def test_get_response_returns_envelope_on_success(monkeypatch, tmp_path):
    monkeypatch.setattr(
        check_auto_reconcile, "_resolve_own_repo_root", lambda: tmp_path
    )

    expected = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "reconciled": [],
            "gates_cleared": [],
            "surfaced": [{"handoff_id": "h-001", "reason": "r", "evidence": "e"}],
            "exit_code": 0,
        },
    }

    async def _fake_dispatch(msg):
        assert msg["method"] == "handoff.reconcile_open"
        assert msg["params"] == {}
        assert msg["_origin_worktree"] == str(tmp_path)
        return expected

    monkeypatch.setattr(
        "coordinator_core.invoke.dispatch.dispatch_message", _fake_dispatch
    )
    assert check_auto_reconcile.get_response() == expected


def test_main_returns_1_and_prints_nothing_when_response_is_none(monkeypatch, capsys):
    monkeypatch.setattr(check_auto_reconcile, "get_response", lambda: None)
    rc = check_auto_reconcile.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert out == ""


def test_main_prints_response_json_and_returns_0(monkeypatch, capsys):
    envelope = {"jsonrpc": "2.0", "id": 1, "result": {"surfaced": []}}
    monkeypatch.setattr(check_auto_reconcile, "get_response", lambda: envelope)
    rc = check_auto_reconcile.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out) == envelope


def test_resolve_own_repo_root_returns_none_outside_git_repo(tmp_path, monkeypatch):
    def _raise(cwd=None):
        raise RuntimeError("not a git repo")

    monkeypatch.setattr(
        "coordinator_core.lifecycle.find_repo_root", _raise
    )
    assert check_auto_reconcile._resolve_own_repo_root() is None


def test_resolve_own_repo_root_targets_invoking_repo_not_claude_klabauter(
    tmp_path, monkeypatch
):
    """Regression for the mistargeted-corpus defect: resolution MUST follow
    the invoking process's cwd, never this module's own on-disk location
    (always inside claude-klabauter's checkout) -- see _resolve_own_repo_root()'s
    docstring and this module's negative-spec.
    """
    import subprocess

    other_repo = tmp_path / "sibling-repo"
    other_repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=other_repo, check=True)

    monkeypatch.chdir(other_repo)
    resolved = check_auto_reconcile._resolve_own_repo_root()

    assert resolved is not None
    assert resolved == other_repo.resolve()

    from pathlib import Path as _Path

    module_own_checkout = _Path(check_auto_reconcile.__file__).resolve().parents[2]
    assert resolved != module_own_checkout


def test_get_response_stamps_origin_worktree_from_cwd_not_module_location(
    tmp_path, monkeypatch
):
    """End-to-end: get_response()'s _origin_worktree must be the invoking
    repo (cwd), never derived from Path(__file__) (which always resolves
    inside claude-klabauter's own checkout).
    """
    import subprocess

    other_repo = tmp_path / "another-caller-repo"
    other_repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=other_repo, check=True)
    monkeypatch.chdir(other_repo)

    seen: dict = {}

    async def _fake_dispatch(msg):
        seen["origin_worktree"] = msg["_origin_worktree"]
        return {"jsonrpc": "2.0", "id": 1, "result": {"surfaced": []}}

    monkeypatch.setattr(
        "coordinator_core.invoke.dispatch.dispatch_message", _fake_dispatch
    )
    assert check_auto_reconcile.get_response() is not None
    assert seen["origin_worktree"] == str(other_repo.resolve())

    from pathlib import Path as _Path

    module_own_checkout = str(_Path(check_auto_reconcile.__file__).resolve().parents[2])
    assert seen["origin_worktree"] != module_own_checkout
