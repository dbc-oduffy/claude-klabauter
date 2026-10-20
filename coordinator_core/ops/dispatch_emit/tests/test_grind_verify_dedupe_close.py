"""A correct dedupe/refute close is not rejected for lacking a diff, and every verify fail leaves its reason."""
import json

from coordinator_core.backlog_grind_assemble import grind_rows
from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_stages
from coordinator_core.ops.dispatch_emit.queue_select import _fold_in_ledger

from .test_grind_compose import _all_fix_batches, _compose, _run, _triage_script

_ROW = "2026-09-02-a-green-check-proves-nothing-until-it-has-been-seen-red"
_DIGEST = "d" * 64


def _verify_call(**kw):
    return grind_stages.compose_verify_agent_call(
        label="v", phase_title="Grind", row_id_js="row.rowId", row_path_js="row.path",
        touched_files_js="row.touchedFiles", evidence_js="row.evidence", fix_plan_js="row.fixPlan",
        profile="lessons", run_id_js="RUN_ID", digest_js="row.digest", **kw,
    )


def test_post_fix_verify_judges_the_covering_artifact_not_a_diff():
    call = _verify_call()
    assert "already covered by a wiki page or doc" in call
    assert "no diff and no commit are not grounds to reject" in call
    assert "covering artifact the evidence cites exists at HEAD" in call


def test_verify_fail_is_recorded_to_the_row_ledger_with_check_expected_and_seen():
    call = _verify_call(pre_fix_js="false")
    assert call.count("--stage verify --verdict fail --outcome fail") == 1
    assert "what it expected and what you saw" in call
    assert "/evidence/" in call and ".verify.md" in call


def test_golden_script_passes_the_row_digest_to_the_verifier():
    script = _compose()
    assert "--stage verify --verdict fail --outcome fail" in script


def test_rejected_after_retry_hands_back_the_verifier_reason():
    reason = "covering page wiki/x.md missing: expected a section on red-first, saw none"
    verdicts = _all_fix_batches(["r0"])
    script_by_kind = {
        "triage": lambda bid: _triage_script(verdicts)(bid, ["r0"]),
        "fix": lambda rid: {"outcome": "done"},
        "verify": lambda rid: {"outcome": "fail", "reason": reason, "undo": {"outcome": "undone"}},
    }
    result, _budget = _run([("b0", ["r0"])], script_by_kind, batch_size=1)
    handed = next(h for h in result["handed_back"] if h["type"] == "rejected-after-retry")
    assert handed["reason"] == f"verify failed twice: {reason}"
    assert "verify failed twice: " in _compose() or "_why" in _compose()


def test_bare_fail_keeps_the_plain_reason():
    assert gc._verify_failed_twice(None) == "verify failed twice"
    assert gc._verify_failed_twice("") == "verify failed twice"


def test_emitted_script_carries_the_reason_into_the_handback():
    script = _compose()
    assert "String(result.reason).slice(0, 400)" in script
    assert "'rejected-after-retry', _why" in script


def test_failed_verify_ledger_line_does_not_skip_verify_on_resume(tmp_path):
    evidence = tmp_path / "state" / "queue-grind" / "lessons" / "evidence" / "r1" / f"{_ROW}.verify.md"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("check: covering page exists; expected wiki/x.md; saw none", encoding="utf-8")
    for stage, outcome in (("triage", "dedupe"), ("verify", "fail")):
        assert grind_rows.main([
            "append", "--profile", "lessons", "--row-id", _ROW, "--digest", _DIGEST,
            "--stage", stage, "--verdict", outcome, "--outcome", outcome,
            "--evidence-file", str(evidence), "--run-stamp", "r1", "--repo-root", str(tmp_path),
        ]) == grind_rows.EXIT_OK
    ledger = tmp_path / "state" / "queue-grind" / "lessons" / f"{_ROW}.jsonl"
    lines = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines()]
    assert [x["stage"] for x in lines] == ["triage", "verify"]
    skip, declined = _fold_in_ledger(_ROW, _DIGEST, {_ROW: lines})
    assert skip == ("triage",) and declined is None
    lines[1]["outcome"] = "pass"
    assert _fold_in_ledger(_ROW, _DIGEST, {_ROW: lines})[0] == ("triage", "verify")


def test_op_mode_verify_appends_one_verify_ledger_line_per_row_pass_or_fail():
    call = grind_stages.compose_verify_op_call(
        label="v", phase_title="Grind", run_dir="rd", op_js="spec.op", batch_id_js="row.batchId",
        row_id_js="row.rowId", row_path_js="row.path", digest_js="row.digest",
        profile="lessons", run_id_js="RUN_ID",
    )
    assert call.count("grind-row append") == 1
    assert "--stage verify --verdict <pass|fail> --outcome <pass|fail>" in call
    assert "/evidence/" in call and ".verify.md" in call
    assert "naming neither" in call


def test_op_mode_verify_without_a_digest_records_nothing():
    call = grind_stages.compose_verify_op_call(label="v", phase_title="Grind", run_dir="rd", op="x", batch_id="b")
    assert "grind-row append" not in call


def test_golden_script_op_verify_records_the_ledger_line():
    assert _compose().count("--stage verify --verdict <pass|fail>") >= 1
