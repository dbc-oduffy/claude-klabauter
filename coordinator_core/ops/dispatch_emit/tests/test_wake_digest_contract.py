"""
Tests for coordinator_core.ops.dispatch_emit.wake_digest (AC1, AC13).

AC1: the schema file itself is a valid draft 2020-12 schema; its $defs hold test_result,
row_verification_result, falsifier_result; stage_schema_literal round-trips each one;
the review block's field names match D1's pinned producer names.

AC13: completion_return_js's field table covers exactly the schema's required properties
(recursively), every string leaf is wrapped in _cap with the schema's own maxLength, and
the generated source references only RUNTIME_VARS / stage-result bindings / emitter
literals — never an executor's own reply text.
"""

import json
import subprocess
from pathlib import Path

import jsonschema
import pytest

from coordinator_core.ops.dispatch_emit import wake_digest as wd
from coordinator_core.win_portability import no_console_creationflags


def _schema():
    return wd.load_schema()


def test_schema_is_valid_draft202012():
    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema.get("x-schema-version") == "1.0.0"
    assert all(
        obj.get("additionalProperties") is False
        for obj in _walk_objects(schema)
    )


def _walk_objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            yield node
        for v in node.values():
            yield from _walk_objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_objects(v)


def test_defs_hold_the_three_stage_schemas():
    schema = _schema()
    defs = schema["$defs"]
    assert set(["test_result", "row_verification_result", "falsifier_result"]) <= set(defs)
    for name in ("test_result", "row_verification_result", "falsifier_result"):
        assert json.loads(wd.stage_schema_literal(name)) == defs[name]


def test_stage_schema_literal_unknown_name_raises():
    with pytest.raises(KeyError):
        wd.stage_schema_literal("no_such_stage")


def test_review_block_field_names_match_d1_pinned_names():
    schema = _schema()
    review = schema["properties"]["review"]["properties"]
    assert "fixes_applied" in review
    assert "unresolved" in review
    assert "brief_conformance" in review
    assert "rebuild_decision" in review
    line_anchor = schema["$defs"]["line_anchor"]["properties"]
    assert set(line_anchor) == {"line", "anchor"}
    delivery = review["delivery"]["properties"]
    assert "verdict" in delivery
    assert "product_files" in delivery


def test_load_schema_reads_the_file_exactly_once(monkeypatch):
    wd.load_schema.cache_clear()
    calls = []
    real_read_text = Path.read_text

    def counting_read_text(self, *a, **kw):
        if self == wd._SCHEMA_PATH:
            calls.append(1)
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", counting_read_text)
    wd.load_schema()
    wd.load_schema()
    wd.load_schema()
    assert len(calls) == 1
    wd.load_schema.cache_clear()


# --- AC13: completion_return_js -------------------------------------------------

def _kwargs(**overrides):
    base = dict(
        chunks=["C1", "C2"],
        width={
            "rows": 2,
            "max_concurrent_rows": 2,
            "critical_path_rows": 1,
            "runtime_cap_on_emitting_host": 14,
        },
        plan_path="docs/plans/x.md",
        deliverable_id="dlv-x",
        run_base_sha="a" * 40,
        test_var="_testResult",
        test_absent_status="not_run",
        test_absent_note=None,
        verification_var="_verifications",
        skipped_rows=["C3"],
        falsifier_var="_falsifier",
        review_vars={
            "prep": "_reviewPrep",
            "wave": "_reviewWave",
            "delivery": "_deliveryVerdict",
            "integration": "_reviewIntegration",
        },
        has_commit_request=True,
    )
    base.update(overrides)
    return base


def test_field_table_covers_every_required_schema_path():
    # completion_return_js raises AssertionError internally if its table under-covers
    # the schema's required paths; a clean return is the coverage proof.
    js = wd.completion_return_js(**_kwargs())
    assert "return {" in js


def test_every_string_leaf_is_capped():
    js = wd.completion_return_js(**_kwargs())
    schema = _schema()
    for path in ("halted", "decision_required", "tests.note", "tests.sidecar",
                 "criterion.observation", "criterion.sidecar",
                 "review.integration_sidecar"):
        n = wd._maxlength(schema, path)
        assert f"{n})" in js, f"expected a _cap(..., {n}) for {path!r}"


def test_generated_js_references_no_executor_result_binding():
    js = wd.completion_return_js(**_kwargs())
    forbidden = ("agentReply", "executorReply", ".reply", ".output", "rawText")
    for token in forbidden:
        assert token not in js


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_generated_js_is_syntactically_valid_and_matches_schema(tmp_path):
    js = wd.completion_return_js(**_kwargs())
    harness = tmp_path / "harness.js"
    harness.write_text(
        "let _halted = null, _incompleteChunks = [], _unansweredBriefs = [], _notStarted = [], _planHeld = {}, "
        "_blockedChunks = [], _gateOwed = {};\n"
        "let _testResult = {status:'pass', tests_run:5, tests_failed:0, build_clean:true, "
        "summary:'ok', sidecar_path:'s.md'};\n"
        "let _verifications = [{chunk:'C1', status:'pass'}];\n"
        "let _falsifier = {status:'met', observation:'ok', sidecar_path:'f.md'};\n"
        "let _reviewPrep = {slices:[1,2]};\n"
        "let _reviewWave = {};\n"
        "let _deliveryVerdict = {verdict:'PASS', product_files:1, claims_unbacked:[]};\n"
        "let _reviewIntegration = {fixes_applied:2, em_may_think_differently:[], "
        "unresolved:[], overflow:0, brief_conformance:{items:1,met:1,unmet:0}, "
        "rebuild_decision:null, sidecar_path:'i.md', integration_stem:'stem', slices:2};\n"
        "console.log(JSON.stringify((function(){\n" + js + "\n})()));\n",
        encoding="utf-8",
    )
    node = _find_node()
    if node is None:
        pytest.skip("node unavailable to execute the generated script")
    result = subprocess.run([node, str(harness)], capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert result.returncode == 0, result.stderr
    digest = json.loads(result.stdout)
    assert wd.validate_digest(digest) == []
    # AC (terminal-commit trailer fix): inline_review carries a real stem
    # (from _reviewIntegration.sidecar_path, never a since-retired
    # `integration_stem`/`slices` field on the integration result itself)
    # and a real slice count (from _reviewPrep.slices.length) -- the params
    # dispatch.terminal_commit renders into the Inline-Review trailer.
    inline_review = digest["next_action"]["params"]["inline_review"]
    assert {k: inline_review[k] for k in ("integration_stem", "slices", "fixes")} == {
        "integration_stem": "i", "slices": 2, "fixes": 2,
    }
    # The run record's stage returns ride along for `dispatch.terminal_commit`.
    assert inline_review["delivery"]["verdict"] == "PASS"
    assert inline_review["tests"]["status"] == "pass"
    assert inline_review["criterion"]["status"] == "met"
    assert inline_review["integration"]["sidecar"] == "i.md"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_generated_js_no_review_no_test_halted_path_validates(tmp_path):
    js = wd.completion_return_js(**_kwargs(
        test_var=None,
        falsifier_var=None,
        review_vars=None,
        has_commit_request=False,
        skipped_rows=[],
    ))
    harness = tmp_path / "harness2.js"
    harness.write_text(
        "let _halted = 'stop rule fired', _incompleteChunks = [], "
        "_unansweredBriefs = [], _notStarted = [], _planHeld = {}, _blockedChunks = [], _gateOwed = {};\n"
        "let _verifications = [];\n"
        "console.log(JSON.stringify((function(){\n" + js + "\n})()));\n",
        encoding="utf-8",
    )
    node = _find_node()
    if node is None:
        pytest.skip("node unavailable to execute the generated script")
    result = subprocess.run([node, str(harness)], capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert result.returncode == 0, result.stderr
    digest = json.loads(result.stdout)
    assert digest["outcome"] == "halted"
    assert digest["halted"] == "stop rule fired"
    assert wd.validate_digest(digest) == []


def _find_node():
    import shutil
    return shutil.which("node")


def _run_digest(tmp_path, *, falsifier_js, incomplete=(), blocked=(), **overrides):
    """Evaluate the emitted return expression under node with the given
    `_falsifier` binding (a JS literal) and chunk arrays; skip without node."""
    js = wd.completion_return_js(**_kwargs(
        review_vars=None, has_commit_request=False, skipped_rows=[], **overrides,
    ))
    node = _find_node()
    if node is None:
        pytest.skip("node unavailable to execute the generated script")
    harness = tmp_path / "criterion.js"
    harness.write_text(
        f"let _halted = null, _incompleteChunks = {json.dumps(list(incomplete))}, "
        "_unansweredBriefs = [], _notStarted = [], _planHeld = {}, _stoppedBy = [], "
        f"_blockedChunks = {json.dumps(list(blocked))}, _gateOwed = {{}};\n"
        "let _testResult = {status:'pass', tests_run:1, tests_failed:0, build_clean:true, "
        "summary:'ok', sidecar_path:'s.md'};\n"
        "let _verifications = [];\n"
        f"let _falsifier = {falsifier_js};\n"
        "console.log(JSON.stringify((function(){\n" + js + "\n})()));\n",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(harness)], capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize(
    "falsifier_js, expected, decision",
    [
        # The boolean wins over the agent's own status in both directions.
        ("{status:'not_met', differs_from_baseline:true, observation:'ok'}", "met", None),
        ("{status:'met', differs_from_baseline:false, observation:'ok'}", "not_met", "falsifier not met"),
        # A null result (halted run) is not_run.
        ("null", "not_run", None),
        # Legacy result with no boolean: the agent's status, regex-demoted.
        ("{status:'met', observation:'passes now'}", "met", None),
        ("{status:'met', observation:'baseline still matches'}", "not_met", "falsifier not met"),
        # A run criterion short of met always raises a decision, carrying the judge's reason.
        ("{status:'indeterminate', observation:'could not run'}", "indeterminate", "falsifier indeterminate"),
        (
            "{status:'indeterminate', differs_from_baseline:null, reason:'met demoted: a clause is not met'}",
            "indeterminate",
            "falsifier indeterminate: met demoted: a clause is not met",
        ),
        ("{status:'not_met', reason:'not wired up: op.x'}", "not_met", "falsifier not met: not wired up: op.x"),
    ],
)
def test_criterion_status_is_computed_from_differs_from_baseline(tmp_path, falsifier_js, expected, decision):
    digest = _run_digest(tmp_path, falsifier_js=falsifier_js)
    assert digest["criterion"]["status"] == expected
    assert digest["decision_required"] == decision
    assert wd.validate_digest(digest) == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_null_falsifier_result_keeps_observation_null(tmp_path):
    digest = _run_digest(tmp_path, falsifier_js="null")
    assert digest["criterion"] == {"status": "not_run", "observation": None, "sidecar": None, "reason": None}


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_demoted_criterion_carries_its_reason(tmp_path):
    digest = _run_digest(
        tmp_path,
        falsifier_js="{status:'indeterminate', differs_from_baseline:null, reason:'met demoted: a clause is not met'}",
    )
    assert digest["criterion"]["reason"] == "met demoted: a clause is not met"
    assert wd.validate_digest(digest) == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_blocked_chunks_read_blocked_and_the_rest_of_incomplete_read_partial(tmp_path):
    digest = _run_digest(
        tmp_path,
        falsifier_js="null",
        incomplete=["C1", "C2"],
        blocked=["C2"],
    )
    kinds = {d["chunk"]: d["kind"] for d in digest["deviations"]}
    assert kinds == {"C1": "partial", "C2": "blocked"}
    assert wd.validate_digest(digest) == []


# --- validate_digest --------------------------------------------------------

def _base_digest(**overrides):
    d = {
        "schema": "wake-digest",
        "version": 1,
        "plan": {"path": "docs/plans/x.md", "deliverable_id": "dlv-x"},
        "outcome": "completed",
        "completed": True,
        "halted": None,
        "chunks": ["C1"],
        "criterion": {"status": "met", "observation": "ok", "sidecar": None},
        "tests": {
            "status": "pass", "run": 1, "failed": 0, "build_clean": True,
            "note": None, "sidecar": None,
            "per_row": {"verified": 1, "passed": 1, "failed": [], "unstructured": [], "skipped": []},
        },
        "review": {
            "status": "not_run", "slices": None, "fixes_applied": None,
            "em_may_think_differently": [], "unresolved": [], "overflow": 0,
            "brief_conformance": None, "rebuild_decision": None,
            "delivery": {"verdict": "not_run", "product_files": None, "claims_unbacked": None},
            "integration_sidecar": None,
        },
        "deviations": [],
        "run_base_sha": "a" * 40,
        "width": {
            "rows": 1, "max_concurrent_rows": 1, "critical_path_rows": 1,
            "runtime_cap": "min(16, CPUs-2)", "runtime_cap_on_emitting_host": 14,
        },
        "predispatch": None,
        "decision_required": None,
        "next_action": {"kind": "none", "op": None, "params": None},
    }
    d.update(overrides)
    return d


def test_validate_digest_accepts_completed_digest():
    assert wd.validate_digest(_base_digest()) == []


def test_validate_digest_accepts_halted_digest():
    d = _base_digest(outcome="halted", completed=False, halted="stop rule fired")
    assert wd.validate_digest(d) == []


def test_validate_digest_accepts_failing_digest():
    d = _base_digest(outcome="incomplete", completed=False)
    d["tests"]["status"] = "fail"
    d["tests"]["failed"] = 3
    assert wd.validate_digest(d) == []


def test_validate_digest_rejects_over_cap_string():
    d = _base_digest()
    d["decision_required"] = "x" * 301
    errs = wd.validate_digest(d)
    assert errs


def test_blocked_chunks_is_a_declared_runtime_var_and_drives_the_deviation_kind():
    assert "_blockedChunks" in wd.RUNTIME_VARS
    js = wd.completion_return_js(**_kwargs())
    assert "(_blockedChunks.includes(id) ? 'blocked' : 'partial')" in js


def test_blocked_chunks_is_a_declared_runtime_var_and_drives_the_deviation_kind():
    assert "_blockedChunks" in wd.RUNTIME_VARS
    js = wd.completion_return_js(**_kwargs())
    assert "(_blockedChunks.includes(id) ? 'blocked' : 'partial')" in js


def test_terminal_commit_params_carry_script_path_and_session_id():
    sid = "0f2b6a5e-1c3d-4e5f-8a9b-0c1d2e3f4a5b"
    js = wd.completion_return_js(**_kwargs(script_path="tasks/run/x.mjs", session_id=sid))
    assert 'script_path: "tasks/run/x.mjs"' in js
    assert f'session_id: "{sid}"' in js
    assert "script_path" not in wd.completion_return_js(**_kwargs())


def _digest_for(tmp_path, *, test_js, verifications_js, gate_owed_js="{}"):
    js = wd.completion_return_js(**_kwargs(review_vars=None, has_commit_request=False, skipped_rows=[]))
    node = _find_node()
    if node is None:
        pytest.skip("node unavailable to execute the generated script")
    harness = tmp_path / "skips.js"
    harness.write_text(
        "let _halted = null, _incompleteChunks = [], _unansweredBriefs = [], _notStarted = [], _planHeld = {}, "
        "_stoppedBy = [], _blockedChunks = [], _gateOwed = " + gate_owed_js + ";\n"
        f"let _testResult = {test_js};\n"
        f"let _verifications = {verifications_js};\n"
        "let _falsifier = {status:'met', observation:'ok', sidecar_path:'f.md'};\n"
        "console.log(JSON.stringify((function(){\n" + js + "\n})()));\n",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(harness)], capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_CLEAN = "{status:'pass', tests_run:5, tests_failed:0, build_clean:true, summary:'ok', sidecar_path:'s.md'}"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_run_level_skips_render_pass_with_skips_never_pass(tmp_path):
    skips = ",".join(f"{{test:'t{i}', reason:'could not import flip_evaluator'}}" for i in range(25))
    digest = _digest_for(
        tmp_path,
        test_js="{status:'pass-with-skips', tests_run:5, tests_failed:0, build_clean:true, sidecar_path:'s.md', skipped:[" + skips + "]}",
        verifications_js="[]",
    )
    assert wd.validate_digest(digest) == []
    assert digest["tests"]["status"] == "pass-with-skips"
    assert len(digest["tests"]["skipped"]) == 20
    assert digest["tests"]["skipped"][0] == {"test": "t0", "reason": "could not import flip_evaluator"}


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_bare_pass_that_lists_skips_is_demoted(tmp_path):
    digest = _digest_for(
        tmp_path,
        test_js="{status:'pass', tests_run:5, tests_failed:0, build_clean:true, sidecar_path:'s.md', skipped:[{test:'a', reason:'r'}]}",
        verifications_js="[]",
    )
    assert digest["tests"]["status"] == "pass-with-skips"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_row_level_skips_are_named_and_not_counted_passed_or_failed(tmp_path):
    digest = _digest_for(
        tmp_path,
        test_js=_CLEAN,
        verifications_js=(
            "[{chunk:'C1', status:'pass'}, "
            "{chunk:'C2', status:'pass-with-skips', skipped:[{test:'t::x', reason:'no module'}]}]"
        ),
    )
    assert wd.validate_digest(digest) == []
    assert digest["tests"]["status"] == "pass-with-skips"
    per_row = digest["tests"]["per_row"]
    assert per_row["passed"] == 1 and per_row["failed"] == []
    assert per_row["pass_with_skips"] == [
        {"chunk": "C2", "skipped": [{"test": "t::x", "reason": "no module"}]}
    ]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_clean_pass_is_unchanged(tmp_path):
    digest = _digest_for(
        tmp_path, test_js=_CLEAN, verifications_js="[{chunk:'C1', status:'pass'}]"
    )
    assert wd.validate_digest(digest) == []
    assert digest["tests"]["status"] == "pass"
    assert digest["tests"]["skipped"] == []
    assert digest["tests"]["per_row"]["passed"] == 1
    assert digest["tests"]["per_row"]["pass_with_skips"] == []


_OWED = "{C2: 'gate-blocker: guard-denied: pnpm typecheck denied'}"
_UNBUILT = "{status:'pass', tests_run:5, tests_failed:0, build_clean:false, summary:'ok', sidecar_path:'s.md'}"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_gate_owed_row_is_a_deviation_with_its_anchor_and_blocks_completed(tmp_path):
    digest = _digest_for(tmp_path, test_js=_UNBUILT, verifications_js="[]", gate_owed_js=_OWED)
    assert wd.validate_digest(digest) == []
    assert digest["deviations"] == [
        {"chunk": "C2", "kind": "gate_owed", "anchor": "gate-blocker: guard-denied: pnpm typecheck denied"}
    ]
    assert digest["outcome"] == "incomplete" and digest["completed"] is False
    assert digest["decision_required"] == "owed build gate unverified: C2"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_gate_owed_completes_once_the_terminal_test_reports_build_clean(tmp_path):
    digest = _digest_for(tmp_path, test_js=_CLEAN, verifications_js="[]", gate_owed_js=_OWED)
    assert wd.validate_digest(digest) == []
    assert digest["outcome"] == "completed" and digest["completed"] is True
    assert digest["decision_required"] is None


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_no_owed_gate_leaves_outcome_unchanged(tmp_path):
    digest = _digest_for(tmp_path, test_js=_UNBUILT, verifications_js="[]")
    assert digest["outcome"] == "completed" and digest["deviations"] == []


def test_gate_owed_var_is_appended_last_and_kind_is_in_the_schema():
    assert wd.RUNTIME_VARS[-1] == "_gateOwed" and wd.RUNTIME_VARS[13] == "_runBase"
    assert "gate_owed" in _schema()["properties"]["deviations"]["items"]["properties"]["kind"]["enum"]
