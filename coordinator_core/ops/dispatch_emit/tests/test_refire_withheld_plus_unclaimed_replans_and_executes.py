"""A refire whose resumed ledger holds a withheld/unlanded row AND an unclaimed register row runs the ledger
re-plan leg and executes the row it adds together with the withheld one (the memo's C3 / R2 shape).

The composed resume block and re-plan leg are executed under node with the agent and stage adoption stubbed.
"""

from __future__ import annotations

import shutil

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import reply_sidecar
from coordinator_core.ops.dispatch_emit.ask_gate import _handler
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_partial_ledger_halt_e2e import (  # noqa: F401
    PLAN,
    SIZING,
    halted,
    repo,
)

pytestmark = [pytest.mark.complete]
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
const _agentDefsClause = () => ''; const _agentHead = () => ''; const _agentRead = () => '';
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


_ADDED = {"added_rows": ["C4"], "stage_reply": {"manifest": {"rows": [{"id": "C3"}, {"id": "C4"}]}}}


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
