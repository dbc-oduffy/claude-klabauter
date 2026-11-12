"""headless-em-launch: an EM launch writes a grant that passes require_fire_authority; refusals write none."""
import importlib.util
import json
import os
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
def home(tmp_path, monkeypatch):
    for name in _IDENTITY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(g.SETTINGS_HOME_KEY, str(tmp_path / "settings"))
    return tmp_path / "settings"


def _run(family="codex", env=None, **kw):
    calls, env = [], {} if env is None else env
    rc = hel.launch(family, list(kw.pop("extra", ())), which=lambda n: f"/bin/{n}",
                    execv=lambda p, c: calls.append((p, c)), environ=env,
                    **{"platform": "linux", **kw})
    return rc, calls, env


def _fire(monkeypatch, child_env):
    """Run require_fire_authority as the child: its env becomes the process env."""
    for k, v in child_env.items():
        monkeypatch.setenv(k, v)
    return authority.require_fire_authority("s.py", None, cwd=None, native=True, backend="codex")


EXEC_FAMILIES = tuple(f for f in hel.FAMILIES if f != "claude")


@pytest.mark.parametrize("family", EXEC_FAMILIES)
def test_launched_em_passes_require_fire_authority(family, monkeypatch):
    rc, calls, env = _run(family, extra=["-m", "x"])
    assert rc == 0
    assert calls == [(f"/bin/{family}", [f"/bin/{family}", "-m", "x"])]
    assert g.headless_em_grant_matches(env, env[hel.SESSION_ENV], family)
    caller = _fire(monkeypatch, env)
    assert caller.session_id == env[hel.SESSION_ENV]


def test_explicit_session_id_wins_over_env_and_is_granted(monkeypatch):
    rc, _, env = _run(session_id="sess-explicit", env={hel.SESSION_ENV: "sess-env"})
    assert rc == 0 and env[hel.SESSION_ENV] == "sess-explicit"
    assert g.grant_path("sess-explicit").exists() and not g.grant_path("sess-env").exists()


def test_env_session_id_is_used_when_no_flag():
    _, _, env = _run(env={hel.SESSION_ENV: "sess-env"})
    assert env[hel.SESSION_ENV] == "sess-env" and g.grant_path("sess-env").exists()


def test_grant_for_other_session_does_not_admit(monkeypatch):
    _, _, env = _run(session_id="sess-a")
    env = {**env, hel.SESSION_ENV: "sess-b"}
    with pytest.raises(ValueError):
        _fire(monkeypatch, env)


@pytest.mark.parametrize("marker", [{"COORDINATOR_AGENT_WORKER": "1"}, {"COORDINATOR_AGENT_ROLE": "worker"}])
def test_worker_env_cannot_mint_a_grant(marker, home):
    rc, calls, env = _run(env=dict(marker))
    assert rc == hel.REFUSED and not calls
    assert g.HEADLESS_EM_TOKEN_ENV not in env and not (home / "state").exists()


def test_worker_marker_in_child_env_still_refuses_fire(monkeypatch):
    _, _, env = _run()
    with pytest.raises(ValueError):
        _fire(monkeypatch, {**env, "COORDINATOR_AGENT_WORKER": "1"})


def test_missing_cli_exits_127_and_writes_no_grant(home):
    calls, env = [], {}
    rc = hel.launch("codex", [], which=lambda n: None, execv=lambda *a: calls.append(a), environ=env,
                    platform="linux")
    assert rc == 127 and not calls and not (home / "state").exists()


def test_unknown_family_refused(home):
    rc, calls, _ = _run("gemini")
    assert rc == hel.REFUSED and not calls and not (home / "state").exists()


def test_windows_exec_refused_without_grant(home):
    rc, calls, env = _run(platform="win32")
    assert rc == hel.REFUSED and not calls and not env and not (home / "state").exists()


def test_windows_prepare_emits_token_without_executing(capsys):
    calls, env = [], {}
    rc = hel.launch("codex", [], which=lambda n: None, execv=lambda *a: calls.append(a), environ=env,
                    platform="win32", prepare_only=True, native_executable="codex.exe")
    plan = json.loads(capsys.readouterr().out)
    assert rc == 0 and not calls and not env
    assert plan["argv"] == ["codex.exe"]
    assert g.headless_em_grant_matches(plan["environment"], plan["session_id"], "codex")


def _win_prepare(family, which, capsys, **kw):
    rc = hel.launch(family, [], which=which, execv=lambda *a: None, environ={}, platform="win32",
                    prepare_only=True, **kw)
    return rc, capsys.readouterr()


@pytest.mark.parametrize("family", hel.FAMILIES)
def test_windows_native_exe_on_path_used_as_is(family, capsys):
    rc, out = _win_prepare(family, lambda n: f"pathbin/{n}" if n.endswith(".exe") else None, capsys)
    tail = ["--bg"] if family == "claude" else []
    assert rc == 0 and json.loads(out.out)["argv"] == [f"pathbin/{family}.exe", *tail]


def test_windows_codex_cmd_shim_maps_to_native_exe(tmp_path, capsys):
    shim = tmp_path / "codex.cmd"
    exe = (tmp_path / "node_modules" / "@openai" / "codex" / "vendor" / "x86_64-pc-windows-msvc" / "codex" / "codex.exe")
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    import platform
    if platform.machine().lower() in {"arm64", "aarch64"}:
        pytest.skip("x64 fixture layout")
    rc, out = _win_prepare("codex", lambda n: str(shim) if n == "codex" else None, capsys)
    assert rc == 0 and json.loads(out.out)["argv"] == [str(exe)]


@pytest.mark.parametrize("family", hel.FAMILIES)
def test_windows_shim_without_native_package_refused_naming_flag(family, tmp_path, capsys, home, monkeypatch):
    # resolve_native_cli falls back to the real <home>/.<family>/bin exe; point home at an empty dir.
    for var in ("USERPROFILE", "HOME"):
        monkeypatch.setenv(var, str(tmp_path / "nohome"))
    rc, out = _win_prepare(family, lambda n: str(tmp_path / f"{family}.cmd") if n == family else None, capsys)
    assert rc == hel.REFUSED and out.out == "" and "--native-executable" in out.err
    assert not (home / "state").exists()


def test_windows_native_executable_flag_wins_over_path(capsys):
    rc, out = _win_prepare("agy", lambda n: "pathbin/agy.exe", capsys, native_executable="given/agy.exe")
    assert rc == 0 and json.loads(out.out)["argv"] == ["given/agy.exe"]


def test_main_parses_family_session_and_passthrough(monkeypatch):
    seen = {}
    monkeypatch.setattr(hel, "launch", lambda family, extra, **k: seen.update(k, family=family, extra=extra) or 0)
    assert hel.main(["grok", "--session-id", "s1", "--prepare-only", "--", "-p", "hi"]) == 0
    assert seen["family"] == "grok" and seen["session_id"] == "s1" and seen["prepare_only"] is True
    assert seen["extra"] == ["-p", "hi"]


def test_main_without_args_refuses():
    assert hel.main([]) == hel.REFUSED


def _pwsh_env(tmp_path, *, real=True, path_key="Path"):
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
    rc = hel.launch(family, [], which=lambda n: None, execv=lambda *a: None, environ=env, platform=platform,
                    prepare_only=True, native_executable=f"{family}.exe")
    assert rc == 0
    return json.loads(capsys.readouterr().out)["environment"]


def test_windows_codex_prepare_carries_shaped_path_real_pwsh_first_alias_dropped(tmp_path, capsys):
    env, inherited, alias, real_dir, bin_dir = _pwsh_env(tmp_path)
    shaped = _prepare(env, capsys)["PATH"].split(os.pathsep)
    assert shaped == [str(bin_dir), str(real_dir)]
    assert str(alias) not in shaped


@pytest.mark.parametrize("path_key", ["Path", "PATH"])
def test_windows_codex_prepare_reads_path_by_case_insensitive_key(tmp_path, capsys, path_key):
    env, *_ = _pwsh_env(tmp_path, path_key=path_key)
    assert "PATH" in _prepare(env, capsys)


def test_windows_codex_prepare_without_real_pwsh_emits_no_path(tmp_path, capsys):
    env, *_ = _pwsh_env(tmp_path, real=False)
    assert "PATH" not in _prepare(env, capsys)


def test_windows_codex_prepare_with_no_caller_path_emits_no_path(tmp_path, capsys):
    env, *_ = _pwsh_env(tmp_path)
    del env["Path"]
    assert "PATH" not in _prepare(env, capsys)


def test_windows_codex_prepare_with_already_shaped_path_emits_no_path(tmp_path, capsys):
    env, _, _, real_dir, bin_dir = _pwsh_env(tmp_path)
    env["Path"] = os.pathsep.join([str(bin_dir), str(real_dir)])
    assert "PATH" not in _prepare(env, capsys)


@pytest.mark.parametrize("family", ["agy", "grok"])
def test_windows_non_codex_families_never_get_a_path(family, tmp_path, capsys):
    env, *_ = _pwsh_env(tmp_path)
    assert "PATH" not in _prepare(env, capsys, family=family)


def test_non_windows_codex_launch_leaves_the_env_without_a_path_change(tmp_path):
    env, inherited, *_ = _pwsh_env(tmp_path)
    rc, calls, env = _run("codex", env=env)
    assert rc == 0 and env["Path"] == inherited and "PATH" not in env


class _Done:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _bg(platform, capsys, stdout="started 1a2b3c4d\n", returncode=0, **kw):
    seen = []

    def runner(argv, **k):
        seen.append((argv, k))
        return _Done(stdout, returncode)

    execs = []
    rc = hel.launch("claude", [], which=lambda n: "claude.exe" if platform == "win32" else "/bin/claude",
                    execv=lambda *a: execs.append(a), environ={}, platform=platform, runner=runner,
                    cwd="/repo", **kw)
    out = capsys.readouterr()
    assert not execs
    return rc, seen, out


def test_claude_grant_is_accepted():
    """write_headless_em_grant accepts claude and the grant matches."""
    token = g.write_headless_em_grant("s1", "claude")
    assert g.headless_em_grant_matches({g.HEADLESS_EM_TOKEN_ENV: token}, "s1", "claude")


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_claude_launch_runs_bg_with_name_and_returns_printed_id(platform, capsys):
    rc, seen, out = _bg(platform, capsys, name="fleet-a")
    (argv, kw), = seen
    assert rc == 0
    assert argv[1:] == ["--bg", "-n", "fleet-a"]
    assert kw["cwd"] == "/repo" and kw["capture_output"] is True and kw["timeout"] == hel.BG_TIMEOUT_S
    assert g.headless_em_grant_matches(kw["env"], kw["env"][hel.SESSION_ENV], "claude")
    assert json.loads(out.out) == {"name": "fleet-a", "id": "1a2b3c4d", "session_id": kw["env"][hel.SESSION_ENV]}


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_claude_resume_runs_bg_resume_id(platform, capsys):
    rc, seen, out = _bg(platform, capsys, resume="1a2b3c4d")
    assert rc == 0 and seen[0][0][1:] == ["--bg", "--resume", "1a2b3c4d"]
    assert json.loads(out.out)["id"] == "1a2b3c4d"


def test_claude_uuid_id_is_returned(capsys):
    uid = "0b6f8a52-1c3d-4e5f-8a9b-0c1d2e3f4a5b"
    rc, _, out = _bg("linux", capsys, stdout=f"session {uid}\n", name="n")
    assert rc == 0 and json.loads(out.out)["id"] == uid


@pytest.mark.parametrize("kw", [{"stdout": "no id here\n"}, {"returncode": 1}])
def test_claude_bg_failure_or_missing_id_is_refused(kw, capsys):
    rc, _, out = _bg("linux", capsys, name="n", **kw)
    assert rc == hel.REFUSED and out.out == ""


def test_name_on_non_claude_family_refused(home):
    rc, calls, _ = _run("codex", name="x")
    assert rc == hel.REFUSED and not calls and not (home / "state").exists()


def test_windows_non_claude_exec_still_refused(home):
    rc, calls, env = _run("grok", platform="win32")
    assert rc == hel.REFUSED and not calls and not (home / "state").exists()


def test_main_forwards_name_resume_cwd(monkeypatch):
    seen = {}
    monkeypatch.setattr(hel, "launch", lambda family, extra, **k: seen.update(k, family=family) or 0)
    assert hel.main(["claude", "--name", "n1", "--cwd", "/r"]) == 0
    assert seen["name"] == "n1" and seen["cwd"] == "/r" and seen["resume"] is None
    assert hel.main(["claude", "--resume", "abc123"]) == 0 and seen["resume"] == "abc123"
