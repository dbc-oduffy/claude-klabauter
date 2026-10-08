"""The pre-dispatch phase and the ``_runRow`` skip clauses of an emitted inventory script.

Pins: the check and review agents precede the first executor registration;
a review prompt carries neither the plan path nor the plan-context preamble;
the ``--plan`` route carries no pre-phase and a ``null`` digest block; an
already-done row is skipped while its dependents run; a routed-out row
routes its dependents out; unusable verdicts fold to still-open.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit import emit, wake_digest
from coordinator_core.ops.dispatch_emit.emit import PlanContext, compose_script, row_block_bytes
from coordinator_core.ops.dispatch_emit.falsifier_integrity_phase import (
    REVIEW_PHASE_TITLE,
    REVIEWER_AGENT_TYPE,
    ReviewInput,
    review_specs,
)
from coordinator_core.ops.dispatch_emit.predispatch import CHECK_PHASE_TITLE, AgentSpec
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.session.record_homes import record_path
from coordinator_core.win_portability import no_console_creationflags

from .conftest import REVIEW_KW

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN = "docs/plans/with-falsifier.md"
PLAN_BODY_MARKER = "Acceptance criteria table: SECRET-PLAN-BODY"


def _row(row_id, writes, deps=(), plan=PLAN):
    return WaveRow(
        id=row_id,
        title=f"title-{row_id}",
        surface="s",
        writes=writes,
        reads=[],
        depends_on=list(deps),
        body=f"Spec: {plan} ({row_id})\nSummary: does {row_id}.\n",
    )


def _review_spec_for(plan=PLAN):
    return review_specs(
        [
            ReviewInput(
                plan_path=plan,
                criterion="the thing holds",
                how="run the check",
                baseline_output="fails today",
                expected_when_true="passes",
                baseline_ref=None,
                report_path=None,
                report_json=None,
            )
        ]
    )


def _compose(waves, *, predispatch=True, specs=None, **kw):
    return compose_script(
        waves,
        name="wf",
        description="inventory",
        plan_path=Path(record_path(".", "mise-inventory", "x.spine.md")).as_posix(),
        predispatch=predispatch,
        review_specs=_review_spec_for() if specs is None else specs,
        **REVIEW_KW,
        **kw,
    )


WAVES = [[_row("A", ["a.py"])], [_row("B", ["b.py"], deps=["A"])]]


def test_pre_phase_precedes_the_first_executor_registration():
    script = _compose(WAVES)
    last_pre = max(
        m.start() for m in re.finditer(r"label: 'check:|label: 'falsifier-integrity:", script)
    )
    assert last_pre < script.index("] = _runRow(")
    assert script.count("label: 'check:") == 2
    assert script.count(f"agentType: '{REVIEWER_AGENT_TYPE}'") == 1
    assert f"phase('{CHECK_PHASE_TITLE}')" in script
    assert REVIEW_PHASE_TITLE in script.split("export const meta", 1)[1].split("};", 1)[0]


def test_composed_pre_phase_script_has_no_contract_errors():
    findings = run_checks(_compose(WAVES))
    assert [f for f in findings if f.severity is Severity.ERROR] == []


def test_review_prompt_names_no_plan_path_and_no_plan_context_preamble():
    context = PlanContext(
        title="Plan title", goal=None, problem_excerpt="PLAN-CONTEXT-PROBLEM-TEXT"
    )
    script = _compose(WAVES, plan_context=context)
    start = script.index("label: 'falsifier-integrity:1'")
    call_start = script.rindex("agent(", 0, start)
    review_call = script[call_start : script.index("catch (e)", start)]
    assert PLAN not in review_call
    assert "PLAN-CONTEXT-PROBLEM-TEXT" not in review_call
    assert "Plan title" not in review_call
    assert "Acceptance" not in review_call and "plan-tasks" not in review_call
    assert PLAN_BODY_MARKER not in script


def test_check_prompts_share_the_already_done_rule_once():
    script = _compose(WAVES)
    assert script.count("A file's existence, a test file's presence") == 1
    assert "{{ALREADY_DONE_RULE}}" not in script


def test_plan_route_compose_has_no_pre_phase_and_a_null_digest_block():
    script = compose_script(
        WAVES, name="wf", description="plan", plan_path="docs/plans/x.md", **REVIEW_KW
    )
    assert CHECK_PHASE_TITLE not in script
    assert REVIEW_PHASE_TITLE not in script
    assert "label: 'check:" not in script
    assert "predispatch: null," in script
    assert "_PRE_SCHEMAS" not in script


def test_review_specs_without_predispatch_is_a_value_error():
    with pytest.raises(ValueError, match="predispatch"):
        compose_script(
            WAVES,
            name="wf",
            description="d",
            plan_path="p.md",
            review_specs=_review_spec_for(),
            **REVIEW_KW,
        )


def test_predispatch_digest_block_and_deviation_kinds_are_rendered():
    script = _compose(WAVES)
    assert "predispatch: { checks_run: 2, already_done: _skippedDone.length" in script
    assert "'already_done'" in script and "'routed_out'" in script
    assert "..._skippedDone" in script and "..._routedOut" in script


def test_completion_return_without_predispatch_references_no_pre_phase_binding():
    js = wake_digest.completion_return_js(
        chunks=["C1"],
        width={"rows": 1, "max_concurrent_rows": 1, "critical_path_rows": 1, "runtime_cap_on_emitting_host": 2},
        plan_path=None,
        deliverable_id=None,
        run_base_sha=None,
        test_var=None,
        test_absent_status="not_run",
        test_absent_note=None,
        verification_var="_verifications",
        skipped_rows=[],
        falsifier_var=None,
        review_vars=None,
        has_commit_request=True,
    )
    for name in ("_skippedDone", "_routedOut", "_reviews", "_unusableChecks"):
        assert name not in js
    assert "predispatch: null," in js


def test_run_row_helper_carries_the_skip_clauses():
    helper = emit._run_row_helper_js()
    assert "const _depResults = await Promise.all(deps);" in helper
    assert "ROUTED-OUT:" in helper and "ALREADY-DONE:" in helper
    assert helper.count("async function _runRow") == 1


def test_row_block_bytes_counts_the_check_thunk_only_when_predispatch():
    rows = [r for wave in WAVES for r in wave]
    plain = row_block_bytes(rows, predispatch=False, plan_path="p.md")
    with_pre = row_block_bytes(rows, predispatch=True, plan_path="p.md")
    assert set(plain) == set(with_pre) == {"A", "B"}
    assert all(with_pre[k] > plain[k] > 0 for k in plain)


_HARNESS = r"""
(async () => {
  const _incompleteChunks = [], _blockedChunks = [], _unansweredBriefs = [], _stoppedBy = [],
    _notStarted = [], _verifications = [];
  let _halted = null;
  const _rowPlan = %(row_plan)s;
  const _haltedPlans = new Set(), _haltedPlanReasons = new Map();
  const _ROW_VERIFY_SCHEMA = {};
  const agent = () => { throw new Error('verify agent must not run'); };
%(helper)s
  %(setup)s
  const calls = [];
  const run = (id) => async () => { calls.push(id); return 'DONE: ' + id; };
  const _rows = {};
  %(rows)s
  const results = {};
  for (const id of Object.keys(_rows)) results[id] = await _rows[id];
  console.log(JSON.stringify({ calls, results, routedOut: _routedOut, skippedDone: _skippedDone }));
})();
"""


def _run_harness(*, row_plan, setup, rows):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = _HARNESS % {
        "row_plan": json.dumps(row_plan),
        "helper": emit._run_row_helper_js(),
        "setup": setup,
        "rows": rows,
    }
    proc = subprocess.run(
        [node, "-e", source],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_dependent_of_a_routed_out_row_routes_out_without_dispatching():
    out = _run_harness(
        row_plan={},
        setup="",
        rows=(
            "_rows.R = Promise.resolve('ROUTED-OUT: upstream');"
            "_rows.X = _runRow('X', [_rows.R], null, run('X'));"
            "_rows.Z = _runRow('Z', [], null, run('Z'));"
        ),
    )
    assert out["calls"] == ["Z"]
    assert out["results"]["X"] == "ROUTED-OUT: a dependency was routed out"
    assert out["routedOut"] == ["X"]


def test_a_falsifier_broken_plan_still_executes():
    """BROKEN is advisory: the row runs; only terminal_commit's implemented stamp is withheld."""
    out = _run_harness(
        row_plan={"R": "docs/plans/broken.md"},
        setup="_falsifierBroken.set('docs/plans/broken.md', ['SCOPE-WIDER-THAN-CLAIM']);",
        rows=(
            "_rows.R = _runRow('R', [], null, run('R'));"
            "_rows.X = _runRow('X', [_rows.R], null, run('X'));"
        ),
    )
    assert out["calls"] == ["R", "X"]
    assert out["routedOut"] == []


def test_a_dependent_of_an_already_done_row_still_dispatches():
    out = _run_harness(
        row_plan={},
        setup="_alreadyDone.add('D');",
        rows=(
            "_rows.D = _runRow('D', [], null, run('D'));"
            "_rows.Y = _runRow('Y', [_rows.D], null, run('Y'));"
        ),
    )
    assert out["calls"] == ["Y"]
    assert out["results"]["D"].startswith("ALREADY-DONE:")
    assert out["skippedDone"] == ["D"]
    assert out["routedOut"] == []


_FOLD_HARNESS = r"""
(async () => {
  const _alreadyDone = new Set(), _falsifierBroken = new Map(), _unusableChecks = [], _reviews = [];
  const _checkIds = %(check_ids)s;
  const _reviewPlans = %(review_plans)s;
  const _preResults = %(results)s;
%(fold)s
  console.log(JSON.stringify({
    done: [..._alreadyDone], unusable: _unusableChecks, reviews: _reviews,
    broken: [..._falsifierBroken.entries()],
  }));
})();
"""


def _run_fold(check_ids, review_plans, results):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = _FOLD_HARNESS % {
        "check_ids": json.dumps(check_ids),
        "review_plans": json.dumps(review_plans),
        "results": json.dumps(results),
        "fold": emit._PRE_PHASE_FOLD_JS,
    }
    proc = subprocess.run(
        [node, "-e", source],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_an_already_done_verdict_without_evidence_folds_to_still_open_and_is_recorded():
    good = {"path": "a.py", "anchor": "f", "excerpt": "def f(): ...", "discharges": "clause 1"}
    out = _run_fold(
        ["empty", "partial", "good", "open", "nullcheck"],
        [],
        [
            {"verdict": "already-done", "evidence": [], "note": ""},
            {"verdict": "already-done", "evidence": [{"path": "a.py", "excerpt": ""}], "note": ""},
            {"verdict": "already-done", "evidence": [good], "note": ""},
            {"verdict": "still-open", "evidence": [], "note": ""},
            None,
        ],
    )
    assert out["done"] == ["good"]
    assert out["unusable"] == ["empty", "partial", "nullcheck"]


def test_broken_review_marks_its_plan_and_unreviewable_or_null_proceeds():
    fired = {"tell": "SCOPE-WIDER-THAN-CLAIM", "status": "FIRED", "note": ""}
    clear = {"tell": "WRONG-DENOMINATOR", "status": "CLEAR", "note": ""}
    out = _run_fold(
        [],
        ["p-broken", "p-sound", "p-unreviewable", "p-null"],
        [
            {"verdict": "BROKEN", "tells": [fired, clear], "contamination": "", "body": ""},
            {"verdict": "SOUND", "tells": [clear], "contamination": "", "body": ""},
            {"verdict": "UNREVIEWABLE", "tells": [], "contamination": "", "body": ""},
            None,
        ],
    )
    assert out["broken"] == [["p-broken", ["SCOPE-WIDER-THAN-CLAIM"]]]
    verdicts = {r["plan"]: r["verdict"] for r in out["reviews"]}
    assert verdicts == {
        "p-broken": "BROKEN",
        "p-sound": "SOUND",
        "p-unreviewable": "UNREVIEWABLE",
        "p-null": "UNREVIEWABLE",
    }


def test_check_agents_run_on_haiku_and_the_phase_precedes_execute():
    script = _compose(WAVES)
    for label in ("check:A", "check:B"):
        call = script[script.index(f"label: '{label}'") :].split("schema:", 1)[0]
        assert "model: 'haiku'" in call
    assert script.index(f"phase('{CHECK_PHASE_TITLE}')") < script.index("phase('Execute')")


def test_cached_review_spec_is_a_literal_thunk_not_an_agent_call_and_flows_through_the_fold():
    cached = ReviewInput(
        plan_path=PLAN, criterion="c", how="h", baseline_output=None, expected_when_true="e",
        baseline_ref=None, report_path=None, report_json=None,
        falsifier_sha="f", plan_sha="p", cache_path="state/mi/.can-report-red/x.verdict.json",
        cached={"verdict": "BROKEN", "tells": ["WRONG-DENOMINATOR"]},
    )
    script = _compose(WAVES, specs=review_specs([cached]))
    assert "label: 'falsifier-integrity:" not in script
    assert "async () => ({\"verdict\": \"BROKEN\"" in script
    assert "cached_result" not in script.split("_PRE_SCHEMAS = ", 1)[1].split("\n", 1)[0]
    assert "_verdictCacheWrites" not in script
    out = _run_fold(
        [], [PLAN],
        [{"verdict": "BROKEN", "tells": [{"tell": "WRONG-DENOMINATOR", "status": "FIRED"}]}],
    )
    assert out["broken"] == [[PLAN, ["WRONG-DENOMINATOR"]]]


def test_fresh_review_spec_persists_its_verdict_and_the_script_awaits_the_write():
    fresh = ReviewInput(
        plan_path=PLAN, criterion="c", how="h", baseline_output=None, expected_when_true="e",
        baseline_ref=None, report_path=None, report_json=None,
        falsifier_sha="fsha", plan_sha="psha", cache_path="state/mi/.can-report-red/x.verdict.json",
    )
    script = _compose(WAVES, specs=review_specs([fresh]), repo_root=Path("/repo"))
    assert "label: 'falsifier-integrity:1'" in script
    assert "/repo/state/mi/.can-report-red/x.verdict.json" in script
    assert "falsifier_sha: 'fsha'" in script and "plan_sha: 'psha'" in script
    assert "label: 'verdict-cache'" in script
    assert script.index("_verdictCacheWrites = []") < script.index("] = _runRow(")
    assert "await Promise.all(_verdictCacheWrites);" in script
    assert [f for f in run_checks(script) if f.severity is Severity.ERROR] == []
