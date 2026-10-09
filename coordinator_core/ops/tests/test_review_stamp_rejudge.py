"""review-stamp rejudge: an unmet stamped criterion closes only through a bound, fresh judge sidecar."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from coordinator_core.ops import review_stamp as m
from coordinator_core.session import record_homes
from coordinator_core.ops.plan_status_transition import main
from coordinator_core.ops.tests.test_plan_status_transition import _write
from coordinator_core.ops.tests.test_plan_status_transition_goal_refusal import _init_repo
from coordinator_core.ops.tests.test_plan_status_transition_records_exit_criterion import _FLAGS, _seed

PLAN_ID = "pln-fixture-spineless-000001"
STAMPED_AT = "2026-10-01T00:00:00Z"


def _plan(tmp_path: Path, status: str) -> Path:
    body = (
        f"---\ntitle: T\nstatus: executing\nplan_id: {PLAN_ID}\nreview_stamp:\n"
        "  terminal_commit_sha: abc\n  terminal_tree_sha: def\n"
        f"  criterion:\n    status: {status}\n    observation: o\n    sidecar: null\n"
        f"  stamped_at: '{STAMPED_AT}'\n---\n\nBody.\n"
    )
    return _write(tmp_path, "p.md", body)


def _seeded(tmp_path: Path, status: str) -> Path:
    """A spineless plan with a prime criterion (so the falsifier flags can record) and a stamp."""
    _init_repo(tmp_path)
    plan = _seed(tmp_path)
    stamp = (
        "review_stamp:\n  terminal_commit_sha: abc\n  terminal_tree_sha: def\n"
        f"  criterion:\n    status: {status}\n    observation: o\n    sidecar: null\n"
        f"  stamped_at: '{STAMPED_AT}'\n"
    )
    text = plan.read_text(encoding="utf-8").replace("status: approved\n", "status: approved\n" + stamp, 1)
    plan.write_text(text, encoding="utf-8")
    return plan


def _judge(tmp_path: Path, status: str, *, plan: str = PLAN_ID, stale: bool = False) -> Path:
    """An engine-shaped `criterion-rejudge` record, as `reverify_delivery record` writes it."""
    recorded = "2026-09-30T00:00:00.000000Z" if stale else "2026-10-02T00:00:00.000000Z"
    name = "stale" if stale else plan
    path = Path(record_homes.home_dir(str(tmp_path), "delivery-verdicts")) / "2026-10" / f"{name}.rejudge.{status}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = {"kind": "criterion-rejudge", "plan_id": plan, "head_sha": "abc", "recorded_at": recorded,
          "criterion": {"status": status, "observation": "judged", "sidecar": None}}
    path.write_text("---\n" + yaml.safe_dump(fm) + "---\n", encoding="utf-8")
    return path


def _criterion(plan: Path) -> dict:
    text = plan.read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---\n")[1])["review_stamp"]["criterion"]


def test_not_met_stamp_with_em_asserted_pass_is_refused_and_names_the_route(tmp_path, capsys):
    p = _seeded(tmp_path, "not_met")
    rc = main(["stamp-implemented", "--plan", str(p), *_FLAGS])
    assert rc != 0
    err = capsys.readouterr().err
    assert "review_stamp criterion is not_met" in err
    assert "--rejudge" in err and "review-stamp.py rejudge --plan" in err
    assert "status: approved" in p.read_text(encoding="utf-8")
    assert "exit_criterion_met" not in p.read_text(encoding="utf-8")


def test_bound_met_judge_sidecar_lets_stamp_implemented_close(tmp_path):
    p = _seeded(tmp_path, "indeterminate")
    judge = _judge(tmp_path, "met")
    result = m.rejudge(p, tmp_path)
    assert result["status"] == "rejudged"
    crit = _criterion(p)
    assert crit["status"] == "met" and crit["prior_status"] == "indeterminate"
    assert crit["sidecar"] == judge.relative_to(tmp_path).as_posix()
    assert crit["judged_at"] > STAMPED_AT
    assert main(["stamp-implemented", "--plan", str(p), *_FLAGS]) in (0, 2)
    assert "status: implemented" in p.read_text(encoding="utf-8")


def test_not_met_judge_sidecar_records_nothing(tmp_path):
    p = _plan(tmp_path, "not_met")
    _judge(tmp_path, "not_met")
    before = p.read_text(encoding="utf-8")
    result = m.rejudge(p, tmp_path)
    assert result["status"] == "unchanged" and "not_met" in result["reason"]
    assert p.read_text(encoding="utf-8") == before


def test_stale_or_foreign_judge_sidecars_are_ignored(tmp_path):
    p = _plan(tmp_path, "not_met")
    _judge(tmp_path, "met", stale=True)
    _judge(tmp_path, "met", plan="pln-other-000000")
    before = p.read_text(encoding="utf-8")
    assert m.rejudge(p, tmp_path)["status"] == "unchanged"
    assert p.read_text(encoding="utf-8") == before


def test_met_stamp_keeps_the_falsifier_flag_path(tmp_path):
    p = _plan(tmp_path, "met")
    assert m.criterion_refusal(p, p.read_text(encoding="utf-8")) is None
    assert m.rejudge(p, tmp_path)["reason"] == "criterion already met"


def test_auto_flip_accepts_a_rejudged_met(tmp_path):
    from coordinator_core.ops.dispatch_emit.terminal_commit import _stamp_plan_implemented

    p = _seeded(tmp_path, "not_met")
    _judge(tmp_path, "met")
    m.rejudge(p, tmp_path)
    reply = _stamp_plan_implemented(tmp_path, "plan.md", "abc1234")
    assert reply["plan_status"] == "implemented", reply


def test_a_judge_sidecar_in_the_share_dir_is_not_a_source(tmp_path):
    p = _seeded(tmp_path, "not_met")
    share = tmp_path / ".coordinator-local" / "subagent-share" / "sess"
    share.mkdir(parents=True)
    (share / "judge.md").write_text(
        f"---\nagent_type: coordinator:exit-criterion-judge\nstatus: met\ntarget_plan: {PLAN_ID}\n---\n",
        encoding="utf-8",
    )
    assert m.rejudge(p, tmp_path)["status"] == "unchanged"


def test_task_output_recorded_by_reverify_record_clears_the_stamp(tmp_path, capsys):
    from coordinator_core.ops.dispatch_emit import reverify_delivery as rd

    p = _seeded(tmp_path, "not_met")
    payload = {"result": json.dumps({
        "reverify_delivery": {"rejudge": True, "plan_id": PLAN_ID, "head_sha": "abc", "plan_path": str(p)},
        "criterion": {"status": "met", "observation": "now true"},
    })}
    assert rd.main(["record", "--result-json", json.dumps(payload), "--repo-root", str(tmp_path)]) == 0
    capsys.readouterr()
    assert m.rejudge(p, tmp_path)["status"] == "rejudged"
    assert _criterion(p)["observation"] == "now true"


def test_emit_rejudge_fires_only_the_criterion_judge(tmp_path):
    from coordinator_core.ops.dispatch_emit.reverify_delivery import emit_rejudge

    p = _seeded(tmp_path, "not_met")
    out = tmp_path / "x.workflow.mjs"
    emit_rejudge(repo_root=tmp_path, plan_path=str(p), out_path=str(out))
    script = out.read_text(encoding="utf-8")
    assert script.count("agent(") == 1 and "exit-criterion-judge" in script and '"rejudge": true' in script
