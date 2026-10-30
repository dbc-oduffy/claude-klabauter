"""Incident replay: rows revised into a live run after its plan point are judged in that run, or land on refire.

Case 1 (verify not yet staged): the late row is read, unioned into verify's ledger ids, and judged.
Case 2 (verify staged): the revise reply says `lands_on_refire`, and a refire after the halt stages both rows."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.win_portability import no_console_creationflags

from coordinator_core.ops.dispatch_emit import ask_gate, ask_stage
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import repo as _bare_repo
from coordinator_core.ops.dispatch_emit.tests.test_plain_resume_refire_stages_current_rows import (
    PLAN_REL,
    _plan,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261029T000000-aaa111"
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


@pytest.fixture
def repo(_bare_repo, monkeypatch):
    (_bare_repo / ".git").mkdir()
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)
    monkeypatch.setattr(run_token, "is_live", lambda *_a, **_k: True)
    _put(_bare_repo, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAN_REL)
    _plan(_bare_repo, {"C1": "open"})
    rl.start(_bare_repo, RUN)
    rl.bind(_bare_repo, RUN, REL, ask_text="the first ask")
    return _bare_repo


def _stage(root):
    params = {"run_id": RUN, "plan_path": PLAN_REL, "writes": ["pkg/C1.py"], "commit_sizing_path": REL}
    reply = ask_stage._handler(params, root)
    assert reply.get("manifest_handle"), reply
    return json.loads((record_dir(root, RUN) / "manifest.json").read_text(encoding="utf-8"))


def _revise(root, text):
    out = rl._handler({"run_id": RUN, "verbatim": text}, repo_root=root)
    assert out["ok"] is True and out["run_live"] is True, out
    return out


def _ledger_ids(root):
    return [i.id for i in rl.read_ledger(root, record_dir(root, RUN)).items]


def _verdict(ids):
    return {"items": [{"id": i, "status": "met", "wired": True, "met_by": "pkg/C1.py"} for i in ids]}


def _compose():
    return compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        in_run_commit=True, script_path="scratch/warp/a.workflow.mjs", **REVIEW_KW,
    )


def test_a_row_revised_before_verify_is_late_unioned_into_verify_and_judged(repo):
    manifest = _stage(repo)
    staged = list(manifest["ledger_item_ids"])
    assert staged and not rl.verify_staged(record_dir(repo, RUN))

    out = _revise(repo, "also cover Windows")
    added = out["item"]
    assert "lands_on_refire" not in out and "pre-verify late-row leg" in out["note"]
    assert rl.late_rows(repo, RUN) == [added]

    script = _compose()
    line = next(ln for ln in script.splitlines() if "const _ledgerIds =" in ln)
    assert "_verifyLateIds" in line
    verify_ids = [*staged, *rl.late_rows(repo, RUN)]
    assert added in verify_ids and sorted(verify_ids) == sorted(_ledger_ids(repo))

    ledger = rl.read_ledger(repo, record_dir(repo, RUN))
    judged = rl.judge(ledger, _verdict(verify_ids))
    assert not any("unjudged" in f["reason"] for f in judged["failures"]), judged
    skipped = rl.judge(ledger, _verdict(staged))
    assert [f["id"] for f in skipped["failures"] if "unjudged" in f["reason"]] == [added]


def test_pre_verify_late_ids_run_one_bounded_executor_leg_before_the_verify_leg():
    script = _compose()
    assert script.count("_lateLeg = async (_ids) =>") == 1
    read = script.index("_verifyLateIds = await _lateRead();")
    leg = script.index("await _lateLeg(_verifyNew);")
    verify = script.index("const _runVerify = ")
    assert read < leg < verify
    assert script.count("await _lateLeg(_verifyNew);") == 1
    assert "_verifyLateIds.filter((i) => !_lateIds.includes(i))" in script
    assert "_ledgerIds = Array.isArray(_ledgerBase) ? [...new Set([..._ledgerBase, ..._verifyLateIds])]" in script


_DRIVER_JS = r"""
const S = { _halted: null, _lateIds: [], _verifyLateIds: [], _lateLeg: null, _rows: {}, _verifications: [],
  _stagedManifest: null, _relayGap: null, _diskHalt: null, _stageRefusal: null, _USAGE_LIMIT_RE: /usage limit/i,
  _manifest: { rows: [{ id: 'C1' }], review_declared_paths: [], test_targets: [], gated: [], owed: [] },
  _lateReads: READS, order: [], events: [] };
S.log = () => {};
S.agent = async (prompt, opts) => {
  S.order.push(opts.label);
  if (opts.label === 'late-rows-read') return { late_rows: S._lateReads.shift() ?? [] };
  if (opts.label === 'ledger-late') return { added_rows: ['C2'], stage_reply: {} };
  return 'DONE ' + opts.label;
};
S._adoptStageReply = async () => { S._stagedManifest = { rows: [{ id: 'C2', writes: ['pkg/C2.py'], deps: [] }] }; };
S._runRow = (id, deps, x, fn) => fn();
S._rowPrompt = (r) => 'brief ' + r.id;
S._groupRun = async () => null;
S.S = S;
const guard = { has: (t, k) => !(k in globalThis), get: (t, k) => (k in S ? S[k] : () => ''),
  set: (t, k, v) => { S[k] = v; return true; } };
const run = new Function('p', 'with (p) { return (async () => {' + BODY + '})(); }');
run(new Proxy({}, guard)).then(() => console.log(JSON.stringify({ order: S.order, rows: S._manifest.rows.map((r) => r.id),
  halted: S._halted, verifyIds: S._verifyLateIds }))).catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
"""


def _between(script, start, end):
    """The script text from the literal `start` through the first match of regex `end` (indent-agnostic)."""
    i = script.index(start)
    return script[i: re.compile(end).search(script, i).end()]


def _run_composed_late_legs(reads, tmp_path):
    """Run the composed `_lateRead`, `_lateLeg` and verify-block late snippet under node with stubbed agents."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    script = _compose()
    read = _between(script, "const _lateRead = async () => {", r"\n\s*};")
    leg = _between(script, "_lateLeg = async (_ids) => {", r"\n\s*};")
    verify = _between(script, "_verifyLateIds = await _lateRead();", r"await Promise\.all\(_verifications\); \} \}")
    body = "\n".join([
        read, leg,
        "_lateIds = await _lateRead(); if (_lateIds.length) { await _lateLeg(_lateIds); }",
        verify,
        "S.order.push('verify');",
    ])
    source = _DRIVER_JS.replace("READS", json.dumps(reads)).replace("BODY", json.dumps(body))
    driver = tmp_path / "late_legs_driver.js"
    driver.write_text(source, encoding="utf-8")
    proc = subprocess.run([node, str(driver)], capture_output=True, text=True, timeout=30,
                          **no_console_creationflags())
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("reads", [[[], ["R2"]], [["R2"], ["R2"]]])
def test_a_row_revised_after_execute_is_executed_once_then_verify_runs_without_a_halt(reads, tmp_path):
    out = _run_composed_late_legs(reads, tmp_path)
    executors = [lbl for lbl in out["order"] if lbl.endswith("C2")]
    assert len(executors) == 1, out["order"]
    assert out["order"].count("ledger-late") == 1, out["order"]
    assert out["order"].index(executors[0]) < out["order"].index("verify"), out["order"]
    assert out["rows"] == ["C1", "C2"] and out["halted"] is None
    assert out["verifyIds"] == ["R2"]


def test_no_late_row_runs_no_extra_leg(tmp_path):
    out = _run_composed_late_legs([[], []], tmp_path)
    assert "ledger-late" not in out["order"] and out["order"][-1] == "verify"


def test_a_row_revised_after_verify_lands_on_refire_and_the_refire_stages_both_rows(repo):
    manifest = _stage(repo)
    r2 = _revise(repo, "also cover Windows")["item"]
    prompts = record_dir(repo, RUN) / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    (prompts / "verify.md").write_text("verify", encoding="utf-8")

    out = _revise(repo, "and cover macOS")
    r3 = out["item"]
    assert out["lands_on_refire"] is True and out["next"]["kind"] == "refire"
    assert "next stage claims" not in json.dumps(out)
    assert r2 not in manifest["ledger_item_ids"] and r3 not in manifest["ledger_item_ids"]

    refire = ask_gate._handler({"sizing_path": REL, "run_id": "refire1", "writes": ["pkg/C1.py"]}, repo)
    assert refire["halt"] is None, refire
    params = {"run_id": "refire1", "plan_path": PLAN_REL, "writes": ["pkg/C1.py"], "commit_sizing_path": REL}
    reply = ask_stage._handler(params, repo)
    assert reply.get("manifest_handle"), reply
    restaged = json.loads((record_dir(repo, "refire1") / "manifest.json").read_text(encoding="utf-8"))
    assert {r2, r3} <= set(restaged["ledger_item_ids"]), restaged["ledger_item_ids"]
