"""session.landing_hold: registration, set/status/clear, and the end-to-end defer-then-replay path."""

from __future__ import annotations

import pytest
from pathlib import Path

pytestmark = pytest.mark.complete

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.op_scopes import OP_KEY_SCOPE
from coordinator_core.ops import _registry_map
from coordinator_core.ops import session_landing_hold as op
from coordinator_core.ops.dispatch_emit import checkpoint_stamp
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


def test_registration_quad() -> None:
    assert ipc._REGISTRY.get("session.landing_hold") is not None
    assert _registry_map.OP_MODULE_MAP["session.landing_hold"] == "coordinator_core.ops.session_landing_hold"
    assert OP_KEY_SCOPE["session.landing_hold"] == "common_dir"
    assert OP_CLASSIFICATION["session.landing_hold"] is OpClass.MUTATING


def test_invalid_params_return_error(tmp_path: Path) -> None:
    assert "error" in _op(tmp_path, action="bogus", session_id=SID)
    assert "error" in _op(tmp_path, action="set")
    assert "error" in _op(tmp_path, action="set", session_id="../x")
    assert "error" in _op(tmp_path, action="clear", session_id=5)


@pytest.mark.spawns_process
def test_set_status_clear_without_deferrals(repo) -> None:
    assert _op(repo, action="status", session_id=SID) == {"action": "status", "held": False, "deferred_count": 0}
    assert _op(repo, action="set", session_id=SID) == {"action": "set", "held": True}
    assert landing_hold.is_held(str(repo), SID)
    assert _op(repo, action="status", session_id=SID)["held"] is True
    out = _op(repo, action="clear", session_id=SID)
    assert out == {"action": "clear", "cleared": True, "replayed": [], "failed": []}
    assert not landing_hold.is_held(str(repo), SID)


@pytest.mark.spawns_process
def test_failed_replay_is_kept_not_dropped(repo) -> None:
    _op(repo, action="set", session_id=SID)
    landing_hold.defer(str(repo), SID, "checkpoint_stamp", {"paths": ["missing.md"], "message": "m", "session_id": SID})

    out = _op(repo, action="clear", session_id=SID)

    assert out["replayed"] == [] and [f["kind"] for f in out["failed"]] == ["checkpoint_stamp"]
    assert not landing_hold.is_held(str(repo), SID)
    assert _op(repo, action="status", session_id=SID)["deferred_count"] == 1
    retry = _op(repo, action="clear", session_id=SID)
    assert [f["kind"] for f in retry["failed"]] == ["checkpoint_stamp"]
