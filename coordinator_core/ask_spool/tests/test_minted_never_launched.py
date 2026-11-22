"""The drain's never-launched sweep: names, notifies, refires or closes an ask quiet at `minted` with no run."""

from __future__ import annotations

import json
import os
import time

import pytest

from coordinator_core.ask_spool import drain
from coordinator_core.group_em import gem_home
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT, write_run_stage_at

NOW = 1_800_000_000.0
OWNER = "owner-sess"
GEM = "gem-sess"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    monkeypatch.setattr(gem_home, "gem_holder", lambda: {"session_id": GEM, "live": True})


class Fire:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, root, run_id):
        self.calls.append(run_id)
        if self.fail:
            raise RuntimeError("spawn refused")
        return {"fire_id": f"fire-{len(self.calls)}", "state": "running"}


def _ask(root, n=1, *, age=1000, stage="minted", script=True, session=OWNER, halted=None, sizing=None):
    run_id = f"ask-20261120T10{n:04d}-{n:06x}"
    rd = root / RECORD_DIR_ROOT / run_id
    rd.mkdir(parents=True, exist_ok=True)
    write_run_stage_at(rd, "minted", session_id=session)
    if session is None:
        path = rd / "run-stage.json"
        path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), "session_id": None}), encoding="utf-8")
    if sizing:
        (rd / "run.json").write_text(json.dumps({"sizing_rel": sizing}), encoding="utf-8")
    if halted:
        (rd / "halted.json").write_text(json.dumps({"status": "halted", "halted": halted}), encoding="utf-8")
        write_run_stage_at(rd, "halted")
    if script:
        path = root / RUN_DIR_ROOT / f"{run_id}.workflow.mjs"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// script", encoding="utf-8")
    os.utime(rd, (NOW - age, NOW - age))
    return run_id


def _stage(root, run_id):
    return json.loads((root / RECORD_DIR_ROOT / run_id / "run-stage.json").read_text(encoding="utf-8"))


def _marker(root, run_id):
    path = root / RECORD_DIR_ROOT / run_id / "halted.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _notices(root, session):
    path = root / ".coordinator-local" / "ask-spool" / "notices" / f"{session}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _drain(root, fire, now=NOW):
    return drain.run(str(root), now=now, fire=fire)


def test_an_ask_minted_16_minutes_ago_is_noticed_to_owner_and_gem_and_refired(tmp_path):
    run_id = _ask(tmp_path, age=16 * 60)
    fire = Fire()
    result = _drain(tmp_path, fire)
    assert result["never_launched"] == 1 and result["refired"] == 1 and result["closed"] == 0
    assert fire.calls == [run_id]
    for session in (OWNER, GEM):
        (n,) = _notices(tmp_path, session)
        assert n["run_id"] == run_id and n["status"] == "never_launched" and n["action"] == "refired"
    assert _marker(tmp_path, run_id) is None


def test_the_same_ask_at_14_minutes_is_untouched(tmp_path):
    _ask(tmp_path, age=14 * 60)
    fire = Fire()
    result = _drain(tmp_path, fire)
    assert fire.calls == [] and "never_launched" not in result
    assert _notices(tmp_path, OWNER) == [] and _notices(tmp_path, GEM) == []


def test_a_refired_ask_is_not_refired_by_the_next_drain(tmp_path):
    _ask(tmp_path, age=1000)
    fire = Fire()
    _drain(tmp_path, fire)
    _drain(tmp_path, fire, now=NOW + 4000)
    assert len(fire.calls) == 1


def test_an_ask_with_a_warp_run_is_stamped_not_refired(tmp_path):
    run_id = _ask(tmp_path, age=1000, sizing="state/sizings/2026-11/2026-11-20-thing.yaml")
    warp = "warp-2026-11-20-thing-20261120T110000-abcdef"
    wd = tmp_path / RECORD_DIR_ROOT / warp
    wd.mkdir(parents=True)
    (wd / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/2026-11/2026-11-20-thing.yaml"}), encoding="utf-8")
    os.utime(tmp_path / RECORD_DIR_ROOT / run_id, (NOW - 1000, NOW - 1000))
    fire = Fire()
    result = _drain(tmp_path, fire)
    assert fire.calls == [] and "never_launched" not in result
    assert _stage(tmp_path, run_id)["warp_run"] == warp
    assert _notices(tmp_path, OWNER) == []


@pytest.mark.parametrize("kind", ["abandoned", "minted_without_dispatch"])
def test_a_child_halted_by_abandon_or_the_drains_own_halt_is_refired(tmp_path, kind):
    run_id = _ask(tmp_path, age=1000, halted=kind)
    fire = Fire()
    result = _drain(tmp_path, fire)
    assert result["refired"] == 1 and fire.calls == [run_id]


def test_a_run_halted_for_another_reason_is_left_alone(tmp_path):
    _ask(tmp_path, age=1000, halted="post_land_tripwire_red")
    fire = Fire()
    result = _drain(tmp_path, fire)
    assert fire.calls == [] and "never_launched" not in result


def test_an_ask_with_a_run_dir_is_left_alone(tmp_path):
    run_id = _ask(tmp_path, age=1000)
    run_dir = tmp_path / RUN_DIR_ROOT / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "briefs.md").write_text("x", encoding="utf-8")
    fire = Fire()
    result = _drain(tmp_path, fire)
    assert fire.calls == [] and "never_launched" not in result


def test_an_ask_holding_a_live_token_is_left_alone(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import abandoned_runs, run_token

    run_id = _ask(tmp_path, age=1000)
    monkeypatch.setattr(run_token, "held_records", lambda root: [{"run_id": run_id, "minted_at": NOW - 10}])
    monkeypatch.setattr(abandoned_runs, "_token_active", lambda root, rec, newest, now: True)
    fire = Fire()
    result = _drain(tmp_path, fire)
    assert fire.calls == [] and "never_launched" not in result


def test_an_ask_with_no_script_is_noticed_unrefirable_and_closed(tmp_path):
    run_id = _ask(tmp_path, age=1000, script=False)
    fire = Fire()
    result = _drain(tmp_path, fire)
    assert result["never_launched"] == 1 and result["closed"] == 1 and result["refired"] == 0
    assert fire.calls == []
    assert _marker(tmp_path, run_id)["halted"] == drain.MINTED_NEVER_LAUNCHED
    assert _stage(tmp_path, run_id)["stage"] == "halted"
    assert [n["action"] for n in _notices(tmp_path, OWNER)] == ["unrefirable"]
    assert [n["action"] for n in _notices(tmp_path, GEM)] == ["unrefirable"]


def test_no_live_gem_writes_the_owner_notice_only(tmp_path, monkeypatch):
    monkeypatch.setattr(gem_home, "gem_holder", lambda: None)
    _ask(tmp_path, age=1000)
    _drain(tmp_path, Fire())
    assert len(_notices(tmp_path, OWNER)) == 1 and _notices(tmp_path, GEM) == []


def test_a_gem_that_owns_the_ask_is_noticed_once(tmp_path):
    _ask(tmp_path, age=1000, session=GEM)
    _drain(tmp_path, Fire())
    assert len(_notices(tmp_path, GEM)) == 1


def test_a_missing_owner_still_notices_the_gem(tmp_path):
    _ask(tmp_path, age=1000, session=None)
    _drain(tmp_path, Fire())
    assert len(_notices(tmp_path, GEM)) == 1


def test_a_failing_fire_is_retried_then_the_ask_is_closed_quoting_the_failure(tmp_path):
    run_id = _ask(tmp_path, age=1000)
    fire = Fire(fail=True)
    now = NOW
    for _ in range(drain.FIRE_ATTEMPT_CAP):
        result = _drain(tmp_path, fire, now=now)
        assert result["never_launched"] == 1 and result["refired"] == 0
        assert _marker(tmp_path, run_id) is None
        now += 3000
        os.utime(tmp_path / RECORD_DIR_ROOT / run_id, (now - 1000, now - 1000))
    assert len(fire.calls) == drain.FIRE_ATTEMPT_CAP
    result = _drain(tmp_path, fire, now=now)
    assert result["closed"] == 1 and len(fire.calls) == drain.FIRE_ATTEMPT_CAP
    marker = _marker(tmp_path, run_id)
    assert marker["halted"] == drain.MINTED_NEVER_LAUNCHED and "spawn refused" in marker["reason"]
    assert [n["action"] for n in _notices(tmp_path, OWNER)] == ["closed"]


def test_a_fire_that_exits_before_execution_counts_as_a_failed_attempt(tmp_path):
    run_id = _ask(tmp_path, age=1000)
    result = _drain(tmp_path, lambda root, rid: {"fire_id": "f", "state": "exited", "execution_started": False})
    assert result["never_launched"] == 1 and result["refired"] == 0
    state = json.loads((tmp_path / RECORD_DIR_ROOT / run_id / "never-launched.json").read_text(encoding="utf-8"))
    assert state["fire_attempts"] == 1 and "exited before execution" in state["fire_failed"] and "fired" not in state


def test_one_drain_refires_at_most_the_per_drain_bound(tmp_path):
    for n in range(1, drain.NEVER_LAUNCHED_FIRES_PER_DRAIN + 3):
        _ask(tmp_path, n=n)
    fire = Fire()
    result = _drain(tmp_path, fire)
    assert result["refired"] == drain.NEVER_LAUNCHED_FIRES_PER_DRAIN == len(fire.calls)


def test_a_minted_ask_past_the_lookback_is_closed_with_no_notice_and_no_fire(tmp_path):
    run_id = _ask(tmp_path, age=drain.MINTED_SWEEP_LOOKBACK_SECONDS + 100)
    fire = Fire()
    result = _drain(tmp_path, fire)
    assert result["closed"] == 1 and result.get("never_launched", 0) == 0
    assert fire.calls == []
    marker = _marker(tmp_path, run_id)
    assert marker["halted"] == drain.MINTED_NEVER_LAUNCHED and _stage(tmp_path, run_id)["stage"] == "halted"
    assert _notices(tmp_path, OWNER) == [] and _notices(tmp_path, GEM) == []


def test_a_done_cursor_makes_the_second_drain_write_nothing_and_skip_the_backlog(tmp_path, monkeypatch):
    run_id = _ask(tmp_path, age=drain.MINTED_SWEEP_LOOKBACK_SECONDS + 100)
    _drain(tmp_path, Fire())
    cursor = tmp_path / ".coordinator-local" / "ask-spool" / "never-launched-backlog.cursor"
    assert json.loads(cursor.read_text(encoding="utf-8"))["done"] is True
    late = _ask(tmp_path, n=2, age=drain.MINTED_SWEEP_LOOKBACK_SECONDS + 100)
    before = (tmp_path / RECORD_DIR_ROOT / run_id / "halted.json").read_bytes()
    reads = []
    from coordinator_core.ops.dispatch_emit import ask_contract

    real = ask_contract.read_run_stage
    monkeypatch.setattr(ask_contract, "read_run_stage", lambda root, rid: reads.append(rid) or real(root, rid))
    result = _drain(tmp_path, Fire())
    assert "closed" not in result and reads == []
    assert (tmp_path / RECORD_DIR_ROOT / run_id / "halted.json").read_bytes() == before
    assert _marker(tmp_path, late) is None


def test_the_backlog_is_closed_in_batches_from_the_cursor(tmp_path):
    n_asks = drain.NEVER_LAUNCHED_BACKLOG_BATCH + 5
    ids = [_ask(tmp_path, n=n, age=drain.MINTED_SWEEP_LOOKBACK_SECONDS + 100) for n in range(1, n_asks + 1)]
    first = _drain(tmp_path, Fire())
    assert first["closed"] == drain.NEVER_LAUNCHED_BACKLOG_BATCH
    cursor = tmp_path / ".coordinator-local" / "ask-spool" / "never-launched-backlog.cursor"
    assert json.loads(cursor.read_text(encoding="utf-8"))["done"] is False
    second = _drain(tmp_path, Fire())
    assert second["closed"] == 5
    assert all(_stage(tmp_path, rid)["stage"] == "halted" for rid in ids)
    assert json.loads(cursor.read_text(encoding="utf-8"))["done"] is True


def test_an_old_ask_that_launched_is_stamped_not_closed(tmp_path):
    sizing = "state/sizings/2026-11/2026-11-20-thing.yaml"
    run_id = _ask(tmp_path, age=drain.MINTED_SWEEP_LOOKBACK_SECONDS + 100, sizing=sizing)
    warp = "warp-2026-11-20-thing-20261120T110000-abcdef"
    wd = tmp_path / RECORD_DIR_ROOT / warp
    wd.mkdir(parents=True)
    (wd / "run.json").write_text(json.dumps({"sizing_rel": sizing}), encoding="utf-8")
    os.utime(tmp_path / RECORD_DIR_ROOT / run_id, (NOW - drain.MINTED_SWEEP_LOOKBACK_SECONDS - 100,) * 2)
    result = _drain(tmp_path, Fire())
    assert "closed" not in result
    assert _stage(tmp_path, run_id)["warp_run"] == warp and _stage(tmp_path, run_id)["stage"] == "minted"


def test_the_sweep_runs_at_default_arguments_against_the_wall_clock(tmp_path):
    run_id = _ask(tmp_path, age=0)
    old = time.time() - 1000
    os.utime(tmp_path / RECORD_DIR_ROOT / run_id, (old, old))
    fire = Fire()
    result = drain.run(str(tmp_path), fire=fire)
    assert result["refired"] == 1 and fire.calls == [run_id]
