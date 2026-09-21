"""Per-session guard overrides cross the compiled warm door.

Subject: `env_forwarding.CALLER_PREFIXES` and every leg that carries it -- the
generated `door_env_set.h`, `door_posix.c`'s `environ` walk, the isolated
borrow (`entry_seam._environ_identity_borrow`), and the server's boot scrub.

Before this, the door forwarded a fixed fifteen names and no override key was
among them, so `COORDINATOR_OVERRIDE_*` / `COORDINATOR_ALLOW_*` set in a
session arrived at the guard as "not requested": every Bash-guard override on
the box read as a hard wall through the door, while the cold path honoured it
(measured by doe-claude-79 against `hooks.preuse_bash_dispatch`).

Negative spec: does not run `door.c` (Windows-only; its binary legs are in
`test_door_stamps_declared_env_set.py`), and does not re-test guard verdicts.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.warm import env_forwarding
from coordinator_core.warm.entry_seam import per_request_state
from coordinator_core.warm.env_forwarding import CALLER_PREFIXES, FORWARDING_SET, is_caller_prefixed
from coordinator_core.warm.tests.test_door_read_deadline_posix import runtime_base  # noqa: F401 -- fixture

_DOOR_DIR = Path(__file__).resolve().parents[1] / "door"


def test_no_declared_name_is_also_forwarded_by_prefix():
    """One rule per name -- otherwise a name could cross twice with two modes."""
    assert not [e.name for e in FORWARDING_SET if is_caller_prefixed(e.name)]


def test_the_http_channel_and_the_door_share_one_prefix_tuple():
    from coordinator_core.warm import hook_http, http_hook_forwarder

    assert hook_http.FORWARDED_ENV_PREFIXES is CALLER_PREFIXES
    # The forwarder is loaded standalone and keeps its own copy; it must agree.
    assert tuple(http_hook_forwarder._FORWARDED_ENV_PREFIXES) == CALLER_PREFIXES


def test_the_committed_header_carries_every_prefix():
    header = (_DOOR_DIR / "door_env_set.h").read_text(encoding="utf-8")
    assert header == env_forwarding.generate_header()
    for prefix in CALLER_PREFIXES:
        assert f"X({prefix})" in header


def test_isolated_borrow_binds_the_callers_overrides_and_restores(monkeypatch):
    monkeypatch.setenv("COORDINATOR_OVERRIDE_SPAWNER_ONLY", "1")
    monkeypatch.setenv("COORDINATOR_ALLOW_SHARED", "spawner")

    env = {"COORDINATOR_OVERRIDE_BLANKET_ADD": "1", "COORDINATOR_ALLOW_SHARED": "caller"}
    with per_request_state(env=env, warm_served=True, isolated=True):
        assert os.environ["COORDINATOR_OVERRIDE_BLANKET_ADD"] == "1"
        assert os.environ["COORDINATOR_ALLOW_SHARED"] == "caller"
        assert "COORDINATOR_OVERRIDE_SPAWNER_ONLY" not in os.environ

    assert "COORDINATOR_OVERRIDE_BLANKET_ADD" not in os.environ
    assert os.environ["COORDINATOR_ALLOW_SHARED"] == "spawner"
    assert os.environ["COORDINATOR_OVERRIDE_SPAWNER_ONLY"] == "1"


def test_a_caller_with_no_overrides_sees_none_of_the_spawners(monkeypatch):
    monkeypatch.setenv("COORDINATOR_OVERRIDE_SPAWNER_ONLY", "1")
    with per_request_state(env={}, warm_served=True, isolated=True):
        assert not [k for k in os.environ if is_caller_prefixed(k)]
    assert os.environ["COORDINATOR_OVERRIDE_SPAWNER_ONLY"] == "1"


def test_unisolated_dispatch_never_touches_the_environment(monkeypatch):
    monkeypatch.setenv("COORDINATOR_OVERRIDE_SPAWNER_ONLY", "1")
    with per_request_state(env={"COORDINATOR_OVERRIDE_X": "1"}, warm_served=True, isolated=False):
        assert "COORDINATOR_OVERRIDE_X" not in os.environ
        assert os.environ["COORDINATOR_OVERRIDE_SPAWNER_ONLY"] == "1"


def test_boot_scrub_drops_the_spawners_overrides(monkeypatch):
    """The unisolated fallback borrows nothing, so the only thing standing
    between it and one session's override leaking to every caller is this."""
    from coordinator_core.warm import server

    for key in [k for k in os.environ if is_caller_prefixed(k)]:
        monkeypatch.setenv(key, os.environ[key])  # recorded, so teardown restores it
    monkeypatch.setenv("COORDINATOR_OVERRIDE_SPAWNER_ONLY", "1")
    monkeypatch.setenv("COORDINATOR_SCOPE_STRICT", "1")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME_UNRELATED", "kept")
    server._scrub_test_harness_env()
    assert not [k for k in os.environ if is_caller_prefixed(k)]
    assert os.environ["COORDINATOR_SETTINGS_HOME_UNRELATED"] == "kept"


# --- the POSIX door, built from source and run against a one-shot socket ---


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.warm_tier
@pytest.mark.skipif(os.name == "nt", reason="door_posix is a macOS/Linux binary")
def test_the_posix_door_carries_prefixed_names_and_nothing_else(tmp_path, runtime_base):
    from coordinator_core.warm.tests.test_door_read_deadline_posix import (
        _ReplyingServer,
        _make_socket_dir,
        _make_stub_engine_root,
        _socket_path_for,
    )

    cc = shutil.which("cc") or shutil.which("clang")
    if cc is None:
        pytest.skip("no C compiler")
    door = tmp_path / "door"
    subprocess.run(
        [cc, "-O2", "-std=c11", '-DPYTHON_BIN="python3"', '-DBUILD_ENGINE_ROOT=""',
         "-o", str(door), str(_DOOR_DIR / "door_posix.c"), str(_DOOR_DIR / "door_core.c")],
        check=True, capture_output=True,
    )

    root = _make_stub_engine_root(tmp_path)
    sock_path = _socket_path_for(runtime_base, root)
    _make_socket_dir(sock_path)
    reply = b'{"jsonrpc":"2.0","id":1,"result":{"stdout":"","stderr":"","exit_code":0}}\n'

    env = {k: v for k, v in os.environ.items() if not is_caller_prefixed(k)}
    env.update(
        COORDINATOR_DOOR_ENGINE_ROOT=str(root),
        COORDINATOR_WARM_RUNTIME_BASE=str(runtime_base),
        COORDINATOR_OVERRIDE_BLANKET_ADD="1",
        COORDINATOR_ALLOW_QUOTED='a "b"',
        COORDINATOR_SCOPE_EMPTY="",
        COORDINATOR_OVERRIDE_="bare-prefix",
        UNRELATED_SECRET="never",
    )
    server = _ReplyingServer(sock_path, reply)
    try:
        subprocess.run([str(door), "ping"], env=env, cwd=str(root), capture_output=True, timeout=60)
    finally:
        server.close()

    carried = json.loads(server.request.decode("utf-8"))["_env"]
    prefixed = {k: v for k, v in carried.items() if is_caller_prefixed(k)}
    assert prefixed == {"COORDINATOR_OVERRIDE_BLANKET_ADD": "1", "COORDINATOR_ALLOW_QUOTED": 'a "b"'}
    assert "UNRELATED_SECRET" not in carried


# --- the Python warm client (the `cc_invoke` leg) ---


@pytest.fixture
def client_request(monkeypatch, tmp_path):
    """The envelope `try_warm_dispatch` builds, captured at serialisation. The
    pipe open is refused (a no-spawn exit) and a spawn would fail the test, so
    nothing here can reach or start a real server."""
    import coordinator_core.warm.client as client

    for key in [k for k in os.environ if is_caller_prefixed(k)]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path))
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: "t" * 16)
    monkeypatch.setattr(client, "_endpoint_name", lambda _token: "/nonexistent")

    def _refuse(_pipe):
        raise PermissionError

    def _no_spawn(*_a, **_k):  # pragma: no cover -- reaching it is the failure
        raise AssertionError("client tried to spawn a server")

    monkeypatch.setattr(client, "_open_pipe", _refuse)
    monkeypatch.setattr(client, "_spawn_once", _no_spawn)

    captured = {}
    real_dumps = json.dumps

    def _spy(obj, *a, **k):
        if isinstance(obj, dict) and "_engine_token" in obj:
            captured.update(obj)
        return real_dumps(obj, *a, **k)

    monkeypatch.setattr(client.json, "dumps", _spy)

    def _send():
        client.try_warm_dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
        assert captured, "no envelope was built"
        return captured

    return _send


def test_the_python_client_carries_overrides_in_env(client_request, monkeypatch):
    monkeypatch.setenv("COORDINATOR_OVERRIDE_BLANKET_ADD", "1")
    monkeypatch.setenv("UNRELATED_SECRET", "never")
    assert client_request()["_env"] == {"COORDINATOR_OVERRIDE_BLANKET_ADD": "1"}


def test_the_python_client_omits_env_when_no_override_is_set(client_request):
    assert "_env" not in client_request()
