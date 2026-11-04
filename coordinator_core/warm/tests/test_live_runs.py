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


def test_minted_run_with_unproven_runner_is_not_live(tmp_path, monkeypatch):
    from coordinator_core import warp_live

    _run(tmp_path, "r1", "minted", 1)
    monkeypatch.setattr(warp_live, "runner_unproven", lambda root, rid, **k: True)
    assert not live_runs.run_is_live(tmp_path, "r1", now=NOW)
    assert not live_runs.run_stage_live(tmp_path, "r1", now=NOW)
    assert live_runs.find_live_runs({"repos.x": str(tmp_path)}, now=NOW) == []


def test_minted_run_with_proven_runner_stays_live_and_other_stages_skip_the_check(tmp_path, monkeypatch):
    from coordinator_core import warp_live

    calls = []
    monkeypatch.setattr(warp_live, "runner_unproven", lambda root, rid, **k: calls.append(rid) or False)
    _run(tmp_path, "r1", "minted", 1)
    _run(tmp_path, "r2", "executing", 1)
    assert live_runs.run_is_live(tmp_path, "r1", now=NOW)
    assert live_runs.run_is_live(tmp_path, "r2", now=NOW)
    assert calls == ["r1"]


def test_a_real_minted_run_with_no_runner_is_not_live_and_a_launched_one_is(tmp_path, monkeypatch):
    from coordinator_core import launch_records, warp_live

    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: managed / name)
    for rid in ("r1", "r2"):
        _run(tmp_path, rid, "minted", 1)
        token = tmp_path / ".git" / "coordinator-warp-run-tokens" / f"{rid}.json"
        token.write_text(
            json.dumps(
                {
                    "run_id": rid,
                    "minted_at": time.time() - 3600,
                    "session_id": "sess1",
                    "script_path": f"scratch/warp/{rid}.mjs",
                }
            ),
            encoding="utf-8",
        )
    path = Path(launch_records.record_path(str(managed), "sess1", "task1"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"scriptPath": "scratch/warp/r2.mjs", "fired_at": time.time() - 3500}), encoding="utf-8"
    )

    assert not live_runs.run_is_live(tmp_path, "r1", now=NOW)
    assert live_runs.run_is_live(tmp_path, "r2", now=NOW)


def test_missing_repo_is_skipped(tmp_path):
    assert live_runs.find_live_runs({"repos.x": str(tmp_path / "nope")}, now=NOW) == []


def test_format_refusal_names_every_run_id_and_repo_and_carries_no_override_key():
    text = live_runs.format_refusal(
        [live_runs.LiveRun("r1", "a", "staged", "t1"), live_runs.LiveRun("r2", "b", "x", "t2")], 7.0
    )
    assert "r1 a stage=staged" in text and "r2 b stage=x" in text
    assert "2 run(s)" in text and "holding 7s" in text
    assert "--allow" not in text


def test_token_free_find_lists_a_staged_run_between_ops(tmp_path):
    repo = tmp_path / "r1"
    _run(repo, "between-ops", "staged", 2, token=None)

    assert live_runs.find_live_runs({"repos.r1": str(repo)}, now=NOW) == []
    got = live_runs.find_live_runs({"repos.r1": str(repo)}, now=NOW, require_token=False)
    assert got == [live_runs.LiveRun("r1", "between-ops", "staged", _ts(2))]


def _fresh_staged_run(root, run_id):
    """A real run-stage.json stamped now, no token, owner live."""
    d = root / "state" / "warp" / run_id
    d.mkdir(parents=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (d / "run-stage.json").write_text(
        json.dumps({"stage": "staged", "last_activity_at": stamp, "session_id": "sess1"}), encoding="utf-8"
    )
    return d / "run-stage.json"


def test_hold_refuses_for_a_foreign_repo_staged_run_after_the_bound(tmp_path, monkeypatch):
    foreign = tmp_path / "foreign"
    _fresh_staged_run(foreign, "run-foreign")
    monkeypatch.setattr(live_runs, "_registered_repos", lambda: {"repos.foreign": str(foreign)})
    clock = [0.0]
    sleeps: list[float] = []

    def fake_sleep(s):
        sleeps.append(s)
        clock[0] += s

    runs, held = live_runs.hold_for_mid_stage_runs(
        bound_s=5.0, poll_s=2.0, sleep=fake_sleep, monotonic=lambda: clock[0]
    )

    assert [(r.repo, r.run_id, r.stage) for r in runs] == [("foreign", "run-foreign", "staged")]
    assert held >= 5.0 and sleeps == [2.0, 2.0, 1.0]
    assert "foreign run-foreign" in live_runs.format_refusal(runs, held)


def test_hold_returns_clear_once_the_run_reaches_a_terminal_stage(tmp_path, monkeypatch):
    foreign = tmp_path / "foreign"
    rec = _fresh_staged_run(foreign, "run-foreign")
    monkeypatch.setattr(live_runs, "_registered_repos", lambda: {"repos.foreign": str(foreign)})
    clock = [0.0]

    def fake_sleep(s):
        clock[0] += s
        rec.write_text(json.dumps({"stage": "complete", "session_id": "sess1"}), encoding="utf-8")

    runs, held = live_runs.hold_for_mid_stage_runs(
        bound_s=60.0, poll_s=2.0, sleep=fake_sleep, monotonic=lambda: clock[0]
    )

    assert runs == [] and held == 2.0


def test_hold_with_no_mid_stage_run_returns_without_sleeping(tmp_path, monkeypatch):
    monkeypatch.setattr(live_runs, "_registered_repos", lambda: {"repos.empty": str(tmp_path / "empty")})

    def no_sleep(_s):
        raise AssertionError("slept with nothing to wait for")

    assert live_runs.hold_for_mid_stage_runs(bound_s=5.0, sleep=no_sleep)[0] == []


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


# --- Phantom-live shapes: runs that ended or never ran but whose run-stage.json still reads live. ---
# Each fixture is one run dir under state/warp, otherwise live (fresh stamp, live owner, token held),
# so only the run's ended state decides. The handover bar is find_live_runs(require_token=False).


def _stranded(root, run_id, stage, halt=None, *, minutes_ago=1, token_age_s=0.0, token_fields=None):
    """A run whose run-stage.json is left at `stage`; `halt` writes the run's real halted.json through
    halted_run.write_halt_marker (which stamps stage=halted) and the stage is then restored, the
    leftover the phantom shapes share."""
    from coordinator_core.ops.dispatch_emit import halted_run

    d = root / "state" / "warp" / run_id
    d.mkdir(parents=True)
    base = root / ".git" / "coordinator-warp-run-tokens"
    base.mkdir(parents=True, exist_ok=True)
    token = {"run_id": run_id, "minted_at": time.time() - token_age_s, **(token_fields or {})}
    (base / f"{run_id}.json").write_text(json.dumps(token), encoding="utf-8")
    stage_data = {"stage": stage, "last_activity_at": _ts(minutes_ago), "session_id": "sess1"}
    if halt is not None:
        halted_run.write_halt_marker(d, halted=halt, sizing_rel="state/sizings/2026-11/x.yaml")
    (d / "run-stage.json").write_text(json.dumps(stage_data), encoding="utf-8")
    return d


def _handover_live(repo):
    return [r.run_id for r in live_runs.find_live_runs({"repos.r1": str(repo)}, now=NOW, require_token=False)]


def test_control_executing_run_with_live_owner_is_live_for_the_handover(tmp_path):
    repo = tmp_path / "r1"
    _stranded(repo, "control", "executing")
    assert _handover_live(repo) == ["control"]
    assert live_runs.run_is_live(repo, "control", now=NOW)


def test_control_freshly_minted_run_inside_the_runner_grace_is_live(tmp_path):
    repo = tmp_path / "r1"
    _stranded(repo, "fresh", "minted", token_age_s=5.0)
    assert _handover_live(repo) == ["fresh"]


def test_control_resumed_marker_does_not_end_a_run(tmp_path):
    repo = tmp_path / "r1"
    d = _stranded(repo, "resumed", "executing")
    (d / "halted.json").write_text(json.dumps({"status": "resumed", "halted": "refused"}), encoding="utf-8")
    assert _handover_live(repo) == ["resumed"]


def test_dead_owner_staged_run_is_not_live(tmp_path):
    repo = tmp_path / "r1"
    _stranded(repo, "dead", "staged")
    data = json.loads((repo / "state" / "warp" / "dead" / "run-stage.json").read_text(encoding="utf-8"))
    data["session_id"] = "gone"
    (repo / "state" / "warp" / "dead" / "run-stage.json").write_text(json.dumps(data), encoding="utf-8")
    assert _handover_live(repo) == []
    assert not live_runs.run_is_live(repo, "dead", now=NOW)


@pytest.mark.designed_red
@pytest.mark.parametrize(
    "halted, stage",
    [
        ("parked", "minted"),
        ("refused", "minted"),
        ("stage_pending", "staged"),
    ],
    ids=["parked-left-minted", "refusal-halted-left-minted", "stage-pending-exit-left-staged"],
)
def test_a_run_with_a_halt_marker_is_not_live_whatever_its_stage_reads(tmp_path, halted, stage):
    """A halted run is over: the halt marker decides, not the stage a halt path forgot to stamp."""
    repo = tmp_path / "r1"
    _stranded(repo, "halted-run", stage, halted)
    assert _handover_live(repo) == []
    assert not live_runs.run_is_live(repo, "halted-run", now=NOW)
    assert not live_runs.run_stage_live(repo, "halted-run", now=NOW)


@pytest.mark.designed_red
def test_a_closed_run_left_at_staged_is_not_live(tmp_path):
    repo = tmp_path / "r1"
    d = _stranded(repo, "closed", "staged")
    (d / "halted.json").write_text(json.dumps({"status": "closed", "halted": "stage_pending"}), encoding="utf-8")
    assert _handover_live(repo) == []


@pytest.mark.designed_red
def test_a_never_started_minted_run_past_the_grace_is_not_live(tmp_path):
    """The Workflow returned at once ('hook did not rewrite it'): token minted long ago, no submission, no launch, stage minted."""
    repo = tmp_path / "r1"
    _stranded(repo, "never-ran", "minted", token_age_s=3600.0)
    assert _handover_live(repo) == []
    assert not live_runs.run_is_live(repo, "never-ran", now=NOW)


@pytest.mark.designed_red
def test_phantoms_beside_a_live_run_leave_only_the_live_run(tmp_path):
    repo = tmp_path / "r1"
    _stranded(repo, "a-live", "executing")
    _stranded(repo, "b-parked", "minted", "parked")
    _stranded(repo, "c-refused", "minted", "refused")
    _stranded(repo, "d-pending", "staged", "stage_pending")
    _stranded(repo, "e-never", "minted", token_age_s=3600.0)
    assert _handover_live(repo) == ["a-live"]
