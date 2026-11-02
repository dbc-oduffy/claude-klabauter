"""Tests for the live mid-stage run detector."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core.session import liveness
from coordinator_core.warm import live_runs

NOW = datetime(2026, 10, 31, 23, 0, 0, tzinfo=timezone.utc)


def _ts(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


LIVE_SESSIONS: set = set()


@pytest.fixture(autouse=True)
def _injected_session_liveness(monkeypatch):
    LIVE_SESSIONS.clear()
    LIVE_SESSIONS.add("sess1")
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: sid in LIVE_SESSIONS)


def _token(root, run_id, kind="live"):
    base = root / ".git" / "coordinator-warp-run-tokens"
    if kind == "claimed":
        base = base / "claimed"
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{run_id}.json").write_text(
        json.dumps({"run_id": run_id, "minted_at": time.time()}), encoding="utf-8"
    )


def _run(root, run_id, stage, minutes_ago, session_id="sess1", token="live"):
    d = root / "state" / "warp" / run_id
    d.mkdir(parents=True)
    if token:
        _token(root, run_id, token)
    data = {"stage": stage, "last_activity_at": _ts(minutes_ago)}
    if session_id is not None:
        data["session_id"] = session_id
    (d / "run-stage.json").write_text(json.dumps(data), encoding="utf-8")


def test_reports_recent_midstage_and_skips_others(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    repo = tmp_path / "r1"
    _run(repo, "live", "staged", 2)
    _run(repo, "halted", "halted", 1)
    _run(repo, "done", "complete", 1)
    _run(repo, "stale", "staged", 600)
    bad = repo / "state" / "warp" / "bad"
    bad.mkdir(parents=True)
    (bad / "run-stage.json").write_text("{not json", encoding="utf-8")

    got = live_runs.find_live_runs({"repos.r1": str(repo)}, now=NOW)

    assert got == [live_runs.LiveRun("r1", "live", "staged", _ts(2))]


def test_missing_repo_is_skipped(tmp_path):
    assert live_runs.find_live_runs({"repos.x": str(tmp_path / "nope")}, now=NOW) == []


def test_format_handover_is_one_line_per_run_and_carries_no_override_key():
    lines = live_runs.format_handover(
        [live_runs.LiveRun("r1", "a", "staged", "t1"), live_runs.LiveRun("r2", "b", "x", "t2")]
    )
    assert len(lines) == 2
    assert "r1 a stage=staged" in lines[0] and "r2 b stage=x" in lines[1]
    assert all("--allow" not in line for line in lines)
    assert live_runs.format_handover([]) == []


def test_window_boundary_is_inclusive(tmp_path):
    repo = tmp_path / "r1"
    _run(repo, "edge", "staged", 30)
    _run(repo, "past", "staged", 31)
    got = live_runs.find_live_runs({"repos.r1": str(repo)}, now=NOW)
    assert [r.run_id for r in got] == ["edge"]


def test_default_registry_is_read_when_repos_omitted(tmp_path, monkeypatch):
    from coordinator_core.ops.fleet import _memo_resolver

    repo = tmp_path / "r1"
    _run(repo, "live", "staged", 2)
    monkeypatch.setattr(_memo_resolver, "read_registry_repos", lambda: {"repos.r1": str(repo)})
    got = live_runs.find_live_runs(now=NOW)
    assert [r.run_id for r in got] == ["live"]


def test_unreadable_registry_reports_no_runs(monkeypatch):
    from coordinator_core.ops.fleet import _memo_resolver

    def broken():
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(_memo_resolver, "read_registry_repos", broken)
    assert live_runs.find_live_runs(now=NOW) == []


def _session_run(root, run_id, stage, minutes_ago, session_id="sess1"):
    _run(root, run_id, stage, minutes_ago, session_id)


def test_hand_landed_run_without_token_is_not_live(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    repo = tmp_path / "r1"
    _run(repo, "hand", "staged", 1, token=None)
    assert not live_runs.run_is_live(repo, "hand", now=NOW)
    assert live_runs.find_live_runs({"repos.r1": str(repo)}, now=NOW) == []


def test_live_token_makes_the_same_run_live(tmp_path):
    repo = tmp_path / "r1"
    _run(repo, "tok", "staged", 1)
    assert live_runs.run_is_live(repo, "tok", now=NOW)
    assert [r.run_id for r in live_runs.find_live_runs({"repos.r1": str(repo)}, now=NOW)] == ["tok"]


def test_claimed_token_keeps_a_terminal_committing_run_live(tmp_path):
    repo = tmp_path / "r1"
    _run(repo, "mid", "staged", 1, token="claimed")
    assert live_runs.run_is_live(repo, "mid", now=NOW)


def test_unreadable_token_store_reads_as_held_but_malformed_does_not(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_token

    repo = tmp_path / "r1"

    def denied(_root):
        raise PermissionError("denied")

    monkeypatch.setattr(run_token, "held_records", denied)
    assert live_runs._token_held(repo, "x") is True

    def malformed(_root):
        raise ValueError("bad")

    monkeypatch.setattr(run_token, "held_records", malformed)
    assert live_runs._token_held(repo, "x") is False


def test_halted_run_is_not_live_with_a_token(tmp_path):
    repo = tmp_path / "r1"
    _run(repo, "h", "halted", 1)
    assert not live_runs.run_is_live(repo, "h", now=NOW)


def test_owner_holds_ignores_the_token(tmp_path, monkeypatch):
    repo = tmp_path / "r1"
    _run(repo, "hand", "staged", 120, token=None)
    monkeypatch.setattr(live_runs, "_owner_live", lambda sid, memo=None: True)
    assert live_runs.run_owner_holds(repo, "hand")


def test_stage_only_predicate_keeps_a_tokenless_midstage_run_live(tmp_path):
    repo = tmp_path / "r1"
    _run(repo, "between", "staged", 1, token=None)
    assert not live_runs.run_is_live(repo, "between", now=NOW)
    assert live_runs.run_stage_live(repo, "between", now=NOW)


def test_stage_only_predicate_still_refuses_terminal_and_stale_runs(tmp_path):
    repo = tmp_path / "r1"
    _run(repo, "done", "halted", 1, token=None)
    _run(repo, "old", "staged", 120, token=None)
    assert not live_runs.run_stage_live(repo, "done", now=NOW)
    assert not live_runs.run_stage_live(repo, "old", now=NOW)


def test_abandoned_runs_reaper_uses_the_stage_only_predicate():
    from coordinator_core.ops.dispatch_emit import abandoned_runs

    assert abandoned_runs.run_stage_live is live_runs.run_stage_live
    assert not hasattr(abandoned_runs, "run_is_live")


def _journal(home, repo, session_id, name, text, minutes_ago):
    from coordinator_core.group_em.idle_report import projects_dir_for

    d = Path(projects_dir_for(str(repo), str(home))) / session_id / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text(text, encoding="utf-8")
    t = (NOW - timedelta(minutes=minutes_ago)).timestamp()
    os.utime(f, (t, t))


def test_fresh_naming_journal_does_not_keep_a_stalled_run_live(tmp_path):
    repo, home = tmp_path / "r1", tmp_path / "home"
    _session_run(repo, "run-a", "staged", 600)
    _journal(home, repo, "sess1", "wf_x.json", '{"run": "run-a"}', 0)
    assert not live_runs.run_is_live(repo, "run-a", now=NOW)
    assert live_runs.find_live_runs({"repos.r1": str(repo)}, now=NOW) == []


def test_fresh_own_activity_stays_live_beside_a_fresh_journal(tmp_path):
    repo, home = tmp_path / "r1", tmp_path / "home"
    _session_run(repo, "run-a", "staged", 2)
    _journal(home, repo, "sess1", "wf_x.json", '{"run": "run-a"}', 0)
    assert live_runs.run_is_live(repo, "run-a", now=NOW)


def test_terminal_stage_is_not_live_even_with_fresh_journal(tmp_path):
    repo, home = tmp_path / "r1", tmp_path / "home"
    _session_run(repo, "done", "halted", 1)
    _journal(home, repo, "sess1", "wf_d.json", "done", 1)
    assert not live_runs.run_is_live(repo, "done", now=NOW)


def test_landed_stage_is_not_live_even_with_fresh_journal(tmp_path):
    repo, home = tmp_path / "r1", tmp_path / "home"
    _session_run(repo, "shipped", "landed", 1)
    _journal(home, repo, "sess1", "wf_l.json", "shipped", 1)
    assert not live_runs.run_is_live(repo, "shipped", now=NOW)


def test_missing_record_or_session_id_is_not_live(tmp_path):
    repo = tmp_path / "r1"
    assert not live_runs.run_is_live(repo, "ghost", now=NOW)
    _run(repo, "nosess", "staged", 1, session_id=None)
    assert not live_runs.run_is_live(repo, "nosess", now=NOW)


def test_traversal_session_id_is_rejected(tmp_path):
    repo = tmp_path / "r1"
    _session_run(repo, "t", "staged", 1, session_id="../x")
    assert not live_runs.run_is_live(repo, "t", now=NOW)


def test_same_root_listed_under_two_keys_reports_once(tmp_path):
    repo = tmp_path / "r1"
    _run(repo, "live", "staged", 2)
    got = live_runs.find_live_runs({"repos.a": str(repo), "repos.b": str(repo)}, now=NOW)
    assert len(got) == 1


def test_dead_owner_with_fresh_activity_is_not_live(tmp_path):
    repo = tmp_path / "r1"
    _run(repo, "dead", "minted", 1, session_id="gone")
    _run(repo, "alive", "minted", 1)
    assert not live_runs.run_is_live(repo, "dead", now=NOW)
    assert live_runs.run_is_live(repo, "alive", now=NOW)
    got = live_runs.find_live_runs({"repos.r1": str(repo)}, now=NOW)
    assert [r.run_id for r in got] == ["alive"]


def test_live_owner_with_terminal_stage_is_not_live(tmp_path):
    repo = tmp_path / "r1"
    _run(repo, "h", "halted", 1)
    _run(repo, "c", "complete", 1)
    assert live_runs.find_live_runs({"repos.r1": str(repo)}, now=NOW) == []


def test_owner_liveness_error_is_not_live(tmp_path, monkeypatch):
    def broken(sid, cwd=None):
        raise RuntimeError("meta unreadable")

    monkeypatch.setattr(liveness, "session_live", broken)
    repo = tmp_path / "r1"
    _run(repo, "x", "staged", 1)
    assert not live_runs.run_is_live(repo, "x", now=NOW)


def test_owner_liveness_is_checked_once_per_session_per_call(tmp_path, monkeypatch):
    calls: list = []

    def counting(sid, cwd=None):
        calls.append(sid)
        return sid in LIVE_SESSIONS

    monkeypatch.setattr(liveness, "session_live", counting)
    repo = tmp_path / "r1"
    for i in range(5):
        _run(repo, f"run-{i}", "staged", 1)
    _run(repo, "dead-1", "staged", 1, session_id="gone")
    _run(repo, "dead-2", "staged", 1, session_id="gone")

    got = live_runs.find_live_runs({"repos.r1": str(repo)}, now=NOW)

    assert len(got) == 5
    assert sorted(calls) == ["gone", "sess1"]


def test_owner_holds_a_stale_non_terminal_run_while_the_owner_lives(tmp_path, monkeypatch):
    repo = tmp_path / "r1"
    _session_run(repo, "long", "staged", 120)
    monkeypatch.setattr(live_runs, "_owner_live", lambda sid, memo=None: True)
    assert live_runs.run_owner_holds(repo, "long")
    assert not live_runs.run_is_live(repo, "long", now=NOW)


def test_owner_does_not_hold_a_terminal_or_ownerless_run(tmp_path, monkeypatch):
    repo = tmp_path / "r1"
    _session_run(repo, "done", "landed", 120)
    monkeypatch.setattr(live_runs, "_owner_live", lambda sid, memo=None: True)
    assert not live_runs.run_owner_holds(repo, "done")
    monkeypatch.setattr(live_runs, "_owner_live", lambda sid, memo=None: False)
    _session_run(repo, "dead", "staged", 1)
    assert not live_runs.run_owner_holds(repo, "dead")
