"""headless-em-launch: an EM launch writes a grant that passes require_fire_authority; refusals write none."""
import importlib.util
import json
import os
import shutil
import sys
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
    monkeypatch.chdir(tmp_path)
    for fam in ("CODEX", "AGY", "GROK"):
        monkeypatch.delenv(f"COORDINATOR_{fam}_CLI", raising=False)
    (tmp_path / "settings" / "unimatrix" / "agy-plugin" / "skills").mkdir(parents=True)  # agy_grant_guard grants an existing dir only
    return tmp_path / "settings"


def _add_dir() -> list[str]:
    from coordinator_core.locked_write import _machine_lock_dir

    return ["--add-dir", str(_machine_lock_dir().parent)]


_GROK_OPS = ("engine_ask.submit", "workflow.fire", "workflow.fire_status", "workflow.fire_wait",
             "workflow.revise")


def _grok_head(platform="linux", settings_home=None, *, model=True, effort=True) -> list[str]:
    exe = Path(settings_home or Path.home() / ".coordinator-claude-settings") / "bin" / (
        "coordinator-invoke.exe" if platform == "win32" else "coordinator-invoke")
    bin_dir = exe.parent
    suffix = ".exe" if platform == "win32" else ""
    workers = [a for n in ("sizing-assemble", "coordinator-doc-new")
               for a in ("--allow", f"Bash({bin_dir / (n + suffix)}:*)")]
    from coordinator_core.agent_pipe.model_tiers import GROK_EM_EFFORT, model_for_role

    body = ["--permission-mode", "auto",
            *[a for op in _GROK_OPS for a in ("--allow", f"Bash({exe} {op}:*)")], *workers]
    if model:
        body += ["--model", model_for_role("grok", "em")]
    if effort:
        body += ["--reasoning-effort", GROK_EM_EFFORT]
    return body


def test_grok_workers_may_run_only_named_engine_executables() -> None:
    args = hel._grok_launch_args([], {}, "linux")
    allows = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--allow"]
    bin_dir = Path.home() / ".coordinator-claude-settings" / "bin"
    assert f"Bash({bin_dir / 'sizing-assemble'}:*)" in allows
    assert f"Bash({bin_dir / 'coordinator-doc-new'}:*)" in allows
    assert not any(r in ("Bash(*)", "Bash") or r.startswith("Bash(*") for r in allows)
    assert all(str(bin_dir) in r for r in allows)


def _agy_skills(settings_home=None) -> str:
    return str(Path(settings_home or os.environ[g.SETTINGS_HOME_KEY])
               / "unimatrix" / "agy-plugin" / "skills")


def _agy_head(cwd=None, settings_home=None) -> list[str]:
    return ["--add-dir", cwd or os.getcwd(), "--add-dir", str(Path.home() / ".coordinator"),
            "--add-dir", _agy_skills(settings_home), "--dangerously-skip-permissions"]


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
    head = {"codex": _add_dir(), "grok": _grok_head(model=False), "agy": _agy_head()}.get(family, [])
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
    tail = ["--bg"] if family == "claude" else _add_dir() if family == "codex" else \
        _grok_head("win32") if family == "grok" else _agy_head() if family == "agy" else []
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
    assert rc == 0 and json.loads(out.out)["argv"] == ["given/agy.exe", *_agy_head()]


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


def test_grok_gets_no_add_dir() -> None:
    rc, calls, _ = _run("grok", env={"PATH": "x"}, extra=["x"])
    assert rc == 0 and "--add-dir" not in calls[0][1]


def _agy_prepare(capsys, extra=(), cwd=None) -> list[str]:
    """The agy argv from the default --prepare-only invocation."""
    rc = hel.launch("agy", list(extra), which=lambda n: None, environ={"PATH": "x"}, platform="linux",
                    prepare_only=True, native_executable="agy", session_id="s1", cwd=cwd)
    assert rc == 0
    return json.loads(capsys.readouterr().out)["argv"]


# Flag sources: `agy --help` (1.4.x) lists `--add-dir  Add a directory to the workspace (repeatable)`,
# `--mode  ... (accept-edits, auto, plan)` and `--dangerously-skip-permissions  Auto-approve all tool
# permission requests without prompting`; the permissions doc (docs/research/claude-klabauter-klabauter-on-agy-codex-9c3013
# gap-report) names accept-edits as a per-invocation non-bypass mode.
def test_agy_prepare_grants_cwd_and_coordinator_home_with_accept_edits(capsys) -> None:
    argv = _agy_prepare(capsys, ["-p", "hi", "--output-format", "json"], cwd="work/tree")
    assert argv == ["agy", *_agy_head("work/tree"), "-p", "hi", "--output-format", "json"]


def _skills(tmp_path):
    skills = tmp_path / "sh" / "unimatrix" / "agy-plugin" / "skills"
    (skills / "sizing").mkdir(parents=True)
    (skills / "sizing" / "SKILL.md").write_text("v1", encoding="utf-8")
    return skills


def test_agy_grants_only_the_plugin_skills_never_the_settings_home_or_engine_root(tmp_path, capsys) -> None:
    """The measured read set is <settings home>/unimatrix/agy-plugin/skills; the roots themselves stay out."""
    skills = _skills(tmp_path)
    env = {"PATH": "x", g.SETTINGS_HOME_KEY: str(tmp_path / "sh"), "COORDINATOR_ENGINE_ROOT": str(tmp_path / "eng")}
    hel.launch("agy", ["-p", "hi"], which=lambda n: None, environ=env, platform="linux",
               prepare_only=True, native_executable="agy", session_id="s1", cwd="w")
    argv = json.loads(capsys.readouterr().out)["argv"]
    granted = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
    assert str(skills) in granted
    assert str(tmp_path / "sh") not in granted and str(tmp_path / "eng") not in granted
    assert str(tmp_path / "sh" / "bin") not in granted


def _guarded_launch(tmp_path, stage):
    _skills(tmp_path)
    env = {"PATH": "x", g.SETTINGS_HOME_KEY: str(tmp_path / "sh")}
    return hel.launch("agy", ["-p", "hi"], which=lambda n: None, environ=env, platform="linux",
                      native_executable="agy", session_id="s1", cwd=str(tmp_path),
                      spawn_captured=stage, open_runs=lambda *a: [])


def test_a_write_into_a_granted_dir_refuses_with_agy_grant_tamper_and_discards_output(tmp_path, capsys) -> None:
    skills = tmp_path / "sh" / "unimatrix" / "agy-plugin" / "skills" / "sizing" / "SKILL.md"

    def stage(cmd, env):
        skills.write_text("tampered", encoding="utf-8")
        return 0, b"STAGE-RESULT", b""

    rc = _guarded_launch(tmp_path, stage)
    out = capsys.readouterr()
    assert rc == hel.REFUSED
    assert "STAGE-RESULT" not in out.out and "STAGE-RESULT" not in out.err
    refusal = json.loads(out.err.strip().splitlines()[-1])
    assert refusal["refused"] == "agy_grant_tamper" and refusal["paths"] == [str(skills)]
    from coordinator_core import agy_grant_guard
    assert agy_grant_guard.read_marker()["paths"] == [str(skills)]


def test_an_untouched_grant_releases_the_stage_output(tmp_path, capsysbinary) -> None:
    rc = _guarded_launch(tmp_path, lambda cmd, env: (0, b"STAGE-RESULT", b""))
    assert rc == 0 and b"STAGE-RESULT" in capsysbinary.readouterr().out


def test_agy_prepare_without_cwd_grants_the_launchers_cwd(capsys) -> None:
    argv = _agy_prepare(capsys, ["-p", "hi"])
    assert argv[1:3] == ["--add-dir", os.getcwd()]


@pytest.mark.parametrize("extra", [["-p", "hi"], ["--dangerously-skip-permissions", "-p", "hi"]])
def test_default_agy_launch_carries_the_bypass_flag_once(capsys, extra) -> None:
    argv = _agy_prepare(capsys, extra, cwd="w")
    assert argv.count("--dangerously-skip-permissions") == 1
    assert "--mode" not in argv and argv[-2:] == ["-p", "hi"]


@pytest.mark.parametrize("mode", [["--mode", "plan"], ["--mode=accept-edits"], ["--sandbox"]])
def test_agy_callers_explicit_mode_is_kept_without_the_bypass_flag(capsys, mode) -> None:
    argv = _agy_prepare(capsys, [*mode, "-p", "hi"], cwd="w")
    assert "--dangerously-skip-permissions" not in argv
    assert all(a in argv for a in mode) and argv[-2:] == ["-p", "hi"]


def test_agy_dirs_already_passed_are_not_doubled(capsys) -> None:
    mine = str(Path.home() / ".coordinator")
    skills = _agy_skills()
    argv = _agy_prepare(capsys, ["--add-dir", mine, "--add-dir", "w", "--add-dir", skills], cwd="w")
    assert argv.count("--add-dir") == 3 and argv.count(mine) == 1 and argv.count("w") == 1
    assert argv.count(skills) == 1


def test_agy_grants_the_generated_plugin_skills_dir_and_nothing_wider(home, capsys) -> None:
    env = {"PATH": "x", g.SETTINGS_HOME_KEY: str(home)}
    rc = hel.launch("agy", [], which=lambda n: None, environ=env, platform="linux", prepare_only=True,
                    native_executable="agy", session_id="s1", cwd="w")
    assert rc == 0
    argv = json.loads(capsys.readouterr().out)["argv"]
    granted = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--add-dir"]
    skills = home / "unimatrix" / "agy-plugin" / "skills"
    assert granted.count(str(skills)) == 1
    for wider in (home, home / "unimatrix", home / "unimatrix" / "agy-plugin"):
        assert str(wider) not in granted

    def covered(path: Path) -> bool:
        return any(path == Path(d) or Path(d) in path.parents for d in granted)

    assert covered(skills / "sizing" / "SKILL.md")
    assert not covered(home / "unimatrix" / "agy-plugin" / "other.txt")


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_grok_launch_defaults_auto_with_absolute_path_allow_per_engine_op(platform, capsys) -> None:
    env = {"PATH": "x", g.SETTINGS_HOME_KEY: os.environ[g.SETTINGS_HOME_KEY], "HOME": "/h"}
    rc = hel.launch("grok", ["-p", "hi"], which=lambda n: None, environ=env, platform=platform,
                    prepare_only=True, native_executable="grok.exe", session_id="s1")
    argv = json.loads(capsys.readouterr().out)["argv"]
    head = _grok_head(platform, env[g.SETTINGS_HOME_KEY])
    assert rc == 0 and argv[:len(head) + 1] == ["grok.exe", *head]
    assert argv[len(head) + 1] == "--prompt-file" and len(argv) == len(head) + 3
    prompt = Path(argv[-1])
    try:
        body = prompt.read_text(encoding="utf-8")
        assert body == hel.GROK_XS_CARD + "hi"
        assert "A code change in this workspace is done in this session, including one larger than an XS edit." in body
        assert "Do not start a workflow for it." in body
        assert "Finish that code change in one pass." in body
        assert "When that change is in the workspace, stop." in body
        assert "A workspace edit that names three files" not in body
        assert "When the ask names the files to add in this workspace" not in body
        assert "do not edit it again" not in body
        assert "names the files to add" not in body
    finally:
        prompt.unlink(missing_ok=True)
    assert "-p" not in argv
    assert argv[1:3] == ["--permission-mode", "auto"]
    rules = [argv[i + 1] for i, a in enumerate(argv) if a == "--allow"]
    assert len(rules) == len(_GROK_OPS) + 2
    for op in _GROK_OPS:
        (rule,) = [r for r in rules if r.endswith(f" {op}:*)")]
        prefix = rule[len("Bash("):].rsplit(f" {op}:*)", 1)[0]
        assert Path(prefix).is_absolute() and Path(prefix).name.startswith("coordinator-invoke")
    assert not any("$HOME" in a or "~" in a for a in argv)
    assert not {"--always-approve", "--dangerously-skip-permissions", "--yolo"} & set(argv)


def test_grok_prompt_file_keeps_the_sealed_bytes_and_codex_is_not_wrapped(tmp_path: Path, capsys) -> None:
    sealed = tmp_path / "prompt.txt"
    sealed.write_text("SEALED-BODY\n", encoding="utf-8")
    before = sealed.read_bytes()
    env = {"PATH": "x", g.SETTINGS_HOME_KEY: os.environ[g.SETTINGS_HOME_KEY], "HOME": str(tmp_path)}
    rc = hel.launch("grok", ["--prompt-file", str(sealed)], which=lambda n: None, environ=env,
                    platform="linux", prepare_only=True, native_executable="grok", session_id="s-card")
    argv = json.loads(capsys.readouterr().out)["argv"]
    assert rc == 0 and sealed.read_bytes() == before
    wrapped = Path(argv[argv.index("--prompt-file") + 1])
    assert wrapped != sealed
    try:
        assert wrapped.read_text(encoding="utf-8") == hel.GROK_XS_CARD + "SEALED-BODY\n"
    finally:
        wrapped.unlink(missing_ok=True)
    rc = hel.launch("codex", ["exec", "-p", "hi"], which=lambda n: "/bin/codex", environ={"PATH": "x"},
                    platform="linux", prepare_only=True, session_id="s-codex")
    codex_argv = json.loads(capsys.readouterr().out)["argv"]
    assert rc == 0 and codex_argv[codex_argv.index("-p") + 1] == "hi"
    assert "--prompt-file" not in codex_argv


def test_grok_cwd_becomes_the_working_dir_flag() -> None:
    rc, calls, _ = _run("grok", extra=["x"], cwd="repo/sub")
    argv = calls[0][1]
    assert rc == 0 and argv.count("--cwd") == 1 and argv[argv.index("--cwd") + 1] == "repo/sub"


def test_grok_caller_cwd_flag_is_not_doubled() -> None:
    rc, calls, _ = _run("grok", extra=["--cwd", "mine", "x"], cwd="repo/sub")
    argv = calls[0][1]
    assert rc == 0 and argv.count("--cwd") == 1 and argv[argv.index("--cwd") + 1] == "mine"


def test_grok_callers_permission_mode_and_allow_are_kept_not_doubled() -> None:
    mine = next(a for a in reversed(_grok_head()) if a.startswith("Bash("))
    rc, calls, _ = _run("grok", extra=["--permission-mode", "plan", "--allow", mine])
    argv = calls[0][1]
    assert rc == 0 and argv.count("--permission-mode") == 1 and "auto" not in argv
    assert argv.count(mine) == 1 and argv.count("--allow") == len(_GROK_OPS) + 2


@pytest.mark.parametrize("family", ["codex", "agy"])
def test_non_grok_families_get_no_permission_mode_or_allow(family) -> None:
    rc, calls, _ = _run(family, extra=["x"])
    assert rc == 0 and "--permission-mode" not in calls[0][1] and "--allow" not in calls[0][1]


def test_codex_and_grok_launch_never_get_an_agy_mode() -> None:
    for family in ("codex", "grok"):
        rc, calls, _ = _run(family, extra=["x"])
        assert rc == 0 and "--mode" not in calls[0][1]


def test_codex_child_gets_physical_engine_override_paths(tmp_path: Path) -> None:
    mirror = tmp_path / "published"
    mirror.mkdir()
    alias = tmp_path / "engine"
    try:
        alias.symlink_to(mirror, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")
    overrides = dict(COORDINATOR_ENGINE_ROOT=str(alias), COORDINATOR_DOOR_ENGINE_ROOT=str(alias))
    rc, _, child = _run("codex", env=overrides)
    assert rc == 0
    assert {name: child[name] for name in overrides} == dict.fromkeys(overrides, str(mirror.resolve()))
    assert overrides["COORDINATOR_ENGINE_ROOT"] == str(alias)


def test_codex_cwd_becomes_the_working_dir_flag() -> None:
    rc, calls, _ = _run("codex", env={"PATH": "x"}, extra=["exec", "do it"], cwd="repo/sub")
    assert rc == 0 and calls[0][1] == ["/bin/codex", "exec", *_add_dir(), "-C", "repo/sub", "do it"]


def test_codex_caller_working_dir_flag_wins_over_cwd() -> None:
    rc, calls, _ = _run("codex", env={"PATH": "x"}, extra=["exec", "-C", "mine"], cwd="repo/sub")
    assert rc == 0 and calls[0][1] == ["/bin/codex", "exec", *_add_dir(), "-C", "mine"]


def test_codex_launch_precreates_scratch_in_the_cwd_before_the_child_starts(tmp_path: Path) -> None:
    work = tmp_path / "fixture"
    work.mkdir()
    seen = []
    rc = hel.launch("codex", ["exec", "x"], which=lambda n: f"/bin/{n}", environ={"PATH": "x"}, platform="linux",
                    cwd=str(work), open_runs=lambda root, sid: [],
                    spawn=lambda cmd, env: seen.append((work / "scratch" / "warp").is_dir()) or 0)
    assert rc == 0 and seen == [True]


def test_codex_prepare_only_creates_scratch_for_the_caller_working_dir_flag(tmp_path: Path, capsys) -> None:
    work = tmp_path / "mine"
    work.mkdir()
    rc = hel.launch("codex", ["exec", "-C", str(work)], which=lambda n: f"/bin/{n}", environ={"PATH": "x"},
                    platform="linux", prepare_only=True)
    capsys.readouterr()
    assert rc == 0 and (work / "scratch" / "warp").is_dir()


def test_codex_launch_refuses_naming_path_and_principal_when_scratch_cannot_be_made(
        tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "fixture"
    work.mkdir()
    real_mkdir = Path.mkdir

    def deny(self, *a, **kw):
        if self.name in ("warp", "scratch"):
            raise PermissionError(5, "Access is denied")
        return real_mkdir(self, *a, **kw)

    monkeypatch.setattr(Path, "mkdir", deny)
    calls = []
    rc = hel.launch("codex", [], which=lambda n: f"/bin/{n}", environ={"PATH": "x"}, platform="linux",
                    cwd=str(work), spawn=lambda *a: calls.append(a))
    err = capsys.readouterr().err
    assert rc == hel.REFUSED and not calls
    assert "refused" in err and str(work / "scratch") in err and "CodexSandboxUsers" in err


@pytest.mark.parametrize(("command", "expected"), [
    ("/bin/bash -lc 'coordinator-invoke engine_ask.submit --params-file p'", 17),
    ("/bin/bash -lc '/opt/bin/coordinator-invoke workflow.fire --params-file p'", 17),
    ("coordinator-invoke workflow.fire --params-file p && false", 0),
    ("coordinator-invoke workflow.fire_wait", 0),
    ("/bin/bash -lc '/opt/bin/headless-em-launch --resume-mint receipt --cwd repo'", 17),
    ("headless-em-launch --resume-mint receipt && false", 0),
    ("headless-em-launch --help", 0),
    ("echo coordinator-invoke engine_ask.submit", 0),
    ("coordinator-invoke engine_ask.submit --params-file p && false", 0),
])
def test_native_json_child_success_propagates_only_submit_failure(command, expected, capfd):
    event = json.dumps({"type": "item.completed", "item": {
        "type": "command_execution", "command": command, "exit_code": 17}})
    # Raw bytes: a text-mode print writes CRLF on Windows, and the launcher relays bytes verbatim.
    cmd = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.argv[1].encode() + b'\\n')", event]
    assert hel._spawn_codex_events(cmd, dict(os.environ)) == expected
    assert capfd.readouterr().out == event + "\n"


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin-only endpoint")
def test_registered_diagnostic_build_serves_ordinary_exec_without_flags(home, tmp_path, monkeypatch):
    from coordinator_core.warm import restricted_endpoint as endpoint

    monkeypatch.delenv("COORDINATOR_PRIVATE_DIAGNOSTIC_DIRECTORY", raising=False)
    root = home / "native-diagnostics"
    root.mkdir(mode=0o700)
    generation = root / "build-fixture"
    generation.mkdir()
    (root / "current").symlink_to(generation)
    (generation / "socket-directory").write_text(str(tmp_path.resolve()))
    native = generation / "codex"
    native.write_text("#!/bin/sh\nexit 0\n")
    native.chmod(0o700)
    monkeypatch.setenv("COORDINATOR_CODEX_CLI", "/usr/bin/true")
    calls = []
    real = endpoint.diagnostic_spawn
    def record(directory, cmd, env):
        calls.append((directory, cmd[0]))
        # Keep this dispatch test independent of the absolute tmp_path socket length.
        Path("rx").mkdir(mode=0o700)
        return real(Path("rx"), cmd, env)
    monkeypatch.setattr(endpoint, "diagnostic_spawn", record)
    rc = hel.launch("codex", ["exec", "--json"], cwd=str(tmp_path), open_runs=lambda *_: [])
    assert rc == 0 and calls == [(tmp_path.resolve(), str(native))]


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin-only endpoint")
@pytest.mark.parametrize("unsafe", ["generation", "binary", "manifest"])
def test_diagnostic_install_rejects_peer_writable_members(home, unsafe):
    root = home / "native-diagnostics"
    root.mkdir(mode=0o700)
    generation = root / "build-fixture"
    generation.mkdir(mode=0o700)
    (root / "current").symlink_to(generation)
    binary = generation / "codex"
    binary.touch(mode=0o700)
    manifest = generation / "socket-directory"
    manifest.write_text("/private")
    member = {"generation": generation, "binary": binary, "manifest": manifest}[unsafe]
    member.chmod(member.stat().st_mode | 0o020)
    with pytest.raises(PermissionError, match="unsafe"):
        hel._diagnostic_install(os.environ)


@pytest.mark.parametrize("case", ["registered", "unregistered", "grant_only", "indeterminate", "wrong_digest", "missing_handle", "wrong_reply_run", "wrong_reply_digest", "unbound_reply", "script_outside_repo", "changed_args", "pinned", "other_registered_em", "other_registered_workspace", "wrong_route"])
def test_resume_mint_uses_existing_authority_and_exact_fire(case, tmp_path, monkeypatch, capsys):
    import hashlib
    from types import SimpleNamespace
    from coordinator_core.invoke import __main__ as invoke

    root = tmp_path.resolve() / "issued"
    rid = "ask-fixture"
    script = root / "scratch/warp" / (rid + ".workflow.mjs")
    script.parent.mkdir(parents=True)
    script.write_text("// fixture")
    if case == "script_outside_repo":
        outside = tmp_path / "outside.mjs"
        script.rename(outside)
        try:
            script.symlink_to(outside)
        except OSError as exc:
            pytest.skip(f"file symlink unavailable: {exc}")
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    stage = root / "state/warp" / rid / "run-stage.json"
    stage.parent.mkdir(parents=True)
    stage.write_text(json.dumps({"stage": "minted"}))
    args = {"repoRoot": str(root)}
    mint = dict(run_id=rid, script_path=str(script), sha256=digest, fire_args=args,
                fire_error="SandboxedSpawnRefused: warm_disabled")
    if case in ("pinned", "changed_args", "wrong_route"):
        mint["fire_params"] = dict(run_id=rid, script_path=str(script), expected=digest, args=args,
                                   backend="claude" if case == "wrong_route" else "codex", executor="headless",
                                   notify_to="original-recipient", private_engine={"fixture": "selected"},
                                   caller_session_id="original-sender")
    if case == "indeterminate":
        mint["fire"] = {"state": "indeterminate"}
    receipt = root / "submit.json"
    receipt.write_text(json.dumps({"jsonrpc": "2.0", "result": mint}))
    if case == "grant_only":
        principal = authority.Principal("launcher-session", "unknown", {}, grant_verified=True)
        monkeypatch.setattr(authority, "require_fire_authority", lambda *a, **kw: principal)
    elif case != "unregistered":
        monkeypatch.setattr(authority, "require_fire_authority", lambda *a, **kw: SimpleNamespace(role="em", session_id="registered"))
    calls = []
    fid = "b" * 32
    def dispatch(argv, cwd):
        calls.append((argv[0], json.loads(argv[1]), cwd))
        record = {"fire_id": fid, "run_id": rid, "script_sha256": digest}
        if case == "missing_handle":
            record.pop("fire_id")
        elif case == "wrong_reply_run":
            record["run_id"] = "ask-other"
        elif case == "wrong_reply_digest":
            record["script_sha256"] = "0" * 64
        elif case == "unbound_reply":
            record = {"fire_id": fid}
        return json.dumps({"result": record}), "", 0
    monkeypatch.setattr(invoke, "_dispatch_argv", dispatch)
    monkeypatch.setattr(g, "write_headless_em_grant", lambda *a: pytest.fail("unexpected grant creation"))
    monkeypatch.chdir(root)
    resolve = Path.resolve
    def restricted_resolve(path, *args, **kwargs):
        if path == root:
            raise PermissionError("ancestor metadata unavailable")
        return resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", restricted_resolve)
    if case in ("pinned", "changed_args", "other_registered_em", "other_registered_workspace"):
        from coordinator_core.session import external_registry
        monkeypatch.setattr(external_registry, "lookup", lambda sid: {
            "role": "em", "provider": "codex", "cwd": str(tmp_path if case == "other_registered_workspace" else root),
        })
        receipt_digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
        if case == "changed_args":
            mint["fire_args"] = {**args, "extra": "changed"}
            receipt.write_text(json.dumps({"jsonrpc": "2.0", "result": mint}))
        code = hel.main(["--resume-mint", str(receipt), "--expected", digest,
                         "--receipt-sha256", receipt_digest, "--expected-session",
                         "different" if case == "other_registered_em" else "registered", "--cwd", str(root)])
    else:
        code = hel.resume_mint(receipt, "0" * 64 if case == "wrong_digest" else digest, root)
    assert (code == 0) == (case in ("registered", "pinned"))
    if case in ("registered", "pinned", "missing_handle", "wrong_reply_run", "wrong_reply_digest", "unbound_reply"):
        assert calls == [("workflow.fire", dict(run_id=rid, script_path=str(script), expected=digest,
                         args=args, backend="codex", executor="headless", caller_session_id="registered",
                         **({"notify_to": "original-recipient", "private_engine": {"fixture": "selected"}}
                            if case == "pinned" else {})), str(root))]
    else:
        assert not calls
    if case == "registered":
        assert json.loads(capsys.readouterr().out)["result"]["fire_id"] == fid
    if case in ("wrong_reply_run", "wrong_reply_digest", "unbound_reply"):
        captured = capsys.readouterr()
        assert json.loads(captured.out)["result"]["fire_id"] == fid
        assert "reconcile the returned fire ID" in captured.err
    if case == "grant_only":
        assert f"run {rid} remains minted: launcher grant verified" in capsys.readouterr().err
        assert json.loads(stage.read_text()) == {"stage": "minted"}


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin-only endpoint")
def test_issued_submit_uses_separate_native_generation(home, tmp_path, monkeypatch):
    root = home / "native-host-submit-v1"
    generation = root / "build-fixture"
    root.mkdir(mode=0o700)
    generation.mkdir(mode=0o700)
    (root / "current").symlink_to(generation)
    (generation / "socket-directory").write_text(str(home / "sockets"))
    for name in ("codex", "coordinator-invoke"):
        (generation / name).touch(mode=0o700)
    issue = tmp_path / "issued"
    issue.mkdir(mode=0o700)
    params = issue / "params.json"
    params.write_text(json.dumps({"ask": "fixture", "ask_source": "relayed"}))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls = []
    def capture(argv, env, *, allocation):
        calls.append((argv, allocation.workspace))
        return 0
    monkeypatch.setattr(hel, "_spawn_codex_events", capture)
    assert hel.launch("codex", ["exec", "--json"], cwd=str(workspace), issued_submit=params,
                      open_runs=lambda *_: []) == 0
    argv, selected_workspace = calls[0]
    assert selected_workspace == workspace.resolve()
    assert argv[0] == str(generation / "codex")
    assert f'shell_environment_policy.set.COORDINATOR_HOST_SUBMIT_SOCKET="{home}/sockets/submit.sock"' in argv
    assert any(word.startswith(f'shell_environment_policy.set.PATH="{generation}:') for word in argv)
