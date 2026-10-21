"""coordinator_core.invoke.tests.test_invoke_stdout_is_reply_only -- coordinator-invoke stdout is one JSON-RPC reply.

Purpose: the writer seen in the fleet run (`Using Node v22.23.1` ahead of the JSON) was the
caller's fnm cd hook, never this chain. This pins the chain against ever growing a shell: with
BASH_ENV/ENV pointing at a script that echoes a marker and an `fnm` shim on PATH that echoes
another, stdout must still parse as exactly one JSON document carrying neither marker.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_INVOKE = _PROJECT_ROOT / "coordinator" / "bin" / "coordinator-invoke.py"
_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_SHELL_MARKER = "MARKER-SHELL-LEAK"
_FNM_MARKER = "Using Node v0.0.0"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True,
        creationflags=_NO_CONSOLE,
    )


def test_stdout_is_exactly_the_reply_under_hostile_shell_env(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "state" / "handoffs").mkdir(parents=True)
    _git(repo, "init", "-q")

    hook = tmp_path / "hook.sh"
    hook.write_text(f"echo {_SHELL_MARKER}\n", encoding="utf-8")

    shims = tmp_path / "shims"
    shims.mkdir()
    fnm = shims / "fnm"
    fnm.write_text(f"#!/bin/sh\necho '{_FNM_MARKER}'\n", encoding="utf-8")
    fnm.chmod(0o755)
    (shims / "fnm.cmd").write_text(f"@echo {_FNM_MARKER}\r\n", encoding="utf-8")

    env = os.environ.copy()
    env["BASH_ENV"] = str(hook)
    env["ENV"] = str(hook)
    env["PATH"] = str(shims) + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = str(_PROJECT_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )

    result = subprocess.run(
        [sys.executable, str(_INVOKE), "baton.close_report", "{}",
         "--repo", str(repo), "--allow-unstamped-dispatch"],
        capture_output=True, text=True, timeout=60, cwd=str(repo), env=env,
        creationflags=_NO_CONSOLE,
    )

    assert _SHELL_MARKER not in result.stdout, result.stdout
    assert _FNM_MARKER not in result.stdout, result.stdout
    reply = json.loads(result.stdout)
    assert reply["jsonrpc"] == "2.0", reply
    assert reply["result"]["op"] == "baton.close_report", (reply, result.stderr)
