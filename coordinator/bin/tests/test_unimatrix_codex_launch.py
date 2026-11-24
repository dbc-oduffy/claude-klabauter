"""Pins for the Codex launcher (daemon + --remote argv) and the SessionStart peer registration."""
import importlib.util
import json
import os
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


def test_standard_launch_overrides_ambient_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('model = "gpt-6-luna"\n')
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [{"slug": "gpt-6.2-astra"}]}))
    args, expected = launch.select_model(["--profile", "fast"])
    assert args == ["--profile", "fast", "--model", "gpt-6.2-astra"]
    assert expected == "gpt-6.2-astra"


@pytest.mark.parametrize("catalog", [None, "invalid json", '{"models": [{"slug": "gpt-6-luna"}]}'])
def test_cold_or_stale_catalog_does_not_claim_astra_unavailable(tmp_path, monkeypatch, catalog):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    if catalog is not None:
        (tmp_path / "models_cache.json").write_text(catalog)
    assert launch.select_model([]) == (["--model", "gpt-6.2-astra"], "gpt-6.2-astra")


def test_model_selection_precedes_prompt_separator(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text('{"models": [{"slug": "gpt-6.2-astra"}]}')
    assert launch.select_model(["--", "--model", "example"])[0] == [
        "--model", "gpt-6.2-astra", "--", "--model", "example"]


@pytest.mark.parametrize("args", [["-m", "worker"], ["--model=worker"], ["-c", 'model="worker"']])
def test_deliberate_model_overrides_remain_explicit(args):
    assert launch.select_model(args) == (args, None)


@pytest.mark.parametrize("observed", ["gpt-6.2-astra", "gpt-6-luna", None])
def test_startup_verification_uses_native_metadata(tmp_path, monkeypatch, observed):
    monkeypatch.setenv("COORDINATOR_CODEX_EXPECTED_MODEL", "gpt-6.2-astra")
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": "session"}}) + "\n" +
                          json.dumps({"type": "turn_context", "payload": {"turn_id": "current", "model": observed}}) + "\n")
    payload = {"session_id": "session", "turn_id": "current", "transcript_path": str(transcript)}
    if observed == "gpt-6.2-astra":
        assert "observed gpt-6.2-astra" in hook.verify_startup_model(payload)
    elif observed:
        with pytest.raises(RuntimeError, match="startup model"):
            hook.verify_startup_model(payload)
    else:
        assert "deferred" in hook.verify_startup_model(payload)


def test_startup_verification_refuses_other_session_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_CODEX_EXPECTED_MODEL", "gpt-6.2-astra")
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "other-session", "model": "gpt-6.2-astra"}}) + "\n")
    assert "deferred" in hook.verify_startup_model({
        "session_id": "session", "turn_id": "current", "transcript_path": str(transcript)})


@pytest.mark.parametrize("previous,current", [("gpt-6-luna", "gpt-6.2-astra"),
                                               ("gpt-6.2-astra", "gpt-6-luna")])
def test_current_turn_after_long_initial_rows(tmp_path, monkeypatch, previous, current):
    monkeypatch.setenv("COORDINATOR_CODEX_EXPECTED_MODEL", "gpt-6.2-astra")
    transcript = tmp_path / "rollout.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"id": "session", "padding": "x" * 160000}},
        {"type": "turn_context", "payload": {"turn_id": "old", "model": previous}},
        {"type": "response_item", "payload": {"padding": "x" * 1200000}},
        {"type": "turn_context", "payload": {"turn_id": "current", "model": current}},
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    payload = {"session_id": "session", "turn_id": "current", "transcript_path": str(transcript)}
    if current == "gpt-6.2-astra":
        assert "observed gpt-6.2-astra" in hook.verify_startup_model(payload)
    else:
        with pytest.raises(hook.StartupModelError, match="startup model gpt-6-luna"):
            hook.verify_startup_model(payload)


def test_resume_defers_historical_model_until_current_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_CODEX_EXPECTED_MODEL", "gpt-6.2-astra")
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "session", "model": "gpt-6-luna"}}) + "\n" + json.dumps({
            "type": "turn_context", "payload": {"turn_id": "old", "model": "gpt-6.2-astra"}}) + "\n")
    payload = {"session_id": "session", "turn_id": "new", "transcript_path": str(transcript)}
    assert "deferred" in hook.verify_startup_model(payload)
    with transcript.open("a") as stream:
        stream.write(json.dumps({"type": "turn_context", "payload": {
            "turn_id": "new", "model": "gpt-6.2-astra"}}) + "\n")
    assert "observed" in hook.verify_startup_model(payload)
    payload.pop("turn_id")
    assert "deferred" in hook.verify_startup_model(payload)


@pytest.mark.parametrize("inherited_expectation", [True, False])
def test_existing_server_native_payload_model_wins_over_history(monkeypatch, inherited_expectation):
    if inherited_expectation:
        monkeypatch.setenv("COORDINATOR_CODEX_EXPECTED_MODEL", "gpt-6.2-astra")
    else:
        monkeypatch.delenv("COORDINATOR_CODEX_EXPECTED_MODEL", raising=False)
    status = hook.verify_startup_model({
        "session_id": "existing", "turn_id": "new", "model": "gpt-6.2-astra"})
    assert "observed gpt-6.2-astra" in status
    assert ("launch expectation unavailable" in status) is not inherited_expectation


@pytest.mark.parametrize("branch", ["existing-server", "no-server", "windows"])
def test_main_selects_astra_on_every_standard_branch(tmp_path, monkeypatch, branch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("COORDINATOR_CODEX_EXPECTED_MODEL", "")
    (tmp_path / "models_cache.json").write_text('{"models": [{"slug": "gpt-6.2-astra"}]}')
    (tmp_path / "config.toml").write_text('model = "gpt-6-luna"\n[profiles.fast]\nmodel = "gpt-6-luna"\n')
    monkeypatch.setattr(launch.shutil, "which", lambda n: "codex.exe")
    monkeypatch.setattr(launch, "Path", type(tmp_path))
    import coordinator_core.codex_runner.codex_spawn_env
    seen = []
    monkeypatch.setattr(launch.os, "execv", lambda p, argv: seen.append(argv))
    def call(argv, **kw):
        assert not kw.get("creationflags", 0) & getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        seen.append(argv)
        return 0
    monkeypatch.setattr(launch.subprocess, "call", call)
    monkeypatch.setattr(launch.os, "name", "nt" if branch == "windows" else "posix")
    def daemon(*args):
        pytest.fail("standard session must not depend on shared server environment")
    monkeypatch.setattr(launch, "ensure_daemon", daemon)
    launch.main(["--profile", "fast"])
    assert seen[0][-2:] == ["--model", "gpt-6.2-astra"]
    assert "--remote" not in seen[0]
    assert os.environ["COORDINATOR_CODEX_EXPECTED_MODEL"] == "gpt-6.2-astra"


def test_native_unavailable_astra_failure_is_propagated(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("COORDINATOR_CODEX_EXPECTED_MODEL", "")
    monkeypatch.setattr(launch.shutil, "which", lambda n: "codex")
    import coordinator_core.codex_runner.codex_spawn_env
    monkeypatch.setattr(launch.os, "name", "nt")
    calls = []
    monkeypatch.setattr(launch.subprocess, "call", lambda argv, **kw: calls.append(argv) or 1)
    assert launch.main([]) == 1
    assert calls == [["codex", "--model", "gpt-6.2-astra"]]
    assert "codex login" in capsys.readouterr().err


@pytest.mark.parametrize("remote", [["--remote", "unix:///existing.sock"],
                                   ["--remote=unix:///existing.sock"]])
@pytest.mark.parametrize("model", [[], ["-m", "worker"]])
def test_explicit_remote_attachment_preserves_endpoint_and_model(monkeypatch, remote, model):
    monkeypatch.setattr(launch.shutil, "which", lambda n: "/bin/codex")
    monkeypatch.setattr(launch.os, "name", "posix")
    monkeypatch.setattr(launch, "ensure_daemon", lambda *a: pytest.fail("explicit endpoint is caller-owned"))
    seen = []
    monkeypatch.setattr(launch.os, "execv", lambda p, argv: seen.append(argv))
    assert launch.main([*remote, *model]) == 0
    selected = model or ["--model", "gpt-6.2-astra"]
    assert seen == [["/bin/codex", *remote, *selected]]
    assert os.environ.get(launch.EXPECTED_MODEL_ENV) == (None if model else "gpt-6.2-astra")


def test_hook_main_reports_startup_mismatch(monkeypatch, capsys):
    import io
    monkeypatch.setenv("COORDINATOR_CODEX_EXPECTED_MODEL", "gpt-6.2-astra")
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO('{"model": "gpt-6-luna"}'))
    monkeypatch.setattr(hook.sys, "argv", ["hook"])
    assert hook.main() == 1
    assert "setup failed: startup model gpt-6-luna" in capsys.readouterr().out


def test_hook_run_reports_verified_startup_and_leaves_workers_alone(monkeypatch):
    monkeypatch.setenv("COORDINATOR_CODEX_EXPECTED_MODEL", "gpt-6.2-astra")
    assert "observed gpt-6.2-astra" in hook.run({"model": "gpt-6.2-astra"})["systemMessage"]
    assert hook.verify_startup_model({"agent_id": "worker", "model": "gpt-6-luna"}) is None

SOCK = "/home/u/.codex/app-server-control/app-server-control.sock"  # abs-path-ok: synthetic socket path fixture, never opened


def test_standard_launch_model_is_the_codex_em_role_model_not_a_literal():
    from coordinator_core.agent_pipe.model_tiers import model_for_role

    em = model_for_role("codex", "em")
    assert not hasattr(launch, "STANDARD_MODEL")
    assert launch.select_model(["--", "x"]) == (["--model", em, "--", "x"], em)
    assert launch.select_model(["-m", "mine"]) == (["-m", "mine"], None)


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
    reg = _bind(tmp_path, T1, "proj-codex-box-50")
    work = tmp_path / "My Proj"
    work.mkdir()
    out = hook.run({"session_id": T1, "cwd": str(work)}, peers_file=pf, registry_dir=reg)
    rows = contract.load_peers(pf)
    assert len(rows) == 1 and rows[0]["vendor"] == "codex" and rows[0]["target"] == T1
    assert rows[0]["name"] == "proj-codex-box-50"
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


def _nt_main(monkeypatch, tmp_path, *, real):
    alias = tmp_path / "WindowsApps"
    alias.mkdir()
    (alias / "pwsh.exe").write_text("")
    pf = tmp_path / "ProgramFiles"
    real_dir = pf / "PowerShell" / "7"
    real_dir.mkdir(parents=True)
    if real:
        (real_dir / "pwsh.exe").write_text("")
    inherited = os.pathsep.join([str(alias), str(real_dir)])
    monkeypatch.setenv("PATH", inherited)
    monkeypatch.setenv("ProgramFiles", str(pf))
    # Import before faking os.name: pathlib refuses to build module-level Paths once os.name is "nt" on a POSIX host.
    sys.path.insert(0, str(launch.HERE.parents[1]))
    import coordinator_core.codex_runner.codex_spawn_env  # noqa: F401
    monkeypatch.setattr(launch.os, "name", "nt")
    monkeypatch.setattr(launch.shutil, "which", lambda n: "codex.exe")
    seen = {}
    monkeypatch.setattr(launch.subprocess, "call", lambda argv, **kw: seen.update(argv=argv, **kw) or 0)
    assert launch.main(["-m", "x"]) == 0
    return seen, inherited, alias, real_dir


def test_windows_main_spawns_codex_with_real_pwsh_first_and_alias_dropped(monkeypatch, tmp_path):
    seen, inherited, alias, real_dir = _nt_main(monkeypatch, tmp_path, real=True)
    assert seen["argv"] == ["codex.exe", "-m", "x"]
    path = next(v for k, v in seen["env"].items() if k.upper() == "PATH").split(os.pathsep)
    assert path == [str(real_dir)] and str(alias) not in path


def test_windows_main_without_real_pwsh_passes_the_env_unchanged(monkeypatch, tmp_path):
    seen, inherited, *_ = _nt_main(monkeypatch, tmp_path, real=False)
    assert next(v for k, v in seen["env"].items() if k.upper() == "PATH") == inherited
