"""review-stamp rejudge: an unmet stamped criterion closes only through a bound, fresh judge sidecar."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import review_stamp as m
from coordinator_core.session import record_homes
from coordinator_core.ops.plan_status_transition import main
from coordinator_core.ops.tests.test_plan_status_transition import _write
from coordinator_core.ops.tests.test_plan_status_transition_goal_refusal import _init_repo
from coordinator_core.ops.tests.test_plan_status_transition_records_exit_criterion import _FLAGS, _seed

# _plan/_seeded build real git repos; needs a real process.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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
    assert crit["status"] == "met"
    assert set(crit) == {"status", "observation", "sidecar"}  # plan.schema.json closes this block
    assert crit["sidecar"] == judge.relative_to(tmp_path).as_posix()
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


def test_rejudge_record_stamps_head_at_record_time(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import reverify_delivery as rd

    monkeypatch.setattr(rd, "_head_sha", lambda _root: "later")
    rel = rd.record_criterion_rejudge(
        repo_root=tmp_path, plan_id=PLAN_ID, head_sha="emitted", criterion={"status": "met", "observation": "o"}
    )
    text = (tmp_path / rel).read_text(encoding="utf-8")
    assert "head_sha: later" in text and "emitted_head_sha: emitted" in text


def test_emit_rejudge_fires_only_the_criterion_judge(tmp_path):
    from coordinator_core.ops.dispatch_emit.reverify_delivery import emit_rejudge

    p = _seeded(tmp_path, "not_met")
    out = tmp_path / "x.workflow.mjs"
    emit_rejudge(repo_root=tmp_path, plan_path=str(p), out_path=str(out))
    script = out.read_text(encoding="utf-8")
    assert script.count("agent(") == 1 and "exit-criterion-judge" in script and '"rejudge": true' in script


from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_register_cascade import (  # noqa: E402
    _SIZING,
    _row as _cascade_row,
    _sizing_text,
)

_ROW = {"id": "r1", "claim": "this-plan", "status": "met", "wired": True, "surface": "ui", "observed_ref": "x.py::f"}


def _register_bearing(tmp_path: Path, rows) -> Path:
    """A not_met stamped plan claiming register row r1, and a met rejudge record carrying `rows`."""
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / _SIZING).write_text(_sizing_text([_cascade_row("r1", claimed=False)]), encoding="utf-8")
    body = (
        f"---\ntitle: T\nstatus: executing\nplan_id: {PLAN_ID}\nsizing_object: {_SIZING}\n"
        f"register_claims:\n  sizing: {_SIZING}\n  rows: [r1]\nreview_stamp:\n"
        "  terminal_commit_sha: abc\n  terminal_tree_sha: def\n"
        "  criterion:\n    status: not_met\n    observation: o\n    sidecar: null\n"
        f"  stamped_at: '{STAMPED_AT}'\n---\n\nBody.\n"
    )
    plan = _write(tmp_path, "p.md", body)
    record = _judge(tmp_path, "met")
    fm = yaml.safe_load(record.read_text(encoding="utf-8").split("---\n")[1])
    if rows is not None:
        fm["criterion"]["register_rows"] = rows
    record.write_text("---\n" + yaml.safe_dump(fm) + "---\n", encoding="utf-8")
    return plan


def test_met_record_without_row_verdicts_does_not_close_a_register_bearing_plan(tmp_path):
    p = _register_bearing(tmp_path, None)
    before = p.read_text(encoding="utf-8")
    result = m.rejudge(p, tmp_path)
    assert result["status"] == "unchanged" and "register row r1" in result["reason"] and "not judged" in result["reason"]
    assert p.read_text(encoding="utf-8") == before


def test_met_record_with_passing_row_verdicts_closes_and_writes_the_rows_back(tmp_path):
    p = _register_bearing(tmp_path, [_ROW])
    result = m.rejudge(p, tmp_path)
    assert result["status"] == "rejudged", result
    assert _criterion(p)["status"] == "met"
    assert result.get("register_sizing") == _SIZING
    row = yaml.safe_load((tmp_path / _SIZING).read_text(encoding="utf-8"))["requirement_register"]["rows"][0]
    assert row["status"] == "met"


_SPINE = """```yaml plan-tasks
- id: A1
  title: a
  change_kind: code-edit
  surface: a.py
  writes: [a.py]
  disposition: {a1}
- id: A2
  title: b
  change_kind: code-edit
  surface: b.py
  writes: [b.py]
  disposition: {a2}
{extra}```
"""


def _hand_worked(tmp_path: Path, *, a2: str = "coded", extra: str = "") -> tuple:
    """A tmp repo with a stampless plan whose rows are coded and a committed HEAD; returns (plan, head)."""
    from coordinator_core.ops.tests.test_plan_status_transition_goal_refusal import _head_sha, _run_git

    _init_repo(tmp_path)
    plan = tmp_path / "p.md"
    plan.write_text(
        f"---\ntitle: T\nstatus: executing\nplan_id: {PLAN_ID}\n---\n\n## Tasks\n\n"
        + _SPINE.format(a1="coded", a2=a2, extra=extra),
        encoding="utf-8",
    )
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    _run_git(["add", "."], tmp_path)
    _run_git(["commit", "-q", "-m", "hand work"], tmp_path)
    return plan, _head_sha(tmp_path)


def _record(
    tmp_path: Path, head: str, status: str = "met", observation: str = "judged", name: str = "r", base="base1"
) -> Path:
    path = Path(record_homes.home_dir(str(tmp_path), "delivery-verdicts")) / "2026-10" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = {"kind": "criterion-rejudge", "plan_id": PLAN_ID, "head_sha": head, "run_base_sha": base,
          "recorded_at": "2026-10-02T00:00:00.000000Z",
          "criterion": {"status": status, "observation": observation, "sidecar": None}}
    path.write_text("---\n" + yaml.safe_dump(fm) + "---\n", encoding="utf-8")
    return path


def _mint(plan: Path, root: Path, **kw):
    return m.mint(plan, root, build_test_path=kw.pop("build_test_path", None), **kw)


def test_judge_only_mint_writes_the_pinned_stamp_and_check_accepts_it(tmp_path):
    import json

    plan, head = _hand_worked(tmp_path)
    rec = _record(tmp_path, head)
    stamp = _mint(plan, tmp_path)
    rel = rec.relative_to(tmp_path).as_posix()
    assert stamp["terminal_commit_sha"] == head and stamp["integration_sidecar"] == rel
    assert stamp["run_base_sha"] == "base1"
    assert stamp["criterion"] == {"status": "met", "observation": "judged", "sidecar": rel}
    assert "delivery" not in stamp and "register" not in stamp
    assert stamp["slices"] == 0 and stamp["fixes_applied"] is None and stamp["unresolved"] == []
    assert stamp["brief_conformance"] == {"items": None, "met": None, "unmet": None}
    assert stamp["build_test"] == {"verdict": "not_run", "ran": None, "failed": None, "sidecar": None}
    schema_path = Path(m.__file__).resolve().parents[1] / "frontmatter" / "schemas" / "plan.schema.json"
    full = json.loads(schema_path.read_text(encoding="utf-8"))
    sub = {**full["properties"]["review_stamp"], "$defs": full.get("$defs", {})}
    import jsonschema

    jsonschema.validate(yaml.safe_load(plan.read_text(encoding="utf-8").split("---\n")[1])["review_stamp"], sub)
    assert m.check(plan, tmp_path, supersession=True) is None


@pytest.mark.parametrize(
    "status,observation", [("not_met", "judged"), ("indeterminate", "judged"), ("met", "  ")]
)
def test_judge_only_mint_refuses_an_unmet_or_unobserved_record(tmp_path, status, observation):
    plan, head = _hand_worked(tmp_path)
    _record(tmp_path, head, status, observation)
    before = plan.read_bytes()
    with pytest.raises(m.MintRefusal):
        _mint(plan, tmp_path)
    assert plan.read_bytes() == before


def test_judge_only_mint_omits_a_null_run_base(tmp_path):
    plan, head = _hand_worked(tmp_path)
    _record(tmp_path, head, base=None)
    assert "run_base_sha" not in _mint(plan, tmp_path)


def test_judge_only_mint_refuses_a_head_that_is_not_an_ancestor(tmp_path):
    plan, _ = _hand_worked(tmp_path)
    _record(tmp_path, "0" * 40)
    before = plan.read_bytes()
    with pytest.raises(m.MintRefusal, match="not HEAD or an ancestor"):
        _mint(plan, tmp_path)
    assert plan.read_bytes() == before


def test_judge_only_mint_refuses_an_open_row(tmp_path):
    plan, head = _hand_worked(tmp_path, a2="open")
    _record(tmp_path, head)
    before = plan.read_bytes()
    with pytest.raises(m.MintRefusal, match="A2"):
        _mint(plan, tmp_path)
    assert plan.read_bytes() == before


def test_judge_only_mint_admits_a_deferred_open_row(tmp_path):
    plan, head = _hand_worked(tmp_path, a2="open")
    plan.write_text(plan.read_text(encoding="utf-8").replace("  disposition: open", "  disposition: open\n  deferred: true"), encoding="utf-8")
    _record(tmp_path, head)
    assert _mint(plan, tmp_path)["criterion"]["status"] == "met"


def test_judge_only_mint_refuses_a_register_gap(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import terminal_commit

    plan, head = _hand_worked(tmp_path)
    _record(tmp_path, head)
    monkeypatch.setattr(terminal_commit, "_register_gate", lambda *_a: "register row r1 not judged")
    before = plan.read_bytes()
    with pytest.raises(m.MintRefusal, match="register row r1"):
        _mint(plan, tmp_path)
    assert plan.read_bytes() == before


def test_judge_only_mint_without_a_record_keeps_the_old_refusal_and_names_the_route(tmp_path):
    plan, _ = _hand_worked(tmp_path)
    with pytest.raises(m.MintRefusal) as exc:
        _mint(plan, tmp_path)
    text = str(exc.value)
    assert text.startswith("review-stamp: no terminal commit found") and "--rejudge" in text


def test_judge_only_branch_never_runs_for_a_plan_that_carries_a_stamp(tmp_path):
    plan, head = _hand_worked(tmp_path)
    _record(tmp_path, head)
    plan.write_text(plan.read_text(encoding="utf-8").replace("status: executing\n", "status: executing\nreview_stamp:\n  terminal_commit_sha: abc\n", 1), encoding="utf-8")
    with pytest.raises(m.MintRefusal, match="no terminal commit found"):
        _mint(plan, tmp_path)


def test_judge_only_mint_requires_a_passing_build_test(tmp_path):
    plan, head = _hand_worked(tmp_path)
    _record(tmp_path, head)
    bt = tmp_path / "bt.md"
    bt.write_text("---\nstatus: fail\ntest_verdict: fail\nrun: 3\nfailed: 1\n---\n", encoding="utf-8")
    before = plan.read_bytes()
    with pytest.raises(m.MintRefusal, match="not pass"):
        _mint(plan, tmp_path, build_test_path=str(bt))
    assert plan.read_bytes() == before
    bt.write_text("---\nstatus: pass\ntest_verdict: pass\nrun: 3\nfailed: 0\n---\n", encoding="utf-8")
    stamp = _mint(plan, tmp_path, build_test_path=str(bt))
    assert stamp["build_test"]["verdict"] == "pass" and stamp["build_test"]["ran"] == 3


def test_rejudge_record_carries_the_judges_row_verdicts(tmp_path):
    from coordinator_core.ops.dispatch_emit import reverify_delivery as rd

    rel = rd.record_criterion_rejudge(
        repo_root=tmp_path, plan_id=PLAN_ID, head_sha="abc",
        criterion={"status": "met", "observation": "o", "register_rows": [_ROW, "junk"]},
    )
    fm = yaml.safe_load((tmp_path / rel).read_text(encoding="utf-8").split("---\n")[1])
    assert fm["criterion"]["register_rows"] == [_ROW]
