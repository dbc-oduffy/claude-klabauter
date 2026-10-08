"""Pins the optional ``prompt`` / ``session_id`` / ``wait`` seam on fire.py."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys

import pytest

from coordinator_core.ops.workflow_fire import fire

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PINNED_ARGV = [
    "claude",
    "-p",
    fire._PROMPT_TEMPLATE.format(script_path="/s/wf.js"),
    "--allowedTools",
    "Workflow", "Read", "Write", "Edit", "Bash", "Grep", "Glob", "ToolSearch",
    "--plugin-dir",
    "/plug",
    "--model",
    "haiku",
    "--max-turns",
    "3",
    "--output-format",
    "json",
]


def test_default_argv_is_pinned():
    assert fire.build_fire_command("/s/wf.js", "/plug") == _PINNED_ARGV


def test_prompt_and_session_id_appear_once():
    argv = fire.build_fire_command(
        "/s/wf.js", "/plug", prompt="PROMPT-X", session_id="sid-123"
    )
    assert argv.count("PROMPT-X") == 1
    assert argv.count("--session-id") == 1
    assert argv.count("sid-123") == 1
    assert argv[argv.index("--session-id") + 1] == "sid-123"
    assert fire._PROMPT_TEMPLATE.format(script_path="/s/wf.js") not in argv


def _stub_claude(tmp_path):
    body = (
        "import json, sys, time\n"
        "time.sleep(0.8)\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,"
        " 'result': 'ok', 'session_id': 'stub'}))\n"
        "sys.exit(3)\n"
    )
    py = tmp_path / "stub_claude.py"
    py.write_text(body, encoding="utf-8")
    if sys.platform == "win32":
        launcher = tmp_path / "stub_claude.cmd"
        launcher.write_text(f'@"{sys.executable}" "{py}" %*\r\n', encoding="utf-8")
    else:
        launcher = tmp_path / "stub_claude"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{py}" "$@"\n', encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    return launcher


def _repo_with_script(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cnw = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, creationflags=cnw)
    script = repo / "wf.js"
    script.write_text("// workflow\n", encoding="utf-8")
    return repo, script


def test_wait_returns_exited_record_with_classified_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(fire, "resolve_plugin_dir", lambda: str(tmp_path))
    repo, script = _repo_with_script(tmp_path)
    stub = _stub_claude(tmp_path)

    def _no_liveness(*_a, **_k):
        raise AssertionError("wait=True must not reach pid-liveness polling")

    monkeypatch.setattr(fire, "_pid_alive", _no_liveness)

    record = fire.fire_workflow(
        str(script), cwd=str(repo), claude_bin=str(stub),
        prompt="custom", session_id="11111111-1111-4111-8111-111111111111", wait=True,
    )
    assert record["state"] == "exited"
    assert record["exit_code"] == 3
    assert record["outcome"] in ("clean", "failed", "truncated", "unknown")
    assert record["outcome"] != "unknown"
    assert record["command"].count("--session-id") == 1
    on_disk = json.loads(
        (fire._record_path(fire._registry_dir(str(repo)), record["fire_id"])).read_text("utf-8")
    )
    assert on_disk["state"] == "exited" and on_disk["exit_code"] == 3


def test_spawn_failure_with_session_id_raises_and_discards(tmp_path, monkeypatch):
    monkeypatch.setattr(fire, "resolve_plugin_dir", lambda: str(tmp_path))
    repo, script = _repo_with_script(tmp_path)
    with pytest.raises(fire.ChildSpawnFailedError):
        fire.fire_workflow(
            str(script), cwd=str(repo), claude_bin=str(tmp_path / "no-such-claude"),
            session_id="22222222-2222-4222-8222-222222222222",
        )
    reg = fire._registry_dir(str(repo))
    assert not [p for p in reg.glob("*.json")]
