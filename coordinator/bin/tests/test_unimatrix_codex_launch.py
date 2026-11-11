"""Pins for the Codex launcher (daemon + --remote argv) and the SessionStart peer registration."""
import importlib.util
import json
import socket
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

SOCK = "/home/u/.codex/app-server-control/app-server-control.sock"  # abs-path-ok: synthetic socket path fixture, never opened


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


def _checkout(tmp_path, name, sub=None):
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    cwd = root / sub if sub else root
    cwd.mkdir(parents=True, exist_ok=True)
    return str(cwd)


T1, T2, T3 = (f"00000000-0000-4000-8000-00000000000{i}" for i in (1, 2, 3))


def _bind(tmp_path, thread, name):
    """Write the engine's binding for thread; returns the registry dir the hook reads."""
    reg = tmp_path / "bindings"
    reg.mkdir(exist_ok=True)
    (reg / f"{thread}.json").write_text(json.dumps({
        "schema_version": 1, "provider": "codex", "session_id": thread, "address_version": 1,
        "name": name, "box": socket.gethostname()}), encoding="utf-8")
    return reg


def _names(pf):
    return {p["target"]: p["name"] for p in contract.load_peers(pf)}


def test_hook_registers_thread_under_its_engine_name(tmp_path):
    pf = tmp_path / "unimatrix" / "peers.json"
    reg = _bind(tmp_path, T1, "coordinator-content-repo-codex-box-50")
    work = tmp_path / "My Proj"
    work.mkdir()
    out = hook.run({"session_id": T1, "cwd": str(work)}, peers_file=pf, registry_dir=reg)
    rows = contract.load_peers(pf)
    assert len(rows) == 1 and rows[0]["vendor"] == "codex" and rows[0]["target"] == T1
    assert rows[0]["name"] == "coordinator-content-repo-codex-box-50"
    assert rows[0]["name"] in out["systemMessage"] and T1 in out["systemMessage"]


def test_same_thread_start_again_keeps_its_name(tmp_path):
    pf = tmp_path / "peers.json"
    reg = _bind(tmp_path, T1, "w-codex-box-1")
    cwd = _checkout(tmp_path, "w")
    hook.run({"session_id": T1, "cwd": cwd}, peers_file=pf, registry_dir=reg)
    first = _names(pf)
    hook.run({"session_id": T1, "cwd": cwd}, peers_file=pf, registry_dir=reg)
    assert _names(pf) == first == {T1: "w-codex-box-1"}


def test_two_threads_in_one_repo_get_distinct_engine_names(tmp_path):
    pf = tmp_path / "peers.json"
    cwd = _checkout(tmp_path, "w")
    for i, t in enumerate((T1, T2, T3), start=1):
        reg = _bind(tmp_path, t, f"w-codex-box-{i}")
        hook.run({"session_id": t, "cwd": cwd}, peers_file=pf, registry_dir=reg)
    assert _names(pf) == {T1: "w-codex-box-1", T2: "w-codex-box-2", T3: "w-codex-box-3"}


def test_thread_without_binding_registers_no_codex_repo_name(tmp_path):
    pf = tmp_path / "peers.json"
    reg = tmp_path / "bindings"
    reg.mkdir()
    out = hook.run({"session_id": T1, "cwd": _checkout(tmp_path, "w")}, peers_file=pf, registry_dir=reg)
    assert "unbound" in out["systemMessage"] and not pf.exists()


def test_legacy_unsuffixed_row_for_the_thread_is_replaced(tmp_path):
    pf = tmp_path / "peers.json"
    pf.write_text(json.dumps({"peers": [{"name": "codex-claude-klabauter", "vendor": "codex", "target": T1}]}))
    reg = _bind(tmp_path, T1, "claude-unreal-holo-codex-box-7")
    hook.run({"session_id": T1, "cwd": _checkout(tmp_path, "example-game-workbench-repo")},
             peers_file=pf, registry_dir=reg)
    assert _names(pf) == {T1: "claude-unreal-holo-codex-box-7"}


def test_same_binding_is_idempotent_and_other_rows_survive(tmp_path):
    pf = tmp_path / "peers.json"
    pf.write_text(json.dumps({"peers": [{"name": "a", "vendor": "agy", "target": "c1"}]}))
    reg = _bind(tmp_path, T1, "w-codex-box-2")
    hook.run({"session_id": T1, "cwd": _checkout(tmp_path, "w")}, peers_file=pf, registry_dir=reg)
    address = contract.read_codex_address(reg, T1, socket.gethostname())
    assert hook.register_peer(pf, address["name"], "codex", T1, address=address) == "present"
    assert [p["name"] for p in contract.load_peers(pf)] == ["a", "w-codex-box-2"]


def test_inherited_launcher_peer_name_never_names_the_session(tmp_path, monkeypatch):
    # machine-b shape: Codex launched from a claude-klabauter shell, working in example-game-workbench-repo
    monkeypatch.setenv("UNIMATRIX_PEER_NAME", "codex-claude-klabauter")
    monkeypatch.chdir(_checkout(tmp_path, "claude-klabauter"))
    pf = tmp_path / "peers.json"
    reg = _bind(tmp_path, T1, "claude-unreal-holo-codex-box-3")
    out = hook.run({"session_id": T1, "cwd": _checkout(tmp_path, "example-game-workbench-repo")},
                   peers_file=pf, registry_dir=reg)
    assert _names(pf) == {T1: "claude-unreal-holo-codex-box-3"}
    assert "claude-klabauter" not in out["systemMessage"]


def test_name_is_the_checkout_not_a_subdirectory_of_it(tmp_path):
    cwd = _checkout(tmp_path, "example-game-workbench-repo", sub="Plugins/Example-Game-Repo")
    assert hook.peer_base({"cwd": cwd}) == "example-game-workbench-repo"


def test_resume_from_another_checkout_keeps_the_engine_name(tmp_path):
    pf = tmp_path / "peers.json"
    reg = _bind(tmp_path, T1, "claude-klabauter-codex-box-4")
    hook.run({"session_id": T1, "cwd": _checkout(tmp_path, "claude-klabauter")}, peers_file=pf, registry_dir=reg)
    hook.run({"session_id": T1, "cwd": _checkout(tmp_path, "example-game-workbench-repo")},
             peers_file=pf, registry_dir=reg)
    assert _names(pf) == {T1: "claude-klabauter-codex-box-4"}


def test_unparseable_peers_file_is_left_untouched(tmp_path):
    pf = tmp_path / "peers.json"
    pf.write_text("{not json")
    with pytest.raises(ValueError):
        hook.register_peer(pf, "cx", "codex", "t1")
    assert pf.read_text() == "{not json"


def test_payload_without_thread_id_registers_nothing(tmp_path):
    pf = tmp_path / "peers.json"
    assert hook.run({"cwd": "/w"}, peers_file=pf) is None
    assert not pf.exists()


def test_hook_process_exits_zero_on_garbage_stdin():
    r = subprocess.run([sys.executable, str(BIN / "unimatrix-codex-hook-sessionstart.py")],
                       input="not json", capture_output=True, text=True)
    assert r.returncode == 0
