"""agent_pipe relay worker: every codex spawn (fresh and resume) gets the shaped env; antigravity keeps its own."""
import os
import sys
from types import SimpleNamespace

import pytest

from coordinator_core.agent_pipe import __main__ as relay
from coordinator_core.codex_runner import codex_spawn_env, sandbox_preflight


def _run(tmp_path, monkeypatch, agent, request_extra=None):
    """Drive _relay at default arguments; return (env passed to Popen or None, shape calls)."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(relay.os, "environ", {"PATH": os.environ.get("PATH", "")})
    shaped = []

    def fake_shape(env, *, writable_root, **kw):
        shaped.append(writable_root)
        env["SHAPED"] = "1"
        return env

    monkeypatch.setattr(codex_spawn_env, "shape_codex_child", fake_shape)
    captured = []
    child = SimpleNamespace(stdout=[], wait=lambda: 0, poll=lambda: 0)
    monkeypatch.setattr(relay.subprocess, "Popen", lambda *a, **kw: captured.append(kw) or child)
    adapter = SimpleNamespace(spawn_argv=lambda p: ["fake"], resume_argv=lambda *a: ["fake", "r"], parse_line=lambda s: None)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    req = {"agent": agent, "reply_to": "r", "prompt": "hi", **(request_extra or {})}
    relay._relay(run_dir, req, adapter)
    return (captured[0]["env"] if captured else None), shaped, cwd


def test_fresh_codex_spawn_env_is_the_shape_codex_child_result_rooted_at_cwd(tmp_path, monkeypatch):
    env, shaped, cwd = _run(tmp_path, monkeypatch, "codex")
    assert env["SHAPED"] == "1"
    assert shaped == [str(cwd)]


def test_antigravity_spawn_env_is_unshaped(tmp_path, monkeypatch):
    env, shaped, _ = _run(tmp_path, monkeypatch, "antigravity")
    assert "SHAPED" not in env and shaped == []


def test_codex_resume_env_is_shaped_but_keeps_the_inherited_identity(tmp_path, monkeypatch):
    env, shaped, cwd = _run(tmp_path, monkeypatch, "codex", {"conversation": "c", "text": "t"})
    assert env["SHAPED"] == "1"
    assert shaped == [str(cwd)]
    assert env == {"PATH": os.environ.get("PATH", ""), "SHAPED": "1"}


def test_antigravity_resume_env_is_unshaped(tmp_path, monkeypatch):
    env, shaped, _ = _run(tmp_path, monkeypatch, "antigravity", {"conversation": "c", "text": "t"})
    assert "SHAPED" not in env and shaped == []


@pytest.mark.parametrize("extra", [{}, {"conversation": "c", "text": "t"}])
def test_shape_refusal_returns_a_failed_result_and_never_spawns(tmp_path, monkeypatch, extra):
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    def refuse(env, **kw):
        raise sandbox_preflight.SandboxSetupRefused("pwsh is the WindowsApps alias")

    monkeypatch.setattr(codex_spawn_env, "shape_codex_child", refuse)
    monkeypatch.setattr(relay.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    adapter = SimpleNamespace(spawn_argv=lambda p: ["fake"], resume_argv=lambda *a: ["fake", "r"], parse_line=lambda s: None)
    (tmp_path / "run").mkdir()
    result = relay._relay(tmp_path / "run", {"agent": "codex", "reply_to": "r", "prompt": "hi", **extra}, adapter)
    assert result["status"] == relay.STATUS_FAILED
    assert result["exit_code"] is None
    assert "SandboxSetupRefused" in result["error"] and "WindowsApps" in result["error"]


@pytest.mark.skipif(sys.platform != "win32", reason="PATH shaping is Windows-only")
def test_codex_windowsapps_only_path_refused_before_popen(tmp_path, monkeypatch):
    apps = tmp_path / "WindowsApps"
    apps.mkdir()
    (apps / "pwsh.exe").write_text("")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "ProgramFiles"))
    monkeypatch.setattr(sandbox_preflight, "_is_windows", lambda: True)
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(relay.os, "environ", {"PATH": str(apps)})
    monkeypatch.setattr(relay.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    adapter = SimpleNamespace(spawn_argv=lambda p: ["fake"], parse_line=lambda s: None)
    (tmp_path / "run").mkdir()
    result = relay._relay(tmp_path / "run", {"agent": "codex", "reply_to": "r", "prompt": "hi"}, adapter)
    assert result["status"] == relay.STATUS_FAILED
    assert "SandboxSetupRefused" in result["error"]
