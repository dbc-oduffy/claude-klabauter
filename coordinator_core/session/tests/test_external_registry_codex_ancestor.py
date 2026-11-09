"""Codex host detection: the native binary shapes and the node launcher qualify, nothing else does."""
import re
from types import SimpleNamespace

import pytest

from coordinator_core.session import external_registry as registry

SID = "00000000-0000-4000-8000-0000000000aa"


def proc(name, cmdline=(), pid=123):
    return SimpleNamespace(pid=pid, name=lambda: name, cmdline=lambda: list(cmdline), create_time=lambda: 456)


@pytest.mark.parametrize("name,cmdline", [
    ("codex", ["codex"]),
    ("codex.exe", ["codex.exe"]),
    ("Codex.EXE", []),
    ("codex-aarch64-apple-darwin", ["/x/vendor/aarch64-apple-darwin/codex/codex-aarch64-apple-darwin"]),
    ("codex-x86_64-unknown-linux-musl", []),
    ("node", ["/opt/homebrew/bin/node", "/opt/homebrew/lib/node_modules/@openai/codex/bin/codex.js", "--model", "x"]),
    ("node.exe", ["node.exe", "..\\npm\\node_modules\\@openai\\codex\\bin\\codex.js"]),
])
def test_codex_host_shapes_qualify(name, cmdline):
    assert registry._is_codex_host(proc(name, cmdline))


@pytest.mark.parametrize("name,cmdline", [
    ("node", ["node", "server.js"]),
    ("node", ["node", "server.js", "codex.js"]),
    ("node", ["node"]),
    ("codex-helper", ["codex-helper"]),
    ("codexd", []),
    ("codex-aarch64-apple-darwin-evil", []),
    ("python", ["python", "codex.js"]),
    ("zsh", []),
])
def test_non_codex_processes_are_refused(name, cmdline):
    assert not registry._is_codex_host(proc(name, cmdline))


def test_unreadable_process_is_not_a_host():
    def boom():
        raise OSError("gone")
    assert not registry._is_codex_host(SimpleNamespace(name=boom, cmdline=boom))


def _start(tmp_path, monkeypatch, parents):
    from coordinator_core.session import core
    import psutil
    monkeypatch.setattr(core, "stable_pid_alive", lambda *a, **k: True)
    monkeypatch.setattr(core, "ensure_session", lambda *a, **k: None)
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(parents=lambda: parents))
    monkeypatch.setenv("COORDINATOR_AGENT_ROLE", "unknown")
    cwd = tmp_path / "repo"
    cwd.mkdir(exist_ok=True)
    return registry.register_current(
        {"session_id": SID, "cwd": str(cwd), "hook_event_name": "SessionStart"}, tmp_path)


def test_darwin_launcher_ancestor_gets_an_engine_address(tmp_path, monkeypatch):
    parents = [proc("zsh"), proc("node", ["node", "/x/@openai/codex/bin/codex.js", "exec", "-"], pid=77)]
    record = _start(tmp_path, monkeypatch, parents)
    assert record["pid"] == 77
    assert re.fullmatch(r"repo-codex-[a-z0-9-]+-[1-9][0-9]*", record["name"])


def test_no_codex_ancestor_still_raises_with_a_message(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="no observed Codex ancestor"):
        _start(tmp_path, monkeypatch, [proc("zsh"), proc("node", ["node", "server.js"])])
