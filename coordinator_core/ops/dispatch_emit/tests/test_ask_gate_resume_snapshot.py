"""ask_gate writes the resume snapshot on a row resume with a run_id and clears it otherwise."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, resume_snapshot
from coordinator_core.ops.dispatch_emit.ask_contract import LedgerHalt, ResumePlan
from coordinator_core.ops.dispatch_emit.ask_stage import _handler as stage_handler
from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan  # noqa: F401

RUN = "ask-20261017T000000-aaaaaa"


@pytest.fixture(autouse=True)
def _no_bind(monkeypatch):
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)


def _call(repo, run_id=RUN):
    params = {"sizing_path": REL}
    if run_id:
        params["run_id"] = run_id
    return ask_gate._handler(params, repo_root=repo / ".git")


def _snap(repo, run_id=RUN):
    return resume_snapshot.read(repo, run_id, PLAN_REL)


def test_resume_writes_plan_ids_minus_resume_rows(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("A1", "coded"), ("A2", "open"), ("A3", "delivered_elsewhere"), ("A4", "open")])
    reply = _call(repo)
    assert reply["resume_rows"] == ["A2", "A4"]
    assert _snap(repo) == resume_snapshot.Snapshot(frozenset({"A1", "A3"}), frozenset({"A2", "A4"}))


def test_no_run_id_writes_nothing(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("A1", "coded"), ("A2", "open")])
    _call(repo, run_id=None)
    assert _snap(repo) is None


def test_no_resume_removes_an_earlier_snapshot(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("A1", "coded"), ("A2", "open")])
    _call(repo)
    assert _snap(repo) == resume_snapshot.Snapshot(frozenset({"A1"}), frozenset({"A2"}))
    (repo / PLAN_REL).unlink()
    reply = _call(repo)
    assert "resume_rows" not in reply
    assert _snap(repo) is None


HELD = (("A2", "performer em"), ("A4", "gated"))


def _halt() -> LedgerHalt:
    return LedgerHalt(RUN, PLAN_REL, REL, ("A2",))


def _stub_resume(monkeypatch, resume, ledger=None):
    """Hand the gate a fixed ResumePlan, as `collect_fire_refusals` would through its `resume_sink`."""
    monkeypatch.setattr(
        ask_gate, "collect_fire_refusals", lambda *a, resume_sink=None, **k: (resume_sink.append(resume), [])[1]
    )
    if ledger is not None:
        monkeypatch.setattr(ask_gate, "_resume_ledger", lambda *a, **k: ledger)


def test_row_resume_writes_held(repo, monkeypatch):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("A1", "coded"), ("A2", "open"), ("A4", "open")])
    _stub_resume(monkeypatch, ResumePlan(PLAN_REL, ("A2",), held=HELD))
    _call(repo)
    assert _snap(repo).held == HELD
    assert _snap(repo).resume_rows == frozenset({"A2"})


def test_ledger_resume_writes_held_and_its_unlanded_rows_as_the_resume_set(repo, monkeypatch):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("A1", "coded"), ("A2", "open"), ("A4", "open")])
    _stub_resume(
        monkeypatch, ResumePlan(PLAN_REL, (), ledger=_halt(), held=HELD),
        ledger={"plan": PLAN_REL, "unlanded": ["A2"]},
    )
    reply = _call(repo)
    assert "resume_rows" not in reply
    snap = _snap(repo)
    assert snap.held == HELD
    assert snap.resume_rows == frozenset({"A2"})
    assert "A2" not in snap.not_to_run


def test_empty_held_reads_empty_on_both_resumes(repo, monkeypatch):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("A1", "coded"), ("A2", "open")])
    _stub_resume(monkeypatch, ResumePlan(PLAN_REL, ("A2",)))
    _call(repo)
    assert _snap(repo).held == ()
    _stub_resume(monkeypatch, ResumePlan(PLAN_REL, (), ledger=_halt()), ledger={"plan": PLAN_REL})
    _call(repo)
    assert _snap(repo).held == ()


def test_halted_gate_writes_no_snapshot_for_a_ledger_resume(repo, monkeypatch):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("A1", "coded"), ("A2", "open")])
    _call(repo)
    _stub_resume(monkeypatch, ResumePlan(PLAN_REL, (), ledger=_halt(), held=HELD), ledger={"plan": PLAN_REL})
    monkeypatch.setattr(ask_gate, "_resume_ledger", lambda *a, **k: (_ for _ in ()).throw(SizingFireRefused(["x"])))
    _call(repo)
    assert _snap(repo) is None


def test_ledger_resume_snapshot_leaves_ledger_path_staging_unchanged(tmp_path):
    rows = "".join(
        f"- id: {i}\n  title: t{i}\n  change_kind: doc-edit\n  surface: docs/x/{i}.md\n  writes:\n    - docs/x/{i}.md\n"
        for i in ("C1", "C2")
    )
    (tmp_path / ".git").mkdir()
    plan = tmp_path / "docs/plans/p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8", newline="\n")
    params = {"run_id": "r1", "plan_path": "docs/plans/p.md", "only_rows": ["C1"]}
    before = read_stage_reply(tmp_path, stage_handler(params, repo_root=tmp_path))
    resume_snapshot.write(tmp_path, "r1", "docs/plans/p.md", ["C2"], ["C1"], held=HELD)
    after = read_stage_reply(tmp_path, stage_handler(params, repo_root=tmp_path))
    assert "error" not in before
    assert after == before


def test_unsafe_run_id_writes_nothing(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("A1", "coded"), ("A2", "open")])
    _call(repo, run_id="../escape")
    assert not (repo / "scratch" / "escape").exists()
