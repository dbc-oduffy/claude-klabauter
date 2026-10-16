"""The composed ask script's one verify leg and ledger-check halt, run under node: a not_met item whose
only remaining acts follow the commit must not stop the run before its commit stage, the composed
blocking rule must agree with `requirements_ledger.judge`, and the verify agent's docs report settles
the ledger gaps that name only declared doc paths it wrote."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.requirements_ledger import Item, Ledger, judge
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _JUDGE_KW, _compose
from coordinator_core.win_portability import no_console_creationflags

_RUN_TOKEN = "a" * 32

_STUBS = r"""
const _seen = { phases: [], labels: [] };
const phase = (name) => { _seen.phases.push(name); };
const parallel = (thunks) => Promise.all(thunks.map((t) => t()));
const agent = async (_prompt, opts) => {
  _seen.labels.push(opts.label);
  if (opts.label === 'verify') {
    if (_VERIFY_STUB && _VERIFY_STUB.__throw) throw new Error('verify refused its schema');
    return _VERIFY_STUB;
  }
  if (opts.label === 'commit') return { committed: true, sha: 'abc', close: null };
  throw new Error('unstubbed agent label ' + opts.label);
};
let _halted = null;
let _manifest = { plan_id: 'p', rows: [], gated: %(gated)s, doc_writes: %(doc_writes)s, test_targets: [] };
let _docs = null, _commit = null, _nextAction = null;
let _incompleteChunks = [], _notStarted = [], _blockedChunks = [], _heldBy = [], _unansweredBriefs = [];
let _stoppedBy = [], _verifications = [], _planHeld = {};
let _reviewIntegration = { fixes_applied: 0, sidecar_path: 'x' }, _reviewPrep = { slices: [1] };
let _reviewWave = [], _deliveryVerdict = null, _deliveryNotRun = null, _testResult = null;
let _haltCommit = null; const _commitHaltRecord = async () => null; const _terminalCommitParamsBlock = (p) => JSON.stringify(p);
let _ledger = null, _falsifierResult = null, _falsifierSpec = null;
const log = () => {};
const _viaRoadmap = false, _RUN_TOKEN = '%(token)s', _sizingRel = 'state/sizings/x.yaml', _planRel = null, REPO_ROOT = '/r';
const _gate = { arm: 'xs' }, _runId = 'run-1', _resumeLedger = null;
let _splitExit = null;
let _writes = [];
const _verdictReports = [];
let %(owner_rows)s = [];
%(agent_defs)s
%(helper)s
%(cap)s
"""


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node


def _segment(in_run_commit: bool = True, judged: bool = False) -> str:
    """The composed script from the parallel tail through the terminal return."""
    script = _compose(
        prompt=None, sizing_rel="state/sizings/x.yaml", in_run_commit=in_run_commit, **(_JUDGE_KW if judged else {})
    )
    start = script.index("  if (!_halted && _manifest && !_manifest.error) {\n    phase(", script.index("phase('Review wave')"))
    ret = script.index("  return { arm:", start)
    stop = script.index("};", script.index("get terminal_commit_cli", ret)) + 2
    return "if (true) {\n" + script[start:stop]


def _run(
    stubs: list[dict],
    in_run_commit: bool = True,
    gated: tuple[str, ...] = (),
    doc_writes: tuple[str, ...] = (),
    doc_paths: tuple[str, ...] = (),
    judged: bool = False,
) -> list[dict]:
    """One result per verdict stub in `stubs`: halted_by, phases run, verify spawns, and the verdict handed to the commit.

    An unjudged stub is the ledger reply itself; a judged one is the judge's result, which carries
    the ledger items beside its verdict. Either is wrapped into the verify agent's reply together
    with the docs report the agent would return."""
    prelude = _STUBS % {
        "gated": json.dumps([{"id": g, "owner_repo": "sibling", "closure_key": None} for g in gated]),
        "doc_writes": json.dumps(list(doc_writes)),
        "token": _RUN_TOKEN,
        "owner_rows": ask_compose._OWNER_ROWS_JS,
        "agent_defs": ask_compose._AGENT_DEFS_HELPER_JS,
        "helper": ask_compose._usage_limit_helper_js(),
        "cap": ask_compose.CAP_HELPER_JS,
    }
    body = _segment(in_run_commit, judged).replace(
        "  return { arm:",
        "  return { phases: _seen.phases, verify_spawns: _seen.labels.filter((l) => l === 'verify').length, arm:",
        1,
    )
    docs = {"doc_paths": list(doc_paths), "note_path": "n.md", "stale_docs": []}

    def reply(stub: dict) -> dict:
        if stub.get("__throw"):
            return stub
        if not judged:
            return {**stub, **docs}
        ledger = {k: stub[k] for k in ("items", "all_of_pm_ask", "conflicts") if k in stub}
        return {"judge": stub, **ledger, **docs}

    program = (
        "const _CASES = JSON.parse(process.env.CASES);\n"
        "async function _once(_VERIFY_STUB) {\n" + prelude + body + "\n}\n"
        "(async () => { const out = []; for (const c of _CASES) out.push(await _once(c));"
        " console.log(JSON.stringify(out)); })().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });\n"
    )
    proc = subprocess.run(
        [_node(), "-"],
        input=program, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "CASES": json.dumps([reply(s) for s in stubs])},
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(autouse=True)
def _xs_arm(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")


def _item(id_: str, status: str, **extra) -> dict:
    return {"id": id_, "status": status, "essential": True, "wired": True, "evidence": "e", **extra}


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_commit_only_not_met_item_reaches_docs_and_commit():
    item = _item("R1", "not_met", remaining=["commit"], evidence="uncommitted")
    (out,) = _run([{"verdict": "fail", "items": [item], "conflicts": []}])
    assert out["halted_by"] is None, out["halted_by"]
    assert out["verify_spawns"] == 1 and out["docs"] is not None and "commit" in out["phases"]
    judged = out["ledger"]["items"]
    assert judged[0]["remaining"] == ["commit"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_verdict_handed_to_the_commit_carries_remaining():
    item = _item("R1", "not_met", remaining=["commit"])
    (out,) = _run([{"verdict": "fail", "items": [item], "conflicts": []}], in_run_commit=False)
    assert out["next_action"]["op"] == "dispatch.terminal_commit"
    handed = out["next_action"]["params"]["ledger_verdict"]["items"]
    assert [i["id"] for i in handed] == ["R1"]
    assert handed[0].get("remaining") == ["commit"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_an_all_post_commit_ledger_does_not_halt_on_a_fail_verdict():
    item = _item("R1", "post_commit", post_commit_acts=["commit"])
    (out,) = _run([{"verdict": "fail", "items": [item], "conflicts": []}])
    assert out["halted_by"] is None, out["halted_by"]
    assert out["verify_spawns"] == 1 and out["docs"] is not None and "commit" in out["phases"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_pre_commit_gap_still_halts_and_is_listed():
    item = _item("R1", "not_met", remaining=["the test is red"], evidence="red")
    (out,) = _run([{"verdict": "fail", "items": [item], "conflicts": []}])
    halted = out["halted_by"]
    assert halted and halted["halted"] == "ledger-check"
    assert [i["id"] for i in halted["items"]] == ["R1"]
    assert out["commit"] is None  # a halt commits its record only (a93b0ee881)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_ledger_halt_with_docs_edits_on_disk_still_yields_revise_and_refire():
    item = _item("R1", "not_met", remaining=["the test is red"], evidence="red")
    (out,) = _run(
        [{"verdict": "fail", "items": [item], "conflicts": []}],
        doc_writes=("docs/a.md",), doc_paths=("docs/a.md",),
    )
    assert out["verify_spawns"] == 1 and out["docs"]["doc_paths"] == ["docs/a.md"]
    assert out["halted_by"]["halted"] == "ledger-check"
    assert out["next_action"]["kind"] == "revise_and_refire" and out["next_action"]["op"] == "workflow.revise"
    assert out["revise_and_refire"] is not None
    assert out["commit"] is None  # a halt commits its record only (a93b0ee881)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_gap_naming_only_in_flight_doc_paths_docs_wrote_is_settled_without_a_second_read():
    item = _item("R1", "not_met", remaining=["docs/a.md"])
    (out,) = _run(
        [{"verdict": "fail", "items": [item], "conflicts": []}],
        doc_writes=("docs/a.md",), doc_paths=("docs/a.md",),
    )
    assert out["halted_by"] is None, out["halted_by"]
    assert out["ledger"]["items"][0]["status"] == "met"
    assert "commit" in out["phases"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_path_the_verify_agent_writes_outside_the_declared_docs_still_blocks_closure():
    """The writer-judge's bound is mechanical: only `_manifest.doc_writes` clears a gap by being written."""
    item = _item("R1", "not_met", remaining=["docs/out-of-bounds.md"])
    (out,) = _run(
        [{"verdict": "fail", "items": [item], "conflicts": []}],
        doc_writes=("docs/a.md",), doc_paths=("docs/a.md", "docs/out-of-bounds.md"),
    )
    assert out["halted_by"] and out["halted_by"]["halted"] == "ledger-check"
    assert [i["id"] for i in out["halted_by"]["items"]] == ["R1"]
    assert out["commit"] is None and out["next_action"]["kind"] == "revise_and_refire"


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize(
    ("doc_writes", "doc_paths", "remaining"),
    [
        (("docs/a.md",), (), ["docs/a.md"]),
        (("docs/a.md",), ("docs/a.md",), ["docs/a.md", "the test is red"]),
        ((), ("docs/a.md",), ["docs/a.md"]),
    ],
)
def test_a_doc_gap_docs_did_not_write_or_mixed_with_another_gap_still_halts(doc_writes, doc_paths, remaining):
    item = _item("R1", "not_met", remaining=remaining)
    (out,) = _run(
        [{"verdict": "fail", "items": [item], "conflicts": []}], doc_writes=doc_writes, doc_paths=doc_paths
    )
    assert out["halted_by"] and out["halted_by"]["halted"] == "ledger-check"
    assert out["commit"] is None  # a halt commits its record only (a93b0ee881)


_PARITY_ITEMS = {
    "met": _item("R1", "met"),
    "drifted": _item("R1", "drifted"),
    "not_met_essential": _item("R1", "not_met"),
    "not_met_non_essential": _item("R1", "not_met", essential=False),
    "not_met_commit_only": _item("R1", "not_met", remaining=["commit"]),
    "not_met_mixed_remaining": _item("R1", "not_met", remaining=["commit", "the test is red"]),
    "not_met_empty_remaining": _item("R1", "not_met", remaining=[]),
    "post_commit_with_acts": _item("R1", "post_commit", post_commit_acts=["commit", "sha_reply"]),
    "post_commit_no_acts": _item("R1", "post_commit"),
    "post_commit_unknown_act": _item("R1", "post_commit", post_commit_acts=["tweet"]),
}


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_composed_halt_matches_the_ledger_judge_whatever_the_checker_verdict_says():
    ledger = Ledger((Item("R1", "text", "ask", ""),), all_of_pm_ask=False)
    names = list(_PARITY_ITEMS)
    cases = [
        {"verdict": verdict, "items": [_PARITY_ITEMS[name]], "conflicts": []}
        for name in names
        for verdict in ("pass", "fail")
    ]
    results = _run(cases)
    mismatches = []
    for n, name in enumerate(names):
        failed = [f["id"] for f in judge(ledger, {"items": [_PARITY_ITEMS[name]]})["failures"]]
        for verdict, out in zip(("pass", "fail"), results[2 * n : 2 * n + 2]):
            halted = out["halted_by"]
            blocked = [i["id"] for i in halted["items"]] if halted else []
            if blocked != failed:
                mismatches.append((name, verdict, blocked, failed))
    assert not mismatches, mismatches


_WITHHELD_ITEMS = {
    "withheld_by_gated": _item("R1", "not_met", withheld_by="C9", remaining=["C9 is gated"]),
    "withheld_by_unknown": _item("R1", "not_met", withheld_by="C7", remaining=["C7 is gated"]),
    "withheld_by_gated_non_essential": _item("R1", "not_met", withheld_by="C9", essential=False),
}


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize("all_of", [False, True])
def test_the_composed_halt_matches_the_ledger_judge_on_a_withheld_row(all_of):
    ledger = Ledger((Item("R1", "text", "ask", ""),), all_of_pm_ask=all_of)
    names = list(_WITHHELD_ITEMS)
    results = _run(
        [{"verdict": "fail", "all_of_pm_ask": all_of, "items": [_WITHHELD_ITEMS[n]], "conflicts": []} for n in names],
        gated=("C9",),
    )
    mismatches = []
    for name, out in zip(names, results):
        failed = [f["id"] for f in judge(ledger, {"items": [_WITHHELD_ITEMS[name]]}, gated={"C9"})["failures"]]
        halted = out["halted_by"]
        blocked = [i["id"] for i in halted["items"]] if halted else []
        if blocked != failed:
            mismatches.append((name, blocked, failed))
    assert not mismatches, mismatches
    assert results[0]["halted_by"] is None and results[1]["halted_by"] is not None


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_withheld_only_ledger_reaches_docs_and_commit_and_hands_withheld_by_on():
    item = _item("R1", "not_met", withheld_by="C9", remaining=["C9 is gated"])
    (out,) = _run(
        [{"verdict": "fail", "all_of_pm_ask": True, "items": [item], "conflicts": []}],
        in_run_commit=False, gated=("C9",),
    )
    assert out["halted_by"] is None, out["halted_by"]
    handed = out["next_action"]["params"]["ledger_verdict"]["items"]
    assert handed[0]["withheld_by"] == "C9"


def test_the_ledger_check_names_the_gated_ids_and_the_schema_carries_withheld_by():
    item = ask_compose._LEDGER_CHECK_SCHEMA["properties"]["items"]["items"]
    assert "withheld_by" in item["properties"]
    script = _compose(prompt=None, sizing_rel="state/sizings/x.yaml", in_run_commit=True)
    assert "withheld_by set to that chunk id" in script and "_manifest?.gated" in script
    judged = _compose(prompt=None, sizing_rel="state/sizings/x.yaml", in_run_commit=True, **_JUDGE_KW)
    assert "withheld_by set to that chunk id" in judged and "_manifest?.gated" in judged


def _judge_result(items: list[dict], **extra) -> dict:
    return {"status": "not_met", "reason": "r", "all_of_pm_ask": True, "items": items, "conflicts": [], **extra}


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_judge_that_carries_a_met_ledger_projects_it_and_reaches_docs_and_commit():
    judge = _judge_result([_item("R1", "met")], status="met", observed=["x"])
    (out,) = _run([judge], judged=True)
    assert out["halted_by"] is None, out["halted_by"]
    assert [i["id"] for i in out["ledger"]["items"]] == ["R1"] and out["ledger"]["all_of_pm_ask"] is True
    assert out["verify_spawns"] == 1 and out["docs"] is not None and "commit" in out["phases"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_verify_agent_that_threw_leaves_the_ledger_itemless_the_criterion_indeterminate_and_the_halt_fires():
    (out,) = _run([{"__throw": True}], judged=True)
    assert out["ledger"] is None and out["verify_spawns"] == 1
    assert out["docs"] is None
    assert out["halted_by"] and out["halted_by"]["halted"] == "ledger-check" and out["halted_by"]["items"] == []
    assert out["next_action"]["kind"] == "revise_and_refire"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_judge_result_without_items_fails_closed_like_an_absent_ledger():
    (out,) = _run([{"status": "met", "observed": ["x"]}], judged=True)
    assert out["ledger"] is None
    assert out["halted_by"] and out["halted_by"]["halted"] == "ledger-check"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_judged_run_applies_the_same_blocking_rule_to_the_folded_ledger():
    (out,) = _run([_judge_result([_item("R1", "drifted")], status="met", observed=["x"])], judged=True)
    assert out["halted_by"] and [i["id"] for i in out["halted_by"]["items"]] == ["R1"]
