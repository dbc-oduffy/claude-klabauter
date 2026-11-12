"""Dispatch refuses undeclared param keys for ops registering a strict params_schema."""

from __future__ import annotations

import asyncio

import pytest

import coordinator_core.ipc as ipc

_STRICT = "test.undeclared_strict"
_LOOSE = "test.undeclared_loose"
_calls: list = []


async def _handler(params, repo_root=None):
    _calls.append(params)
    return {"ok": True}


@pytest.fixture(autouse=True)
def _ops():
    _calls.clear()
    ipc.register_op(
        _STRICT,
        _handler,
        params_schema={
            "type": "object",
            "properties": {"root": {"type": "string"}, "apply": {"type": "boolean"}},
            "additionalProperties": False,
        },
    )
    ipc.register_op(
        _LOOSE,
        _handler,
        params_schema={"type": "object", "properties": {"root": {"type": "string"}}},
    )
    try:
        yield
    finally:
        for op in (_STRICT, _LOOSE):
            ipc._REGISTRY.pop(op, None)
            ipc._PARAMS_SCHEMA.pop(op, None)


def _dispatch(method, params):
    return asyncio.run(
        ipc.dispatch_message({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    )


def test_undeclared_key_refused_before_handler_runs():
    resp = _dispatch(_STRICT, {"root": "x", "dry_run": True})
    assert resp["error"]["code"] == ipc.INVALID_PARAMS
    msg = resp["error"]["message"]
    assert "dry_run" in msg and _STRICT in msg and "['apply', 'root']" in msg
    assert _calls == []


def test_declared_keys_and_underscore_envelope_keys_accepted():
    resp = _dispatch(_STRICT, {"root": "x", "apply": True, "_origin_worktree": "/w"})
    assert "error" not in resp, resp
    assert len(_calls) == 1


def test_non_strict_schema_accepts_extra_keys():
    resp = _dispatch(_LOOSE, {"root": "x", "extra": 1})
    assert "error" not in resp, resp


def test_undeclared_param_keys_unit():
    assert ipc.undeclared_param_keys(_STRICT, {"a": 1, "root": 2, "_z": 3}) == ["a"]
    assert ipc.undeclared_param_keys(_LOOSE, {"a": 1}) == []
    assert ipc.undeclared_param_keys("test.no_such_op", {"a": 1}) == []
