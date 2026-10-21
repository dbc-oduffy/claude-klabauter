"""Routed manifest rows run through `_routedRun` in the engine-ask script; unrouted scripts are unchanged."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_ROADMAP_TEXT = (
    "export const meta = { name: 'rb', phases: [{ title: 'Synthesize', detail: 'x' }] };\n"
    "return { gate_report_path: 'g.json' };"
)
_TABLE = {
    "schema_version": 1,
    "default_agent_type": "coordinator:executor",
    "work_classes": [
        {"id": "ue", "match": {"keywords": ["unreal"]}, "agent_type": "ue:python",
         "required_tools": ["mcp__example_game_repo-control__*"], "on_mismatch": "redirect"},
    ],
}


def _script(repo_root: str) -> str:
    return ask_compose.compose_ask_script(
        repo_root=repo_root, prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        roadmap_blitz_text=_ROADMAP_TEXT, **REVIEW_KW,
    )


@pytest.fixture
def routed_repo(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "route-table.json").write_text(json.dumps(_TABLE), encoding="utf-8")
    return str(tmp_path)


def _block(script: str) -> str:
    start = script.index("if (_manifest.fanout) log(")
    marker = "await _commitWave(w, _waveIds);"
    end = script.index(marker, start) + len(marker)
    return script[start:end] + "\n  }"


def _row(rid: str, **over) -> dict:
    return {"id": rid, "agent_type": "coordinator:executor", "model": "sonnet", "brief_path": f"b/{rid}.md",
            "writes": [f"{rid}.py"], "wave": 0, "deps": [], **over}


_ROUTE = {
    "work_class": "ue",
    "triage": {"agent_type": "ue:triage", "model": "haiku"},
    "primary": {"agent_type": "ue:python", "model": "opus"},
    "targets": {"ue:python": "opus", "ue:bp": "sonnet"},
}

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
const _replies = %(replies)s;
const agent = async (prompt, opts) => {
  calls.push({ label: opts.label, agentType: opts.agentType, model: opts.model, prompt });
  return _replies[opts.agentType] ?? _replies['*'];
};
const _runRow = (id, deps, scope, run, commit) => (async () => { const r = await run(); return r == null ? 'INCOMPLETE ' + id : r; })();
const _commitWave = async () => {};
const _waves = [...new Set(_manifest.rows.map((r) => r.wave))];
const _rows = {};
async function main() {
%(block)s
  const settled = {};
  for (const [k, v] of Object.entries(_rows)) settled[k] = await v;
  console.log(JSON.stringify({ calls, settled, logs }));
}
main();
"""


def _run(repo_root: str, rows: list[dict], replies: dict) -> dict:
    program = _HARNESS % {
        "manifest": json.dumps({"error": None, "rows": rows}),
        "replies": json.dumps(replies),
        "block": _block(_script(repo_root)),
    }
    out = subprocess.run(
        [shutil.which("node"), "-e", program], capture_output=True, text=True, timeout=60,
        **no_console_creationflags(),
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_no_route_table_emits_no_routing_bytes(tmp_path):
    script = _script(str(tmp_path))
    assert "_routedRun" not in script and "_routeOf" not in script


def test_routed_run_declared_once_and_only_routed_rows_use_it(routed_repo):
    script = _script(routed_repo)
    assert script.count("async function _routedRun(") == 1
    assert script.count("_routedRun(_routeOf(r)") == 1
    assert "r.route ? _routedRun" in script


@pytest.mark.spawns_process
@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_routed_row_dispatches_triage_first_and_plain_row_is_untouched(routed_repo):
    rows = [_row("C1", route=_ROUTE), _row("C2")]
    got = _run(routed_repo, rows, {"ue:triage": "disposition: proceed", "*": "DONE: ok"})
    c1 = [c for c in got["calls"] if c["label"].endswith("C1")]
    c2 = [c for c in got["calls"] if c["label"].endswith("C2")]
    assert [c["agentType"] for c in c1] == ["ue:triage", "ue:python"]
    assert "disposition" in c1[0]["prompt"] and "disposition" not in c1[1]["prompt"]
    assert len(c2) == 1 and c2[0]["agentType"] == "coordinator:executor" and c2[0]["model"] == "sonnet"


@pytest.mark.spawns_process
@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_triage_pick_dispatches_target_with_its_model(routed_repo):
    rows = [_row("C1", route=_ROUTE)]
    got = _run(
        routed_repo, rows,
        {"ue:triage": "disposition: redirect\nredirect_agent_type: ue:bp\nreason: blueprint", "*": "DONE: ok"},
    )
    assert [c["agentType"] for c in got["calls"]] == ["ue:triage", "ue:bp"]
    assert got["calls"][1]["model"] == "sonnet"
    assert got["calls"][1]["prompt"].count("disposition") == 0


@pytest.mark.spawns_process
@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_refusal_settles_blocked_route_refused(routed_repo):
    rows = [_row("C1", route=_ROUTE)]
    got = _run(routed_repo, rows, {"*": "disposition: refuse\nreason: just script it is wrong"})
    assert got["settled"]["C1"].startswith("BLOCKED: route-refused")
    assert "just script it is wrong" in got["settled"]["C1"]


@pytest.mark.spawns_process
@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_grouped_routed_row_is_not_merged_into_a_shared_agent(routed_repo):
    rows = [_row("C1", route=_ROUTE, group="g1"), _row("C2", route=_ROUTE, group="g1")]
    got = _run(routed_repo, rows, {"*": "DONE: ok"})
    labels = sorted(c["label"][-2:] for c in got["calls"])
    assert labels == ["C1", "C1", "C2", "C2"]
    assert not any(c["label"].endswith("g1") for c in got["calls"])
