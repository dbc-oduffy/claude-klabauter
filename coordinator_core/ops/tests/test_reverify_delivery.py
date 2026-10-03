"""reverify_delivery: the one-stage script, the append-only superseding record, and mint's read of it."""

from __future__ import annotations

import pytest

from coordinator_core.ops import review_stamp as m
from coordinator_core.ops.dispatch_emit import reverify_delivery as rd
from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _v5_fragment
from coordinator_core.ops.tests.test_review_stamp_superseding_record import (
    _git,
    _plan,
    _record,
    _repo,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_CLAIMS = [{"claim": "adds the frobnicator", "anchor": "no frob.py in diff"}]
_FAIL = {"verdict": "FAIL", "product_files": 1, "unbacked": _CLAIMS}


def _fail_record(tmp_path):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    record = _record(repo, head, delivery=_FAIL)
    return repo, record, record.relative_to(repo).as_posix()


def _supersede(repo, rel, verdict, unbacked=()):
    return rd.record_delivery_verdict(
        repo_root=repo, supersedes=rel, plan_id="pln-example-abc123",
        head_sha="h" * 40, verdict=verdict, unbacked=list(unbacked),
    )


def test_latest_supersession_wins_and_original_is_untouched(tmp_path):
    repo, record, rel = _fail_record(tmp_path)
    before = record.read_bytes()
    assert rd.latest_delivery_supersession(repo, rel) is None
    _supersede(repo, rel, "FAIL", _CLAIMS)
    _supersede(repo, rel, "PASS")
    assert rd.latest_delivery_supersession(repo, rel)["verdict"] == "PASS"
    assert rd.latest_delivery_supersession(repo, "other.md") is None
    assert record.read_bytes() == before


def test_mint_accepts_a_superseding_pass(tmp_path):
    repo, record, rel = _fail_record(tmp_path)
    with pytest.raises(m.MintRefusal, match="'FAIL'"):
        m.mint(_plan(repo), repo, build_test_path=None, superseding_record=record)
    _supersede(repo, rel, "PASS")
    stamp = m.mint(_plan(repo), repo, build_test_path=None, superseding_record=record)
    assert stamp["delivery"]["verdict"] == "PASS"


def test_mint_still_refuses_a_superseding_fail_and_lists_claims(tmp_path):
    repo, record, rel = _fail_record(tmp_path)
    _supersede(repo, rel, "FAIL", [{"claim": "still missing", "anchor": "nothing"}])
    with pytest.raises(m.MintRefusal) as exc:
        m.mint(_plan(repo), repo, build_test_path=None, superseding_record=record)
    assert "still missing [lacked: nothing]" in str(exc.value)
    assert "frobnicator" not in str(exc.value)


def test_script_has_one_delivery_verifier_call_naming_prior_claims(tmp_path):
    script = rd.compose_reverify_script(
        fragment=_v5_fragment(), stage_schemas=_STAGE_SCHEMAS, plan_path="docs/plans/example.md",
        run_record_rel="state/x/rec.md", plan_id="pln-1", run_base_sha="a" * 40,
        head_sha="b" * 40, claims=_CLAIMS,
    )
    assert script.count("agent(") == 1
    assert "delivery-verifier" in script
    assert "adds the frobnicator" in script and "no frob.py in diff" in script
    assert "b" * 40 in script and "state/x/rec.md" in script
    assert "frozen diff" in script


def test_prior_claims_refuses_a_non_fail_record(tmp_path):
    repo = _repo(tmp_path)
    record = _record(repo, _git(repo, "rev-parse", "HEAD"))
    with pytest.raises(rd.ReverifyRefused):
        rd.prior_unbacked_claims(record)


def test_a_task_output_resolves_to_the_plans_bookkeeping_record_and_emits_a_receipt(tmp_path, monkeypatch):
    """example-stats-repo passed the task .output; the plan's review-wave bookkeeping is the record,
    and the emitted script carries the receipt the foreign-emission hook demands."""
    repo, record, _ = _fail_record(tmp_path)
    share = repo / ".coordinator-local" / "subagent-share" / "sid-1"
    share.mkdir(parents=True)
    bookkeeping = share / "pln-example-abc123.review-wave-bookkeeping.md"
    bookkeeping.write_bytes(record.read_bytes())
    plan = repo / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("---\nplan_id: pln-example-abc123\nstatus: executing\n---\nbody\n", encoding="utf-8")
    task_output = tmp_path / "task.output"
    task_output.write_text("not a run record\n", encoding="utf-8")
    monkeypatch.setattr(rd, "_head_sha", lambda root: "b" * 40)
    out = repo / "docs" / "plans" / "p.reverify.workflow.mjs"
    result = rd.emit_reverify(
        repo_root=repo, plan_path=str(plan), run_record=str(task_output), out_path=str(out)
    )
    assert result["supersedes"].endswith("pln-example-abc123.review-wave-bookkeeping.md")
    assert result["receipt"] and (out.parent / (out.name + ".emitted.json")).is_file()


_NOT_MET = {"status": "not_met", "observation": "old", "sidecar": None}


def _fail_not_met(tmp_path):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    record = _record(repo, head, delivery=_FAIL, criterion=_NOT_MET)
    return repo, record, record.relative_to(repo).as_posix()


def _supersede_with(repo, rel, verdict, criterion):
    return rd.record_delivery_verdict(
        repo_root=repo, supersedes=rel, plan_id="pln-example-abc123",
        head_sha="h" * 40, verdict=verdict, unbacked=[], criterion=criterion,
    )


def test_reverify_pass_and_met_clears_a_frozen_not_met_criterion(tmp_path):
    repo, record, rel = _fail_not_met(tmp_path)
    with pytest.raises(m.MintRefusal):
        m.mint(_plan(repo), repo, build_test_path=None, superseding_record=record)
    _supersede_with(repo, rel, "PASS", {"status": "met", "observation": "ran it"})
    stamp = m.mint(_plan(repo), repo, build_test_path=None, superseding_record=record)
    assert stamp["criterion"]["status"] == "met"


def test_reverify_not_met_criterion_still_refuses(tmp_path):
    repo, record, rel = _fail_not_met(tmp_path)
    _supersede_with(repo, rel, "PASS", {"status": "not_met", "observation": "still"})
    with pytest.raises(m.MintRefusal, match="not_met"):
        m.mint(_plan(repo), repo, build_test_path=None, superseding_record=record)


def test_old_shape_record_keeps_the_frozen_criterion(tmp_path):
    repo, record, rel = _fail_not_met(tmp_path)
    _supersede(repo, rel, "PASS")
    assert rd.latest_criterion_supersession(repo, rel) is None
    with pytest.raises(m.MintRefusal, match="not_met"):
        m.mint(_plan(repo), repo, build_test_path=None, superseding_record=record)


def test_script_runs_the_criterion_judge_and_returns_it():
    fragment = _v5_fragment()
    fragment["execute_review"]["stages"].append(
        {"kind": "judge", "agents": [{"agentType": "coordinator:criterion-judge", "model": "opus",
                                      "effort": "low", "schema": "judge-result"}]}
    )
    schemas = {**_STAGE_SCHEMAS, "judge-result": {"type": "object", "properties": {"status": {"type": "string"}},
                                                  "required": ["status"]}}
    script = rd.compose_reverify_script(
        fragment=fragment, stage_schemas=schemas, plan_path="p.md",
        run_record_rel="r.md", plan_id="pln-1", run_base_sha="a" * 40,
        head_sha="b" * 40, claims=_CLAIMS,
    )
    assert script.count("agent(") == 2
    assert "criterion: _judge" in script


def test_record_cli_accepts_bare_and_wrapped_results_and_rejects_others(tmp_path, capsys):
    import json

    repo, _, rel = _fail_not_met(tmp_path)
    bare = {
        "reverify_delivery": {"plan_id": "pln-example-abc123", "head_sha": "h" * 40},
        "verdict": "PASS", "claims_unbacked": [],
        "criterion": {"status": "met", "observation": "ok"},
    }
    argv = ["record", "--run-record", rel, "--repo-root", str(repo), "--result-json"]
    assert rd.main(argv + [json.dumps(bare)]) == 0
    assert rd.main(argv + [json.dumps({"result": bare, "status": "completed"})]) == 0
    assert rd.main(argv + [json.dumps({"output": json.dumps(bare)})]) == 0
    assert rd.main(argv + [json.dumps({"nothing": 1})]) == 1
    assert rd.latest_criterion_supersession(repo, rel)["status"] == "met"
