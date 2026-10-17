"""`--rejudge --as-agent-brief` and `reverify-delivery record --judge-json`: the Agent-tool route
to the criterion judge matches the Workflow route's prompt, schema, ident and record."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import reverify_delivery as rd
from coordinator_core.ops.review_mint.execute_review import _prompt_literal
from coordinator_core.ops.tests.test_rejudge_hand_worked_plan import _case_one

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_KEYS = {"agent_type", "model", "prompt", "schema", "ident"}


def _brief(repo: Path, plan: Path, capsys) -> dict:
    rc = cli_module.main(["--repo-root", str(repo), "--plan", str(plan), "--rejudge", "--as-agent-brief"])
    captured = capsys.readouterr()
    assert rc == cli_module.EXIT_OK, captured.err
    return json.loads(captured.out)


def test_brief_prints_the_five_keys_and_fires_nothing(tmp_path, capsys):
    repo, plan = _case_one(tmp_path)
    before = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*") if ".git" not in p.parts)
    brief = _brief(repo, plan, capsys)
    after = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*") if ".git" not in p.parts)
    assert set(brief) == _KEYS
    assert brief["ident"]["rejudge"] is True
    assert before == after


def test_brief_prompt_schema_and_ident_match_the_workflow_script(tmp_path, capsys):
    repo, plan = _case_one(tmp_path)
    brief = _brief(repo, plan, capsys)
    out = repo / "judge.workflow.mjs"
    rd.emit_rejudge(repo_root=repo, plan_path=str(plan), out_path=str(out))
    script = out.read_text(encoding="utf-8")
    assert _prompt_literal(brief["prompt"]) in script
    assert json.dumps(brief["schema"], sort_keys=True) in script
    assert json.dumps(brief["ident"], sort_keys=True) in script
    assert brief["agent_type"] in script


def test_as_agent_brief_without_rejudge_is_refused_in_one_line(tmp_path, capsys):
    repo, plan = _case_one(tmp_path)
    rc = cli_module.main(["--repo-root", str(repo), "--plan", str(plan), "--as-agent-brief"])
    captured = capsys.readouterr()
    assert rc == cli_module.EXIT_USAGE
    assert captured.out == ""
    assert "--as-agent-brief needs --rejudge" in captured.err
    assert len(captured.err.strip().splitlines()) == 1


def _record_fm(repo: Path) -> dict:
    records = list((repo / "state" / "delivery-verdicts").glob("*/*.md"))
    assert len(records) == 1
    return yaml.safe_load(records[0].read_text(encoding="utf-8").split("---\n")[1])


def _judged() -> dict:
    return {"status": "met", "observation": "the criterion holds at HEAD"}


def test_judge_json_record_matches_the_workflow_routes_record(tmp_path, capsys):
    (tmp_path / "agent").mkdir()
    (tmp_path / "workflow").mkdir()
    repo, plan = _case_one(tmp_path / "agent")
    brief = _brief(repo, plan, capsys)
    judge_f = tmp_path / "judge.json"
    ident_f = tmp_path / "ident.json"
    judge_f.write_text(json.dumps(_judged()), encoding="utf-8")
    ident_f.write_text(json.dumps(brief["ident"]), encoding="utf-8")
    assert rd.main(
        ["record", "--judge-json", str(judge_f), "--ident", str(ident_f), "--repo-root", str(repo)]
    ) == 0
    agent_fm = _record_fm(repo)
    capsys.readouterr()

    wrepo, wplan = _case_one(tmp_path / "workflow")
    out = wrepo / "judge.workflow.mjs"
    rd.emit_rejudge(repo_root=wrepo, plan_path=str(wplan), out_path=str(out))
    ident = _brief(wrepo, wplan, capsys)["ident"]
    payload = json.dumps({"result": json.dumps({"reverify_delivery": ident, "criterion": _judged()})})
    assert rd.main(["record", "--result-json", payload, "--repo-root", str(wrepo)]) == 0
    workflow_fm = _record_fm(wrepo)

    volatile = {"recorded_at", "head_sha", "emitted_head_sha", "run_base_sha"}
    assert list(agent_fm) == list(workflow_fm)
    assert {k: v for k, v in agent_fm.items() if k not in volatile} == {
        k: v for k, v in workflow_fm.items() if k not in volatile
    }
    assert agent_fm["kind"] == rd.REJUDGE_KIND
    assert agent_fm["plan_id"] == brief["ident"]["plan_id"]


def test_judge_json_accepts_the_whole_brief_as_ident_and_needs_no_run_record(tmp_path, capsys):
    repo, plan = _case_one(tmp_path)
    brief = _brief(repo, plan, capsys)
    assert rd.main([
        "record", "--judge-json", json.dumps(_judged()), "--ident", json.dumps(brief),
        "--repo-root", str(repo),
    ]) == 0
    assert _record_fm(repo)["criterion"]["status"] == "met"


def test_judge_json_without_ident_is_refused(tmp_path, capsys):
    repo, _plan = _case_one(tmp_path)
    rc = rd.main(["record", "--judge-json", json.dumps(_judged()), "--repo-root", str(repo)])
    assert rc != 0
    assert not (repo / "state" / "delivery-verdicts").exists()
