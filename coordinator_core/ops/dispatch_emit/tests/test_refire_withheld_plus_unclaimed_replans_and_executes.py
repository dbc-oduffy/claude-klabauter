"""A refire whose resumed ledger holds a withheld/unlanded row AND an unclaimed register row runs the ledger
re-plan leg and executes the row it adds together with the withheld one (the memo's C3 / R2 shape).

The composed resume block and re-plan leg are executed under node with the agent and stage adoption stubbed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, reply_sidecar
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_gate import _handler
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_partial_ledger_halt_e2e import (  # noqa: F401
    PLAN,
    SIZING,
    halted,
    repo,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node absent")

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_LINES = (
    "let _ledgerExec = false;", "let _xsTail = false;", "let _ledgerAdded = [];",
    "const _resumeLedger = ", "const _ledgerUnlanded = ", "const _ledgerDerived = ", "const _ledgerUnserved = ",
    "const _ledgerOwes = ", "if (_resumeLedger) { _planRel = ", "if (_ledgerOwes && !_stageRefusal) {",
)
_HARNESS = """
let _halted = null, _stagedManifest = null, _relayGap = null, _diskHalt = null, _stageRefusal = null;
let _planRel = 'p.md', _sizingRel = 's.yaml', _runId = 'r', _crossRepoApproved = false, _SESSION_ID = 's';
let _gate = %(gate)s;
const _calls = [];
const _agentDefsClause = () => '';
const _agentReplies = %(replies)s;
async function agent(prompt, opts) { _calls.push(opts.label); return _agentReplies.shift() ?? { added_rows: [], stage_reply: null }; }
async function _adoptStageReply(r) { if (r && r.manifest) _stagedManifest = r.manifest; }
%(consts)s
(async () => {
%(body)s
console.log(JSON.stringify({
  calls: _calls, ledgerExec: _ledgerExec, xsTail: _xsTail, added: _ledgerAdded, halted: _halted,
  staged: _stagedManifest ? _stagedManifest.rows : null,
}));
})();
"""


def _compose(monkeypatch, arm: str) -> list[str]:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    script = compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="x.mjs", in_run_commit=True, **REVIEW_KW,
    )
    return [ln.strip() for ln in script.split("\n")]


def _drive(monkeypatch, arm: str, resume_ledger: dict, replies: list) -> dict:
    lines = _compose(monkeypatch, arm)
    picked = [ln for prefix in _LINES for ln in lines if ln.startswith(prefix)]
    assert len(picked) == len(_LINES), [ln[:50] for ln in picked]
    body = "\n".join(picked)
    names = sorted(set(re.findall(r"\b_PF[A-Z]?\d+\b", body)))
    program = _HARNESS % {
        "gate": json.dumps({"arm": arm, "resume_ledger": resume_ledger}),
        "replies": json.dumps(replies),
        "consts": "\n".join(f"const {n} = '';" for n in names),
        "body": body,
    }
    done = subprocess.run(
        ["node", "-"], input=program, capture_output=True, text=True, **no_console_creationflags()
    )
    assert done.returncode == 0, done.stderr[-1500:]
    return json.loads(done.stdout)


def _ledger(**over) -> dict:
    base = {
        "plan": "p.md", "blocking": ["R1", "R2"], "unlanded": ["C3"], "unclaimed_rows": ["R2"],
        "remaining": {}, "notes": [],
    }
    return {**base, **over}


_ADDED = {"added_rows": ["C4"], "stage_reply": {"manifest": {"rows": [{"id": "C3"}, {"id": "C4"}]}}}


@needs_node
@pytest.mark.parametrize("arm", ["m_plus", "s", "xs"])
def test_withheld_row_plus_unclaimed_register_row_runs_the_replan_leg_and_stages_the_new_row(monkeypatch, arm):
    out = _drive(monkeypatch, arm, _ledger(), [_ADDED])
    assert out["calls"] == ["ledger-replan"]
    assert out["added"] == ["C4"] and out["ledgerExec"] is True and out["halted"] is None
    assert [r["id"] for r in out["staged"]] == ["C3", "C4"]


@needs_node
@pytest.mark.parametrize("arm", ["m_plus", "s", "xs"])
def test_a_replan_that_adds_no_row_halts_on_the_unclaimed_row_and_never_executes_only_the_withheld_one(monkeypatch, arm):
    out = _drive(monkeypatch, arm, _ledger(), [])
    assert out["calls"] == ["ledger-replan", "ledger-replan"]
    assert out["added"] == [] and out["halted"] is not None
    assert "R2" in out["halted"]["reason"]


@needs_node
@pytest.mark.parametrize("arm", ["m_plus", "s", "xs"])
def test_a_withheld_row_with_nothing_unclaimed_skips_the_replan_leg(monkeypatch, arm):
    out = _drive(monkeypatch, arm, _ledger(blocking=["R1"], unclaimed_rows=[]), [_ADDED])
    assert out["calls"] == [] and out["ledgerExec"] is True and out["halted"] is None


def test_gate_hands_the_script_both_the_withheld_row_and_the_unclaimed_register_row(halted):  # noqa: F811
    root = halted
    doc = yaml.safe_load((root / SIZING).read_text(encoding="utf-8"))
    rows = doc.setdefault("requirement_register", {}).setdefault("rows", [])
    rows.append({"id": "R2", "status": "open", "source_text": "second", "surface": "internal"})
    (root / SIZING).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    text = (root / PLAN).read_text(encoding="utf-8")
    text = text.replace("---\n", f"---\nregister_claims:\n  sizing: {SIZING}\n  rows: [R1]\n", 1)
    (root / PLAN).write_text(text, encoding="utf-8", newline="\n")

    out = _handler({"sizing_path": SIZING, "run_id": "refire1", "writes": ["b.py"]}, repo_root=root)

    assert out["halt"] is None, out
    ledger = out["resume_ledger"]
    assert ledger["unclaimed_rows"] == ["R2"] and "C2" in ledger["unlanded"]
    assert "C2" in sorted(r["id"] for r in reply_sidecar.read_reply(root, out["manifest_handle"])["rows"])
