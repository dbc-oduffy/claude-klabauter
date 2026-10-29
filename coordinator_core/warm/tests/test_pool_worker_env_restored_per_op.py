"""A pooled worker serves every op with the environ it started with.

An op that mutates `os.environ` (e.g. sets `_COORDINATOR_TEST_FORBID_REAL_SETTINGS_HOME`)
must not leak that into the next op the same worker serves.
"""

from __future__ import annotations

import os

from coordinator_core.warm import server

_FORBID = "_COORDINATOR_TEST_FORBID_REAL_SETTINGS_HOME"


async def _mutating_then_reporting(msg, *, caller=None, corr_id=None) -> dict:
    mode = msg["params"]["mode"]
    if mode == "mutate":
        os.environ[_FORBID] = "1"
        os.environ["LEAK_PROBE_EXISTING"] = "mutated"
        os.environ["LEAK_PROBE_NEW"] = "x"
        return {"jsonrpc": "2.0", "id": msg["id"], "result": "mutated"}
    return {
        "jsonrpc": "2.0",
        "id": msg["id"],
        "result": {
            "forbid": os.environ.get(_FORBID),
            "existing": os.environ.get("LEAK_PROBE_EXISTING"),
            "new": os.environ.get("LEAK_PROBE_NEW"),
        },
    }


def _msg(i, mode):
    return {"jsonrpc": "2.0", "id": i, "method": "ping", "params": {"mode": mode}}


def test_next_op_sees_pristine_environ(monkeypatch):
    monkeypatch.setattr("coordinator_core.ipc.dispatch_message", _mutating_then_reporting)
    monkeypatch.delenv(_FORBID, raising=False)
    monkeypatch.delenv("LEAK_PROBE_NEW", raising=False)
    monkeypatch.setenv("LEAK_PROBE_EXISTING", "pristine")
    monkeypatch.setattr(server, "_worker_pristine_environ", dict(os.environ))

    server._pool_dispatch_worker(_msg(1, "mutate"), None)
    out = server._pool_dispatch_worker(_msg(2, "report"), None)["result"]

    assert out["forbid"] is None
    assert out["new"] is None
    assert out["existing"] == "pristine"


def test_restore_resets_changed_and_pops_added_keys(monkeypatch):
    monkeypatch.setenv("LEAK_PROBE_EXISTING", "pristine")
    monkeypatch.delenv("LEAK_PROBE_NEW", raising=False)
    monkeypatch.setattr(server, "_worker_pristine_environ", dict(os.environ))
    os.environ["LEAK_PROBE_EXISTING"] = "mutated"
    os.environ["LEAK_PROBE_NEW"] = "x"

    server._restore_worker_environ_to_pristine()

    assert os.environ["LEAK_PROBE_EXISTING"] == "pristine"
    assert "LEAK_PROBE_NEW" not in os.environ
    assert dict(os.environ) == server._worker_pristine_environ


def test_uncaptured_worker_is_untouched(monkeypatch):
    monkeypatch.setattr(server, "_worker_pristine_environ", None)
    monkeypatch.setenv("LEAK_PROBE_NEW", "kept")

    server._restore_worker_environ_to_pristine()

    assert os.environ["LEAK_PROBE_NEW"] == "kept"
