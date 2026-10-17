"""Tests for ``dispatch.supersede_slice``: marker write, idempotence, refusals, registry resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import slice_supersede
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import read_superseded_by, status_rel

HELD = "ask-20261016T152609-db6c90"
BY = "ask-20261017T033957-569176"


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(slice_supersede, "main_worktree_root", lambda p: Path(p))
    return tmp_path


def _call(root: Path, **params) -> dict:
    return slice_supersede._handler(params, repo_root=root)


def test_marks_a_held_run(root):
    reply = _call(root, run_id=HELD, by=BY)
    assert reply == {"ok": True, "run_id": HELD, "superseded_by": BY, "written": True}
    assert read_superseded_by(root, HELD) == BY


def test_same_by_is_idempotent(root):
    _call(root, run_id=HELD, by=BY)
    reply = _call(root, run_id=HELD, by=BY)
    assert reply["ok"] is True and reply["written"] is False


def test_different_by_is_refused(root):
    _call(root, run_id=HELD, by=BY)
    reply = _call(root, run_id=HELD, by="ask-other")
    assert reply["ok"] is False and BY in reply["error"]
    assert read_superseded_by(root, HELD) == BY


@pytest.mark.parametrize("state", ["landed", "running"])
def test_landed_or_running_run_is_refused(root, state):
    status = root / status_rel(HELD)
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"status": state}), encoding="utf-8")
    reply = _call(root, run_id=HELD, by=BY)
    assert reply["ok"] is False and state in reply["error"]
    assert read_superseded_by(root, HELD) is None


@pytest.mark.parametrize("bad", ["../escape", "a/b", ""])
def test_unsafe_id_is_refused(root, bad):
    assert _call(root, run_id=bad, by=BY)["ok"] is False
    assert _call(root, run_id=HELD, by=bad)["ok"] is False


def test_self_supersession_is_refused(root):
    assert _call(root, run_id=HELD, by=HELD)["ok"] is False


def test_missing_params_are_refused(root):
    assert _call(root, run_id=HELD)["ok"] is False


def test_op_is_resolvable_through_the_registry():
    from coordinator_core.ipc import get_op_handler

    assert get_op_handler(slice_supersede.OP_NAME) is not None
