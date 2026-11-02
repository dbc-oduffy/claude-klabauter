"""submit_engine_ask: args.join is validated pre-write and forwarded; _slice_deny holds a join on unlanded inputs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c

PARENT = "ask-parent-20261102T120050-1929ec"
SIB = "ask-20261102T120050-aaaaaa"
JOIN = "ask-20261102T120050-bbbbbb"
EXT = "ask-20261102T120137-d2a4df"


def _payload(tmp_path: Path, args: dict) -> dict:
    return {
        "tool_name": "Workflow",
        "cwd": str(tmp_path),
        "session_id": "s1",
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": args},
    }


def _reason(env: dict) -> str:
    return env["hookSpecificOutput"]["permissionDecisionReason"]


def _boom(*_a, **_k):
    raise AssertionError("the engine must not be reached")


def _deny_fanout(params, root):
    _deny_fanout.seen = params
    return {"error": "stop here"}


_JOIN = {"ask": "join them", "inputs": [{"run_id": EXT}]}
_ASKS = [{"ask": "a"}, {"ask": "b"}]


@pytest.mark.parametrize(
    "args, needle",
    [
        ({"ask": "x", "join": _JOIN}, "join rides an asks submission"),
        ({"asks": _ASKS, "join": "nope"}, "join must be an object"),
        ({"asks": _ASKS, "join": {"inputs": [{"run_id": EXT}]}}, "join carries no ask text"),
        ({"asks": _ASKS, "join": {"ask": "j", "inputs": []}}, "join.inputs must be a non-empty list"),
        ({"asks": _ASKS, "join": {"ask": "j", "inputs": [{"repo": "/r"}]}}, "join.inputs[0] carries no run_id"),
        ({"asks": _ASKS, "join": {"ask": "j", "inputs": [{"run_id": EXT, "x": 1}]}}, "unknown key 'x'"),
        ({"asks": _ASKS, "join": {"ask": "j", "inputs": [{"run_id": EXT, "repo": 3}]}}, "repo must be"),
    ],
)
def test_malformed_join_is_refused_before_the_engine(tmp_path, args, needle):
    (tmp_path / ".git").mkdir()
    env = mod.evaluate(_payload(tmp_path, args), emit=_boom, fanout=_boom)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert needle in _reason(env)


def test_join_is_an_accepted_key_and_reaches_the_fanout_as_params_join(tmp_path):
    (tmp_path / ".git").mkdir()
    mod.evaluate(_payload(tmp_path, {"asks": _ASKS, "join": _JOIN}), emit=_boom, fanout=_deny_fanout)
    assert _deny_fanout.seen["join"] == _JOIN


def test_a_single_ask_with_a_join_stays_a_fanout(tmp_path):
    (tmp_path / ".git").mkdir()
    _deny_fanout.seen = None
    mod.evaluate(_payload(tmp_path, {"asks": [{"ask": "a"}], "join": _JOIN}), emit=_boom, fanout=_deny_fanout)
    assert _deny_fanout.seen is not None and _deny_fanout.seen["join"] == _JOIN


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _join_parent(root, external):
    runs = [
        c.SiblingRun(SIB, "a", status=c.STATUS_RUNNING),
        c.SiblingRun(JOIN, "join", after=(SIB,), status=c.STATUS_AFTER, after_external=(external,)),
    ]
    _write(root, c.parent_record_rel(PARENT), json.dumps(c.ParentRecord(parent_id=PARENT, runs=tuple(runs)).to_json()))
    _write(root, c.status_rel(SIB), json.dumps({"status": "landed", "parent_id": PARENT}))


def test_unlanded_local_input_denies_naming_it_and_the_status_call(tmp_path):
    _join_parent(tmp_path, c.ExternalInput(EXT))
    _write(tmp_path, c.status_rel(EXT), json.dumps({"status": "running"}))
    why = mod._slice_deny(tmp_path, (PARENT, JOIN))
    assert EXT in why
    assert f"dispatch.sibling_status {{parent_id: {PARENT}}}" in why


def test_unlanded_foreign_input_names_its_repo(tmp_path):
    other = tmp_path / "other"
    root = tmp_path / "root"
    _join_parent(root, c.ExternalInput(EXT, str(other)))
    why = mod._slice_deny(root, (PARENT, JOIN))
    assert EXT in why and str(other) in why


def test_landed_inputs_release_the_join(tmp_path):
    other = tmp_path / "other"
    root = tmp_path / "root"
    _join_parent(root, c.ExternalInput(EXT, str(other)))
    _write(other, c.status_rel(EXT), json.dumps({"status": "landed"}))
    assert mod._slice_deny(root, (PARENT, JOIN)) is None
