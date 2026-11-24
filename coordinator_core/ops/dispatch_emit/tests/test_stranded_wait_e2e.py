"""End to end on the 0bac9d shape: a waiter parked co-tenant-wait behind a superseded, launcherless owner is refired by the idle tick, owner first."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ask_spool import record as spool
from coordinator_core.ops.dispatch_emit import co_tenant_owner, run_token, stage_wait, stranded_wait
from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_RUN_ID_ARG

pytestmark = pytest.mark.complete

WAITER = "ask-20261123T080000-280a50"
OWNER = "ask-20261123T084702-0bac9d"
LANDED_REFIRE = "ask-20261124T020000-0bac9d"
OWNER_SIZING = "state/sizings/2026-11/2026-11-23-owner-0bac9d.yaml"
WAITER_SIZING = "state/sizings/2026-11/2026-11-23-waiter-280a50.yaml"
PEER = "pkg/peer.py"
TICK_BUDGET_MS = 50


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _spooled(root: Path) -> list[dict]:
    return [
        json.loads(spool.read_entry(spool.entry_path(str(root), sid))[1])["tool_input"]["args"]
        for sid in spool.list_pending(str(root))
    ]


def _refires_of(root: Path, run_id: str) -> list[dict]:
    return [a for a in _spooled(root) if a[RESUME_RUN_ID_ARG] == run_id]


def _stage(root: Path, run_id: str, stage: str) -> None:
    run_dir = root / "state" / "warp" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run-stage.json").write_text(json.dumps({"stage": stage, "last_activity_at": "2026-11-24T02:00:00Z"}), encoding="utf-8")


def _halt_owner_at_commit(root: Path) -> None:
    run_dir = root / "state" / "warp" / OWNER
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "halted.json").write_text(json.dumps({
        "status": "superseded",
        "sizing_rel": OWNER_SIZING,
        "halted": "halted at commit: verify-overlay red",
        "run_artifacts": {"withheld": {PEER: "kept"}},
    }), encoding="utf-8")
    _stage(root, OWNER, "halted")


def _timed_tick(root: Path, now: float) -> tuple[list[dict], float]:
    start = time.process_time()
    acts = stranded_wait.sweep(root, now=now)
    return acts, (time.process_time() - start) * 1000


def test_idle_tick_refires_the_superseded_owner_first_then_the_waiter_once_the_owner_lands(repo, monkeypatch) -> None:
    run_token.mint(repo, WAITER, "w.mjs", marker=True, session_id="waiter-sess", sizing_rel=WAITER_SIZING)
    stage_wait.park(repo, WAITER, [OWNER], [PEER], session_id="waiter-sess", sizing_rel=WAITER_SIZING, reason="co-tenant-wait")
    _halt_owner_at_commit(repo)
    assert not run_token.is_live(repo, OWNER) and run_token.escrowed(repo, OWNER) is None
    assert co_tenant_owner._halted_marker(repo, OWNER) is not None

    spawns: list = []

    def _spawn(*args, **kwargs):
        spawns.append(args)
        raise AssertionError("idle tick spawned a process")

    monkeypatch.setattr(subprocess, "Popen", _spawn)
    now = time.time()

    first, first_ms = _timed_tick(repo, now)
    assert [(a["action"], a["owner"], a["waiter"]) for a in first] == [("owner-refire", OWNER, WAITER)]
    (owner_args,) = _spooled(repo)
    assert owner_args[RESUME_RUN_ID_ARG] == OWNER and owner_args["sizing"] == OWNER_SIZING
    assert stage_wait.read(repo, WAITER)["started"] is False

    idle, idle_ms = _timed_tick(repo, now + 5)
    assert idle == [] and len(_spooled(repo)) == 1

    _stage(repo, LANDED_REFIRE, "landed")

    second, second_ms = _timed_tick(repo, now + 10)
    assert [(a["action"], a["owner"], a["waiter"]) for a in second] == [("wake", OWNER, WAITER)]
    (waiter_args,) = _refires_of(repo, WAITER)
    assert waiter_args["sizing"] == WAITER_SIZING and waiter_args["repo"] == str(repo.resolve())
    assert len(_refires_of(repo, OWNER)) == 1
    assert stage_wait.read(repo, WAITER)["started"] is True

    after, after_ms = _timed_tick(repo, now + 15)
    assert after == [] and len(_refires_of(repo, WAITER)) == 1

    print(
        f"TICKS process_ms owner-refire={first_ms:.2f} idle={idle_ms:.2f} wake={second_ms:.2f} after={after_ms:.2f} "
        f"spawns={len(spawns)} spool_ids={spool.list_pending(str(repo))}"
    )
    assert max(first_ms, idle_ms, second_ms, after_ms) < TICK_BUDGET_MS
    assert spawns == []


def test_owner_that_never_lands_ends_the_wait_by_proceeding_without_it(repo) -> None:
    run_token.mint(repo, WAITER, "w.mjs", marker=True, session_id="waiter-sess", sizing_rel=WAITER_SIZING)
    stage_wait.park(repo, WAITER, [OWNER], [PEER], session_id="waiter-sess", sizing_rel=WAITER_SIZING, reason="co-tenant-wait")
    _halt_owner_at_commit(repo)
    moment = time.time()
    for n in range(stranded_wait.MAX_STRANDED_REFIRES):
        acts = stranded_wait.sweep(repo, now=moment + n * (stranded_wait.REFIRE_COOLDOWN_S + 1))
        assert [a["action"] for a in acts] == ["owner-refire"]
        assert _refires_of(repo, WAITER) == []

    final = stranded_wait.sweep(repo, now=moment + 10 * (stranded_wait.REFIRE_COOLDOWN_S + 1))

    assert [a["action"] for a in final] == ["proceed-without-owner"]
    assert stage_wait.read(repo, WAITER)["proceeded_without"] == [OWNER]
    assert len(_refires_of(repo, WAITER)) == 1
    assert co_tenant_owner._proceeded_without(repo, WAITER) == frozenset({OWNER})
