"""host_targets.plan: path derivation per OS, detection, purity."""
from __future__ import annotations

import os
import sys

import pytest

from coordinator_core.install import host_targets
from coordinator_core.install.host_targets import HOSTS, HostPlan, plan, resolve_home


def _exe_name(name):
    # shutil.which on Windows only matches PATHEXT suffixes, never a bare name.
    return name + ".exe" if sys.platform == "win32" else name


def _env(tmp_path, **extra):
    env = {"PATH": str(tmp_path / "emptybin")}
    env.update(extra)
    return env


def test_hosts_constant():
    assert HOSTS == ("codex", "agy")


def test_unknown_host_rejected(tmp_path):
    with pytest.raises(ValueError):
        plan("gemini", tmp_path, _env(tmp_path))


@pytest.mark.parametrize("platform,key", [("win32", "USERPROFILE"), ("darwin", "HOME"), ("linux", "HOME")])
def test_resolve_home_key_per_platform(tmp_path, platform, key):
    other = "HOME" if key == "USERPROFILE" else "USERPROFILE"
    env = {key: str(tmp_path / "right"), other: str(tmp_path / "wrong")}
    assert resolve_home(env, platform) == tmp_path / "right"


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_codex_paths_from_home(tmp_path, platform):
    p = plan("codex", None, _env(tmp_path, HOME=str(tmp_path), USERPROFILE=str(tmp_path)), platform)
    assert isinstance(p, HostPlan)
    assert p.config_path == tmp_path / ".codex" / "config.toml"
    assert p.hooks_path == tmp_path / ".codex" / "hooks.json"
    assert not hasattr(p, "hooks_fragment")
    assert p.changes and p.host_provides and p.unreachable


def test_codex_home_env_wins(tmp_path):
    ch = tmp_path / "elsewhere"
    p = plan("codex", tmp_path, _env(tmp_path, CODEX_HOME=str(ch)))
    assert p.config_path == ch / "config.toml"
    assert p.skills_dir == ch / "skills"


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_agy_paths_from_home(tmp_path, platform):
    p = plan("agy", tmp_path, _env(tmp_path), platform)
    assert p.config_path == tmp_path / ".gemini" / "config" / "mcp_config.json"
    assert p.hooks_path == tmp_path / ".gemini" / "config" / "hooks.json"
    assert p.changes and p.host_provides and p.unreachable


@pytest.mark.parametrize("host", ["codex", "agy"])
def test_plan_names_no_hooks_fragment(tmp_path, host):
    p = plan(host, tmp_path, _env(tmp_path))
    assert not hasattr(p, "hooks_fragment")
    assert not any("fragment" in c for c in p.changes)


def test_binary_absent_is_none(tmp_path):
    env = _env(tmp_path)
    assert plan("agy", tmp_path, env).binary is None
    assert plan("codex", tmp_path, env, "linux").binary is None


def test_agy_binary_found_on_path(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / _exe_name("agy")
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert os.path.normcase(plan("agy", tmp_path, {"PATH": str(bindir)}).binary) == os.path.normcase(str(exe))


def test_codex_binary_via_resolve_codex(tmp_path, monkeypatch):
    fnm = "/x/fnm/node-versions/v24/installation"
    codex_js = f"{fnm}/lib/node_modules/@openai/codex/bin/codex.js"
    monkeypatch.setattr(host_targets, "resolve_codex", lambda platform, env: [f"{fnm}/bin/node", codex_js])
    binary = plan("codex", tmp_path, _env(tmp_path)).binary
    assert binary == codex_js
    assert not binary.endswith("/node")


def test_codex_single_element_argv_reports_that_codex(tmp_path, monkeypatch):
    monkeypatch.setattr(host_targets, "resolve_codex", lambda platform, env: ["/usr/bin/codex"])
    assert plan("codex", tmp_path, _env(tmp_path)).binary == "/usr/bin/codex"


def test_codex_falls_back_to_which_when_resolver_misses(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / _exe_name("codex")
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)

    def miss(platform, env):
        raise host_targets.CodexUnavailable("none")

    monkeypatch.setattr(host_targets, "resolve_codex", miss)
    assert os.path.normcase(plan("codex", tmp_path, {"PATH": str(bindir)}).binary) == os.path.normcase(str(exe))


def test_plan_writes_nothing(tmp_path):
    env = _env(tmp_path)
    plan("codex", tmp_path, env)
    plan("agy", tmp_path, env)
    assert list(tmp_path.iterdir()) == []
