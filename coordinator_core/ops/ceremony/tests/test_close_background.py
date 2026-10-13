"""Tests for close.background: record writes per leg, isolation of a raising leg, budget guard."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from coordinator_core.authz.classification import OpClass, OP_CLASSIFICATION
from coordinator_core.authz.completion_evidence import OP_COMPLETION_EVIDENCE, EvidenceClass
from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.dispatch_emit.run_close import LegResult

RUN = "run-1"


def _ctx(root, batons=(), memo=None):
    return cb.JobContext(root, root / ".git", RUN, tuple(batons), memo)


def _queue(root, legs=cb.BACKGROUND_LEGS, minutes=10):
    run_close.write_close_job(root, RUN, legs, datetime.now().astimezone() + timedelta(minutes=minutes))


def _record(root):
    return json.loads((root / run_close.close_job_path(RUN)).read_text(encoding="utf-8"))["legs"]


def _ok(status="ran", detail="done"):
    return lambda ctx: LegResult(status, detail)


def test_each_leg_outcome_is_recorded_in_ledger_order(tmp_path):
    _queue(tmp_path)
    order = []

    def leg(name):
        def run(ctx):
            order.append(name)
            return LegResult("ran", f"{name} paid")
        return run

    runners = {name: leg(name) for name in cb.BACKGROUND_LEGS}
    outcomes = cb.run_background(_ctx(tmp_path), runners)

    assert order == list(cb.BACKGROUND_LEGS)
    assert set(outcomes) == set(cb.BACKGROUND_LEGS)
    rec = _record(tmp_path)
    for name in cb.BACKGROUND_LEGS:
        assert rec[name] == {"status": "ran", "detail": f"{name} paid"}


def test_a_raising_leg_is_recorded_failed_and_the_others_still_run(tmp_path):
    _queue(tmp_path)

    def boom(ctx):
        raise RuntimeError("exploded at " + "C" + ":\\u\\x " + "/" + "home/y")

    runners = {name: _ok() for name in cb.BACKGROUND_LEGS}
    runners["orientation-refresh"] = boom
    cb.run_background(_ctx(tmp_path), runners)

    rec = _record(tmp_path)
    assert rec["orientation-refresh"]["status"] == "failed"
    assert "RuntimeError" in rec["orientation-refresh"]["detail"]
    run_close.check_path_hygiene(rec["orientation-refresh"]["detail"])
    for name in ("baton-drain", "memo-lifecycle", "post-close"):
        assert rec[name]["status"] == "ran"


def test_only_legs_listed_queued_in_the_record_run(tmp_path):
    _queue(tmp_path, legs=("baton-drain", "post-close"))
    seen = []
    runners = {name: (lambda ctx, n=name: (seen.append(n), LegResult("ran"))[1]) for name in cb.BACKGROUND_LEGS}
    cb.run_background(_ctx(tmp_path), runners)
    assert seen == ["baton-drain", "post-close"]


def test_budget_spent_records_the_remaining_legs_failed_not_run(tmp_path):
    _queue(tmp_path)
    clock = iter([0.0, 10.0, 500.0, 500.0, 500.0])
    ran = []
    runners = {name: (lambda ctx, n=name: (ran.append(n), LegResult("ran"))[1]) for name in cb.BACKGROUND_LEGS}
    cb.run_background(_ctx(tmp_path), runners, budget_ms=400.0, cpu_ms=lambda: next(clock))

    rec = _record(tmp_path)
    assert ran == ["baton-drain"]
    assert rec["baton-drain"]["status"] == "ran"
    for name in ("orientation-refresh", "memo-lifecycle", "post-close"):
        assert rec[name]["status"] == "failed"
        assert "not started" in rec[name]["detail"]


def test_past_deadline_runs_nothing(tmp_path):
    _queue(tmp_path, minutes=-5)
    ran = []
    runners = {name: (lambda ctx, n=name: (ran.append(n), LegResult("ran"))[1]) for name in cb.BACKGROUND_LEGS}
    assert cb.run_background(_ctx(tmp_path), runners) == {}
    assert ran == []


def test_missing_record_raises_and_the_handler_reports_it(tmp_path):
    with pytest.raises(FileNotFoundError):
        cb.run_background(_ctx(tmp_path), {})
    (tmp_path / ".git").mkdir()
    reply = cb._handler({"run_id": RUN}, tmp_path / ".git")
    assert "error" in reply


def test_handler_rejects_a_request_without_run_id(tmp_path):
    (tmp_path / ".git").mkdir()
    assert "run_id" in cb._handler({}, tmp_path / ".git")["error"]


def test_handler_runs_the_default_legs_for_a_run_with_nothing_to_pay(tmp_path):
    (tmp_path / ".git").mkdir()
    _queue(tmp_path, legs=("baton-drain", "memo-lifecycle"))
    reply = cb._handler({"run_id": RUN, "claimed_batons": [], "source_memo": None}, tmp_path / ".git")
    assert reply["legs"]["baton-drain"]["status"] == "not-applicable"
    assert reply["legs"]["memo-lifecycle"]["status"] == "not-applicable"
    assert _record(tmp_path)["memo-lifecycle"]["status"] == "not-applicable"


def test_memo_already_archived_is_discharged(tmp_path):
    from coordinator_core.memo_corpus import memo_corpus_root

    archive = tmp_path / "state" / "cross-repo" / "archive"
    archive.mkdir(parents=True)
    (archive / "m.md").write_text("x", encoding="utf-8")
    assert memo_corpus_root(str(tmp_path)).replace("\\", "/").endswith("state/cross-repo")
    assert cb._memo_lifecycle(_ctx(tmp_path, memo="m.md")).status == "discharged"


def test_memo_in_neither_place_is_failed_by_name(tmp_path):
    (tmp_path / "state" / "cross-repo" / "inbox").mkdir(parents=True)
    result = cb._memo_lifecycle(_ctx(tmp_path, memo="gone.md"))
    assert result.status == "failed" and "gone.md" in result.detail


def test_post_close_names_the_skipped_chain_closure_and_is_failed(tmp_path, monkeypatch):
    calls = []

    class Stub:
        @staticmethod
        def refresh(root, timeout):
            calls.append("structural")

        @staticmethod
        def run(ceremony, root):
            calls.append("scip")

    monkeypatch.setattr(cb, "_bin_script", lambda name: Stub)
    result = cb._post_close(_ctx(tmp_path))
    assert calls == ["structural", "scip"]
    assert result.status == "failed"
    assert "baton-chain-closure" in result.detail and "206ms" in result.detail


def test_post_close_survives_a_failing_launcher(tmp_path, monkeypatch):
    class Stub:
        @staticmethod
        def refresh(root, timeout):
            raise OSError("no")

        @staticmethod
        def run(ceremony, root):
            return "ok"

    monkeypatch.setattr(cb, "_bin_script", lambda name: Stub)
    result = cb._post_close(_ctx(tmp_path))
    assert "structural-index OSError" in result.detail and "scip-rebuild" in result.detail


def test_scrub_removes_every_path_shape():
    text = "a " + "C" + ":\\x\\y b /abs/p c rel\\back d ok/rel"
    out = cb._scrub(text)
    run_close.check_path_hygiene(out)
    assert "ok/rel" in out


def test_op_is_registered_mutating_fire_and_forget_common_dir():
    from coordinator_core.op_scopes import _OP_KEY_SCOPE as OP_SCOPES

    assert OP_CLASSIFICATION["close.background"] is OpClass.MUTATING
    assert OP_COMPLETION_EVIDENCE["close.background"] is EvidenceClass.FIRE_AND_FORGET
    assert OP_SCOPES["close.background"] == "common_dir"
