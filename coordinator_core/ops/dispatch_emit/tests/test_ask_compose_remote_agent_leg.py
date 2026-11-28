"""The ask script places an executor row and a falsifier reviewer row through `agent_leg_remote`, falling back to the in-harness agent."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, emit
from coordinator_core.subagent_sandbox.provision_report import resolve_plugin_root
from coordinator_core.win_portability import no_console_creationflags

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PLUGIN_ROOT = resolve_plugin_root()
_PLACE = ask_compose._placed_leg_js("// anchor", "ask-x-1", "/fixture/repo", "/fixture/repo/state/run/ask-x-1", "'Execute'")


def _node_run(body: str) -> list:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    program = (
        "const prompts = [];\n"
        "let reply = null;\n"
        "let boom = null;\n"
        "const agent = async (prompt, opts) => { prompts.push([prompt, opts]); if (boom) throw boom; return reply; };\n"
        f"(async () => {{\n{_PLACE}\n{body}\n}})().catch((e) => {{ console.log(JSON.stringify({{ threw: String(e) }})); }});\n"
    )
    out = subprocess.run([node, "-"], input=program, capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert out.returncode == 0, out.stderr
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


_ROW = "const r = { id: 'C5', brief_path: 'scratch/b.md', model: 'sonnet', writes: ['a.py', 'b dir/c.py'] };\n"
_LOCAL = "const local = async () => 'LOCAL';\n"


def test_a_placed_reply_is_the_leg_result_and_the_local_agent_never_runs():
    out = _node_run(
        _ROW + _LOCAL + "reply = { placed: true, reply: 'DONE: C5', box_id: 'gipsy' };\n"
        "const res = await _placeLeg('executor', '', local);\n"
        "console.log(JSON.stringify({ res, n: prompts.length, label: prompts[0][1].label, schema: prompts[0][1].schema.required, "
        "prompt: prompts[0][0] }));"
    )[0]
    assert out["res"] == "DONE: C5" and out["n"] == 1
    assert (out["label"], out["schema"]) == ("place-C5:executor", ["placed"])
    cmd = out["prompt"]
    assert "python -m coordinator_core.ops.agent_leg_remote --leg executor --run-id ask-x-1" in cmd
    assert '--out "/fixture/repo/state/run/ask-x-1/executor-C5.result.json"' in cmd
    assert '--prompt-file "scratch/b.md" --model "sonnet" --writes "a.py" "b dir/c.py"' in cmd


@pytest.mark.parametrize("placed", [{"placed": False, "reply": None, "reason": "no_remote_below_local"}, {"placed": True, "reply": None}, None])
def test_an_unplaced_or_empty_reply_runs_the_local_agent(placed):
    out = _node_run(
        _ROW + _LOCAL + f"reply = {json.dumps(placed)};\nconsole.log(JSON.stringify({{ res: await _placeLeg('executor', '', local) }}));"
    )
    assert out == [{"res": "LOCAL"}]


def test_a_placing_leg_failure_runs_the_local_agent_but_a_limit_error_rethrows():
    failed = _node_run(_ROW + _LOCAL + "boom = new Error('dispatch broke');\nconsole.log(JSON.stringify({ res: await _placeLeg('review', '', local) }));")
    assert failed == [{"res": "LOCAL"}]
    limited = _node_run(
        _ROW + _LOCAL + "globalThis._errLimitText = (e) => (String(e).includes('limit') ? 'limit' : null);\n"
        "boom = new Error('usage limit reached');\nconsole.log(JSON.stringify({ res: await _placeLeg('review', '', local) }));"
    )
    assert limited[0]["threw"].endswith("usage limit reached")


def test_a_review_leg_names_its_schema_and_takes_no_writes():
    out = _node_run(
        _ROW + _LOCAL + "reply = { placed: true, reply: { verdict: 'ok' } };\n"
        "const res = await _placeLeg('review', ' --schema-name falsifier_integrity_result', local);\n"
        "console.log(JSON.stringify({ res, prompt: prompts[0][0] }));"
    )[0]
    assert out["res"] == {"verdict": "ok"}
    assert "--leg review" in out["prompt"] and "--writes" not in out["prompt"]
    assert out["prompt"].count("--schema-name falsifier_integrity_result") == 1


@pytest.fixture(scope="module")
def script() -> str:
    assert _PLUGIN_ROOT, "plugin root did not resolve at import; plan-blitz.mjs is unreachable"
    mp = pytest.MonkeyPatch()
    mp.setenv("CLAUDE_PLUGIN_ROOT", _PLUGIN_ROOT)
    mp.setattr(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9)
    try:
        return ask_compose.compose_ask_script(
            repo_root=str(_REPO_ROOT), prompt="probe ask", sizing_rel=None, run_id="ask-probe-000000", session_id="probe"
        )
    finally:
        mp.undo()


def test_the_composed_ask_defines_the_placer_inside_the_launcher_and_wraps_both_legs(script):
    assert script.count("const _placeLeg = async (leg, extra, localCall) =>") == 1
    assert script.index("const _launchRow =") < script.index("const _placeLeg =")
    assert "_placeLeg('executor', '', () => agent(_rowPrompt(r)," in script
    assert "_placeLeg('review', ' --schema-name falsifier_integrity_result', () => agent(" in script
    assert "agent_leg_remote --leg " in script


def test_only_the_default_row_arm_is_placed(script):
    assert "r.group ? _groupRun(r) : _placeLeg('executor'" in script
    assert script.count("_placeLeg('executor'") == 1
    assert script.count("_placeLeg('review'") == 1
