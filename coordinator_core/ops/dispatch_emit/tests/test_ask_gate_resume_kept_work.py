"""ask_gate keeps fingerprint-matched rows of a halted run and resumes an unchanged refused plan at readiness."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops import plan_readiness_verdict as readiness
from coordinator_core.ops.dispatch_emit import ask_gate, halted_run, kept_work, resume_snapshot
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _m_repo, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan

RUN = "ask-20261001T000000-aaaaaa"
NEW_RUN = "ask-20261002T000000-bbbbbb"
REVIEWS = [{"reviewer": "staff-eng", "verdict": "WARN"}]
COVERAGE = {"verdict": "pass"}


@pytest.fixture(autouse=True)
def _no_bind(monkeypatch):
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)


def _halt(repo, kind="commit", run=RUN):
    record = repo / RECORD_DIR_ROOT / run
    halted_run.write_halt_marker(record, halted=kind, sizing_rel=REL, plan_rel=PLAN_REL)
    return record


def _keep_c2(repo, record):
    target = repo / "some" / "C2.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"print('c2')\n")
    kept_work.write(record, kept_work.kept_chunks(repo, {"some/C2.py": "C2"}, {"some/C2.py": "halt-record-only"}), None)
    return target


def _row_resume(repo):
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("C1", "coded"), ("C2", "open"), ("C3", "open")])
    return _halt(repo)


def test_kept_row_stays_in_resume_rows_and_is_listed_kept(repo):  # noqa: F811
    record = _row_resume(repo)
    _keep_c2(repo, record)
    sink: list = []
    v = gate(repo, REL, run_id=NEW_RUN, kept_sink=sink)
    assert v.halt is None and v.resume_rows == ["C2", "C3"]
    assert sink == [(["C2"], RUN)]


def test_handler_snapshot_lists_the_kept_row(repo):  # noqa: F811
    record = _row_resume(repo)
    _keep_c2(repo, record)
    reply = ask_gate._handler({"sizing_path": REL, "run_id": NEW_RUN}, repo_root=repo / ".git")
    assert reply["resume_rows"] == ["C2", "C3"]
    snap = resume_snapshot.read(repo, NEW_RUN, PLAN_REL)
    assert snap.kept == frozenset({"C2"}) and snap.kept_run == RUN
    assert "kept" not in reply


def test_a_changed_byte_is_not_kept(repo):  # noqa: F811
    record = _row_resume(repo)
    target = _keep_c2(repo, record)
    target.write_bytes(b"print('edited')\n")
    sink: list = []
    v = gate(repo, REL, run_id=NEW_RUN, kept_sink=sink)
    assert v.resume_rows == ["C2", "C3"] and sink == []


def test_a_live_note_naming_the_row_is_not_kept_and_a_retracted_one_is(repo):  # noqa: F811
    record = _row_resume(repo)
    _keep_c2(repo, record)
    notes = record / "notes.jsonl"
    notes.write_text(json.dumps({"note": "redo C2 with the new API", "at": "t"}) + "\n", encoding="utf-8")
    sink: list = []
    gate(repo, REL, run_id=NEW_RUN, kept_sink=sink)
    assert sink == []
    with notes.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"retracts": "N1", "reason": "r", "at": "t", "from": "em"}) + "\n")
    gate(repo, REL, run_id=NEW_RUN, kept_sink=sink)
    assert sink == [(["C2"], RUN)]


def test_a_run_that_kept_nothing_leaves_the_verdict_alone(repo):  # noqa: F811
    _row_resume(repo)
    sink: list = []
    v = gate(repo, REL, run_id=NEW_RUN, kept_sink=sink)
    assert v.resume_rows == ["C2", "C3"] and sink == []


def _refusal(repo, monkeypatch, plan_bytes=b"plan v1\n", register=None):
    over = {"requirement_register": register} if register else {}
    _m_repo(repo, monkeypatch, plan="docs/plans/2026-10-01-gate.md", **over)
    _plan(repo, rows=[("A1", "open")])
    plan = repo / PLAN_REL
    plan.write_bytes(plan.read_bytes() + plan_bytes)
    record = _halt(repo, kind="refusal")
    halted = {
        "halted": "refusal",
        "plan": PLAN_REL,
        "blitz": {"pulled": [{"planPath": PLAN_REL, "reviews": REVIEWS, "coverage": COVERAGE}]},
    }
    kept_work.write(record, {}, kept_work.plan_gate(halted, repo))
    return plan


def _readiness(monkeypatch, verdict):
    seen: list = []

    def fake(params, repo_root=None):
        seen.append(params)
        return {"verdict": verdict}

    monkeypatch.setattr(readiness, "_handler", fake)
    return seen


def test_unchanged_plan_with_passing_readiness_resumes_ready(repo, monkeypatch):  # noqa: F811
    _refusal(repo, monkeypatch)
    seen = _readiness(monkeypatch, "ready")
    out = ask_gate._handler({"sizing_path": REL, "run_id": NEW_RUN}, repo_root=repo / ".git")
    assert out["halt"] is None and out["arm"] == "m_plus" and out["resume_ready"] is True, out
    assert "manifest_handle" in out, out
    assert seen == [{"plan": PLAN_REL, "reviews": REVIEWS, "coverage": COVERAGE}]


def test_edited_plan_is_not_resume_ready(repo, monkeypatch):  # noqa: F811
    plan = _refusal(repo, monkeypatch)
    plan.write_bytes(plan.read_bytes() + b"edited\n")
    seen = _readiness(monkeypatch, "ready")
    v = gate(repo, REL, run_id=NEW_RUN)
    assert v.resume_ready is False and "resume_ready" not in v.to_json() and seen == []


def test_a_pending_ruling_is_not_resume_ready(repo, monkeypatch):  # noqa: F811
    register = {"rows": [{rr.ROW_ANCHOR: "revision@2026-10-23T00:00:00Z", rr.ROW_TEXT: "J1: use sonnet", rr.ROW_STATUS: rr.STATUS_OPEN}]}
    _refusal(repo, monkeypatch, register=register)
    seen = _readiness(monkeypatch, "ready")
    v = gate(repo, REL, run_id=NEW_RUN)
    assert v.pending_rulings == ["J1"] and v.resume_ready is False and seen == []


@pytest.mark.parametrize("verdict", ["pulled", "judgment"])
def test_an_identical_pull_is_todays_verdict(repo, monkeypatch, verdict):  # noqa: F811
    _refusal(repo, monkeypatch)
    _readiness(monkeypatch, verdict)
    out = ask_gate._handler({"sizing_path": REL, "run_id": NEW_RUN}, repo_root=repo / ".git")
    assert out["halt"] is None and out["resume_plan"] == PLAN_REL
    assert "resume_ready" not in out and "manifest_handle" not in out, out


def test_no_halted_run_is_not_resume_ready(repo, monkeypatch):  # noqa: F811
    _m_repo(repo, monkeypatch, plan="docs/plans/2026-10-01-gate.md")
    _plan(repo, rows=[("A1", "open")])
    seen = _readiness(monkeypatch, "ready")
    v = gate(repo, REL, run_id=NEW_RUN)
    assert v.resume_ready is False and seen == []


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_kept_json_adds_no_spawn(repo, monkeypatch):  # noqa: F811
    from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
    from coordinator_core.win_portability import no_console_creationflags

    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, check=True, **no_console_creationflags())
    record = _row_resume(repo)
    with _count_spawns_attributed(monkeypatch) as without:
        gate(repo, REL, run_id=NEW_RUN)
    _keep_c2(repo, record)
    sink: list = []
    with _count_spawns_attributed(monkeypatch) as with_kept:
        gate(repo, REL, run_id=NEW_RUN, kept_sink=sink)
    assert sink and len(with_kept) == len(without), ([s.argv for s in with_kept], [s.argv for s in without])
