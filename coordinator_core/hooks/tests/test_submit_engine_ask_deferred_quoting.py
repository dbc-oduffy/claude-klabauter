"""A deferred emit and its chain-follow relay carry ask text with shell metacharacters byte for byte, never through inline shell quoting."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask_emit_budget import _NODE_HARNESS
from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY

NASTY = "it's a \"quoted\" `tick` and $(echo pwned) with $HOME\nsecond line"


def _node(script: Path, tmp_path: Path, replies: list, results: list) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    harness = tmp_path / "harness.js"
    harness.write_text(_NODE_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [node, str(harness), str(script), json.dumps({"replies": replies, "results": results})],
        capture_output=True, text=True, timeout=30, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(proc.stdout)


@pytest.mark.spawns_process
def test_deferred_emit_names_a_params_file_holding_the_ask_byte_for_byte(tmp_path):
    script = mod._write_deferred_script(tmp_path, {"ask": NASTY, "session_id": ""})
    text = script.read_text(encoding="utf-8")
    assert "const PARAMS =" not in text

    got = _node(script, tmp_path, [{"path": "", "run_id": "", "error": "stop here"}], [])
    (prompt,) = [c["agent"] for c in got["calls"] if "agent" in c]
    (params_file,) = re.findall(r'--params-file "([^"]+)"', prompt)
    assert json.loads(Path(params_file).read_text(encoding="utf-8"))["ask"] == NASTY
    assert "pwned" not in prompt and "second line" not in prompt


@pytest.mark.spawns_process
def test_relay_is_told_to_leave_the_params_file_in_place_after_the_emit_reads_it(tmp_path):
    """The relay's `rm -f` is refused by block-subagent-destructive-action, so the prompt forbids touching any file."""
    script = mod._write_deferred_script(tmp_path, {"ask": NASTY, "session_id": ""})
    got = _node(script, tmp_path, [{"path": "", "run_id": "", "error": "stop here"}], [])
    (prompt,) = [c["agent"] for c in got["calls"] if "agent" in c]
    (params_file,) = re.findall(r'--params-file "([^"]+)"', prompt)
    assert Path(params_file).is_file()
    assert not re.search(r"\brm\b", prompt)
    assert "Do not delete or modify any file" in prompt


def test_short_params_also_ride_a_params_file(tmp_path):
    script = mod._write_deferred_script(tmp_path, {"ask": "x", "session_id": ""})
    assert re.search(r'^const PARAMS_FILE = "[^"]+defer-params-', script.read_text(encoding="utf-8"), re.M)
    assert not hasattr(mod, "DEFERRED_PARAMS_INLINE_MAX")


@pytest.mark.spawns_process
def test_chain_follow_command_feeds_the_params_on_stdin_byte_for_byte(tmp_path):
    script = mod._write_deferred_script(tmp_path, {"ask": "go", "session_id": ""})
    ask_reply = {"path": "scratch/warp/ask-1.workflow.mjs", "run_id": "ask-1", "fire_args": {}}
    handoff = {CHAIN_HANDOFF_KEY: {"ask": NASTY, "em_context": "don't"}}
    got = _node(script, tmp_path, [ask_reply, {"path": "", "run_id": "", "error": "stop here"}], [handoff])

    chain_prompts = [c["agent"] for c in got["calls"] if "agent" in c][1:]
    assert len(chain_prompts) == 1
    prompt = chain_prompts[0]
    # A quoted heredoc does no expansion, so its body between the tag lines is exactly what the command reads on stdin.
    opener = re.search(r"--params-file - <<'(\w+)'\n", prompt)
    assert opener, prompt
    tag = opener.group(1)
    body = prompt[opener.end() :].split(f"\n{tag}\n", 1)[0]
    sent = json.loads(body)
    assert sent["ask"] == NASTY and sent["em_context"] == "don't"
    assert "'\\''" not in prompt
