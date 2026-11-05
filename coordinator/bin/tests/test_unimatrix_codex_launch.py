"""Pins for the Codex launcher (daemon + --remote argv) and the SessionStart peer registration."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]


def _load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


launch = _load("unimatrix-codex-launch.py", "unimatrix_codex_launch")
hook = _load("unimatrix-codex-hook-sessionstart.py", "unimatrix_codex_hook")
contract = _load("unimatrix-peer-contract.py", "unimatrix_peer_contract_t")

SOCK = "/home/u/.codex/app-server-control/app-server-control.sock"


def test_tui_argv_attaches_remote_unix_socket_and_passes_args():
    assert launch.tui_argv("/bin/codex", SOCK, ["-m", "x"]) == [
        "/bin/codex", "--remote", f"unix://{SOCK}", "-m", "x"]


def test_default_socket_uses_daemon_start():
    assert launch.daemon_argv("codex", SOCK, SOCK) == (["codex", "app-server", "daemon", "start"], False)


def test_overridden_socket_listens_detached_on_that_socket():
    argv, detach = launch.daemon_argv("codex", "/tmp/x.sock", SOCK)
    assert argv == ["codex", "app-server", "--listen", "unix:///tmp/x.sock"] and detach


def test_live_daemon_is_not_restarted():
    calls = []
    started = launch.ensure_daemon("codex", SOCK, SOCK, run=lambda *a, **k: calls.append(a),
                                   popen=lambda *a, **k: calls.append(a), live=lambda s: True)
    assert started is False and calls == []


def test_dead_daemon_is_started_then_awaited():
    ran = []
    states = iter([False, False, True])

    class Done:
        returncode, stdout, stderr = 0, "", ""

    started = launch.ensure_daemon(
        "codex", SOCK, SOCK, run=lambda argv, **k: ran.append(argv) or Done(),
        live=lambda s: next(states), sleep=lambda s: None)
    assert started is True and ran == [["codex", "app-server", "daemon", "start"]]


def test_daemon_start_failure_raises():
    class Bad:
        returncode, stdout, stderr = 1, "", "boom"

    with pytest.raises(RuntimeError, match="boom"):
        launch.ensure_daemon("codex", SOCK, SOCK, run=lambda *a, **k: Bad(), live=lambda s: False,
                             sleep=lambda s: None)


def test_help_states_launcher_is_optional(capsys):
    assert launch.main(["--help"]) == 0
    assert "NOT required" in capsys.readouterr().out


def test_daemon_failure_falls_back_to_plain_codex(monkeypatch, capsys):
    execed = []
    monkeypatch.setattr(launch.shutil, "which", lambda n: "/bin/codex")
    monkeypatch.setattr(launch.os, "name", "posix")
    monkeypatch.setattr(launch, "ensure_daemon",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(launch.os, "execv", lambda p, c: execed.append(c))
    launch.main(["-m", "x"])
    assert execed == [["/bin/codex", "-m", "x"]]
    assert "without --remote" in capsys.readouterr().err


def test_live_daemon_still_attaches_remote(monkeypatch):
    execed = []
    monkeypatch.setattr(launch.shutil, "which", lambda n: "/bin/codex")
    monkeypatch.setattr(launch.os, "name", "posix")
    monkeypatch.setattr(launch, "ensure_daemon", lambda *a, **k: False)
    monkeypatch.setattr(launch.os, "execv", lambda p, c: execed.append(c))
    launch.main(["-m", "x"])
    assert execed[0][1] == "--remote" and execed[0][-2:] == ["-m", "x"]


def test_daemon_never_ready_raises():
    with pytest.raises(RuntimeError, match="not accepting"):
        launch.ensure_daemon("codex", "/tmp/x.sock", SOCK, popen=lambda *a, **k: None,
                             live=lambda s: False, sleep=lambda s: None, ready_seconds=0)


def test_hook_registers_thread_in_loadable_peers_file(tmp_path):
    pf = tmp_path / "unimatrix" / "peers.json"
    out = hook.run({"session_id": "t1", "cwd": "/w/My Proj"}, peers_file=pf, env={})
    assert "codex-My-Proj" in out["systemMessage"] and "t1" in out["systemMessage"]
    assert contract.load_peers(pf) == [{"name": "codex-My-Proj", "vendor": "codex", "target": "t1"}]


def test_second_session_start_rebinds_same_name_to_new_thread(tmp_path):
    pf = tmp_path / "peers.json"
    env = {"UNIMATRIX_PEER_NAME": "cx"}
    hook.run({"session_id": "t1", "cwd": "/w"}, peers_file=pf, env=env)
    hook.run({"session_id": "t2", "cwd": "/w"}, peers_file=pf, env=env)
    assert contract.load_peers(pf) == [{"name": "cx", "vendor": "codex", "target": "t2"}]


def test_same_binding_is_idempotent_and_other_rows_survive(tmp_path):
    pf = tmp_path / "peers.json"
    pf.write_text(json.dumps({"peers": [{"name": "a", "vendor": "agy", "target": "c1"}]}))
    hook.run({"session_id": "t1", "cwd": "/w"}, peers_file=pf, env={"UNIMATRIX_PEER_NAME": "cx"})
    assert hook.register_peer(pf, "cx", "codex", "t1") == "present"
    assert [p["name"] for p in contract.load_peers(pf)] == ["a", "cx"]


def test_unparseable_peers_file_is_left_untouched(tmp_path):
    pf = tmp_path / "peers.json"
    pf.write_text("{not json")
    with pytest.raises(ValueError):
        hook.register_peer(pf, "cx", "codex", "t1")
    assert pf.read_text() == "{not json"


def test_payload_without_thread_id_registers_nothing(tmp_path):
    pf = tmp_path / "peers.json"
    assert hook.run({"cwd": "/w"}, peers_file=pf, env={}) is None
    assert not pf.exists()


def test_hook_process_exits_zero_on_garbage_stdin():
    r = subprocess.run([sys.executable, str(BIN / "unimatrix-codex-hook-sessionstart.py")],
                       input="not json", capture_output=True, text=True)
    assert r.returncode == 0
