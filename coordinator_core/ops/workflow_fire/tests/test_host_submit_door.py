import json
import os
import shutil
import socket
import subprocess
import sys
import uuid

import psutil
import pytest

from coordinator_core.ops.workflow_fire.host_submit_door import _issued_file, serve_submit_connection
from coordinator_core.warm.tests.test_door_read_deadline_posix import (
    _make_socket_dir, _make_stub_engine_root, _socket_path_for, runtime_base,
)


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin-only endpoint")
@pytest.mark.parametrize("wrong_peer", [True, False])
def test_installed_command_returns_receiver_exit(tmp_path, runtime_base, monkeypatch, wrong_peer):
    door = shutil.which("coordinator-invoke")
    if not door:
        pytest.skip("installed coordinator-invoke required")
    root = _make_stub_engine_root(tmp_path)
    address = _socket_path_for(runtime_base, root)
    _make_socket_dir(address)
    work = tmp_path / "workspace"
    state = work / ".coordinator-local"
    state.mkdir(parents=True)
    params = {"ask": "Check duplicate Elm records", "ask_source": "relayed", "caller": "codex-em"}
    (state / "em-submit.json").write_text(json.dumps(params))
    calls = []

    from coordinator_core.hooks import _engine_ask_gates
    from coordinator_core.ops.workflow_fire import headless_em_grant, op
    from coordinator_core.ops.workflow_fire.host_submit import HostSubmit
    from coordinator_core.session import external_registry

    parent = psutil.Process()
    sid = "fixture-launch"
    settings = tmp_path / "host"
    settings.mkdir(mode=0o700)
    monkeypatch.setenv(headless_em_grant.SETTINGS_HOME_KEY, str(settings))
    caller = {"CODEX_THREAD_ID": str(uuid.uuid4()), "COORDINATOR_SESSION_ID": sid,
              headless_em_grant.HEADLESS_EM_TOKEN_ENV: headless_em_grant.write_headless_em_grant(sid, "codex")}
    assert headless_em_grant.observe_codex_registration(caller, caller["CODEX_THREAD_ID"], parent.pid, str(work))
    row = {"session_id": caller["CODEX_THREAD_ID"], "role": "em", "provider": "codex", "pid": parent.pid,
           "start_epoch": int(parent.create_time()), "cwd": str(work)}
    monkeypatch.setattr(external_registry, "lookup", lambda thread: row)
    allocation = HostSubmit(authorized_params=params, workspace=work, receipt_directory=settings,
                            launcher_session_id=sid)
    def mint(**kwargs):
        calls.append(kwargs["session_id"])
        return {"run_id": "r1", "script_path": "fixture", "sha256": "fixture"}
    monkeypatch.setattr(_engine_ask_gates, "submit_ask", mint)
    monkeypatch.setattr(op, "_workflow_fire", lambda *args: {"refused": "fixture-refusal"})

    env = dict(PATH=os.defpath, HOME=str(tmp_path), COORDINATOR_DOOR_ENGINE_ROOT=str(root),
               COORDINATOR_WARM_RUNTIME_BASE=str(runtime_base))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(address))
        listener.listen(1)
        listener.settimeout(3)
        with subprocess.Popen([door, "engine_ask.submit", "--params-file", ".coordinator-local/em-submit.json"],
                              cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
            with listener.accept()[0] as conn:
                serve_submit_connection(conn, allocation=allocation, caller_env=caller,
                                        native_process=parent,
                                        command_process=parent if wrong_peer else psutil.Process(child.pid))
            stdout, stderr = child.communicate(timeout=3)
    assert child.returncode == 1, stderr
    result = json.loads(stdout)["result"]
    if wrong_peer:
        assert not calls
        assert result == {"refused": "launch-identity-mismatch"}
    else:
        assert calls == [sid]
        assert result["run_id"] == "r1"
        assert result["fire"]["refused"] == "fixture-refusal"
        assert result["launch_observation"]["native_session_id"] == caller["CODEX_THREAD_ID"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory descriptors required")
def test_params_file_replacement_refuses_symlink(tmp_path):
    state = tmp_path / ".coordinator-local"
    state.mkdir()
    target = tmp_path / "other.json"
    target.write_text("{}")
    (state / "em-submit.json").symlink_to(target)
    with pytest.raises(OSError):
        _issued_file(tmp_path)


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin-only endpoint")
def test_selected_submit_socket_never_redrives_or_forwards_environment(tmp_path, runtime_base):
    from pathlib import Path

    compiler = shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler required")
    source = Path(__file__).resolve().parents[3] / "warm" / "door"
    door = tmp_path / "coordinator-invoke"
    subprocess.run([compiler, "-O2", "-std=c11", f'-DPYTHON_BIN="{sys.executable}"', "-o", str(door), str(source / "door_posix.c"),
                    str(source / "door_core.c")], check=True, capture_output=True)
    root = _make_stub_engine_root(tmp_path)
    address = runtime_base / "submit.sock"
    argv = [str(door), "engine_ask.submit", "--params-file", ".coordinator-local/em-submit.json"]
    env = dict(PATH=os.defpath, HOME=str(tmp_path), COORDINATOR_DOOR_ENGINE_ROOT=str(root),
               COORDINATOR_HOST_SUBMIT_SOCKET=str(address), COORDINATOR_WARM="0",
               COORDINATOR_ALLOW_HEADLESS_EM_TOKEN="fixture-private-value")
    cold_env = env.copy()
    del cold_env["COORDINATOR_HOST_SUBMIT_SOCKET"]
    cold = subprocess.run(argv, env=cold_env, cwd=tmp_path, capture_output=True, timeout=3)
    from coordinator_core.warm.tests.test_door_read_deadline_posix import _FALLBACK_MARKER
    assert _FALLBACK_MARKER.encode() in cold.stdout

    absent = subprocess.run(argv, env=env, cwd=tmp_path, capture_output=True, timeout=3)
    assert absent.returncode == 1 and b"endpoint unavailable" in absent.stderr
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(address))
        listener.listen(1)
        listener.settimeout(3)
        for reply in (None, {"stdout": "accepted\n", "stderr": "", "exit_code": 0}):
            with subprocess.Popen(argv, cwd=tmp_path, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
                with listener.accept()[0] as conn:
                    conn.settimeout(3)
                    request = json.loads(conn.makefile("rb").readline())
                    assert request == {"jsonrpc": "2.0", "id": 1, "method": "invoke.from_argv",
                                       "params": {"argv": argv[1:], "cwd": str(tmp_path.resolve())}}
                    if reply:
                        conn.sendall((json.dumps({"jsonrpc": "2.0", "id": 1, "result": reply}) + "\n").encode())
                stdout, stderr = child.communicate(timeout=3)
                if reply:
                    assert child.returncode == 0 and stdout == b"accepted\n"
                else:
                    assert child.returncode == 1 and b"unconfirmed" in stderr
