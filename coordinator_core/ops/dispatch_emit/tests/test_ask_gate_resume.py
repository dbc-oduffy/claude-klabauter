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


def test_refire_gate_computes_the_resume_plan_once(repo, monkeypatch):  # noqa: F811
    from coordinator_core.ops.dispatch_emit import ask_gate, sizing_fire

    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("A1", "coded"), ("A2", "open")])
    calls = []
    real = sizing_fire.resumable_plan

    def counted(*a, **k):
        calls.append(a[1])
        return real(*a, **k)

    monkeypatch.setattr(sizing_fire, "resumable_plan", counted)
    monkeypatch.setattr(ask_gate, "resumable_plan", counted)
    v = gate(repo, REL)
    assert v.resume_rows == ["A2"]
    assert len(calls) == 1


@pytest.mark.parametrize("rows", [[("A1", "coded"), ("A2", "open")], [("A1", "coded")]])
def test_resumable_plan_reads_the_halt_pointer_once(repo, monkeypatch, rows):  # noqa: F811
    from coordinator_core.ops.dispatch_emit import sizing_fire

    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=rows)
    reads = []
    real = sizing_fire._own_pointer

    def counted(*a, **k):
        reads.append(a)
        return real(*a, **k)

    monkeypatch.setattr(sizing_fire, "_own_pointer", counted)
    from coordinator_core.ops.dispatch_emit.sizing_fire import load_sizing

    resumable_plan(load_sizing(repo, REL), REL, repo, "s")
    assert len(reads) == 1


def test_fully_landed_plan_resumes_to_verify_not_refuses(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, disposition="coded")
    v = gate(repo, REL)
    assert v.halt is None and v.resume_plan is None and v.resume_ledger is not None
    assert "already ran" not in str(v.to_json())


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
    assert (plan.plan_rel, plan.incomplete, plan.ledger, plan.landed_open) == ("docs/plans/other.md", ("A1",), None, ())


def test_resumable_plan_refuses_an_edge_escaping_plans(repo):  # noqa: F811
    (repo / "elsewhere.md").write_text("x", encoding="utf-8")
    assert resumable_plan({"plan": "elsewhere.md"}, REL, repo, "m_plus") is None


def test_resumable_plan_coded_resumes_at_verify(repo):  # noqa: F811
    _plan(repo, disposition="coded")
    plan = resumable_plan({}, REL, repo, "s")
    assert plan.incomplete == () and plan.ledger is not None


ACCEPTED_PLAN_REL = "docs/plans/gate.md"


def _accepted_plan(repo, disposition="open"):
    _plan(repo, disposition=disposition, rel=ACCEPTED_PLAN_REL)
    path = repo / ACCEPTED_PLAN_REL
    path.write_text(path.read_text(encoding="utf-8").replace("run_id: fixture", "status: approved"), encoding="utf-8", newline="\n")


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_accepted_plan_with_empty_plan_edge_is_bound_and_resumed(repo):  # noqa: F811
    import yaml

    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, **no_console_creationflags())
    _put(repo, tshirt="M", route="plan", accepted=ACCEPTED)
    _accepted_plan(repo)
    v = gate(repo, REL)
    assert v.halt is None and v.resume_plan == ACCEPTED_PLAN_REL and v.resume_rows == ["A1"]
    assert yaml.safe_load((repo / REL).read_text(encoding="utf-8"))["plan"] == ACCEPTED_PLAN_REL


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_accepted_plan_all_landed_is_bound_and_never_the_exists_refusal(repo):  # noqa: F811
    import yaml

    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, **no_console_creationflags())
    _put(repo, tshirt="M", route="plan", accepted=ACCEPTED)
    _accepted_plan(repo, disposition="coded")
    v = gate(repo, REL)
    assert "already exists for this ask" not in str((v.halt or {}).get("reason"))
    assert yaml.safe_load((repo / REL).read_text(encoding="utf-8"))["plan"] == ACCEPTED_PLAN_REL


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_unaccepted_plan_on_disk_is_not_bound(repo):  # noqa: F811
    import yaml

    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, **no_console_creationflags())
    _put(repo, tshirt="M", route="plan", accepted=ACCEPTED)
    _plan(repo, rel=ACCEPTED_PLAN_REL)
    gate(repo, REL)
    assert not yaml.safe_load((repo / REL).read_text(encoding="utf-8")).get("plan")


def test_foreign_or_surplus_baton_is_dropped_not_refused(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo)
    v = gate(repo, REL, baton="docs/batons/other.md")
    assert v.halt is None and v.resume_plan == PLAN_REL


def test_all_landed_refire_no_longer_names_unread_revise_notes(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, disposition="coded")
    v = gate(repo, REL, run_id="warp-2026-10-21-x-73b78a-20261021T235625-cfe57e")
    assert v.halt is None and "unread revise note" not in str(v.to_json())


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_m_with_plan_edge_returns_resume_plan_and_baton(repo):  # noqa: F811
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, **no_console_creationflags())
    _put(repo, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAN_REL)
    _plan(repo)
    v = gate(repo, REL)
    assert v.halt is None and v.arm == "m_plus"
    assert v.resume_plan == PLAN_REL and v.baton["path"].startswith("state/handoffs/")
