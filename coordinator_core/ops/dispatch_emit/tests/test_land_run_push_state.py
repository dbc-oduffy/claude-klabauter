"""A committing product land_run reply names the push.state readback; a non-committing one carries none."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import push_state as push_state_mod
from coordinator_core.ops.dispatch_emit import land_run

pytestmark = [pytest.mark.cadence]

PARAMS = {"run_id": "ask-20261126T000000-abcdef", "script_path": "run.mjs"}
READBACK = {"state": "pushed", "line": "pushed at aaaaaaaaaaaa"}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("coordinator_core.lifecycle.main_worktree_root", lambda repo_root: tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _lock_namespace(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    locks = tmp_path_factory.mktemp("locks")
    monkeypatch.setattr("coordinator_core.locked_write._machine_lock_dir", lambda: locks)


def _stub(monkeypatch: pytest.MonkeyPatch, reply: dict) -> None:
    monkeypatch.setattr(land_run, "_dispatch_mode", lambda mode, root, repo_root, params: dict(reply))
    monkeypatch.setattr(land_run, "_chain_landed_tranche", lambda root, params, reply: None)


def test_committed_reply_carries_the_push_readback(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {"committed": True, "sha": "a" * 40})
    monkeypatch.setattr(push_state_mod, "push_state", lambda root: dict(READBACK))

    reply = land_run._dispatch(dict(PARAMS), repo)

    assert reply["push_state"] == READBACK
    assert reply["push_line"] == "pushed at aaaaaaaaaaaa"


def test_pending_readback_is_stated_not_hidden(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {"committed": True, "sha": "a" * 40})
    monkeypatch.setattr(
        push_state_mod, "push_state", lambda root: {"state": "pending", "line": "pending: 1 commit, retrying"}
    )

    reply = land_run._dispatch(dict(PARAMS), repo)

    assert reply["push_line"] == "pending: 1 commit, retrying"


def test_uncommitted_reply_has_no_push_readback(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {"committed": False, "refused": "nothing"})
    monkeypatch.setattr(push_state_mod, "push_state", lambda root: pytest.fail("readback read without a commit"))

    reply = land_run._dispatch(dict(PARAMS), repo)

    assert "push_state" not in reply
    assert "push_line" not in reply


def test_readback_failure_never_fails_the_landing(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {"committed": True, "sha": "a" * 40})

    def boom(root: Path) -> dict:
        raise RuntimeError("ref read failed")

    monkeypatch.setattr(push_state_mod, "push_state", boom)

    reply = land_run._dispatch(dict(PARAMS), repo)

    assert reply["committed"] is True
    assert "push_line" not in reply
