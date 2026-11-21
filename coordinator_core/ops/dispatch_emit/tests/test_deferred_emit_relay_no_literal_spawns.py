"""The chain-emit relay agent's prompt carries no machine-produced brief value; a script-written file hands the digest to the emit."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import deferred_emit_script as des
from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

DIGEST = "c2c2e2a1c78e" + "0123456789abcdef" * 3 + "0123456789ab"
BRIEF_FILE = "scratch/warp/brief-spill-aaaaaaaaaaaa.md"
SIZING = "state/sizings/2026-11/2026-11-16-x-8e0f3c.yaml"
_ASK = "ask-20261116T074832-d9c25e"
_CHAIN = "warp-2026-11-16-x-8e0f3c-20261116T080000-abcdef"


def _prompt_for(carry_file: str, handoff: dict | None = None) -> str:
    """The prompt `_relayEmit` hands the chain-emit agent, captured by running the real chain-hop template under node."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    values = des._chain_follow_values(carry_file)
    values["__RUN_SCRIPT_SUFFIX__"] = json.dumps(".workflow.mjs")
    chain_js = re.sub("|".join(values), lambda m: values[m.group(0)], des._EMIT_RELAY_JS + des._CHAIN_FOLLOW_JS)
    if handoff is None:
        handoff = {"ask": True, "sizing_path": SIZING, "session_id": "sess-1", "chain_parent": _ASK, "target_root": "/t", "launcher_child": True}
    code = (
        "const RUN_SCRIPT_SUFFIX = '.workflow.mjs';\n"
        f"const ASK_RUN_ID = {json.dumps(_ASK)};\n"
        f"const ASK_SCRIPT_PATH = {json.dumps('scratch/warp/' + _ASK + '.workflow.mjs')};\n"
        f"const first = {{ ok: true, result: {{ {json.dumps(CHAIN_HANDOFF_KEY)}: {json.dumps(handoff)} }} }};\n"
        "const prompts = [];\n"
        "const _normPath = (p) => p;\n"
        "const _startLoop = async () => ({ ok: true, result: { done: true } });\n"
        f"const agent = async (p) => {{ prompts.push(p); return {{ path: 'scratch/warp/{_CHAIN}.workflow.mjs', run_id: '{_CHAIN}', fire_args: {{}} }}; }};\n"
        "(async () => {\n" + chain_js + "\n})().then(() => console.log(JSON.stringify(prompts)));\n"
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    (prompt,) = json.loads(out.stdout)
    return prompt


def _written_script(root: Path) -> tuple[Path, dict]:
    params = {"ask": "build it", "ask_brief_file": BRIEF_FILE, "em_context_digest": DIGEST}
    script = des.write_deferred_script(root, params)
    return script, params


def _carry_file_of(script_text: str) -> str:
    return json.loads(re.search(r"^const _carryFile = (.*);$", script_text, re.M).group(1))


def test_the_relay_command_hands_the_emit_the_digest_intact(tmp_path):
    """Runs the prompt's fixed merge command on the heredoc body as a relay would copy it; the emit's stdin carries the digest."""
    script, params = _written_script(tmp_path)
    text = script.read_text(encoding="utf-8")
    carry_file = _carry_file_of(text)
    prompt = _prompt_for(carry_file)
    merge_py = re.search(r'(?:python3?) -c "(.*?)" "', prompt).group(1)
    body = re.search(r"<<'CHAIN_PARAMS_JSON' \| .*?\n(.*?)\nCHAIN_PARAMS_JSON\n", prompt, re.S).group(1)
    run_dir = tmp_path / "state" / "warp" / _ASK
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    out = subprocess.run([sys.executable, "-c", merge_py, carry_file, _ASK], input=body, capture_output=True, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    emitted = json.loads(out.stdout)
    assert emitted["em_context_digest"] == DIGEST
    assert emitted["ask_brief_file"] == BRIEF_FILE
    assert emitted["sizing_path"] == SIZING
    assert emitted["chain_parent"] == _ASK
    assert emitted["ask"] is True
    assert emitted["launcher_child"] is True


def test_research_handoff_merge_drops_warp_home_and_keeps_the_carry(tmp_path):
    """A research-sizing hand-off names no sizing_path, so the plain merge runs; its stdin must not carry warp_home."""
    script, params = _written_script(tmp_path)
    carry_file = _carry_file_of(script.read_text(encoding="utf-8"))
    assert "warp_home" in json.loads(Path(carry_file).read_text(encoding="utf-8"))
    prompt = _prompt_for(carry_file, {"ask": True, "chain_parent": _ASK, "value_class": "corpus"})
    assert "run.json" not in prompt, "the research hand-off takes the plain merge, not the run.json join"
    merge_py = re.search(r'(?:python3?) -c "(.*?)" "', prompt).group(1)
    body = re.search(r"<<'CHAIN_PARAMS_JSON' \| .*?\n(.*?)\nCHAIN_PARAMS_JSON\n", prompt, re.S).group(1)
    out = subprocess.run([sys.executable, "-c", merge_py, carry_file], input=body, capture_output=True, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    emitted = json.loads(out.stdout)
    assert "warp_home" not in emitted
    assert emitted["em_context_digest"] == DIGEST and emitted["launcher_child"] is True
    assert emitted["value_class"] == "corpus"
