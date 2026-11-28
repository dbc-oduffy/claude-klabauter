"""The watch tick's fleet idle-balance check and the held loop's off-tick fleet refresh."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.group_em import capacity_admit, capacity_record, fleet_live, watch
from coordinator_core.group_em.atomic_record import holder_lock

pytestmark = pytest.mark.complete

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setattr(watch, "_fleet_thread", None)
    return home


def _row(box: str, alias: str, cpu: float, **extra: object) -> dict:
    return {"box": box, "alias": alias, "state": "ok", "sampled_at": NOW - 5, "cores": 8, "cpu_pct": cpu, **extra}


def _seed(rows: list) -> None:
    fleet_live.write_live_snapshot(rows, NOW - 5)


def _stamp_local(load_pct: float) -> None:
    assert capacity_record.stamp({"cores_logical": 8, "load_pct": load_pct, "ram_avail_mb": 9000}, now=NOW)


def _tick(prior: dict, emitted: list) -> dict:
    return watch._balance_tick("repo", emitted.append, prior, now=NOW)


def test_idle_box_with_work_elsewhere_emits_after_the_dwell() -> None:
    _seed([_row("local", "machine-a", 5.0), _row("far-1", "gipsy", 95.0)])
    _stamp_local(5.0)
    out: list = []
    dwell = _tick({"local": NOW - 16 * 60}, out)
    assert out == ["IDLE-BOX machine-a idle 16m while work on gipsy -- route via groupem.fleet_book"]
    assert dwell == {"local": NOW - 16 * 60}


def test_dwell_under_fifteen_minutes_is_silent_and_carried() -> None:
    _seed([_row("local", "machine-a", 5.0), _row("far-1", "gipsy", 95.0)])
    _stamp_local(5.0)
    out: list = []
    dwell = _tick({"local": NOW - 14 * 60}, out)
    assert out == []
    assert dwell == {"local": NOW - 14 * 60}


def test_first_sight_starts_the_clock_at_now() -> None:
    _seed([_row("local", "machine-a", 5.0), _row("far-1", "gipsy", 95.0)])
    _stamp_local(5.0)
    out: list = []
    assert _tick({}, out) == {"local": NOW}
    assert out == []


def test_no_work_anywhere_is_silent() -> None:
    _seed([_row("local", "machine-a", 5.0), _row("far-1", "gipsy", 20.0)])
    _stamp_local(5.0)
    out: list = []
    _tick({"local": NOW - 3600, "far-1": NOW - 3600}, out)
    assert out == []


def test_a_held_grant_on_a_far_box_counts_as_work() -> None:
    _seed([_row("local", "machine-a", 5.0), _row("far-1", "gipsy", 20.0)])
    _stamp_local(5.0)
    grants = capacity_admit.grants_path()
    grants.parent.mkdir(parents=True, exist_ok=True)
    grants.write_text(
        json.dumps({"run": "r1", "box_id": "far-1", "granted_at": NOW - 10, "ttl_s": 600}) + "\n",
        encoding="utf-8",
    )
    out: list = []
    _tick({"local": NOW - 20 * 60}, out)
    assert len(out) == 1 and "IDLE-BOX machine-a" in out[0] and "work on gipsy" in out[0]


def test_a_wait_on_this_box_is_work_for_the_far_idle_box() -> None:
    _seed([_row("local", "machine-a", 20.0), _row("far-1", "gipsy", 5.0)])
    _stamp_local(20.0)
    waits = capacity_admit.waits_path()
    waits.parent.mkdir(parents=True, exist_ok=True)
    waits.write_text(json.dumps({"run": "w1", "since": NOW - 30, "seen_at": NOW - 5}) + "\n", encoding="utf-8")
    out: list = []
    _tick({"far-1": NOW - 20 * 60}, out)
    assert out == ["IDLE-BOX gipsy idle 20m while work on machine-a -- route via groupem.fleet_book"]


def test_a_stale_far_row_is_never_idle_and_its_load_is_never_work() -> None:
    stale = _row("far-1", "gipsy", 5.0, sampled_at=NOW - 3600)
    fleet_live.write_live_snapshot([_row("local", "machine-a", 95.0), stale], NOW - 5)
    _stamp_local(95.0)
    out: list = []
    dwell = _tick({"far-1": NOW - 3600}, out)
    assert out == [] and "far-1" not in dwell


def test_the_fresh_capacity_record_overrides_a_snapshot_row_that_read_busy() -> None:
    _seed([_row("local", "machine-a", 95.0, sampled_at=NOW - 200), _row("far-1", "gipsy", 95.0)])
    _stamp_local(5.0)
    out: list = []
    _tick({"local": NOW - 16 * 60}, out)
    assert len(out) == 1 and out[0].startswith("IDLE-BOX machine-a idle 16m")


def test_stale_heterogeneous_local_row_is_not_overlaid_with_a_fresh_load() -> None:
    hetero = _row(
        "local", "machine-a", 5.0, sampled_at=NOW - 1000,
        cores_by_class={"p": 8, "e": 16}, free_cores_by_class={"p": 0, "e": 16},
    )
    _seed([hetero, _row("far-1", "gipsy", 95.0)])
    _stamp_local(5.0)
    out: list = []
    assert _tick({"local": NOW - 3600}, out) == {}
    assert out == []


def test_heterogeneous_row_with_busy_p_cores_is_not_idle_even_when_e_cores_are_free() -> None:
    hetero = _row(
        "local", "machine-a", 5.0,
        cores_by_class={"p": 8, "e": 16}, free_cores_by_class={"p": 1, "e": 16},
    )
    _seed([hetero, _row("far-1", "gipsy", 95.0)])
    _stamp_local(5.0)
    out: list = []
    assert _tick({"local": NOW - 3600}, out) == {}
    assert out == []


def test_balance_tick_never_raises_and_returns_the_prior_dwell(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(now: float) -> tuple:
        raise RuntimeError("snapshot")

    monkeypatch.setattr(watch, "_balance_rows", _boom)
    assert watch._balance_tick("repo", lambda line: None, {"local": 5.0, "bad": "x"}, now=NOW) == {"local": 5.0}
    assert watch._balance_tick("repo", lambda line: None, None, now=NOW) == {}


def test_missing_snapshot_and_record_is_silent() -> None:
    out: list = []
    assert _tick({}, out) == {}
    assert out == []


def test_save_prev_parked_persists_the_balance_key(tmp_path: Path) -> None:
    repo = str(tmp_path / "repo")
    assert watch.save_prev_parked(repo, {}, balance={"local": 12.5})
    assert watch._load_prev_record(repo)["balance"] == {"local": 12.5}
    assert watch.save_prev_parked(repo, {})
    assert "balance" not in watch._load_prev_record(repo)


def test_tick_once_runs_the_balance_check_and_carries_its_dwell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = str(tmp_path / "repo")
    seen: list = []

    def _fake(repo_root: str, emit: object, prior: object, now: float | None = None) -> dict:
        seen.append(prior)
        return {"local": 42.0}

    monkeypatch.setattr(watch, "poll_once", lambda *a, **k: ({}, [], {}, None))
    for name in ("_load_alarm_tick", "_memory_tick", "_stdin_python_tick"):
        monkeypatch.setattr(watch, name, lambda *a, **k: None)
    monkeypatch.setattr(watch, "_process_census", lambda: None)
    monkeypatch.setattr(watch, "_held_runs_tick", lambda *a, **k: None)
    monkeypatch.setattr(watch, "capacity_tick", lambda: True)
    monkeypatch.setattr(watch, "link_tick", lambda *a, **k: {})
    monkeypatch.setattr(watch, "_stamp_box_heartbeat", lambda: None)
    monkeypatch.setattr(watch, "_prune_spool", lambda *a, **k: None)
    monkeypatch.setattr(watch.watch_heartbeat, "stamp", lambda *a, **k: None)
    monkeypatch.setattr(watch, "_balance_tick", _fake)

    assert watch.tick_once(repo, caller_session_id="s", stream=open(tmp_path / "o.txt", "w")) == 0
    assert watch.tick_once(repo, caller_session_id="s", stream=open(tmp_path / "o.txt", "w")) == 0
    assert seen == [None, {"local": 42.0}]
    assert watch._load_prev_record(repo)["balance"] == {"local": 42.0}


def _gather_spy(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []
    monkeypatch.setattr(fleet_live, "gather", lambda *a, **k: calls.append(time.time()) or [])
    return calls


def test_kick_fleet_refresh_gathers_off_thread_when_the_snapshot_is_old(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _gather_spy(monkeypatch)
    thread = watch.kick_fleet_refresh()
    assert thread is not None
    thread.join(5)
    assert len(calls) == 1


def test_kick_fleet_refresh_skips_a_fresh_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _gather_spy(monkeypatch)
    fleet_live.write_live_snapshot([{"box": "local", "alias": "a", "state": "ok", "sampled_at": time.time()}],
                                   time.time())
    assert watch.kick_fleet_refresh() is None
    assert calls == []


def test_kick_fleet_refresh_skips_when_another_watcher_holds_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _gather_spy(monkeypatch)
    lock_base = fleet_live.live_snapshot_path().with_name("fleet-refresh")
    with holder_lock(lock_base, timeout=1.0):
        thread = watch.kick_fleet_refresh()
        assert thread is not None
        thread.join(5)
    assert calls == []


def test_kick_fleet_refresh_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watch, "_fleet_snapshot_due", lambda now=None: (_ for _ in ()).throw(RuntimeError("x")))
    assert watch.kick_fleet_refresh() is None


def test_held_loops_kick_the_fleet_refresh_and_tick_once_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect

    assert "kick_fleet_refresh()" in inspect.getsource(watch.until_event)
    assert "kick_fleet_refresh()" in inspect.getsource(watch.main)
    assert "kick_fleet_refresh" not in inspect.getsource(watch.tick_once)
