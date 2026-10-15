"""ask_gate resume: a plan on disk with rows still to run rides the verdict as resume_plan/resume_rows; a fully landed plan refuses."""

from __future__ import annotations

import subprocess
import textwrap

import pytest

from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused, resumable_plan
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

PLAN_REL = "docs/plans/2026-10-01-gate.md"


def _plan(repo, disposition="open", rel=PLAN_REL, rows=None):
    """`rows` is `[(id, disposition), ...]`; absent, one row A1 at `disposition`."""
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: {d}\n  writes:\n    - some/{i}.py\n"
        for i, d in (rows or [("A1", disposition)])
    )
    path.write_text(
        f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n",
        encoding="utf-8",
        newline="\n",
    )


def test_s_with_unfired_plan_gates_with_resume_plan_and_no_refusal(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo)
    v = gate(repo, REL)
    assert v.halt is None and v.arm == "s" and v.resume_plan == PLAN_REL
    assert v.to_json()["resume_plan"] == PLAN_REL


def test_fully_landed_plan_still_refuses(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, disposition="coded")
    v = gate(repo, REL)
    assert v.arm is None and v.halt["kind"] == "refusal"
    assert "already ran" in v.halt["reason"] and "new sizing" in v.halt["reason"]


def test_refire_of_a_halted_plan_resumes_its_incomplete_rows(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("A1", "coded"), ("A2", "open"), ("A3", "coded"), ("A4", "open")])
    v = gate(repo, REL)
    assert v.halt is None and v.resume_plan == PLAN_REL
    assert v.resume_rows == ["A2", "A4"]
    assert v.to_json()["resume_rows"] == ["A2", "A4"]


def test_refire_with_stale_row_reports_still_resumes(repo):  # noqa: F811
    from coordinator_core.ops.dispatch_emit import emit

    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("A1", "coded"), ("A2", "open")])
    report = repo / emit._dispatch_report_path(PLAN_REL, "A1")
    report.parent.mkdir(parents=True)
    report.write_text("DONE\n", encoding="utf-8")
    v = gate(repo, REL)
    assert v.halt is None and v.resume_rows == ["A2"]


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_checkpoint_landed_rows_are_not_resumed(repo):  # noqa: F811
    def git(*a):
        subprocess.run(["git", *a], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())

    git("init", "-q")
    git("config", "user.email", "t@t.example")
    git("config", "user.name", "t")
    _plan(repo, rows=[("A1", "open"), ("A2", "open"), ("A3", "open")])
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    git("add", "x.txt")
    git("commit", "-q", "-m", "checkpoint(wave 1): 1 row — A1", "-m", f"Checkpoint-Plan: {PLAN_REL}\nCheckpoint-Base: abc1234")
    plan = resumable_plan({"plan": PLAN_REL}, REL, repo, "m_plus")
    assert plan.plan_rel == PLAN_REL and plan.incomplete == ("A2", "A3")


def test_s_without_plan_has_no_resume_plan_key(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    v = gate(repo, REL)
    assert v.halt is None and "resume_plan" not in v.to_json()


def test_resumable_plan_none_without_a_plan(repo):  # noqa: F811
    assert resumable_plan({}, REL, repo, "s") is None
    assert resumable_plan({}, REL, repo, "m_plus") is None


def test_resumable_plan_follows_the_plan_edge_at_m_plus(repo):  # noqa: F811
    _plan(repo, rel="docs/plans/other.md")
    plan = resumable_plan({"plan": "docs/plans/other.md"}, REL, repo, "m_plus")
    assert plan == ("docs/plans/other.md", ("A1",))


def test_resumable_plan_refuses_an_edge_escaping_plans(repo):  # noqa: F811
    (repo / "elsewhere.md").write_text("x", encoding="utf-8")
    assert resumable_plan({"plan": "elsewhere.md"}, REL, repo, "m_plus") is None


def test_resumable_plan_coded_raises(repo):  # noqa: F811
    _plan(repo, disposition="coded")
    with pytest.raises(SizingFireRefused, match="already ran"):
        resumable_plan({}, REL, repo, "s")


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_m_with_plan_edge_returns_resume_plan_and_baton(repo):  # noqa: F811
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, **no_console_creationflags())
    _put(repo, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAN_REL)
    _plan(repo)
    v = gate(repo, REL)
    assert v.halt is None and v.arm == "m_plus"
    assert v.resume_plan == PLAN_REL and v.baton["path"].startswith("state/handoffs/")
