"""stranded_wait: a waiter parked behind a dead blocker ends by owner refire, wake, or proceeding without the owner."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ask_spool import record as spool
from coordinator_core.ops.dispatch_emit import pending_oracle, run_token, stage_wait, stranded_wait
from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_RUN_ID_ARG

pytestmark = pytest.mark.complete

WAITER = "ask-20261123T084702-aaaaaa"
OWNER = "ask-20261123T084702-0bac9d"
OWNER_SIZING = "state/sizings/owner.yaml"
WAITER_SIZING = "state/sizings/waiter.yaml"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _marker(root: Path, run_id: str, **fields: object) -> None:
    run_dir = root / "state" / "warp" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "halted.json").write_text(json.dumps(fields), encoding="utf-8")


def _park(root: Path, run_id: str = WAITER, owner: str = OWNER) -> None:
    stage_wait.park(root, run_id, [owner], [], session_id="s1", sizing_rel=WAITER_SIZING, reason="co-tenant-wait")


def _spooled(root: Path) -> list[dict]:
    return [
        json.loads(spool.read_entry(spool.entry_path(str(root), sid))[1])["tool_input"]["args"]
        for sid in spool.list_pending(str(root))
    ]


def _owner_refires(root: Path) -> list[dict]:
    return [a for a in _spooled(root) if a[RESUME_RUN_ID_ARG] == OWNER]


@pytest.mark.parametrize(
    "marker",
    [
        {"status": "halted", "sizing_rel": OWNER_SIZING},
        {"status": "superseded", "sizing_rel": OWNER_SIZING},
        {"status": "halted", "sizing_rel": OWNER_SIZING, "halted": "minted, not started: script unverified"},
    ],
    ids=["halted", "superseded", "launcherless"],
)
def test_dead_owner_gets_its_refire_spooled_and_the_waiter_keeps_waiting(repo, marker) -> None:
    _marker(repo, OWNER, **marker)
    _park(repo)

    acts = stranded_wait.sweep(repo, now=1000.0)

    assert [(a["action"], a["owner"], a["attempt"]) for a in acts] == [("owner-refire", OWNER, 1)]
    (args,) = _owner_refires(repo)
    assert args["sizing"] == OWNER_SIZING
    assert stage_wait.read(repo, WAITER)["started"] is False
    assert [a for a in _spooled(repo) if a[RESUME_RUN_ID_ARG] == WAITER] == []


def test_second_tick_spools_nothing_while_the_refire_is_outstanding(repo) -> None:
    _marker(repo, OWNER, status="halted", sizing_rel=OWNER_SIZING)
    _park(repo)
    stranded_wait.sweep(repo, now=time.time() + 10)

    assert stranded_wait.sweep(repo, now=time.time() + 20) == []
    assert len(_owner_refires(repo)) == 1
    assert stage_wait.read(repo, WAITER)["owner_refires"] == {OWNER: 1}


def test_owner_landed_wakes_the_waiter_and_spools_its_refire_once(repo) -> None:
    _marker(repo, OWNER, status="landed")
    _park(repo)

    first = stranded_wait.sweep(repo, now=1000.0)
    second = stranded_wait.sweep(repo, now=1001.0)

    assert [(a["action"], a["waiter"]) for a in first] == [("wake", WAITER)]
    assert second == []
    (args,) = _spooled(repo)
    assert args[RESUME_RUN_ID_ARG] == WAITER and args["sizing"] == WAITER_SIZING
    assert stage_wait.read(repo, WAITER)["started"] is True


def test_owner_with_no_marker_no_longer_holds_the_path(repo) -> None:
    _park(repo)

    assert [a["action"] for a in stranded_wait.sweep(repo, now=1000.0)] == ["wake"]
    assert [a[RESUME_RUN_ID_ARG] for a in _spooled(repo)] == [WAITER]


def test_marker_without_a_sizing_is_dropped_as_no_owner(repo) -> None:
    _marker(repo, OWNER, status="halted")
    _park(repo)

    assert [a["action"] for a in stranded_wait.sweep(repo, now=1000.0)] == ["wake"]
    assert _owner_refires(repo) == []


def test_live_owner_with_launcher_is_left_alone(repo, monkeypatch) -> None:
    _marker(repo, OWNER, status="halted", sizing_rel=OWNER_SIZING)
    _park(repo)
    monkeypatch.setattr(run_token, "is_live", lambda _root, _run: True)
    monkeypatch.setattr(run_token, "escrowed", lambda _root, _run: "token")

    assert stranded_wait.sweep(repo, now=1000.0) == []
    assert _spooled(repo) == []


def test_live_launcherless_halted_owner_is_refired(repo, monkeypatch) -> None:
    _marker(repo, OWNER, status="halted", sizing_rel=OWNER_SIZING)
    _park(repo)
    monkeypatch.setattr(run_token, "is_live", lambda _root, _run: True)
    monkeypatch.setattr(run_token, "escrowed", lambda _root, _run: None)

    acts = stranded_wait.sweep(repo, now=1000.0)

    assert [a["action"] for a in acts] == ["owner-refire"]
    assert len(_spooled(repo)) == 1


def test_resumed_owner_is_left_alone(repo) -> None:
    _marker(repo, OWNER, status="resumed", sizing_rel=OWNER_SIZING)
    _park(repo)

    assert stranded_wait.sweep(repo, now=1000.0) == []
    assert _spooled(repo) == []


def test_past_the_cap_the_waiter_proceeds_without_the_owner(repo) -> None:
    _marker(repo, OWNER, status="halted", sizing_rel=OWNER_SIZING)
    _park(repo)
    moment = time.time()
    for n in range(stranded_wait.MAX_STRANDED_REFIRES):
        acts = stranded_wait.sweep(repo, now=moment + n * (stranded_wait.REFIRE_COOLDOWN_S + 1))
        assert [a["attempt"] for a in acts] == [n + 1]
    assert len(_owner_refires(repo)) == stranded_wait.MAX_STRANDED_REFIRES

    final = stranded_wait.sweep(repo, now=moment + 10 * (stranded_wait.REFIRE_COOLDOWN_S + 1))

    assert [a["action"] for a in final] == ["proceed-without-owner"]
    record = stage_wait.read(repo, WAITER)
    assert record["proceeded_without"] == [OWNER] and record["blockers"] == [] and record["started"] is True
    assert [a for a in _spooled(repo) if a[RESUME_RUN_ID_ARG] == WAITER] != []
    assert stranded_wait.sweep(repo, now=moment + 99999) == []


def test_owner_that_rehalted_after_the_refire_is_refired_without_the_cooldown(repo) -> None:
    _marker(repo, OWNER, status="halted", sizing_rel=OWNER_SIZING)
    _park(repo)
    moment = time.time()
    stranded_wait.sweep(repo, now=moment)
    assert stranded_wait.sweep(repo, now=moment + 1) == []
    time.sleep(0.05)
    _marker(repo, OWNER, status="halted", sizing_rel=OWNER_SIZING)

    acts = stranded_wait.sweep(repo, now=moment + 5)

    assert [a["attempt"] for a in acts] == [2]


def test_on_idle_tick_reports_whether_it_acted_and_never_raises(repo, monkeypatch) -> None:
    _marker(repo, OWNER, status="landed")
    _park(repo)

    assert stranded_wait.on_idle_tick([repo], now=1000.0) is True
    assert stranded_wait.on_idle_tick([repo], now=1001.0) is False
    monkeypatch.setattr(stranded_wait, "sweep", lambda *_a, **_k: 1 / 0)
    assert stranded_wait.on_idle_tick([repo]) is False


def test_spool_refire_default_source_ids_are_unchanged_and_a_new_source_never_dedupes(repo) -> None:
    assert pending_oracle.spool_refire(repo, OWNER, OWNER_SIZING, 1) is True
    assert pending_oracle.spool_refire(repo, OWNER, OWNER_SIZING, 1) is False
    ids = spool.list_pending(str(repo))
    assert len(ids) == 1
    assert pending_oracle.spool_refire(repo, OWNER, OWNER_SIZING, 1, source="stranded-wait", reason="r") is True
    assert len(spool.list_pending(str(repo))) == 2


def test_fifty_record_tick_is_cheap_and_spawns_nothing(repo, monkeypatch) -> None:
    for n in range(50):
        owner = f"ask-20261123T084702-{n:06x}"
        _marker(repo, owner, status="halted", sizing_rel=OWNER_SIZING)
        _park(repo, run_id=f"ask-20261124T000000-{n:06x}", owner=owner)
    moment = time.time()
    assert len(stranded_wait.sweep(repo, now=moment)) == 50

    def _no_spawn(*_a, **_k):
        raise AssertionError("sweep spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    # The real-home leak check realpaths on every settings_home() call, but only under pytest (conftest
    # sets the env); the warm tick never pays it, so the budget is measured without it.
    from coordinator_core import _settings_home

    assert os.environ.get("COORDINATOR_SETTINGS_HOME")
    monkeypatch.delenv(_settings_home.FORBID_REAL_SETTINGS_HOME_ENV, raising=False)
    start = time.process_time()
    acts = stranded_wait.sweep(repo, now=moment + 1)
    elapsed_ms = (time.process_time() - start) * 1000

    assert acts == []
    assert elapsed_ms < 50
