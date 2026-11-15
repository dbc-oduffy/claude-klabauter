"""run_agent shapes the FINAL codex child env, after the private-CLI overlay (DoE run 5b192d work:X1)."""

from __future__ import annotations

import functools
import os

import pytest

from coordinator_core.codex_runner import codex_spawn_env, sandbox_preflight
from coordinator_core.codex_runner import workflow as agents


def _dir_with_pwsh(root, *parts):
    d = root.joinpath(*parts)
    d.mkdir(parents=True)
    (d / "pwsh.exe").write_text("")
    return str(d)


class _Spawned(Exception):
    pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Windows branch forced; every spawn stubbed; the early parent-env checks neutralised."""
    pf = tmp_path / "ProgramFiles"
    apps = _dir_with_pwsh(tmp_path, "WindowsApps")
    private = tmp_path / "private-bin"
    private.mkdir()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(sandbox_preflight, "_is_windows", lambda: True)
    monkeypatch.setattr(agents, "shape_codex_child", functools.partial(
        codex_spawn_env.shape_codex_child, environ={"ProgramFiles": str(pf)}, windows=True))
    monkeypatch.setattr(agents, "preflight_auth", lambda **_kw: None)
    monkeypatch.setattr(agents, "check_workspace_write", lambda *a, **kw: None)
    monkeypatch.setattr(agents, "resolve_codex", lambda **_kw: ["codex"])
    monkeypatch.setattr(agents, "_protected_observer_path", lambda *a, **kw: None)
    seen = {}

    def spawn(argv, prompt, *, env, **_kw):
        seen["env"] = dict(env)
        raise _Spawned

    monkeypatch.setattr(agents, "_run_admitted", spawn)
    overlay_path = os.pathsep.join([str(private), apps])
    monkeypatch.setattr(agents, "_private_cli_settings",
                        lambda *a, **kw: ({"PATH": overlay_path}, "", {}))
    return {"pf": pf, "apps": apps, "private": str(private), "repo": repo, "seen": seen}


def _run(rig, tmp_path, *, private):
    kwargs = {"repo_root": rig["repo"], "artifact_dir": tmp_path / "agent"}
    if private:
        kwargs.update(private_cli={}, on_spawn=lambda _p: None, on_native_thread=lambda _t: None)
    with pytest.raises(_Spawned):
        agents.run_agent("x", {}, **kwargs)
    return rig["seen"]["env"]


def test_private_overlay_path_gets_ps7_after_private_bin_and_windowsapps_dropped(rig, tmp_path):
    ps7 = _dir_with_pwsh(rig["pf"], "PowerShell", "7")
    entries = _run(rig, tmp_path, private=True)["PATH"].split(os.pathsep)
    assert entries == [rig["private"], ps7]


def test_private_overlay_without_ps7_refuses_before_spawn(rig, tmp_path):
    with pytest.raises(agents.AgentFailed, match="WindowsApps|pwsh"):
        agents.run_agent("x", {}, repo_root=rig["repo"], artifact_dir=tmp_path / "agent",
                         private_cli={}, on_spawn=lambda _p: None, on_native_thread=lambda _t: None)
    assert "env" not in rig["seen"]


def test_no_private_overlay_still_shapes_and_refuses(rig, tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "_environment", lambda: {"PATH": rig["apps"]})
    with pytest.raises(agents.AgentFailed):
        agents.run_agent("x", {}, repo_root=rig["repo"], artifact_dir=tmp_path / "agent")
    assert "env" not in rig["seen"]
    ps7 = _dir_with_pwsh(rig["pf"], "PowerShell", "7")
    env = _run(rig, tmp_path, private=False)
    assert env["PATH"].split(os.pathsep) == [ps7]


def test_spawned_temp_and_tmp_are_the_writable_scratch_dir(rig, tmp_path, monkeypatch):
    _dir_with_pwsh(rig["pf"], "PowerShell", "7")
    monkeypatch.setattr(agents, "_environment", lambda: {"PATH": rig["apps"], "TEMP": "denied-temp", "TMP": "denied-temp"})
    env = _run(rig, tmp_path, private=False)
    expected = rig["repo"] / "scratch" / "codex-tmp"
    assert expected.is_dir()
    assert env["TEMP"] == env["TMP"] == str(expected)


def test_codex_under_restricted_token_refuses_nested_spawn_before_any_spawn(rig, tmp_path, monkeypatch):
    from coordinator_core.warm import sandboxed_spawn

    monkeypatch.setattr(sandboxed_spawn, "caller_is_sandboxed", lambda: True)
    with pytest.raises(agents.AgentFailed, match="nested in the codex sandbox"):
        agents.run_agent("x", {}, repo_root=rig["repo"], artifact_dir=tmp_path / "agent")
    assert "env" not in rig["seen"]


def test_codex_unrestricted_still_reaches_the_spawn(rig, tmp_path, monkeypatch):
    from coordinator_core.warm import sandboxed_spawn

    monkeypatch.setattr(sandboxed_spawn, "caller_is_sandboxed", lambda: False)
    with pytest.raises(_Spawned):
        agents.run_agent("x", {}, repo_root=rig["repo"], artifact_dir=tmp_path / "agent")


def test_child_env_forwards_credential_context_and_never_api_keys(monkeypatch):
    from coordinator_core.codex_runner.review import CREDENTIAL_CONTEXT_ENV, CREDENTIAL_CONTEXT_ENV_WINDOWS

    keys = CREDENTIAL_CONTEXT_ENV + CREDENTIAL_CONTEXT_ENV_WINDOWS
    for key in keys:
        monkeypatch.setenv(key, f"v-{key}")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    env = agents._environment()
    assert all(env.get(k) == f"v-{k}" for k in keys)
    assert "OPENAI_API_KEY" not in env
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert "CLAUDE_CONFIG_DIR" not in agents._environment()


@pytest.mark.parametrize("inherited", [None, "0", "1"])
def test_child_env_keeps_warmth_on_and_pins_both_autostarts_off(monkeypatch, inherited):
    if inherited is None:
        monkeypatch.delenv("COORDINATOR_WARM", raising=False)
    else:
        monkeypatch.setenv("COORDINATOR_WARM", inherited)
    from coordinator_core.warm.engine_root import current_engine_clone

    env = agents._environment()
    assert "COORDINATOR_WARM" not in env
    assert env["COORDINATOR_WARM_LISTENER_AUTOSTART"] == "0"
    assert env["COORDINATOR_WARM_SERVER_AUTOSTART"] == "0"
    assert env["COORDINATOR_ENGINE_ROOT"] == str(current_engine_clone())
