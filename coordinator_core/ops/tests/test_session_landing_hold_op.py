"""session.landing_hold: registration, set/status/clear, and the end-to-end defer-then-replay path."""

from __future__ import annotations

import pytest

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.op_scopes import OP_KEY_SCOPE
from coordinator_core.ops import _registry_map
from coordinator_core.ops import session_landing_hold as op
from coordinator_core.ops.dispatch_emit import checkpoint_stamp
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_checkpoint_stamp import (
    PLAN,
    _git as _git_cp,
    _message,
    _repo,
    _rows,
)
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    RUN,
    _call,
    _git,
    _write_record,
    repo,
)
from coordinator_core.session import landing_hold

SID = "11111111-2222-4333-8444-555555555555"


def _op(root, **params):
    return op._session_landing_hold(params, repo_root=root / ".git")


def test_registration_quad():
    assert ipc._REGISTRY.get("session.landing_hold") is not None
    assert _registry_map.OP_MODULE_MAP["session.landing_hold"] == "coordinator_core.ops.session_landing_hold"
    assert OP_KEY_SCOPE["session.landing_hold"] == "common_dir"
    assert OP_CLASSIFICATION["session.landing_hold"] is OpClass.MUTATING


def test_invalid_params_return_error(tmp_path):
    assert "error" in _op(tmp_path, action="bogus", session_id=SID)
    assert "error" in _op(tmp_path, action="set")
    assert "error" in _op(tmp_path, action="set", session_id="../x")
    assert "error" in _op(tmp_path, action="clear", session_id=5)


@pytest.mark.spawns_process
def test_set_status_clear_without_deferrals(repo):
    assert _op(repo, action="status", session_id=SID) == {"action": "status", "held": False, "deferred_count": 0}
    assert _op(repo, action="set", session_id=SID) == {"action": "set", "held": True}
    assert landing_hold.is_held(str(repo), SID)
    assert _op(repo, action="status", session_id=SID)["held"] is True
    out = _op(repo, action="clear", session_id=SID)
    assert out == {"action": "clear", "cleared": True, "replayed": [], "failed": []}
    assert not landing_hold.is_held(str(repo), SID)


@pytest.mark.spawns_process
def test_clear_replays_deferred_halted_record(repo):
    _write_record(repo, RUN)
    head = _git(["rev-parse", "HEAD"], repo).strip()
    _op(repo, action="set", session_id=SID)

    deferred = _call(repo, session_id=SID)

    assert deferred["deferred"] == "session-landing-hold"
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
    assert _op(repo, action="status", session_id=SID)["deferred_count"] == 1

    out = _op(repo, action="clear", session_id=SID)

    assert out["failed"] == [] and [r["kind"] for r in out["replayed"]] == ["halted_record"]
    new_head = _git(["rev-parse", "HEAD"], repo).strip()
    assert new_head != head and out["replayed"][0]["sha"]
    assert (repo / RECORD_DIR_ROOT / RUN / "run.json").is_file()
    assert not landing_hold.is_held(str(repo), SID)
    assert _git(["log", "--format=%s", f"{head}..HEAD"], repo).count("rows coded") == 0


@pytest.mark.spawns_process
def test_clear_replays_deferred_row_mark_through_land_stamp(tmp_path):
    root, sha = _repo(tmp_path)
    _op(root, action="set", session_id=SID)

    out = checkpoint_stamp.stamp_checkpoint(root, _message(), sha, session_id=SID)

    assert out["stamp_deferred"] == "session-landing-hold"
    assert _git_cp(root, "rev-parse", "HEAD") == sha
    assert _rows(root)["C1"]["disposition"] == "coded"

    cleared = _op(root, action="clear", session_id=SID)

    assert cleared["failed"] == [] and [r["kind"] for r in cleared["replayed"]] == ["checkpoint_stamp"]
    assert _git_cp(root, "rev-parse", "HEAD") == cleared["replayed"][0]["sha"]
    assert _git_cp(root, "rev-parse", "HEAD~1") == sha
    assert _git_cp(root, "status", "--porcelain", "--", PLAN) == ""
    assert not landing_hold.is_held(str(root), SID)


@pytest.mark.spawns_process
def test_failed_replay_is_kept_not_dropped(repo):
    _op(repo, action="set", session_id=SID)
    landing_hold.defer(str(repo), SID, "checkpoint_stamp", {"paths": ["missing.md"], "message": "m", "session_id": SID})

    out = _op(repo, action="clear", session_id=SID)

    assert out["replayed"] == [] and [f["kind"] for f in out["failed"]] == ["checkpoint_stamp"]
    assert not landing_hold.is_held(str(repo), SID)
    assert _op(repo, action="status", session_id=SID)["deferred_count"] == 1
    retry = _op(repo, action="clear", session_id=SID)
    assert [f["kind"] for f in retry["failed"]] == ["checkpoint_stamp"]
