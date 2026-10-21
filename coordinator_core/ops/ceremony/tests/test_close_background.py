"""Tests for close.background: record writes per leg, isolation of a raising leg, budget guard,
post-close memory admission."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from coordinator_core.authz.classification import OpClass, OP_CLASSIFICATION
from coordinator_core.authz.completion_evidence import OP_COMPLETION_EVIDENCE, EvidenceClass
from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.dispatch_emit.run_close import LegResult

pytestmark = pytest.mark.spawns_process

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


def test_budget_spent_records_the_remaining_legs_deferred_queued_not_run(tmp_path):
    _queue(tmp_path, legs=("baton-drain", "orientation-refresh", "memo-lifecycle", "post-close"))
    clock = iter([0.0, 10.0, 500.0, 500.0, 500.0])
    ran = []
    runners = {name: (lambda ctx, n=name: (ran.append(n), LegResult("ran"))[1]) for name in cb.BACKGROUND_LEGS}
    cb.run_background(_ctx(tmp_path), runners, budget_ms=400.0, cpu_ms=lambda: next(clock))

    rec = _record(tmp_path)
    assert ran == ["baton-drain"]
    assert rec["baton-drain"]["status"] == "ran"
    for name in ("orientation-refresh", "memo-lifecycle", "post-close"):
        assert rec[name]["status"] == "queued"
        assert rec[name]["detail"].startswith("deferred#1:")


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


def _chain_stub(calls, verdicts=None, raises=None):
    class Stub:
        @staticmethod
        def refresh(root, timeout):
            calls.append("structural")

        @staticmethod
        def run(ceremony, root):
            calls.append("scip")

        @staticmethod
        def chain_verdicts(root, paths):
            calls.append(("chain", list(paths)))
            if raises:
                raise raises
            return verdicts or []

    return Stub


def _verdict(chain, closed):
    from types import SimpleNamespace

    return SimpleNamespace(chain=chain, closed=closed)


BATONS = ("state/handoffs/a.md", "state/handoffs/b.md")


@pytest.fixture(autouse=True)
def _admission(monkeypatch):
    """Ample trusted reading, notices recorded; individual tests re-patch the reading."""
    from coordinator_core.ops.ceremony import post_close_admission as pca

    notices = []
    monkeypatch.setattr(pca, "read_available", lambda: (65536, True))
    monkeypatch.setattr(pca, "notice", lambda verdict, run_id: notices.append(verdict.label) or True)
    return notices


@pytest.fixture(autouse=True)
def _reaper(monkeypatch):
    """Stub the abandoned-run reaper; tests re-patch the reply."""
    from coordinator_core.ops.dispatch_emit import abandoned_runs

    calls = []
    monkeypatch.setattr(
        abandoned_runs, "_handler",
        lambda params, repo_root=None: (calls.append(repo_root), {"ok": True, "reaped": []})[1],
    )
    return calls


def test_post_close_calls_the_reaper_once_and_reports_zero(tmp_path, monkeypatch, _reaper):
    _queue(tmp_path, legs=("post-close",))
    monkeypatch.setattr(cb, "_bin_script", lambda name: _chain_stub([]))
    result = cb._post_close(_ctx(tmp_path))
    assert _reaper == [tmp_path] and "reaped: 0 run(s)" in result.detail and result.status == "ran"


def test_post_close_reports_reaped_count(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import abandoned_runs

    monkeypatch.setattr(abandoned_runs, "_handler", lambda p, repo_root=None: {"ok": True, "reaped": ["a", "b"]})
    monkeypatch.setattr(cb, "_bin_script", lambda name: _chain_stub([]))
    assert "reaped: 2 run(s)" in cb._post_close(_ctx(tmp_path)).detail


def test_post_close_raising_reaper_fails_the_leg_after_launchers_ran(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import abandoned_runs

    def boom(p, repo_root=None):
        raise RuntimeError("x")

    calls = []
    monkeypatch.setattr(abandoned_runs, "_handler", boom)
    monkeypatch.setattr(cb, "_bin_script", lambda name: _chain_stub(calls))
    result = cb._post_close(_ctx(tmp_path))
    assert calls[:2] == ["structural", "scip"]
    assert result.status == "failed" and "reap RuntimeError" in result.detail


def test_post_close_not_ok_reaper_reply_names_its_error(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import abandoned_runs

    monkeypatch.setattr(abandoned_runs, "_handler", lambda p, repo_root=None: {"ok": False, "error": "status failed"})
    monkeypatch.setattr(cb, "_bin_script", lambda name: _chain_stub([]))
    result = cb._post_close(_ctx(tmp_path))
    assert result.status == "failed" and "reap status failed" in result.detail


def test_post_close_runs_chain_closure_and_names_a_closed_chain(tmp_path, monkeypatch):
    calls = []
    stub = _chain_stub(calls, [_verdict("dlv-x", True), _verdict("dlv-x", True)])
    _queue(tmp_path, legs=("post-close",))
    monkeypatch.setattr(cb, "_bin_script", lambda name: stub)
    result = cb._post_close(_ctx(tmp_path, batons=BATONS))
    assert calls == ["structural", "scip", ("chain", list(BATONS))]
    assert result.status == "ran"
    assert "2 baton(s), 1 chain(s) closed: dlv-x" in result.detail


def test_post_close_open_chain_is_ran_with_zero_closed(tmp_path, monkeypatch):
    stub = _chain_stub([], [_verdict("dlv-x", False)])
    _queue(tmp_path, legs=("post-close",))
    monkeypatch.setattr(cb, "_bin_script", lambda name: stub)
    result = cb._post_close(_ctx(tmp_path, batons=BATONS[:1]))
    assert result.status == "ran" and "0 chain(s) closed" in result.detail


def test_post_close_with_no_claimed_baton_never_loads_the_chain_script(tmp_path, monkeypatch):
    loaded = []
    stub = _chain_stub([])
    _queue(tmp_path, legs=("post-close",))
    monkeypatch.setattr(cb, "_bin_script", lambda name: (loaded.append(name), stub)[1])
    result = cb._post_close(_ctx(tmp_path))
    assert "run claims no baton" in result.detail and result.status == "ran"
    assert "baton-chain-closure" not in loaded


def test_post_close_names_a_raising_chain_closure_and_still_runs_launchers(tmp_path, monkeypatch):
    calls = []
    stub = _chain_stub(calls, raises=RuntimeError("boom"))
    monkeypatch.setattr(cb, "_bin_script", lambda name: stub)
    result = cb._post_close(_ctx(tmp_path, batons=BATONS))
    assert calls[:2] == ["structural", "scip"]
    assert result.status == "failed" and "baton-chain-closure RuntimeError" in result.detail


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


def _reading(monkeypatch, avail, trusted=True):
    from coordinator_core.ops.ceremony import post_close_admission as pca

    monkeypatch.setattr(pca, "read_available", lambda: (avail, trusted))


def test_low_reading_defers_both_launchers_and_the_leg_stays_ran(tmp_path, monkeypatch, _admission):
    calls = []
    _queue(tmp_path, legs=("post-close",))
    monkeypatch.setattr(cb, "_bin_script", lambda name: _chain_stub(calls))
    _reading(monkeypatch, 4608)
    result = cb._post_close(_ctx(tmp_path))
    assert calls == []
    assert result.status == "ran"
    assert "deferred: structural-index" in result.detail and "scip-rebuild" in result.detail
    assert _admission == []


def test_reading_admitting_only_the_first_defers_the_second(tmp_path, monkeypatch, _admission):
    calls = []
    _queue(tmp_path, legs=("post-close",))
    monkeypatch.setattr(cb, "_bin_script", lambda name: _chain_stub(calls))
    _reading(monkeypatch, 2048 + 8192)
    result = cb._post_close(_ctx(tmp_path))
    assert calls == ["structural"]
    assert _admission == ["structural-index"]
    assert "launched: structural-index; deferred: scip-rebuild" in result.detail


def test_untrusted_reading_defers_both_as_unreadable(tmp_path, monkeypatch, _admission):
    calls = []
    _queue(tmp_path, legs=("post-close",))
    monkeypatch.setattr(cb, "_bin_script", lambda name: _chain_stub(calls))
    _reading(monkeypatch, 65536, trusted=False)
    result = cb._post_close(_ctx(tmp_path))
    assert calls == [] and _admission == []
    assert result.detail.count("memory unreadable") == 2


def test_ample_reading_launches_both_with_one_notice_each(tmp_path, monkeypatch, _admission):
    calls = []
    _queue(tmp_path, legs=("post-close",))
    monkeypatch.setattr(cb, "_bin_script", lambda name: _chain_stub(calls))
    result = cb._post_close(_ctx(tmp_path))
    assert calls == ["structural", "scip"]
    assert _admission == ["structural-index", "scip-rebuild"]
    assert "deferred" not in result.detail


def test_deferral_survives_detail_truncation_behind_a_long_chain_list(tmp_path, monkeypatch):
    names = [_verdict(f"dlv-{'x' * 30}-{i}", True) for i in range(8)]
    monkeypatch.setattr(cb, "_bin_script", lambda name: _chain_stub([], names))
    _reading(monkeypatch, 4608)
    result = cb._post_close(_ctx(tmp_path, batons=BATONS))
    assert len(result.detail) == run_close.DETAIL_CAP
    assert "deferred: structural-index" in result.detail


def test_op_is_registered_mutating_fire_and_forget_common_dir():
    from coordinator_core.op_scopes import _OP_KEY_SCOPE as OP_SCOPES

    assert OP_CLASSIFICATION["close.background"] is OpClass.MUTATING
    assert OP_COMPLETION_EVIDENCE["close.background"] is EvidenceClass.FIRE_AND_FORGET
    assert OP_SCOPES["close.background"] == "common_dir"


def test_background_ledger_ends_in_the_four_legacy_legs_without_review():
    assert cb.BACKGROUND_LEGS[-4:] == ("baton-drain", "orientation-refresh", "memo-lifecycle", "post-close")
    assert "review" not in cb.DEFAULT_RUNNERS


def _stub_launchers(monkeypatch):
    class Stub:
        @staticmethod
        def refresh(root, timeout):
            return None

        @staticmethod
        def run(ceremony, root):
            return None

        @staticmethod
        def chain_verdicts(root, paths):
            return []

    monkeypatch.setattr(cb, "_bin_script", lambda name: Stub)


def test_post_close_launches_close_review_when_the_record_queues_review(tmp_path, monkeypatch):
    _stub_launchers(monkeypatch)
    _queue(tmp_path, legs=("review", "post-close"))
    spawned = []
    monkeypatch.setattr(
        "coordinator_core.ops.ceremony.detached_spawn.spawn_detached",
        lambda repo_root, script, args=None, env_extra=None: (spawned.append((script, list(args))), True)[1],
    )
    result = cb._post_close(_ctx(tmp_path, batons=("state/handoffs/h1.md",)))
    assert len(spawned) == 1
    script, args = spawned[0]
    assert script.replace("\\", "/").endswith("ops/ceremony/close_review.py")
    assert args[0] == RUN and args[1:3] == ["--root", str(tmp_path)]
    assert json.loads(args[4]) == {"claimed_batons": ["state/handoffs/h1.md"]}
    assert "close.review" in result.detail


def test_post_close_does_not_launch_close_review_when_review_is_not_queued(tmp_path, monkeypatch):
    _stub_launchers(monkeypatch)
    _queue(tmp_path, legs=("post-close",))
    spawned = []
    monkeypatch.setattr(
        "coordinator_core.ops.ceremony.detached_spawn.spawn_detached",
        lambda *a, **k: spawned.append(a) or True,
    )
    cb._post_close(_ctx(tmp_path))
    assert spawned == []


def test_post_close_names_a_close_review_that_did_not_start(tmp_path, monkeypatch):
    _stub_launchers(monkeypatch)
    _queue(tmp_path, legs=("review", "post-close"))
    monkeypatch.setattr("coordinator_core.ops.ceremony.detached_spawn.spawn_detached", lambda *a, **k: False)
    assert "close.review not started" in cb._post_close(_ctx(tmp_path)).detail



def test_close_review_still_imports_its_close_background_names():
    import importlib

    importlib.import_module("coordinator_core.ops.ceremony.close_review")


_DID = "dlv-close-cascade-000000"
_SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _cascade_repo(tmp_path, baton_state):
    from coordinator_core.ops.tests.test_deliverable_cascade_kinds import _git, _init_repo, _seed_handoff

    repo = tmp_path / "repo"
    _init_repo(repo)
    name = "2026-10-21-run-baton.md"
    path = _seed_handoff(repo, name, deployment_state=baton_state, deliverable_id=_DID)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            f"deployment_state: {baton_state}\n", f"deployment_state: {baton_state}\nshipped_in: abc1234\n", 1
        ),
        encoding="utf-8",
    )
    if baton_state == "shipped":
        dest = repo / "archive" / "handoffs" / name[:7] / name
        dest.parent.mkdir(parents=True)
        path.rename(dest)
    _seed_handoff(repo, "2026-10-21-peer.md", deliverable_id=_DID)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", f"seed\n\nSession-Id: {_SESSION_ID}")
    return repo, f"state/handoffs/{name}", _git


def _drain(repo, rel):
    run_close.write_close_job(repo, RUN, ("baton-drain",), datetime.now().astimezone() + timedelta(minutes=10))
    ctx = cb.JobContext(repo, repo / ".git", RUN, (rel,), None, "a" * 40)
    return cb.run_background(ctx, {"baton-drain": cb._baton_drain})["baton-drain"]


def test_baton_drain_fires_the_cascade_for_an_archived_shipped_baton(tmp_path, monkeypatch):
    import asyncio

    from coordinator_core.ops.cascade_backstop_sweep import _handler as sweep_handler

    monkeypatch.setenv("CLAUDE_SESSION_ID", _SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    repo, rel, git = _cascade_repo(tmp_path, "shipped")
    before = git(repo, "rev-list", "--count", "HEAD").stdout.strip()
    assert asyncio.run(sweep_handler({"deliverable_id": _DID}, repo_root=repo / ".git"))["divergences"]

    result = _drain(repo, rel)

    assert result.status == "ran", result.detail
    assert "cascade: 1 fired, 1 advanced, 0 refused, 0 skipped" in result.detail
    peer = (repo / "state" / "handoffs" / "2026-10-21-peer.md").read_text(encoding="utf-8")
    assert "deployment_state: shipped" in peer and f"advanced_by: {_DID}" in peer
    assert int(git(repo, "rev-list", "--count", "HEAD").stdout.strip()) == int(before) + 1
    assert asyncio.run(sweep_handler({"deliverable_id": _DID}, repo_root=repo / ".git"))["divergences"] == []


def test_baton_drain_skips_non_terminal_batons_without_calling_the_op(tmp_path, monkeypatch):
    import coordinator_core.warm.entry_seam as entry_seam

    def boom(*a, **k):
        raise AssertionError("reentrant_dispatch must not be called")

    monkeypatch.setattr(entry_seam, "reentrant_dispatch", boom)
    repo, rel, _ = _cascade_repo(tmp_path, "ready_to_fire")

    result = _drain(repo, rel)

    assert result.status == "ran", result.detail
    assert "cascade: 0 fired, 0 advanced, 0 refused, 1 skipped" in result.detail


def test_baton_drain_cascade_error_fails_the_leg_after_the_archive_step_ran(tmp_path, monkeypatch):
    import coordinator_core.ops.dispatch_emit.run_baton_cascade as rbc

    def bad(*a, **k):
        raise RuntimeError("secret")

    monkeypatch.setattr(rbc, "fire_for_batons", bad)
    repo, rel, _ = _cascade_repo(tmp_path, "ready_to_fire")

    result = _drain(repo, rel)

    assert result.status == "failed" and "cascade: failed RuntimeError" in result.detail
    assert "archived 0 of 1 claimed baton(s)" in result.detail
