"""A routing verify that names the covering artifact flips the row to dedupe once; the routing verify
dispatches a judging agent type, never the test-runner."""
import json
import re

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_stages as stages
from coordinator_core.ops.dispatch_emit import grind_profile as gp

from .test_grind_verify_mode import _FIXTURE_PROFILE_DIR, _routing_profile, _script

_ROUTING = {
    "triage": {"kind": "triage", "edges": {"bug": "verify-routing", "dedupe": "verify-close"}, "on_fail": None},
    "verify-routing": {"kind": "verify", "edges": {"pass": "fix"}, "on_fail": None},
    "fix": {"kind": "fix", "edges": {"done": "verify"}, "on_fail": None},
    "verify": {"kind": "verify", "edges": {"pass": "commit"}, "on_fail": None},
    "verify-close": {"kind": "verify", "edges": {"pass": "commit"}, "on_fail": None},
    "commit": {"kind": "commit", "edges": {}, "on_fail": None},
}


class _Budget:
    total = None
    _s = 0

    def spent(self):
        return self._s

    def remaining(self):
        return None

    def spend(self, n):
        self._s += n


def _run(verify_replies, routing=_ROUTING):
    calls = []
    replies = list(verify_replies)

    def agent(kind, unit):
        calls.append(kind)
        if kind == "triage":
            return [{"row": "r0", "verdict": "bug", "tshirt_size": "S", "tradeoff": ""}]
        if kind == "verify":
            return replies.pop(0)
        return {}

    result = gc.run_admission(
        [("b0", ["r0"])], routing, "triage", reserve=gc.batch_reserve(1),
        max_agent_calls=None, budget=_Budget(), agent=agent,
    )
    return calls, result


def test_covered_by_flips_to_dedupe_and_passes_over_the_close_verify_instead_of_the_retry():
    calls, result = _run([
        {"outcome": "fail", "reason": "covered", "covered_by": "wiki/x.md:12"},
    ])
    assert calls.count("verify") == 1 and "fix" not in calls
    assert "rejected-after-retry" not in [h["type"] for h in result["handed_back"] if h["row"] == "r0"]


def test_the_flip_is_terminal_the_close_verify_never_re_judges_a_covered_row():
    calls, result = _run([
        {"outcome": "fail", "covered_by": "wiki/x.md:12"},
        {"outcome": "fail", "covered_by": "wiki/x.md:12"},
    ])
    assert calls.count("verify") == 1 and "fix" not in calls
    assert "rejected-after-retry" not in [h["type"] for h in result["handed_back"] if h["row"] == "r0"]


def test_a_fail_without_covered_by_still_retries_once():
    calls, result = _run([{"outcome": "fail"}, {"outcome": "fail"}])
    assert calls.count("verify") == 2 and "fix" not in calls
    assert [h["type"] for h in result["handed_back"] if h["row"] == "r0"] == ["rejected-after-retry"]


def test_a_profile_without_a_dedupe_edge_does_not_flip():
    routing = {**_ROUTING, "triage": {"kind": "triage", "edges": {"bug": "verify-routing"}, "on_fail": None}}
    calls, result = _run([{"outcome": "fail", "covered_by": "a:1"}, {"outcome": "fail"}], routing)
    assert [h["type"] for h in result["handed_back"] if h["row"] == "r0"] == ["rejected-after-retry"]


def _per_edge_profile(tmp_path):
    """The fixture graph with one pre-fix verify node per hand-back and per fix/discard edge."""
    doc = yaml.safe_load((_FIXTURE_PROFILE_DIR / "fixture.yaml").read_text(encoding="utf-8"))
    g = doc["graph"]
    nodes = sorted(stages.ROUTING_VERIFY_HAND_BACK_NODES) + ["verify-routing-fix", "verify-routing-discard"]
    for i, node in enumerate(nodes):
        doc["verdicts"].append(f"v{i}")
        g["triage"]["edges"][f"v{i}"] = node
        g[node] = {"kind": "verify", "verify": {"default": "agent"}, "edges": {"pass": "fix"}}
    (tmp_path / "fixture.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    profile = gp.load_profile("fixture", tmp_path)
    gp.validate_graph(profile)
    return profile


def _routing_block(script):
    block = script[script.index("if (VERIFY_PRE_FIX.includes(row.node)) {\n"):]
    return block[: block.index("\n  }\n")]


def test_emitted_script_runs_hand_back_edges_on_haiku_and_fix_discard_on_sonnet(tmp_path):
    script = _script(_per_edge_profile(tmp_path))
    hand_back = sorted(stages.ROUTING_VERIFY_HAND_BACK_NODES)
    assert "const VERIFY_HAND_BACK = " + json.dumps(hand_back) + ";" in script
    for node in ("verify-routing-fix", "verify-routing-discard"):
        assert node not in re.search(r"const VERIFY_HAND_BACK = (\[.*?\]);", script).group(1)
    block = _routing_block(script)
    haiku_part, sonnet_part = block.split("    if (VERIFY_HAND_BACK.includes(row.node)) {\n")[1].split("\n    }\n", 1)
    assert "model: 'haiku'" in haiku_part and "model: 'sonnet'" not in haiku_part
    assert "model: 'sonnet'" in sonnet_part and "model: 'haiku'" not in sonnet_part
    for part in (haiku_part, sonnet_part):
        assert f"agentType: '{stages.ROUTING_VERIFY_AGENT_TYPE}'" in part


def test_script_without_hand_back_nodes_emits_one_sonnet_routing_call(tmp_path):
    script = _script(_routing_profile(tmp_path))
    assert "const VERIFY_HAND_BACK = [];" in script
    assert "VERIFY_HAND_BACK.includes" not in script
    assert "model: 'haiku'" not in _routing_block(script)


def test_hand_back_model_is_haiku_and_premise_checker_pin_stays_sonnet():
    kw = dict(label="v", phase_title="Grind", routing=True, profile="lessons",
              digest_js="row.digest", run_id_js="RUN_ID")
    hand_back = stages.compose_verify_agent_call(hand_back=True, **kw)
    default = stages.compose_verify_agent_call(**kw)
    assert "model: 'haiku'" in hand_back and "model: 'sonnet'" not in hand_back
    assert "model: 'sonnet'" in default and "model: 'haiku'" not in default
    assert emit._AGENT_MODELS[stages.ROUTING_VERIFY_AGENT_TYPE] == "sonnet"
    assert stages.STAGE_MODELS["verify-routing"] == "sonnet"
    assert stages.ROUTING_VERIFY_HAND_BACK_NODES.isdisjoint({"verify-routing-fix", "verify-routing-discard"})


def test_carve_out_still_refuses_a_run_override_of_the_pinned_type():
    with pytest.raises(emit.MalformedAgentOverrideError):
        stages.compose_verify_agent_call(label="v", phase_title="Grind", routing=True, hand_back=True,
                                         models={"verify-routing": "opus"})


def test_emitted_script_flips_once_on_covered_by(tmp_path):
    script = _script(_routing_profile(tmp_path))
    stage = script[script.index("async function _verifyStage"):script.index("async function _commitStage")]
    assert "result.covered_by && VERIFY_PRE_FIX.includes(row.node) && !row.dedupeFlipped" in stage
    assert "routeAfterTriage('dedupe', null, '')" in stage
    assert "row.dedupeFlipped = true;" in stage and "row.verifyRetried = true;" in stage
    assert stage.index("row.dedupeFlipped = true;") < stage.index("if (!row.verifyRetried) {")
    assert '"covered_by": {"type": "string"}' in script


def test_routing_verify_pins_a_judging_agent_type_not_the_test_runner():
    call = stages.compose_verify_agent_call(label="v", phase_title="Grind", routing=True, profile="lessons",
                                            digest_js="row.digest", run_id_js="RUN_ID")
    assert stages.ROUTING_VERIFY_AGENT_TYPE == "coordinator:premise-checker"
    assert stages.ROUTING_VERIFY_AGENT_TYPE != emit._TEST_AGENT_TYPE
    assert emit._AGENT_MODELS[stages.ROUTING_VERIFY_AGENT_TYPE] == "sonnet"
    assert f"agentType: '{stages.ROUTING_VERIFY_AGENT_TYPE}'" in call
    assert emit._TEST_AGENT_TYPE not in call
    assert "covered_by" in call and "--verdict dedupe" in call


_BATCH_HARNESS_JS = r"""
import fs from 'node:fs';
const [,, scriptPath] = process.argv;
const src = fs.readFileSync(scriptPath, 'utf8').replace(/\r\n/g, '\n').replace(/export const meta = \{[\s\S]*?\n\};\n/, '');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const ids = (prompt) => [...new Set([...prompt.matchAll(/"row_id":"(row\d+)"/g)].map((m) => m[1]))];
const routingCalls = [];
const retried = new Set();
const verdictFor = (row, first) => {
  if (row === 'row1') return { row_id: row, outcome: 'fail', reason: 'wrong target', right_home: 'docs/elsewhere.md' };
  if (row === 'row2' && !retried.has(row)) { retried.add(row); return { row_id: row, outcome: 'fail', reason: 'unclear' }; }
  return { row_id: row, outcome: 'pass', reason: '' };
};
const reply = (opts, prompt) => {
  const label = opts.label || '';
  const rows = ids(prompt);
  if (label === 'triage') {
    return { rows: rows.map((r) => ({ row: r, verdict: 'confirmed-bug', tshirt_size: 'S', has_tradeoff: false, evidence: 'e', fix_plan: 'p' })), stale: [] };
  }
  if (label === 'verify-routing') {
    routingCalls.push(rows);
    const answered = rows.filter((r) => r !== 'row5' && !(r === 'row4' && routingCalls.length === 1));
    return { verdicts: answered.map((r) => verdictFor(r)) };
  }
  if (label === 'fix') return { outcome: 'done', touched_files: ['f' + rows[0] + '.py'] };
  if (label.startsWith('verify')) return { outcome: 'pass', exit_code: 0, output: {} };
  if (label === 'commit') {
    return { committed: true, sha: 'abc123', rows: rows.map((r) => ({ row_id: r, outcome: 'settled', sha: 'abc123' })), unlanded: [] };
  }
  return {};
};
let ledgerPrompt = '';
const agent = (prompt, opts = {}) => {
  if ((opts.label || '').startsWith('ledger-sweep')) ledgerPrompt = prompt;
  return new Promise((res) => setTimeout(() => res(reply(opts, prompt)), 1));
};
const budget = { spent: () => 0, remaining: () => null, total: null };
const fn = new AsyncFunction('args', 'agent', 'phase', 'log', 'budget', 'parallel', src);
const out = await fn(
  { run_stamp: '20261114T000000Z', script_path: 'x.mjs', profile_dir: 'pd', repo_root: '/r', run_token: 'tok' },
  agent, () => {}, () => {}, budget, async (xs) => Promise.all(xs),
);
const record = decodeURIComponent(/RUNREC-OPEN-\S+\n\d+ [0-9a-f]{64}\n(\S+)\nRUNREC-CLOSE-/.exec(ledgerPrompt)[1]);
console.log(JSON.stringify({ routingCalls, record }));
"""


def test_a_six_row_batch_issues_at_most_three_routing_verify_calls_and_applies_each_verdict(tmp_path):
    import shutil
    import subprocess

    from coordinator_core.ops.dispatch_emit.queue_select import Manifest, ManifestEntry

    from .conftest import REVIEW_KW

    node = shutil.which("node")
    if node is None:
        pytest.skip("node absent")
    profile = _routing_profile(tmp_path)
    entries = tuple(
        ManifestEntry(row_id=f"row{i}", path=f"state/bug-backlog/row{i}.yaml", digest=f"{i:064x}", batch_key="P1")
        for i in range(6)
    )
    manifest = Manifest(entries=entries, batch_sizes={"P1": 6}, source=None, digest="deadbeef")
    knobs = {**gp.resolve_appetite(profile, "standard"), "batch_size": 6}
    script = gc.compose_grind_script(
        manifest, profile, knobs,
        run_dir="state/queue-grind/fixture/run-1", appetite="standard", agent_type_host=None,
        queue_dirs=["state/bug-backlog"], **REVIEW_KW,
    )
    (tmp_path / "grind.mjs").write_bytes(script.encode("utf-8"))
    (tmp_path / "harness.mjs").write_text(_BATCH_HARNESS_JS, encoding="utf-8")
    out = subprocess.run(
        [node, str(tmp_path / "harness.mjs"), str(tmp_path / "grind.mjs")],
        capture_output=True, text=True, check=False, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0, out.stderr
    seen = json.loads(out.stdout)
    calls = seen["routingCalls"]
    assert len(calls) <= 3
    assert calls[0] == [f"row{i}" for i in range(6)]
    assert calls[1] == ["row4", "row5"]
    assert calls[2] == ["row2"]
    record = json.loads(seen["record"])
    handed = {h["row"]: h for h in record["handed_back"]}
    assert handed["row5"]["type"] == "stage-dead"
    assert handed["row5"]["reason"] == "verify-routing returned no verdict for this row"
    assert not [h for r, h in handed.items() if r != "row5" and h["type"] in {"stage-dead", "rejected-after-retry"}
                and "verify-routing" in h["reason"]], handed


def test_post_fix_verify_stays_on_the_test_runner(tmp_path):
    script = _script(_routing_profile(tmp_path))
    stage = script[script.index("async function _verifyCall"):script.index("async function _verifyStage")]
    routing_call, post_fix = stage.split("if (VERIFY_PRE_FIX.includes(row.node)) {")[1].split("\n  }\n", 1)
    assert f"agentType: '{stages.ROUTING_VERIFY_AGENT_TYPE}'" in routing_call
    assert emit._TEST_AGENT_TYPE not in routing_call
    assert f"agentType: '{emit._TEST_AGENT_TYPE}'" in post_fix
