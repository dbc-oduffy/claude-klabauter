"""A resume through `op._dispatch_emit` (no submit hook) passes the resume gate: a cancelled run and a foreign
driver are refused, a takeover resumes and is logged, the same driver resumes."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.resume_gate import TAKEOVER_LOG
from coordinator_core.ops.dispatch_emit.tests.test_refire_inherits_halted_base import (  # noqa: F401
    REL,
    WARP,
    _halt,
    composed,
    repo,
)

pytestmark = pytest.mark.complete


def _drive(repo, session, **extra):
    return op._dispatch_emit({"ask": True, "sizing_path": REL, "session_id": session, **extra}, repo_root=repo)


def _stage(repo, session):
    (record_dir(repo, WARP) / "run-stage.json").write_text(
        json.dumps({"stage": "commit", "session_id": session}), encoding="utf-8")


def test_a_cancelled_run_is_refused_on_the_op_path(repo, composed):
    _halt(repo, WARP, "a" * 40)
    path = record_dir(repo, WARP) / "halted.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "halted": "cancelled"}), encoding="utf-8")
    with pytest.raises(ValueError, match="cancelled.*fresh"):
        _drive(repo, "s1")


def test_a_foreign_driver_is_refused_on_the_op_path(repo, composed):
    _halt(repo, WARP, "a" * 40)
    _stage(repo, "other")
    with pytest.raises(ValueError, match="takeover"):
        _drive(repo, "s1")
    assert not (record_dir(repo, WARP) / TAKEOVER_LOG).exists()


def test_a_foreign_driver_with_takeover_resumes_and_logs_on_the_op_path(repo, composed):
    _halt(repo, WARP, "a" * 40)
    _stage(repo, "other")
    reply = _drive(repo, "s1", takeover={"reason": "peer died"})
    assert "run_id" in reply, reply
    assert reply["run_id"] == WARP
    (row,) = [json.loads(l) for l in (record_dir(repo, WARP) / TAKEOVER_LOG).read_text().splitlines()]
    assert (row["from_session"], row["to_session"], row["reason"]) == ("other", "s1", "peer died")


def test_the_same_driver_resumes_on_the_op_path(repo, composed):
    _halt(repo, WARP, "a" * 40)
    _stage(repo, "s1")
    assert _drive(repo, "s1")["run_id"] == WARP
    assert not (record_dir(repo, WARP) / TAKEOVER_LOG).exists()
