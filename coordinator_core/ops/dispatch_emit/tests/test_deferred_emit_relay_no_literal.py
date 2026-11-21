"""The chain-emit relay agent's prompt carries no machine-produced brief value; a script-written file hands the digest to the emit."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import deferred_emit_script as des
from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY

pytestmark = pytest.mark.complete

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


def test_relay_prompt_carries_no_digest_sha_or_run_id_literal(tmp_path):
    script, params = _written_script(tmp_path)
    text = script.read_text(encoding="utf-8")
    carry_file = _carry_file_of(text)
    assert DIGEST not in text, "the script itself pins no digest literal"
    prompt = _prompt_for(carry_file)
    assert DIGEST not in prompt and DIGEST[:12] not in prompt
    assert BRIEF_FILE not in prompt
    assert not re.search(r"[0-9a-f]{40,}", prompt)
    assert _CHAIN not in prompt
    assert SIZING not in prompt and "sess-1" not in prompt and "chain_parent\": " not in prompt.split("CHAIN_PARAMS_JSON'", 1)[1]
    body = re.search(r"<<'CHAIN_PARAMS_JSON' \| .*?\n(.*?)\nCHAIN_PARAMS_JSON\n", prompt, re.S).group(1)
    assert json.loads(body) == {}, "the heredoc body holds no hand-off value"
    assert "launcher_child" not in body and str(tmp_path) not in prompt.replace(carry_file, "")
    assert carry_file in prompt, "the fixed command names the script-written carry file"


def test_the_carry_file_the_script_wrote_holds_the_digest(tmp_path):
    script, params = _written_script(tmp_path)
    carry_file = _carry_file_of(script.read_text(encoding="utf-8"))
    assert Path(carry_file).is_absolute() and Path(carry_file).parent == tmp_path / "scratch" / "warp"
    assert Path(carry_file).name == script.name.replace(".workflow.mjs", ".carry.json"), "one fixed carry name per script"
    assert json.loads(Path(carry_file).read_text(encoding="utf-8")) == {
        "ask_brief_file": BRIEF_FILE, "em_context_digest": DIGEST,
        "launcher_child": True, "target_root": tmp_path.as_posix(),
        "warp_home": (tmp_path / "state" / "warp").as_posix(),
    }


def test_no_brief_keys_keeps_the_plain_heredoc_command():
    """With no carry file (a direct start) nothing is merged: the command is the bare invoke and the heredoc names the launcher flag."""
    prompt = _prompt_for("")
    assert " -c " not in prompt
    assert "--params-file - <<'CHAIN_PARAMS_JSON'\n" in prompt
    assert '"launcher_child":true' in prompt


def test_interpreter_name_is_chosen_at_write_time(monkeypatch):
    for name, expected in (("nt", "python"), ("posix", "python3")):
        monkeypatch.setattr(des.os, "name", name)
        assert des._chain_follow_values("f.json")["__PYTHON__"] == json.dumps(expected)
    prompt = _prompt_for("/x/f.json")
    assert re.search(r"command once[^\n]*:\n(python3?) -c ", prompt)
    assert "in place of" not in prompt
