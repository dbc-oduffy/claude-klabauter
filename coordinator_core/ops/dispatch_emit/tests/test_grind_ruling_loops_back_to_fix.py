"""
coordinator_core.ops.dispatch_emit.tests.test_grind_ruling_loops_back_to_fix

Purpose: pins that PM-bound hand-backs raised inside the grind loop are adjudicated by ONE post-loop call
and that a ruled, actionable, non-pmOnly one continues into a second fix pass with the ruling on the row
(admission model and emitted JS twin); a pm-only, spin-out, gated or missing verdict stays handed back with
its adjudication record, and a continued row that tradeoffs again fails closed without a second adjudication.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_profile as gp

from .test_grind_compose import _compose

_FIXTURE_PROFILE_DIR = Path(__file__).parent / "fixtures" / "queue-profiles"
_RULING = "apply the one-line allow-list entry"


def _routing():
    profile = gp.load_profile("fixture", _FIXTURE_PROFILE_DIR)
    gp.validate_graph(profile)
    return gc.build_routing_table(profile)


class _Budget:
    total = None

    def __init__(self):
        self._spent = 0

    def spend(self, amount):
        self._spent += amount

    def spent(self):
        return self._spent

    def remaining(self):
        return None


def _triage(row):
    return {
        "row": row, "verdict": "confirmed-bug", "tshirt_size": "S",
        "has_tradeoff": True, "tradeoff": "a corner", "ruling_on_tradeoff": "no-ruling",
    }


def _plain(row):
    return {"row": row, "verdict": "confirmed-bug", "tshirt_size": "S", "has_tradeoff": False, "tradeoff": ""}


def _ruled(row, **over):
    return {"row": row, "verdict": "ruled", "ruling": _RULING, "externalAction": "", "actionable": True, **over}


def _run(reply, rows=("r0",), *, plain=(), fix=None, verify=None, post_stages=(), sink=None, close_sink=None, undo_sink=None):
    """Drive run_admission: every row in ``rows`` tradeoffs at triage (a hand-back) unless named in ``plain``."""
    budget = _Budget()
    fix_calls: dict = {}
    verify_calls: dict = {}
    params: list = []

    def agent(kind, unit_id):
        budget.spend(gc.STAGE_OUTPUT_TOKENS.get(kind, 10))
        if sink is not None:
            sink.append((kind, unit_id))
        if kind == "triage":
            return [_plain(r) if r in plain else _triage(r) for r in rows]
        if kind == "adjudicate":
            if isinstance(reply, Exception):
                raise reply
            return reply
        if kind == "fix":
            fix_calls[unit_id] = fix_calls.get(unit_id, 0) + 1
            return fix(unit_id, fix_calls[unit_id]) if fix else {"outcome": "done"}
        if kind == "verify":
            verify_calls[unit_id] = verify_calls.get(unit_id, 0) + 1
            return verify(unit_id, verify_calls[unit_id]) if verify else {"outcome": "pass"}
        if kind == "commit":
            queued = params[-1]["grind"]["rows"]
            return {"committed": True, "sha": f"sha{len(params)}", "rows": [
                {"row_id": q["row_id"], "outcome": "settled", "sha": f"sha{len(params)}"} for q in queued
            ]}
        return {"outcome": "recorded", "swept": True}

    return gc.run_admission(
        [("b0", list(rows))], _routing(), "triage",
        reserve=gc.batch_reserve(len(rows)), budget=budget, agent=agent,
        commit_params_sink=params, run_close_sink=close_sink, undo_paths_sink=undo_sink,
        post_stages=post_stages,
    )


def test_actionable_ruling_continues_into_one_fix_pass_and_lands():
    sink: list = []
    result = _run({"rulings": [_ruled("r0")]}, sink=sink)
    assert result["handed_back"] == []
    assert [(s["row"], s["outcome"]) for s in result["settled"]] == [("r0", "committed")]
    assert result["settled"][0]["sha"] == "sha1"
    kinds = [k for k, _ in sink]
    assert kinds.count("fix") == 1 and kinds.count("adjudicate") == 1 and kinds.count("commit") == 1
    assert kinds.index("triage") < kinds.index("adjudicate") < kinds.index("fix") < kinds.index("commit")
    assert kinds.index("commit") < kinds.index("ledger-sweep")
    assert result["by_triage_check"]["ruled_tradeoff"] == 1


def test_a_continued_rows_tradeoff_is_judged_against_the_ruling_not_re_adjudicated():
    def tradeoff(on):
        return lambda rid, n: {
            "outcome": "done", "has_tradeoff": True, "tradeoff": "again", "ruling_on_tradeoff": on,
        }

    accepted = _run({"rulings": [_ruled("r0")]}, fix=tradeoff("accepts"))
    assert accepted["handed_back"] == [] and accepted["settled"][0]["row"] == "r0"

    sink: list = []
    refused = _run({"rulings": [_ruled("r0")]}, fix=tradeoff("rejects"), sink=sink)
    assert [h["type"] for h in refused["handed_back"]] == ["repeat-refused"]
    assert refused["handed_back"][0]["ruling"] == _RULING
    assert refused["settled"] == []
    assert [k for k, _ in sink].count("adjudicate") == 1

    silent = _run({"rulings": [_ruled("r0")]}, fix=tradeoff("silent"))
    assert [h["type"] for h in silent["handed_back"]] == ["needs-judgment"]
    assert "adjudication" not in silent["handed_back"][0]


def test_only_actionable_rows_continue_and_the_rest_ride_their_adjudication():
    rows = ("r0", "r1", "r2", "r3", "r4")
    reply = {"rulings": [
        _ruled("r0"),
        _ruled("r1", verdict="pm-only", actionable=False),
        _ruled("r2", verdict="spin-out"),
        _ruled("r3", externalAction="merge to main"),
        _ruled("r4", ruling=" "),
    ]}
    sink: list = []
    result = _run(reply, rows, sink=sink)
    assert [(s["row"], s["outcome"]) for s in result["settled"]] == [("r0", "committed")]
    held = {h["row"]: h for h in result["handed_back"]}
    assert set(held) == {"r1", "r2", "r3", "r4"}
    assert all(h["type"] == "needs-judgment" and "adjudication" in h for h in held.values())
    assert held["r3"]["adjudication"]["pmOnly"] is True
    kinds = [(k, u) for k, u in sink]
    assert [u for k, u in kinds if k == "fix"] == ["r0"]
    assert [k for k, _ in kinds].count("adjudicate") == 1


def test_a_row_missing_from_the_reply_fails_closed_to_pm_only():
    result = _run({"rulings": [_ruled("r0")]}, ("r0", "r1"))
    assert [s["row"] for s in result["settled"]] == ["r0"]
    (held,) = result["handed_back"]
    assert held["row"] == "r1"
    assert held["adjudication"]["pmOnly"] is True and held["adjudication"]["verdict"] == "unavailable"


def test_a_throwing_adjudicator_fails_every_bound_hand_back_closed():
    sink: list = []
    result = _run(RuntimeError("boom"), ("r0", "r1"), sink=sink)
    assert [h["adjudication"]["pmOnly"] for h in result["handed_back"]] == [True, True]
    assert "fix" not in [k for k, _ in sink]


def test_several_hand_backs_make_exactly_one_adjudicate_call_and_one_fix_each():
    rows = ("r0", "r1", "r2")
    sink: list = []
    result = _run({"rulings": [_ruled(r) for r in rows]}, rows, sink=sink)
    kinds = [k for k, _ in sink]
    assert kinds.count("adjudicate") == 1 and kinds.count("fix") == 3
    assert result["spend"]["agent_calls_by_stage_kind"]["adjudicate"] == 1
    assert sorted(s["row"] for s in result["settled"]) == list(rows)
    assert result["handed_back"] == []
    assert kinds.count("commit") == 1


def test_continued_rows_reach_the_post_stages_after_their_commit():
    sink: list = []
    _run({"rulings": [_ruled("r0")]}, post_stages=("review", "review-fix"), sink=sink)
    kinds = [k for k, _ in sink]
    assert kinds.index("adjudicate") < kinds.index("fix") < kinds.index("commit") < kinds.index("review")
    assert kinds.index("review") < kinds.index("review-fix") < kinds.index("ledger-sweep")


def test_a_continuation_pass_that_throws_lands_what_it_finished_and_hands_back_the_rest():
    def verify(rid, n):
        if rid == "r2" and n == 1:
            raise RuntimeError("verify blew up")
        return {"undo": {"outcome": "undone"}} if rid == "r2" else {"outcome": "pass"}

    close_sink: list = []
    undo_sink: list = []
    sink: list = []
    with pytest.raises(RuntimeError, match="verify blew up"):
        _run(
            {"rulings": [_ruled("r1"), _ruled("r2")]}, ("r1", "r2"),
            fix=lambda rid, n: {"outcome": "done", "touched_files": [f"{rid}.py"]},
            verify=verify, sink=sink, close_sink=close_sink, undo_sink=undo_sink,
        )
    kinds = [k for k, _ in sink]
    assert kinds.count("commit") == 1 and kinds.index("commit") < kinds.index("ledger-sweep")
    record = close_sink[-1]["record"]
    assert record["counts"]["by_outcome"] == {"committed": 1}
    assert [(h["row"], h["type"]) for h in record["handed_back"]] == [("r2", "stage-dead")]
    assert undo_sink == [{"row": "r2", "paths": ["r2.py"]}]


def test_no_pm_bound_hand_back_spawns_no_adjudicate_call():
    budget = _Budget()
    kinds: list = []

    def agent(kind, unit_id):
        kinds.append(kind)
        if kind == "triage":
            return [{"row": "r0", "verdict": "duplicate", "tshirt_size": "S", "has_tradeoff": False, "tradeoff": ""}]
        return {"outcome": "recorded", "swept": True}

    gc.run_admission([("b0", ["r0"])], _routing(), "triage", reserve=gc.batch_reserve(1), budget=budget, agent=agent)
    assert "adjudicate" not in kinds


def test_emitted_hand_back_row_never_adjudicates_per_hand_back():
    script = _compose()
    body = script[script.index("async function _handBackRow("):]
    body = body[: body.index("async function _applyRouteRow(")]
    for gone in ("_adjudicateHandback", "ruledOnce", "_reloop", "_ruledFixNode"):
        assert gone not in body and gone not in script.replace("async function _adjudicateHandback(", "")
    assert "_adjudicate" not in body and "_continueRuled" not in body


def test_emitted_adjudicate_runs_once_after_the_loop_and_continuation_follows_it():
    script = _compose()
    outer_open = "try {\ntry { await runGrind(); }"
    assert script.index("async function _adjudicatePmBound(") < script.index(outer_open)
    assert script.index("const PM_BOUND_TYPES") < script.index(outer_open)
    assert script.count("await _adjudicatePmBound();") == 1
    assert script.count("await _continueRuled();") == 1
    adjudicated = script.index("await _adjudicatePmBound();")
    assert adjudicated > script.index("await runGrind()")
    assert adjudicated < script.index("await _continueRuled();") < script.index("const _reviewPaths")
    assert script.index("await _continueRuled();") < script.index("await _runClose();")


def test_emitted_fix_prompt_carries_the_in_run_ruling():
    script = _compose()
    fix_call = script[script.index("async function _fixCall("): script.index("const _UNDO_EXEMPT")]
    assert "row.rulingNote" in fix_call
    body = script[script.index("async function _continueRuled("): script.index("try {\ntry { await runGrind(); }")]
    assert "row.rulingNote =" in body and "row.pmRuling = h.adjudication.ruling" in body
    assert "SIZE_FIX_TARGET" in body and "_adjudicate" not in body


def test_default_compose_has_one_sonnet_medium_adjudicate_call_and_no_apm():
    script = _compose()
    start = script.index("async function _adjudicateBatch(")
    block = script[start: script.index("async function _adjudicatePmBound(")]
    assert len(re.findall(r"label: 'adjudicate'", script)) == 1
    assert "coordinator:apm" not in script and "model: 'opus'" not in block
    assert "model: 'sonnet'" in block and "effort: 'medium'" in block
    assert block.count("await agent(") == 1


_CONTINUE_HARNESS_JS = r"""
import fs from 'node:fs';
const [,, scriptPath, mode] = process.argv;
const src = fs.readFileSync(scriptPath, 'utf8').replace(/\r\n/g, '\n').replace(/export const meta = \{[\s\S]*?\n\};\n/, '');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const labels = [];
const fixPrompts = {};
let ledgerPrompt = '';
const rowNo = (prompt) => { const m = /row(\d+)/.exec(prompt); return m ? Number(m[1]) : 0; };
const reply = (opts, prompt) => {
  const label = opts.label || '';
  labels.push(label);
  if (label.startsWith('ledger-sweep')) ledgerPrompt = prompt;
  const rows = [...new Set([...prompt.matchAll(/"row_id":"(row\d+)"/g)].map((m) => m[1]))];
  if (label === 'triage') {
    return { rows: rows.map((r) => ({ row: r, verdict: 'confirmed-bug', tshirt_size: 'S', has_tradeoff: r === 'row0', tradeoff: r === 'row0' ? 'a corner' : '', ruling_on_tradeoff: 'no-ruling', evidence: 'e', fix_plan: 'p' })), stale: [] };
  }
  if (label === 'adjudicate') {
    const verdict = mode === 'pm-only' ? 'pm-only' : 'ruled';
    return { rulings: [{ row: 'row0', verdict, ruling: 'RULING-TEXT-X', externalAction: '', actionable: verdict === 'ruled' }] };
  }
  if (label === 'fix') {
    const n = rowNo(prompt);
    (fixPrompts['row' + n] = fixPrompts['row' + n] || []).push(prompt);
    return { outcome: 'done', touched_files: ['f' + n + '.py'], has_tradeoff: false, ruling_on_tradeoff: 'no-ruling' };
  }
  if (label.startsWith('verify')) return { outcome: 'pass', exit_code: 0, output: {} };
  if (label === 'commit') {
    return { committed: true, sha: 'abc' + labels.filter((l) => l === 'commit').length, rows: rows.map((r) => ({ row_id: r, outcome: 'settled', sha: 'abc' })) };
  }
  return {};
};
const agent = (prompt, opts = {}) => new Promise((res) => setTimeout(() => res(reply(opts, prompt)), 1));
const budget = { spent: () => 0, remaining: () => null, total: null };
const fn = new AsyncFunction('args', 'agent', 'phase', 'log', 'budget', 'parallel', src);
await fn(
  { run_stamp: '20261017T000000Z', script_path: 'x.mjs', profile_dir: 'pd', repo_root: '/r', run_token: 'tok' },
  agent, () => {}, () => {}, budget, async (xs) => Promise.all(xs),
);
const record = JSON.parse(decodeURIComponent([...ledgerPrompt.matchAll(/printf %s (\S+) >> \S+\.rec/g)].map((m) => m[1]).join('')));
const rulingFix = (fixPrompts.row0 || []);
console.log(JSON.stringify({
  adjudicates: labels.filter((l) => l === 'adjudicate').length,
  commits: labels.filter((l) => l === 'commit').length,
  fixesRow0: rulingFix.length,
  rulingInFix: rulingFix.length > 0 && rulingFix[rulingFix.length - 1].includes('RULING-TEXT-X'),
  rulingInOtherFix: Object.keys(fixPrompts).filter((k) => k !== 'row0').some((k) => fixPrompts[k].some((p) => p.includes('RULING-TEXT-X'))),
  handedBackRows: record.handed_back.map((h) => h.row + ':' + h.type),
  committed: record.counts.by_outcome.committed || 0,
  ruledTradeoff: record.counts.by_triage_check.ruled_tradeoff,
  adjudicationKept: record.handed_back.filter((h) => h.row === 'row0').every((h) => !!h.adjudication),
  order: labels.filter((l) => ['triage', 'adjudicate', 'fix', 'commit'].includes(l)).join(','),
}));
"""


def _run_continue_harness(tmp_path, mode: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node absent")
    script = tmp_path / "grind.mjs"
    script.write_bytes(_compose().encode("utf-8"))
    harness = tmp_path / "harness.mjs"
    harness.write_text(_CONTINUE_HARNESS_JS, encoding="utf-8")
    out = subprocess.run(
        [node, str(harness), str(script), mode], capture_output=True, text=True, check=False, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_emitted_grind_continues_a_ruled_actionable_row_into_fix_with_the_ruling_in_the_brief(tmp_path):
    seen = _run_continue_harness(tmp_path, "ruled")
    assert seen["adjudicates"] == 1 and seen["fixesRow0"] == 1 and seen["commits"] == 2
    assert seen["rulingInFix"] is True and seen["rulingInOtherFix"] is False
    assert seen["committed"] == 7 and seen["ruledTradeoff"] == 1
    assert not any(h.startswith("row0:") for h in seen["handedBackRows"])
    order = seen["order"].split(",")
    assert order.index("adjudicate") < len(order) - 1 - order[::-1].index("fix")
    assert order[-1] == "commit" and order[order.index("adjudicate") + 1] == "fix"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_emitted_grind_hands_a_pm_only_ruling_back_without_a_fix_pass(tmp_path):
    seen = _run_continue_harness(tmp_path, "pm-only")
    assert seen["adjudicates"] == 1 and seen["fixesRow0"] == 0 and seen["commits"] == 1
    assert "row0:needs-judgment" in seen["handedBackRows"] and seen["adjudicationKept"] is True
    assert seen["committed"] == 6 and seen["ruledTradeoff"] == 0
