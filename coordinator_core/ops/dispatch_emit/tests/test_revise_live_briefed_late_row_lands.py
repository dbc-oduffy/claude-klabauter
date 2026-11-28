"""A workflow.revise on a live run whose rows are briefed lands its row before verify, or is refused at send.

Landing case (the 91f144 shape): the staged manifest already lists the revised id with a row nobody dispatched.
The real revise handler accepts the note, the real stage composes it into the late row's brief, and the
composed late legs run under node: the row is dispatched with that brief and settles before verify's first event.
Refusal case: a script without the late leg answers the revise with `run_will_not_read`."""

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
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.halt_resume_build import COMPOSED_SCRIPT_FILE
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import repo as _bare_repo
from coordinator_core.ops.dispatch_emit.tests.test_late_revise_row_judged_e2e import (
    _DRIVER_JS,
    _between,
    _compose,
)
from coordinator_core.ops.dispatch_emit.tests.test_plain_resume_refire_stages_current_rows import PLAN_REL

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261116T000000-91f144"
NOTE = "pin not_admitted_by_reason as reason -> {cohort: int}"
WRITES = ["pkg/C1.py", "pkg/C2.py"]


def _plan(repo, late_register_row=None):
    rows = [("C1", ["R1"]), *([("C2", [late_register_row])] if late_register_row else [])]
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: open\n  change_kind: code-edit\n"
        f"  surface: pkg/{i}.py\n  writes:\n    - pkg/{i}.py\n  register_rows: {json.dumps(reg)}\n"
        for i, reg in rows
    )
    (repo / PLAN_REL).parent.mkdir(parents=True, exist_ok=True)
    (repo / PLAN_REL).write_text(
        f"---\nsizing_object: {REL}\n---\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n",
        encoding="utf-8", newline="\n",
    )


def _stage(repo):
    params = {"run_id": RUN, "plan_path": PLAN_REL, "writes": WRITES, "commit_sizing_path": REL}
    reply = ask_stage._handler(params, repo)
    assert reply.get("manifest_handle"), reply
    return json.loads((record_dir(repo, RUN) / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture
def repo(_bare_repo, monkeypatch):
    (_bare_repo / ".git").mkdir()
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)
    monkeypatch.setattr(run_token, "is_live", lambda *_a, **_k: True)
    _put(_bare_repo, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAN_REL)
    rl.start(_bare_repo, RUN)
    rl.bind(_bare_repo, RUN, REL, ask_text="the first ask")
    return _bare_repo


def _briefed_live_run_revised(repo):
    """Stage C1, brief it, revise at default params, then restage as the pre-verify re-plan does."""
    _plan(repo)
    first = _stage(repo)
    assert [r["id"] for r in first["rows"]] == ["C1"] and all(r.get("brief_path") for r in first["rows"])
    out = rl._handler({"run_id": RUN, "verbatim": NOTE}, repo_root=repo)
    assert out["ok"] is True and out["run_live"] is True and out["refused"] == [], out
    assert out["briefed"] is True and "late-row leg" in out["note"] and "will_not_read" not in out
    item = out["item"]
    _plan(repo, item)
    return item, _stage(repo)


def _run(reads_by_served, restaged, brief_texts, tmp_path):
    """Run the composed late legs under node; reads answer from the real `workflow.late_rows`."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    script = _compose()
    read = _between(script, "const _lateRead = async (_final) => {", r"\n\s*};")
    serve = _between(script, "const _lateServedParam = () => {", r"\n\s*};")
    launch = _between(script, "const _launchRow = (r) => {", r"\n  };")
    leg = _between(script, "_lateLeg = async (_ids) => {", r"\n\s*};")
    verify = _between(script, "_verifyLateIds = await _lateRead(true);", r"await Promise\.all\(_verifications\); \} \}")
    body = "\n".join([
        "const _resumeLedger = false;", serve, read, launch, leg,
        "_lateIds = await _lateRead(); if (_lateIds.length) { await _lateLeg(_lateIds); }",
        verify,
        "S.order.push('verify');",
    ])
    source = (
        _DRIVER_JS.replace("READS", "[]").replace("ADDED", "[]").replace("BODY", json.dumps(body))
        .replace("_manifest: { rows: [{ id: 'C1' }]", "_manifest: { rows: [{ id: 'C1', register_rows: ['R1'] }]")
        .replace("S.agent = async (prompt, opts) => {\n  S.order.push(opts.label);",
                 "const TABLE = " + json.dumps(reads_by_served) + ";\nconst BRIEFS = " + json.dumps(brief_texts) + ";\n"
                 "S.prompts = {};\nS.agent = async (prompt, opts) => {\n  S.order.push(opts.label); S.prompts[opts.label] = prompt;")
        .replace("if (opts.label === 'late-rows-read') return { late_rows: S._lateReads.shift() ?? [] };",
                 "if (opts.label === 'late-rows-read') { const m = /\"served\":(\\[[^\\]]*\\])/.exec(prompt); "
                 "return { late_rows: TABLE[m ? m[1] : 'none'] ?? [] }; }")
        .replace("S._adoptStageReply = async () => { S._stagedManifest = { rows: [{ id: 'C2', writes: ['pkg/C2.py'], deps: [] }] }; };",
                 "S._adoptStageReply = async () => { S._stagedManifest = " + json.dumps(restaged) + "; };")
        .replace("S._rowPrompt = (r) => 'brief ' + r.id;", "S._rowPrompt = (r) => BRIEFS[r.id] ?? '';")
        .replace("rows: S._manifest.rows.map((r) => r.id),", "rows: S._manifest.rows.map((r) => r.id), prompts: S.prompts,")
        .replace("stage_reply: {} }", "stage_reply: { manifest_handle: 'h' } }")
    )
    driver = tmp_path / "revise_live_driver.js"
    driver.write_text(source, encoding="utf-8")
    proc = subprocess.run([node, str(driver)], capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_note_revised_into_a_live_briefed_run_is_dispatched_with_its_brief_and_lands_before_verify(repo, tmp_path):
    item, restaged = _briefed_live_run_revised(repo)
    assert item in restaged["ledger_item_ids"]
    late = next(r for r in restaged["rows"] if r["id"] == "C2")
    assert item in late["register_rows"]
    brief = (repo / late["brief_path"]).read_text(encoding="utf-8")
    assert NOTE in brief

    # The 91f144 shape: the disk manifest lists the id, yet the script dispatched only C1.
    assert rl.late_rows(repo, RUN, ["R1"]) == [item] and rl.late_rows(repo, RUN) == []
    served = {json.dumps(["R1"], separators=(",", ":")): [item], json.dumps(["R1", item], separators=(",", ":")): []}
    assert all(rl.late_rows(repo, RUN, json.loads(k)) == v for k, v in served.items())

    out = _run(served, restaged, {late["id"]: brief}, tmp_path)
    labels = out["order"]
    executors = [lbl for lbl in labels if lbl.endswith("C2")]
    assert len(executors) == 1, labels
    assert labels.count("ledger-late") == 1, labels
    assert labels.index(executors[0]) < labels.index("verify"), labels
    assert NOTE in out["prompts"][executors[0]]
    assert out["rows"] == ["C1", "C2"] and out["halted"] is None and out["unplanned"] == []


def test_a_script_without_the_late_leg_refuses_the_revise_with_run_will_not_read(repo):
    _plan(repo)
    _stage(repo)
    script = _compose()
    marker = rl._LATE_LEG_MARKER
    assert marker in script
    (record_dir(repo, RUN) / COMPOSED_SCRIPT_FILE).write_text(script.replace(marker, "_noLeg = async (_ids) =>"), encoding="utf-8")
    out = rl._handler({"run_id": RUN, "verbatim": NOTE}, repo_root=repo)
    assert out["will_not_read"] is True and out["briefed"] is True
    assert {"input": "verbatim", "reason": "run_will_not_read"} in out["refused"]
    assert "no leg of this run executes or reads this revision" in out["note"]


def test_the_composed_script_with_the_late_leg_accepts_the_same_revise(repo):
    _plan(repo)
    _stage(repo)
    (record_dir(repo, RUN) / COMPOSED_SCRIPT_FILE).write_text(_compose(), encoding="utf-8")
    out = rl._handler({"run_id": RUN, "verbatim": NOTE}, repo_root=repo)
    assert out["refused"] == [] and "will_not_read" not in out and out["briefed"] is True
