"""headless-em-launch: an EM launch writes a grant that passes require_fire_authority; refusals write none."""
import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

from coordinator_core.ops.workflow_fire import authority
from coordinator_core.ops.workflow_fire import headless_em_grant as g

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("headless_em_launch", BIN / "headless-em-launch.py")
hel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hel)

_IDENTITY_VARS = ("COORDINATOR_SESSION_ID", "CODEX_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID",
                  "COORDINATOR_AGENT_SESSION", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
                  "COORDINATOR_AGENT_WORKER", "COORDINATOR_AGENT_ROLE", g.HEADLESS_EM_TOKEN_ENV)


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in _IDENTITY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(g.SETTINGS_HOME_KEY, str(tmp_path / "settings"))
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.delenv("npm_config_prefix", raising=False)
    for fam in ("CODEX", "AGY", "GROK"):
        monkeypatch.delenv(f"COORDINATOR_{fam}_CLI", raising=False)
    return tmp_path / "settings"


def _add_dir() -> list[str]:
    from coordinator_core.locked_write import _machine_lock_dir

    return ["--add-dir", str(_machine_lock_dir().parent)]



def _run(family="codex", env=None, **kw):
    """Launch with a recording spawn; returns (rc, [(cli, argv)], the child's env, else the caller's env)."""
    calls, env = [], {} if env is None else env
    seen = {}

    def spawn(cmd, child_env):
        calls.append((cmd[0], cmd))
        seen.update(child_env)
        return code

    code = kw.pop("code", 0)
    kw.setdefault("open_runs", lambda root, sid: [])
    rc = hel.launch(family, list(kw.pop("extra", ())), which=lambda n: f"/bin/{n}",
                    spawn=spawn, environ=env, **{"platform": "linux", **kw})
    return rc, calls, (dict(seen) if calls else env)


def _fire(monkeypatch, child_env):
    """Run require_fire_authority as the child: its env becomes the process env."""
    for k, v in child_env.items():
        monkeypatch.setenv(k, v)
    return authority.require_fire_authority("s.py", None, cwd=None, native=True, backend="codex")


EXEC_FAMILIES = tuple(f for f in hel.FAMILIES if f != "claude")


@pytest.mark.parametrize("family", EXEC_FAMILIES)
def test_launched_em_passes_require_fire_authority(family, monkeypatch: pytest.MonkeyPatch) -> None:
    rc, calls, env = _run(family, extra=["-m", "x"])
    assert rc == 0
    head = _add_dir() if family == "codex" else []
    assert calls == [(f"/bin/{family}", [f"/bin/{family}", *head, "-m", "x"])]
    assert g.headless_em_grant_matches(env, env[hel.SESSION_ENV], family)
    caller = _fire(monkeypatch, env)
    assert caller.session_id == env[hel.SESSION_ENV]


def test_explicit_session_id_wins_over_env_and_is_granted(monkeypatch: pytest.MonkeyPatch) -> None:
    rc, _, env = _run(session_id="sess-explicit", env={hel.SESSION_ENV: "sess-env"})
    assert rc == 0 and env[hel.SESSION_ENV] == "sess-explicit"
    assert g.grant_path("sess-explicit").exists() and not g.grant_path("sess-env").exists()


def test_env_session_id_is_used_when_no_flag() -> None:
    _, _, env = _run(env={hel.SESSION_ENV: "sess-env"})
    assert env[hel.SESSION_ENV] == "sess-env" and g.grant_path("sess-env").exists()


def test_grant_for_other_session_does_not_admit(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, env = _run(session_id="sess-a")
    env = {**env, hel.SESSION_ENV: "sess-b"}
    with pytest.raises(ValueError):
        _fire(monkeypatch, env)


@pytest.mark.parametrize("marker", [{"COORDINATOR_AGENT_WORKER": "1"}, {"COORDINATOR_AGENT_ROLE": "worker"}])
def test_worker_env_cannot_mint_a_grant(marker, home) -> None:
    rc, calls, env = _run(env=dict(marker))
    assert rc == hel.REFUSED and not calls
    assert g.HEADLESS_EM_TOKEN_ENV not in env and not (home / "state").exists()


def test_worker_marker_in_child_env_still_refuses_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, env = _run()
    with pytest.raises(ValueError):
        _fire(monkeypatch, {**env, "COORDINATOR_AGENT_WORKER": "1"})


def test_missing_cli_exits_127_and_writes_no_grant(home) -> None:
    calls, env = [], {}
    rc = hel.launch("codex", [], which=lambda n: None, spawn=lambda *a: calls.append(a), environ=env,
                    platform="linux")
    assert rc == 127 and not calls and not (home / "state").exists()


def test_unknown_family_refused(home) -> None:
    rc, calls, _ = _run("gemini")
    assert rc == hel.REFUSED and not calls and not (home / "state").exists()


def test_windows_exec_refused_without_grant(home) -> None:
    rc, calls, env = _run(platform="win32")
    assert rc == hel.REFUSED and not calls and not env and not (home / "state").exists()


def test_windows_prepare_emits_token_without_executing(capsys: pytest.CaptureFixture[str]) -> None:
    calls, env = [], {}
    rc = hel.launch("codex", [], which=lambda n: None, spawn=lambda *a: calls.append(a), environ=env,
                    platform="win32", prepare_only=True, native_executable="codex.exe")
    plan = json.loads(capsys.readouterr().out)
    assert rc == 0 and not calls and not env
    assert plan["argv"] == ["codex.exe", *_add_dir()]
    assert g.headless_em_grant_matches(plan["environment"], plan["session_id"], "codex")


def _win_prepare(family, which, capsys, **kw):
    rc = hel.launch(family, [], which=which, spawn=lambda *a: 0, environ={}, platform="win32",
                    prepare_only=True, **kw)
    return rc, capsys.readouterr()


@pytest.mark.parametrize("family", hel.FAMILIES)
def test_windows_native_exe_on_path_used_as_is(family, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = _win_prepare(family, lambda n: f"pathbin/{n}" if n.endswith(".exe") else None, capsys)
    tail = ["--bg"] if family == "claude" else _add_dir() if family == "codex" else []
    assert rc == 0 and json.loads(out.out)["argv"] == [f"pathbin/{family}.exe", *tail]


def test_windows_codex_cmd_shim_maps_to_native_exe(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    shim = tmp_path / "codex.cmd"
    exe = (tmp_path / "node_modules" / "@openai" / "codex" / "vendor" / "x86_64-pc-windows-msvc" / "codex" / "codex.exe")
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    import platform
    if platform.machine().lower() in {"arm64", "aarch64"}:
        pytest.skip("x64 fixture layout")
    rc, out = _win_prepare("codex", lambda n: str(shim) if n == "codex" else None, capsys)
    assert rc == 0 and json.loads(out.out)["argv"] == [str(exe), *_add_dir()]


@pytest.mark.parametrize("family", hel.FAMILIES)
def test_windows_shim_without_native_package_refused_naming_flag(family, tmp_path: Path, capsys: pytest.CaptureFixture[str], home, monkeypatch: pytest.MonkeyPatch) -> None:
    # resolve_native_cli falls back to the real <home>/.<family>/bin exe; point home at an empty dir.
    for var in ("USERPROFILE", "HOME"):
        monkeypatch.setenv(var, str(tmp_path / "nohome"))
    rc, out = _win_prepare(family, lambda n: str(tmp_path / f"{family}.cmd") if n == family else None, capsys)
    assert rc == hel.REFUSED and out.out == "" and "--native-executable" in out.err
    assert not (home / "state").exists()


def test_windows_native_executable_flag_wins_over_path(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = _win_prepare("agy", lambda n: "pathbin/agy.exe", capsys, native_executable="given/agy.exe")
    assert rc == 0 and json.loads(out.out)["argv"] == ["given/agy.exe"]


def test_main_parses_family_session_and_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}
    monkeypatch.setattr(hel, "launch", lambda family, extra, **k: seen.update(k, family=family, extra=extra) or 0)
    assert hel.main(["grok", "--session-id", "s1", "--prepare-only", "--", "-p", "hi"]) == 0
    assert seen["family"] == "grok" and seen["session_id"] == "s1" and seen["prepare_only"] is True
    assert seen["extra"] == ["-p", "hi"]


def test_main_without_args_refuses() -> None:
    assert hel.main([]) == hel.REFUSED


def _pwsh_env(tmp_path, *, real: bool = True, path_key: str = "Path"):
    """A Windows-shaped caller env: WindowsApps alias dir first, real pwsh dir (when `real`) later."""
    alias = tmp_path / "WindowsApps"
    alias.mkdir()
    (alias / "pwsh.exe").write_text("")
    bin_dir = tmp_path / "private-bin"
    bin_dir.mkdir()
    pf = tmp_path / "ProgramFiles"
    real_dir = pf / "PowerShell" / "7"
    real_dir.mkdir(parents=True)
    if real:
        (real_dir / "pwsh.exe").write_text("")
    inherited = os.pathsep.join([str(bin_dir), str(alias), str(real_dir)])
    return {path_key: inherited, "ProgramFiles": str(pf)}, inherited, alias, real_dir, bin_dir


def _prepare(env, capsys, family="codex", platform="win32"):
    rc = hel.launch(family, [], which=lambda n: None, spawn=lambda *a: 0, environ=env, platform=platform,
                    prepare_only=True, native_executable=f"{family}.exe")
    assert rc == 0
    return json.loads(capsys.readouterr().out)["environment"]


def test_windows_codex_prepare_carries_shaped_path_real_pwsh_first_alias_dropped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env, inherited, alias, real_dir, bin_dir = _pwsh_env(tmp_path)
    shaped = _prepare(env, capsys)["PATH"].split(os.pathsep)
    assert shaped == [str(bin_dir), str(real_dir)]
    assert str(alias) not in shaped


@pytest.mark.parametrize("path_key", ["Path", "PATH"])
def test_windows_codex_prepare_reads_path_by_case_insensitive_key(tmp_path: Path, capsys: pytest.CaptureFixture[str], path_key) -> None:
    env, *_ = _pwsh_env(tmp_path, path_key=path_key)
    assert "PATH" in _prepare(env, capsys)


def test_windows_codex_prepare_without_real_pwsh_emits_no_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env, *_ = _pwsh_env(tmp_path, real=False)
    assert "PATH" not in _prepare(env, capsys)


def test_windows_codex_prepare_with_no_caller_path_emits_no_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env, *_ = _pwsh_env(tmp_path)
    del env["Path"]
    assert "PATH" not in _prepare(env, capsys)


def test_windows_codex_prepare_with_already_shaped_path_emits_no_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env, _, _, real_dir, bin_dir = _pwsh_env(tmp_path)
    env["Path"] = os.pathsep.join([str(bin_dir), str(real_dir)])
    assert "PATH" not in _prepare(env, capsys)


@pytest.mark.parametrize("family", ["codex", "grok", "agy", "claude"])
def test_prepare_environment_carries_the_family_as_agent_provider(family, capsys: pytest.CaptureFixture[str]) -> None:
    assert _prepare({"PATH": "x"}, capsys, family=family, platform="linux")["COORDINATOR_AGENT_PROVIDER"] == family


def test_launch_child_env_carries_the_family_as_agent_provider() -> None:
    rc, calls, env = _run("codex", env={"PATH": "x"})
    assert rc == 0 and env["COORDINATOR_AGENT_PROVIDER"] == "codex"


def test_windows_grok_never_gets_a_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env, *_ = _pwsh_env(tmp_path)
    assert "PATH" not in _prepare(env, capsys, family="grok")


def _fake_invoke(home):
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / ("coordinator-invoke.exe" if os.name == "nt" else "coordinator-invoke")
    exe.write_text("")
    exe.chmod(0o755)
    return bin_dir, exe


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_agy_prepare_path_resolves_coordinator_invoke(platform, home, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bin_dir, exe = _fake_invoke(home)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = {"PATH": str(elsewhere), g.SETTINGS_HOME_KEY: str(home)}
    child = _prepare(env, capsys, family="agy", platform=platform)
    entries = child["PATH"].split(os.pathsep)
    assert str(bin_dir) in entries and str(elsewhere) in entries
    assert Path(shutil.which("coordinator-invoke", path=child["PATH"])) == exe


def test_agy_prepare_without_caller_path_still_carries_the_bin_dir(home, capsys: pytest.CaptureFixture[str]) -> None:
    bin_dir, _ = _fake_invoke(home)
    child = _prepare({g.SETTINGS_HOME_KEY: str(home)}, capsys, family="agy", platform="linux")
    assert child["PATH"].split(os.pathsep) == [str(bin_dir)]


def test_agy_prepare_does_not_duplicate_a_bin_dir_already_on_path(home, capsys: pytest.CaptureFixture[str]) -> None:
    bin_dir, _ = _fake_invoke(home)
    env = {"PATH": os.pathsep.join(["x", str(bin_dir)]), g.SETTINGS_HOME_KEY: str(home)}
    child = _prepare(env, capsys, family="agy", platform="linux")
    assert child["PATH"].split(os.pathsep).count(str(bin_dir)) == 1


def test_agy_bin_dir_falls_back_to_default_settings_home_under_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv(g.SETTINGS_HOME_KEY, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    child = _prepare({"PATH": "x"}, capsys, family="agy", platform="linux")
    assert str(tmp_path / ".coordinator-claude-settings" / "bin") in child["PATH"].split(os.pathsep)


@pytest.mark.parametrize("family", ["codex", "grok", "claude"])
def test_agy_path_shaping_never_touches_other_families(family, home, capsys: pytest.CaptureFixture[str]) -> None:
    _fake_invoke(home)
    env = {"PATH": "x", g.SETTINGS_HOME_KEY: str(home)}
    assert "PATH" not in _prepare(env, capsys, family=family, platform="linux")


def test_non_windows_codex_launch_leaves_the_env_without_a_path_change(tmp_path: Path) -> None:
    env, inherited, *_ = _pwsh_env(tmp_path)
    rc, calls, env = _run("codex", env=env)
    assert rc == 0 and env["Path"] == inherited and "PATH" not in env


class _Done:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _bg(platform, capsys, stdout: str = "started 1a2b3c4d\n", returncode: int = 0, **kw):
    seen = []

    def runner(argv, **k) -> _Done:
        seen.append((argv, k))
        return _Done(stdout, returncode)

    execs = []
    rc = hel.launch("claude", [], which=lambda n: "claude.exe" if platform == "win32" else "/bin/claude",
                    spawn=lambda *a: execs.append(a), environ={}, platform=platform, runner=runner,
                    cwd="/repo", **kw)
    out = capsys.readouterr()
    assert not execs
    return rc, seen, out


def test_claude_grant_is_accepted() -> None:
    """write_headless_em_grant accepts claude and the grant matches."""
    token = g.write_headless_em_grant("s1", "claude")
    assert g.headless_em_grant_matches({g.HEADLESS_EM_TOKEN_ENV: token}, "s1", "claude")


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_claude_launch_runs_bg_with_name_and_returns_printed_id(platform, capsys: pytest.CaptureFixture[str]) -> None:
    rc, seen, out = _bg(platform, capsys, name="fleet-a")
    (argv, kw), = seen
    assert rc == 0
    assert argv[1:] == ["--bg", "-n", "fleet-a"]
    assert kw["cwd"] == "/repo" and kw["capture_output"] is True and kw["timeout"] == hel.BG_TIMEOUT_S
    assert g.headless_em_grant_matches(kw["env"], kw["env"][hel.SESSION_ENV], "claude")
    assert json.loads(out.out) == {"name": "fleet-a", "id": "1a2b3c4d", "session_id": kw["env"][hel.SESSION_ENV]}


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_claude_resume_runs_bg_resume_id(platform, capsys: pytest.CaptureFixture[str]) -> None:
    rc, seen, out = _bg(platform, capsys, resume="1a2b3c4d")
    assert rc == 0 and seen[0][0][1:] == ["--bg", "--resume", "1a2b3c4d"]
    assert json.loads(out.out)["id"] == "1a2b3c4d"


def test_claude_uuid_id_is_returned(capsys: pytest.CaptureFixture[str]) -> None:
    uid = "0b6f8a52-1c3d-4e5f-8a9b-0c1d2e3f4a5b"
    rc, _, out = _bg("linux", capsys, stdout=f"session {uid}\n", name="n")
    assert rc == 0 and json.loads(out.out)["id"] == uid


@pytest.mark.parametrize("kw", [{"stdout": "no id here\n"}, {"returncode": 1}])
def test_claude_bg_failure_or_missing_id_is_refused(kw, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _, out = _bg("linux", capsys, name="n", **kw)
    assert rc == hel.REFUSED and out.out == ""


def test_name_on_non_claude_family_refused(home) -> None:
    rc, calls, _ = _run("codex", name="x")
    assert rc == hel.REFUSED and not calls and not (home / "state").exists()


def test_windows_non_claude_exec_still_refused(home) -> None:
    rc, calls, env = _run("grok", platform="win32")
    assert rc == hel.REFUSED and not calls and not (home / "state").exists()


def test_main_forwards_name_resume_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}
    monkeypatch.setattr(hel, "launch", lambda family, extra, **k: seen.update(k, family=family) or 0)
    assert hel.main(["claude", "--name", "n1", "--cwd", "/r"]) == 0
    assert seen["name"] == "n1" and seen["cwd"] == "/r" and seen["resume"] is None
    assert hel.main(["claude", "--resume", "abc123"]) == 0 and seen["resume"] == "abc123"


_OPEN = [{"fire_id": "fire-1", "run_id": "run-1", "stage": "staged", "fire_state": "running"}]


@pytest.mark.parametrize("family", EXEC_FAMILIES)
def test_open_run_makes_a_clean_child_exit_return_3_naming_the_run_and_its_wait(family, capsys):
    asked = []
    rc, calls, env = _run(family, open_runs=lambda root, sid: asked.append(sid) or _OPEN)
    assert rc == hel.EXIT_OPEN_RUNS == 3 and calls
    assert asked == [env[hel.SESSION_ENV]]
    assert capsys.readouterr().err.strip() == (
        "headless-em-launch: run run-1 is staged; wait: coordinator-invoke "
        "workflow.fire_wait '{\"fire_id\": \"fire-1\"}'")


def test_open_run_with_nonzero_child_returns_the_child_code_and_still_reports(capsys):
    rc, *_ = _run(code=7, open_runs=lambda root, sid: _OPEN)
    assert rc == 7 and "run run-1 is staged" in capsys.readouterr().err


def test_open_run_clean_case_returns_the_childs_zero(capsys):
    rc, *_ = _run(open_runs=lambda root, sid: [])
    assert rc == 0 and capsys.readouterr().err == ""


def test_open_run_check_failure_is_not_a_clean_exit(capsys):
    def boom(root, sid):
        raise RuntimeError("not inside a git repository")

    rc, *_ = _run(open_runs=boom)
    assert rc == hel.EXIT_OPEN_RUNS and "exit check failed" in capsys.readouterr().err


def test_open_run_gate_reads_real_fire_records_for_the_launched_session(tmp_path, monkeypatch, capsys):
    import json as _json

    from coordinator_core.ops.dispatch_emit.ask_contract import write_run_stage
    from coordinator_core.ops.workflow_fire import fire

    reg = tmp_path / "fires"
    reg.mkdir()
    monkeypatch.setattr(fire, "_registry_dir", lambda *a: reg)
    (reg / "fire-1.json").write_text(_json.dumps(
        {"fire_id": "fire-1", "state": "exited", "run_id": "run-1", "caller_session_id": "sess-x"}))
    write_run_stage(tmp_path, "run-1", "staged")
    rc, *_ = _run(session_id="sess-x", cwd=str(tmp_path), open_runs=None)
    assert rc == 3 and "run run-1 is staged" in capsys.readouterr().err
    write_run_stage(tmp_path, "run-1", "landed")
    rc, *_ = _run(session_id="sess-x", cwd=str(tmp_path), open_runs=None)
    assert rc == 0


@pytest.mark.skipif(os.name == "nt", reason="Windows has no signal deaths: SIGTERM is a plain exit code 15")
def test_spawn_maps_a_signal_death_to_128_plus_signum():
    import sys
    code = hel._spawn([sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"],
                      dict(os.environ))
    assert code == 128 + 15


def test_exit_check_flag_runs_only_the_check_without_a_family(capsys):
    assert hel.main(["--exit-check", "--session-id", "s1"], open_runs=lambda r, s: _OPEN) == 3
    assert "run run-1 is staged" in capsys.readouterr().err
    assert hel.main(["--exit-check", "--session-id", "s1"], open_runs=lambda r, s: []) == 0


def test_exit_check_flag_requires_a_session_id(capsys):
    assert hel.main(["--exit-check"]) == hel.REFUSED
    assert "--session-id" in capsys.readouterr().err


@pytest.mark.parametrize("family", ["codex", "agy", "grok"])
def test_prepare_only_json_names_the_exit_check_argv(family, capsys):
    import sys
    rc = hel.launch(family, [], which=lambda n: None, environ={}, platform="win32", prepare_only=True,
                    native_executable=f"{family}.exe", session_id="s9")
    plan = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert plan["exit_check"] == [sys.executable, str(Path(hel.__file__).resolve()),
                                  "--exit-check", "--session-id", "s9"]


def test_codex_gets_the_machine_lock_root_as_a_writable_dir() -> None:
    rc, calls, _ = _run("codex", env={"PATH": "x"}, extra=["--model", "m"])
    assert rc == 0 and calls[0][1] == ["/bin/codex", *_add_dir(), "--model", "m"]


def test_codex_exec_takes_the_dir_after_the_subcommand() -> None:
    rc, calls, _ = _run("codex", env={"PATH": "x"}, extra=["exec", "do it"])
    assert rc == 0 and calls[0][1] == ["/bin/codex", "exec", *_add_dir(), "do it"]


def test_codex_dir_already_passed_is_not_doubled() -> None:
    rc, calls, _ = _run("codex", env={"PATH": "x"}, extra=_add_dir())
    assert rc == 0 and calls[0][1] == ["/bin/codex", *_add_dir()]


@pytest.mark.parametrize("family", ["agy", "grok"])
def test_other_families_get_no_add_dir(family) -> None:
    rc, calls, _ = _run(family, env={"PATH": "x"}, extra=["x"])
    assert rc == 0 and "--add-dir" not in calls[0][1]


def test_codex_cwd_becomes_the_working_dir_flag() -> None:
    rc, calls, _ = _run("codex", env={"PATH": "x"}, extra=["exec", "do it"], cwd="repo/sub")
    assert rc == 0 and calls[0][1] == ["/bin/codex", "exec", *_add_dir(), "-C", "repo/sub", "do it"]


def test_codex_caller_working_dir_flag_wins_over_cwd() -> None:
    rc, calls, _ = _run("codex", env={"PATH": "x"}, extra=["exec", "-C", "mine"], cwd="repo/sub")
    assert rc == 0 and calls[0][1] == ["/bin/codex", "exec", *_add_dir(), "-C", "mine"]
