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


def test_private_overlay_path_gets_ps7_ahead_of_windowsapps_private_bin_first(rig, tmp_path):
    ps7 = _dir_with_pwsh(rig["pf"], "PowerShell", "7")
    entries = _run(rig, tmp_path, private=True)["PATH"].split(os.pathsep)
    assert entries[:3] == [rig["private"], ps7, rig["apps"]]


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
    assert env["PATH"].split(os.pathsep)[:2] == [ps7, rig["apps"]]


def test_spawned_temp_and_tmp_are_the_writable_scratch_dir(rig, tmp_path, monkeypatch):
    _dir_with_pwsh(rig["pf"], "PowerShell", "7")
    monkeypatch.setattr(agents, "_environment", lambda: {"PATH": rig["apps"], "TEMP": "denied-temp", "TMP": "denied-temp"})
    env = _run(rig, tmp_path, private=False)
    expected = rig["repo"] / "scratch" / "codex-tmp"
    assert expected.is_dir()
    assert env["TEMP"] == env["TMP"] == str(expected)
