"""The ask execute loop runs one agent per manifest `group` and reads each member's own result line."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.fanout_cost import read_fanout
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_ROADMAP_TEXT = (
    "export const meta = { name: 'rb', phases: [{ title: 'Synthesize', detail: 'x' }] };\n"
    "return { gate_report_path: 'g.json' };"
)


def _script() -> str:
    return ask_compose.compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        roadmap_blitz_text=_ROADMAP_TEXT, **REVIEW_KW,
    )


def _block(script: str) -> str:
    start = script.index("if (_manifest.fanout) log(")
    marker = "await _commitWave(w, _waveIds);"
    end = script.index(marker, start) + len(marker)
    return script[start:end] + "\n  }"


def _manifest(rows: list[dict]) -> dict:
    return {"error": None, "rows": rows, "fanout": {"expectedAgents": 2}}


def _row(rid: str, **over) -> dict:
    return {"id": rid, "agent_type": "coordinator:executor", "model": "sonnet", "brief_path": f"b/{rid}.md",
            "writes": [f"{rid}.py"], "wave": 0, "deps": [], **over}


_HARNESS = """
const calls = [];
const logs = [];
const log = (m) => logs.push(m);
const REPO_ROOT = 'REPO';
const _manifest = %(manifest)s;
const _agentDefsClause = () => '';
const _rowSidecar = () => '';
const _runBase = 'a'.repeat(40);
const _rowPlan = {};
const _verdictReports = [];
const agent = async (prompt, opts) => {
  calls.push({ label: opts.label, prompt });
  return %(reply)s;
};
const _runRow = (id, deps, scope, run, commit) => (async () => { const r = await run(); return r == null ? 'INCOMPLETE ' + id : r; })();
const _commitWave = async () => {};
const _waves = [...new Set(_manifest.rows.map((r) => r.wave))];
const _rows = {};
async function main() {
%(block)s
  const settled = {};
  for (const [k, v] of Object.entries(_rows)) settled[k] = await v;
  console.log(JSON.stringify({ calls: calls.map((c) => ({ label: c.label, prompt: c.prompt })), settled, logs }));
}
main();
"""


def _run(rows: list[dict], reply: str) -> dict:
    block = _block(_script())
    program = _HARNESS % {"manifest": json.dumps(_manifest(rows)), "reply": json.dumps(reply), "block": block}
    out = subprocess.run(
        [shutil.which("node"), "-"], input=program, capture_output=True, text=True, timeout=60,
        **no_console_creationflags(),
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_schema_admits_row_group_and_manifest_fanout():
    props = ask_compose._MANIFEST_SCHEMA["properties"]
    assert props["rows"]["items"]["properties"]["group"] == {"type": "string"}
    assert "group" not in props["rows"]["items"]["required"]
    assert props["fanout"]["type"] == ["object", "null"]


def test_meta_reports_fanout_lower_bound():
    fanout = read_fanout(_script())
    assert fanout is not None and fanout["basis"] == "lower-bound"
    assert isinstance(fanout["expectedAgents"], int) and fanout["expectedAgents"] >= 1
    assert fanout["startupTokensTotal"] == fanout["expectedAgents"] * fanout["startupTokensPerAgent"]


@pytest.mark.spawns_process
@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_group_runs_one_agent_and_solo_runs_its_own():
    rows = [_row("C1", group="g1"), _row("C2", group="g1"), _row("C3")]
    got = _run(rows, "work\nC1: DONE ok\nC2: PARTIAL half\nC3: DONE solo")
    labels = [c["label"] for c in got["calls"]]
    assert len(labels) == 2 and labels[0].endswith("g1") and labels[1].endswith("C3")
    group_prompt = got["calls"][0]["prompt"]
    assert "b/C1.md" in group_prompt and "b/C2.md" in group_prompt
    assert group_prompt.index("b/C1.md") < group_prompt.index("b/C2.md")
    assert got["settled"]["C1"] == "DONE: ok"
    assert got["settled"]["C2"] == "PARTIAL: half"
    assert any("Fanout" in m for m in got["logs"])


@pytest.mark.spawns_process
@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_member_absent_from_the_group_reply_reads_incomplete():
    rows = [_row("C1", group="g1"), _row("C2", group="g1")]
    got = _run(rows, "work\nC1: DONE ok")
    assert got["settled"]["C1"] == "DONE: ok"
    assert got["settled"]["C2"] == "INCOMPLETE C2"
