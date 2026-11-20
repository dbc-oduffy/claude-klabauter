"""A pool recycle never kills a waited-on op silently: the waiting caller gets a definite failure naming the
op and a stall dump written before the kill, `warm.request_status` names the op, and every -32004 names the
stall dir (client-minted ones through `<settings home>/state/warm-stall-dir.txt`).

Drives the real `_ServerContext._pool_dispatch` -> `_recycle_stalled_pool` path with a thread-backed pool
stand-in whose retirement fails the held futures the way a killed worker does.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest

from coordinator_core.warm import client, io_probe, server

_MUTATING_OP = "dispatch.ask_stage"
_READ_OP = "ping"


class _VersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


class _Pool:
    def __init__(self) -> None:
        self.futures: list = []
        self._processes: dict = {}

    def submit(self, *_a, **_k):
        fut = concurrent.futures.Future()
        fut.set_running_or_notify_cancel()
        self.futures.append(fut)
        return fut

    def shutdown(self, wait=True, cancel_futures=False):
        pass


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(io_probe, "arm", lambda *_a, **_k: False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    ctx = server._ServerContext(name="pipe-recycle", sid="sid-recycle", version_state=_VersionState(), engine_root=tmp_path)
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._stall_dir = tmp_path / "stall-dumps"
    ctx._stall_dir.mkdir()
    pool = _Pool()
    ctx._dispatch_pool = pool
    monkeypatch.setattr(ctx, "_write_recycle_line", lambda *a, **k: None, raising=False)
    seen: dict = {"dumps_at_retire": None}

    def _retire(stuck, grace_secs=None, ledger=None):
        seen["dumps_at_retire"] = sorted(p.name for p in ctx._stall_dir.glob("*.txt"))
        for fut in pool.futures:
            if not fut.done():
                fut.set_exception(BrokenProcessPool("worker terminated by the recycle"))

    monkeypatch.setattr(server.pool_retire, "retire_pool", _retire)
    return ctx, pool, seen, home


def _run_recycled(ctx, pool, op: str, key: str) -> dict:
    server._ack_store.admit(key, op)
    out: list = []
    thread = threading.Thread(
        target=lambda: out.append(ctx._pool_dispatch({"jsonrpc": "2.0", "id": 9, "method": op, "params": {}}, dispatch_key=key)),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not pool.futures:
        time.sleep(0.01)
    assert pool.futures, "the op never reached the pool"
    ctx._recycle_stalled_pool(pool, [])
    thread.join(10.0)
    assert out, "the caller was never answered"
    return out[0]


def test_a_recycled_mutating_op_gets_a_definite_failure_naming_op_and_prekill_dump(rig) -> None:
    ctx, pool, seen, _home = rig
    key = "c6-recycle-mutating"
    reply = _run_recycled(ctx, pool, _MUTATING_OP, key)

    error = reply["error"]
    assert error["code"] == server.WARM_DISPATCH_INDETERMINATE
    assert _MUTATING_OP in error["message"]
    assert "NOT retried" in error["message"] and "never resend" in error["message"]
    dump = error["data"]["diagnosis"]["stall_dump"]
    assert Path(dump).is_file()
    assert Path(dump).name in seen["dumps_at_retire"], "the stack was captured after the pool was retired"
    assert error["data"]["stall_dir"] == str(ctx._stall_dir)
    assert f"Stall dir: {ctx._stall_dir}." in error["message"]


def test_request_status_names_the_killed_op_and_dump(rig) -> None:
    ctx, pool, _seen, _home = rig
    key = "c6-recycle-status"
    reply = _run_recycled(ctx, pool, _MUTATING_OP, key)

    status = server._ack_store.status(key)
    assert status["reason"] == server.dispatch_ack.REASON_ENGINE_RESTARTED
    assert status["op"] == _MUTATING_OP
    assert status["stall_dump"] == reply["error"]["data"]["diagnosis"]["stall_dump"]


def test_a_recycled_read_only_op_is_failed_definitely_and_marked_safe_to_rerun(rig) -> None:
    ctx, pool, _seen, _home = rig
    reply = _run_recycled(ctx, pool, _READ_OP, "c6-recycle-read")

    error = reply["error"]
    assert error["code"] != server.WARM_DISPATCH_INDETERMINATE
    assert _READ_OP in error["message"] and "re-running it is safe" in error["message"]
    assert Path(error["data"]["diagnosis"]["stall_dump"]).is_file()


def test_the_engine_publishes_the_stall_dir_pointer_under_the_settings_home(rig) -> None:
    ctx, _pool, _seen, home = rig
    target = server.publish_stall_dir_pointer(ctx._stall_dir)

    assert target == home / "state" / server.STALL_DIR_POINTER_NAME
    assert target.read_text(encoding="utf-8") == str(ctx._stall_dir)
    assert not list(target.parent.glob("*.tmp")) and [p.name for p in target.parent.iterdir()] == [target.name]


def test_an_absent_settings_home_is_a_typed_refusal_naming_the_variable(rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, _pool, _seen, _home = rig
    missing = tmp_path / "no-such-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(missing))
    with pytest.raises(server.StallDirPointerRefused, match="COORDINATOR_SETTINGS_HOME"):
        server.publish_stall_dir_pointer(ctx._stall_dir)
    assert not missing.exists()


def test_a_client_minted_indeterminate_names_the_published_stall_dir(rig) -> None:
    ctx, _pool, _seen, _home = rig
    server.publish_stall_dir_pointer(ctx._stall_dir)

    envelope = client._indeterminate_envelope({"id": 3, "method": _MUTATING_OP}, "no reply")
    assert envelope["error"]["data"]["stall_dir"] == str(ctx._stall_dir)
    assert f"stall_dir={ctx._stall_dir}" in envelope["error"]["message"]


def test_a_client_minted_indeterminate_without_a_pointer_says_unknown(rig) -> None:
    envelope = client._indeterminate_envelope({"id": 3, "method": _MUTATING_OP}, "no reply")
    assert envelope["error"]["data"]["stall_dir"] == "unknown"
    assert "stall_dir=unknown" in envelope["error"]["message"]


def test_a_timeout_envelope_already_naming_the_stall_dir_is_not_named_twice(rig) -> None:
    from coordinator_core import ipc

    ctx, _pool, _seen, _home = rig
    server.publish_stall_dir_pointer(ctx._stall_dir)
    envelope = ipc._timeout_error_envelope(_MUTATING_OP, 5.0, 4)
    server._name_stall_dir(envelope, ctx._stall_dir)
    assert envelope["error"]["message"].count(str(ctx._stall_dir)) == 1


def test_the_cold_run_timeout_envelope_names_the_published_stall_dir(rig) -> None:
    from coordinator_core import ipc

    ctx, _pool, _seen, _home = rig
    server.publish_stall_dir_pointer(ctx._stall_dir)

    envelope = ipc._timeout_error_envelope(_MUTATING_OP, 5.0, 4)
    assert envelope["error"]["data"]["stall_dir"] == str(ctx._stall_dir)
    assert f"stall_dir={ctx._stall_dir}" in envelope["error"]["message"]
