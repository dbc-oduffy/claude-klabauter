"""Ratchet: every lib/ import in every installed unimatrix host script resolves inside the installed lib/,
and the INSTALLED host guard evaluates (never degraded/import_error) and denies a deny-class command
for codex and agy. OS-agnostic: the guard runs via sys.executable over tmp_path paths."""
import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

_BIN = Path(__file__).resolve().parents[1]
_ENGINE_ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("unimatrix_install_for_lib_ratchet", _BIN / "unimatrix-install.py")
ui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ui)

# Modules an installed script may import that are neither stdlib nor lib/: none today.
_LIB_NAMES = {p.stem for p in (_BIN / "lib").glob("*.py")}


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    dest = tmp_path_factory.mktemp("home") / "unimatrix"
    ui.install_scripts(ui._BIN_DIR, dest)
    return dest


def _import_sites(tree):
    """(module name, inside an `except ImportError` handler's try body) for every import statement."""
    guarded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(
                h.type is not None and "ImportError" in ast.dump(h.type) for h in node.handlers):
            guarded.update(id(n) for stmt in node.body for n in ast.walk(stmt))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0], id(node) in guarded
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0], id(node) in guarded


def test_every_installed_script_lib_import_resolves_in_the_installed_lib(installed):
    installed_lib = {p.stem for p in (installed / "lib").glob("*.py")}
    scripts = sorted(installed.glob("*.py"))
    assert any(p.name == "unimatrix-relay.py" for p in scripts) and any(p.name == "unimatrix-host-guard.py" for p in scripts)
    unresolved = {}
    for script in scripts:
        for mod, in_import_error_fallback in _import_sites(ast.parse(script.read_text(encoding="utf-8"))):
            if mod in _LIB_NAMES and mod not in installed_lib and not in_import_error_fallback:
                unresolved.setdefault(script.name, set()).add(mod)
    assert not unresolved, f"lib imports with no installed lib/ module and no ImportError fallback: {unresolved}"


def test_installed_lib_closure_is_self_contained(installed):
    """Every lib module a shipped lib module imports at any depth is itself shipped."""
    shipped = {p.stem for p in (installed / "lib").glob("*.py")}
    assert {"cc_invoke", "engine_bootstrap"} <= shipped
    missing = {}
    for path in sorted((installed / "lib").glob("*.py")):
        for mod, _ in _import_sites(ast.parse(path.read_text(encoding="utf-8"))):
            if mod in _LIB_NAMES and mod not in shipped:
                missing.setdefault(path.name, set()).add(mod)
    assert not missing, f"installed lib modules import unshipped lib modules: {missing}"


def test_a_removed_lib_module_fails_the_closure_check(installed, tmp_path):
    copy = tmp_path / "unimatrix"
    ui.install_scripts(ui._BIN_DIR, copy)
    (copy / "lib" / "engine_bootstrap.py").unlink()
    shipped = {p.stem for p in (copy / "lib").glob("*.py")}
    sites = {m for m, _ in _import_sites(ast.parse((copy / "lib" / "cc_invoke.py").read_text(encoding="utf-8")))}
    assert "engine_bootstrap" in sites and "engine_bootstrap" not in shipped


def _run_guard(installed, host, payload, env):
    proc = subprocess.run(
        [sys.executable, str(installed / "unimatrix-host-guard.py"), "--host", host, "--engine-root", str(_ENGINE_ROOT)],
        input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0
    return json.loads(proc.stdout), proc.stderr.strip().splitlines()[-1]


def _hermetic_env(tmp_path):
    reg = tmp_path / "registry"
    reg.mkdir()
    (reg / "registry.local.toml").write_text('[policy]\nnon_owned_remotes = ["github.com/some-stranger/"]\n',
                                             encoding="utf-8")
    return dict(os.environ, COORDINATOR_SETTINGS_HOME=str(tmp_path / "settings-home"),
                MACHINE_LOCAL_REGISTRY_DIR=str(reg), COORDINATOR_WARM_RUNTIME_BASE=str(tmp_path / "warm"))


def _repo_with_foreign_origin(tmp_path):
    git_dir = tmp_path / "repo" / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "config").write_text('[remote "origin"]\n\turl = https://github.com/some-stranger/thing.git\n',
                                    encoding="utf-8")
    return git_dir.parent


def _payload(host, command, cwd):
    if host == "codex":
        return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "s", "cwd": str(cwd),
                "tool_input": {"command": command}}
    return {"hookEventName": "PreToolUse", "conversationId": "s", "workspacePaths": [str(cwd)],
            "toolCall": {"name": "run_command", "args": {"CommandLine": command}}}


def _claude_em_denies(monkeypatch, env, command, cwd):
    """The Claude EM path's verdict: the in-process Bash guard chain on a Claude-shaped payload."""
    from coordinator_core.bash_guards.dispatch import evaluate_payload_json

    for key in ("COORDINATOR_SETTINGS_HOME", "MACHINE_LOCAL_REGISTRY_DIR", "COORDINATOR_WARM_RUNTIME_BASE"):
        monkeypatch.setenv(key, env[key])
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    out = evaluate_payload_json(json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "s",
                                            "cwd": str(cwd), "tool_input": {"command": command}}))
    hso = out.get("hookSpecificOutput") if isinstance(out, dict) else None
    return isinstance(hso, dict) and hso.get("permissionDecision") == "deny"


@pytest.mark.parametrize("host", ["codex", "agy"])
def test_installed_guard_matches_the_claude_em_verdict_for_a_force_push_to_a_non_owned_remote(
        installed, tmp_path, monkeypatch, host):
    if not (_ENGINE_ROOT / "coordinator_core" / "host_hooks" / "shim.py").is_file():
        pytest.skip("no engine tree beside this checkout")
    repo = _repo_with_foreign_origin(tmp_path)
    env = _hermetic_env(tmp_path)
    claude_denies = _claude_em_denies(monkeypatch, env, "git push --force origin main", repo)
    assert claude_denies, "the Claude EM path must refuse a force-push to a non-owned remote"
    answer, state = _run_guard(installed, host, _payload(host, "git push --force origin main", repo), env)
    assert state == "engine", (answer, state)
    host_denies = (answer["hookSpecificOutput"]["permissionDecision"] == "deny" if host == "codex"
                   else answer["decision"] == "deny")
    assert host_denies == claude_denies


@pytest.mark.parametrize("host", ["codex", "agy"])
def test_installed_guard_without_cc_invoke_degrades_proving_the_ratchet_bites(installed, tmp_path, host):
    copy = tmp_path / "unimatrix"
    ui.install_scripts(ui._BIN_DIR, copy)
    (copy / "lib" / "cc_invoke.py").unlink()
    answer, state = _run_guard(copy, host, _payload(host, "git push --force origin main",
                                                    _repo_with_foreign_origin(tmp_path)), _hermetic_env(tmp_path))
    assert state == "degraded"
