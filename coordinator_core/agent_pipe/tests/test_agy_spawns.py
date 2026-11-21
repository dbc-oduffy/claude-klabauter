"""Tests for the Antigravity adapter against the captured real stream."""
from __future__ import annotations

from pathlib import Path

import sys

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


FIXTURE = Path(__file__).parent / "fixtures" / "agy_stream.ndjson"
CONV = "894074b8-1367-4122-8a8f-ef1f745ba338"


FAKE_ACP = [sys.executable, str(Path(__file__).parent / "fixtures" / "fake_acp_agent.py")]


_FAKE_STAGE_AGY = '''
import json, subprocess, sys
sys.stdin.read()
if "--dangerously-skip-permissions" in sys.argv:
    out = subprocess.run([sys.executable, "-c", "print('shell-ran-ok')"], capture_output=True, text=True).stdout.strip()
    result = {"status": "success", "response": "DONE: row via agy -- ran shell: " + out}
else:
    result = {"status": "success", "response": "", "denied_actions": ["command/Bash"]}
print(json.dumps({"event": "result", "result": result}))
'''


def test_agy_family_stage_runs_a_shell_command_and_is_consumed_as_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from coordinator_core.codex_runner import workflow

    fake = tmp_path / "fake_agy.py"
    fake.write_text(_FAKE_STAGE_AGY, encoding="utf-8")
    monkeypatch.setattr(workflow, "resolve_agy", lambda: [sys.executable, str(fake)])
    argv = workflow._family_argv("agy", tmp_path)
    assert "--dangerously-skip-permissions" in argv and "--settings" not in argv and "--mode" not in argv
    out = tmp_path / "stdout.jsonl"
    with out.open("wb") as fh:
        proc = subprocess.run(argv, input=b'{"event":"user"}\n', stdout=fh, stderr=subprocess.PIPE, check=False)
    assert proc.returncode == 0
    text = workflow._family_text("agy", out)
    assert text.startswith("DONE: row via agy") and text.endswith("shell-ran-ok")

    denied = tmp_path / "denied.jsonl"
    with denied.open("wb") as fh:
        subprocess.run([sys.executable, str(fake)], input=b"", stdout=fh, check=False)
    with pytest.raises(ValueError, match="no response"):
        workflow._family_text("agy", denied)
