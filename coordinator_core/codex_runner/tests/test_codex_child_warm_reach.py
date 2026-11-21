"""A codex child's env reaches only its own pinned clone's live warm engine, and starts nothing when none is live."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core import engine_root as door_ladder
from coordinator_core.codex_runner import workflow as agents
from coordinator_core.temp_layout import mkdtemp_in
from coordinator_core.warm import breadcrumb, client, election, engine_root, skew, supervisor

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

ASK = {"jsonrpc": "2.0", "id": 1, "method": "dispatch.ask_gate", "params": {"ask": "unused"}}
_CHILD_KEYS = (
    "COORDINATOR_WARM",
    "COORDINATOR_WARM_LISTENER_AUTOSTART",
    "COORDINATOR_WARM_SERVER_AUTOSTART",
    "COORDINATOR_ENGINE_ROOT",
)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Mirrors _SERVER_CHILD in coordinator_core/ops/workflow_fire/tests/test_headless_ask_gate_warm_reach.py,
# plus a per-root request log so a test can prove which engine was (not) asked.
_SERVER_CHILD = r'''
import os, sys
from pathlib import Path

repo, root = sys.argv[1], Path(sys.argv[2])
sys.path.insert(0, repo)
from coordinator_core.warm import breadcrumb, server, skew

token = skew.compute_client_token(root)


class _Version:
    server_sha = "child-warm-reach"
    last_skew_axes = ()

    def is_skewed(self, client_token):
        return False

    def is_token_skewed(self, client_token):
        return False


def _dispatch(self, msg, **_kw):
    with open(root / "requests.log", "a", encoding="utf-8") as log:
        log.write(msg["method"] + "\n")
    return {"jsonrpc": "2.0", "id": msg["id"], "result": {"served_by": "warm", "engine_root": str(root)}}


server._ServerContext._pool_dispatch = _dispatch
on_windows = sys.platform == "win32"
elected = server._elect_windows_pipe(root, token) if on_windows else server._elect_unix_socket_endpoint(root, token)
ctx = server._ServerContext(name=elected.endpoint, sid=elected.identity, version_state=_Version(),
                            engine_root=root, listen_socket=elected.listen_socket, endpoint_path=elected.endpoint_path)
breadcrumb.write_breadcrumb(pipe=elected.endpoint, pid=os.getpid(),
                            stable_pid_start_epoch=server._self_stable_pid_start_epoch() or 0,
                            engine_sha=_Version.server_sha, engine_root=root,
                            transport=breadcrumb.TRANSPORT_PIPE if on_windows else breadcrumb.TRANSPORT_UNIX)
if elected.first_handle is not None:
    ctx.serve_forever(elected.first_handle)
else:
    ctx.serve_forever_unix(elected.listen_socket)
'''


def _stamped_root(base: Path, name: str) -> Path:
    root = base / name
    (root / "coordinator_core" / "warm").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "coordinator_core" / "_engine_stamp").write_text("sha:child-warm-reach\n", encoding="utf-8")
    return root


def _requests(root: Path) -> list[str]:
    log = root / "requests.log"
    return log.read_text(encoding="utf-8").split() if log.exists() else []


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Path:
    """A test-isolated warm runtime base, so the in-process client may reach a server at all."""
    base = mkdtemp_in("cw-", None if os.name == "nt" else "/tmp")
    request.addfinalizer(lambda: shutil.rmtree(base, ignore_errors=True))
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(base))
    monkeypatch.setattr(client, "_spawned_this_process", False)
    return base


@pytest.fixture
def boot_server(request: pytest.FixtureRequest):
    procs: list[subprocess.Popen] = []

    def boot(root: Path) -> None:
        script = root / "coordinator_core" / "warm" / "server.py"
        script.write_text(_SERVER_CHILD, encoding="utf-8")
        repo = str(Path(client.__file__).resolve().parents[2])
        with open(root / "server.log", "wb") as log:
            proc = subprocess.Popen([sys.executable, str(script), repo, str(root)], stdout=log,
                                    stderr=subprocess.STDOUT, creationflags=_NO_WINDOW)
        procs.append(proc)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if breadcrumb.read_breadcrumb(root):
                return
            if proc.poll() is not None:
                pytest.fail(f"warm server child exited {proc.returncode}")
            time.sleep(0.1)
        pytest.fail("warm server child not ready within 30s")

    request.addfinalizer(lambda: [p.kill() for p in procs if p.poll() is None])
    return boot


def _become_the_child_of(monkeypatch, root: Path) -> dict[str, str]:
    """Run as a codex child pinned to `root`: its env from the real `_environment`, its client keyed on `root`.

    An absent COORDINATOR_WARM falls to the machine registry rung, enabled on a warm box; '1' stands in for it
    so the verdict never depends on this machine's registry.
    """
    monkeypatch.setattr(engine_root, "current_engine_clone", lambda: root)
    env = agents._environment()
    for key in _CHILD_KEYS:
        value = env.get(key)
        if key == "COORDINATOR_WARM" and value is None:
            value = "1"
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(breadcrumb, "_default_engine_clone", lambda: root)
    monkeypatch.setattr(election, "_default_engine_clone", lambda: root)
    monkeypatch.setattr(client, "_engine_clone_root", lambda: root)
    monkeypatch.setattr(client, "_configured_root_is_foreign", lambda: False)
    monkeypatch.setattr(client, "_sidecar_keyed_clone", lambda: None)
    return env


def test_child_env_names_its_own_clone_as_the_engine_root_the_door_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _stamped_root(tmp_path, "a")
    other = _stamped_root(tmp_path, "b")
    env = _become_the_child_of(monkeypatch, root)
    assert env["COORDINATOR_ENGINE_ROOT"] == str(root)
    resolved, _ = door_ladder.coordinator_engine_root_with_class()
    assert resolved == str(root) and resolved != str(other)


def test_live_engine_of_the_pinned_clone_answers_the_childs_ask_gate_warm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime, boot_server) -> None:
    root = _stamped_root(tmp_path, "a")
    boot_server(root)
    env = _become_the_child_of(monkeypatch, root)
    reply = client.try_warm_dispatch(dict(ASK))
    assert reply is not None, (
        f"child went cold: COORDINATOR_WARM={env.get('COORDINATOR_WARM')!r}, "
        f"cold reason {client.last_cold_reason()!r}")
    assert reply["result"] == {"served_by": "warm", "engine_root": str(root)}
    assert _requests(root) == ["dispatch.ask_gate"]


def test_no_live_engine_goes_cold_and_starts_neither_listener_nor_warm_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime) -> None:
    root = _stamped_root(tmp_path, "a")
    server_spawns: list = []
    listener_spawns: list = []
    monkeypatch.setattr(client, "spawn_detached", lambda *a, **k: server_spawns.append(a) or True)
    monkeypatch.setattr(supervisor, "spawn_detached", lambda *a, **k: listener_spawns.append(a) or True)
    monkeypatch.setattr(supervisor, "is_engine_root", lambda r: True)
    monkeypatch.setattr(supervisor, "read_discovery", lambda r: None)
    monkeypatch.setattr(supervisor, "should_spawn", lambda r, now=None: True)
    _become_the_child_of(monkeypatch, root)
    assert client.try_warm_dispatch(dict(ASK)) is None
    supervisor.ensure_listener(root)
    assert server_spawns == [], "the warm client spawned a warm server in the child"
    assert listener_spawns == [], "ensure_listener spawned the listener in the child"
    assert breadcrumb.read_breadcrumb(root) is None


def test_a_second_live_clone_is_never_dispatched_into(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime, boot_server) -> None:
    root = _stamped_root(tmp_path, "a")
    other = _stamped_root(tmp_path, "b")
    assert skew.compute_client_token(root) == skew.compute_client_token(other)
    boot_server(root)
    boot_server(other)
    _become_the_child_of(monkeypatch, root)
    reply = client.try_warm_dispatch(dict(ASK))
    assert reply is not None and reply["result"]["engine_root"] == str(root)
    assert _requests(root) == ["dispatch.ask_gate"]
    assert _requests(other) == []


def test_a_child_pinned_where_no_engine_lives_never_reaches_the_other_live_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime, boot_server) -> None:
    root = _stamped_root(tmp_path, "a")
    other = _stamped_root(tmp_path, "b")
    boot_server(other)
    monkeypatch.setattr(client, "spawn_detached", lambda *a, **k: True)
    _become_the_child_of(monkeypatch, root)
    assert client.try_warm_dispatch(dict(ASK)) is None
    assert _requests(other) == []


def _denied(winerror) -> PermissionError:
    exc = PermissionError(13, "denied")
    exc.winerror = winerror
    return exc


def test_a_restricted_token_pipe_denial_names_its_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_cold_reason", None)
    monkeypatch.setattr(client, "_process_token_is_restricted", lambda: True)
    client._record_restricted_token_denial(_denied(5))
    assert "restricted" in (client.last_cold_reason() or "")


def test_an_unrestricted_or_other_denial_records_no_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_process_token_is_restricted", lambda: False)
    monkeypatch.setattr(client, "_cold_reason", None)
    client._record_restricted_token_denial(_denied(5))
    assert client.last_cold_reason() is None
    monkeypatch.setattr(client, "_process_token_is_restricted", lambda: True)
    client._record_restricted_token_denial(_denied(32))
    assert client.last_cold_reason() is None


def test_the_first_cold_reason_is_never_overwritten_by_a_restricted_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "_cold_reason", "earlier reason")
    monkeypatch.setattr(client, "_process_token_is_restricted", lambda: True)
    client._record_restricted_token_denial(_denied(5))
    assert client.last_cold_reason() == "earlier reason"
