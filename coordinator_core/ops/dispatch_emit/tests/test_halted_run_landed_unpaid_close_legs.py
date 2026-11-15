"""dispatch.record_halted_run_landed pays an existing close job's unpaid legs instead of reading it as a finished close."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RID = "warp-2026-11-06-solo-20261106T000000-abcdef"
UUID = "123e4567-e89b-12d3-a456-426614174000"
LEGS = ("coded-stamp", "plan-reconciliation")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def launches(monkeypatch):
    calls: list = []
    monkeypatch.setattr(op, "_launch", lambda root, run_id: calls.append(run_id) or True)
    return calls


@pytest.fixture
def repo(tmp_path, launches):
    _git(tmp_path, "init", "-q")
    (tmp_path / "one").write_text("one", encoding="utf-8")
    _git(tmp_path, "add", "--", "one")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "one")
    d = tmp_path / "state" / "warp" / RID
    d.mkdir(parents=True)
    (d / "halted.json").write_text(
        json.dumps({"status": "halted", "halted": "x", "sizing_rel": None, "plan": "docs/plans/p.md"}), encoding="utf-8",
    )
    sha = _git(tmp_path, "rev-parse", "HEAD")
    first = op._handler({"run_id": RID, "sha": sha, "session_id": UUID}, tmp_path)
    assert first["ok"] and first["written"] is True, first
    launches.clear()
    return tmp_path, sha


def _job(root: Path, deadline: datetime, settle_all: bool = False) -> None:
    run_close.write_close_job(
        root, RID, () if settle_all else LEGS, deadline,
        settled={leg: run_close.LegResult("ran") for leg in LEGS} if settle_all else None,
    )


def _land(root: Path, sha: str) -> dict:
    out = op._handler({"run_id": RID, "sha": sha, "session_id": UUID}, root)
    assert out["ok"], out
    return out


def _past() -> datetime:
    return datetime.now().astimezone() - timedelta(hours=1)


def test_stale_unpaid_job_is_requeued_and_relaunched(repo, launches):
    root, sha = repo
    _job(root, _past())
    close = _land(root, sha)["close"]
    assert close.get("detail") != "close job already recorded"
    assert close["launched"] is True and close["requeued"] is True
    assert sorted(close["queued"]) == sorted(LEGS)
    assert launches == [RID]
    legs = run_close.read_close_job(root, RID, datetime.now().astimezone())
    assert {leg: r.status for leg, r in legs.items()} == {leg: "queued" for leg in LEGS}


def test_failed_relaunch_replies_the_next_action(repo, launches, monkeypatch):
    root, sha = repo
    monkeypatch.setattr(op, "_launch", lambda root, run_id: False)
    _job(root, _past())
    close = _land(root, sha)["close"]
    assert close["launched"] is False
    assert RID in close["next_action"] and "close_background.py" in close["next_action"]


def test_fully_paid_job_stays_not_applicable_and_spawns_nothing(repo, launches):
    root, sha = repo
    _job(root, _past(), settle_all=True)
    close = _land(root, sha)["close"]
    assert close == {"status": "not-applicable", "detail": "close job already recorded"}
    assert launches == []


def test_job_inside_its_deadline_is_in_flight_is_not_relaunched(repo, launches):
    root, sha = repo
    _job(root, datetime.now().astimezone() + timedelta(minutes=5))
    close = _land(root, sha)["close"]
    assert close == {"status": "not-applicable", "detail": "close job already recorded"}
    assert launches == []


def _plan_less_halt(root: Path, sizing_plan, plan_exists: bool = True):
    (root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    if plan_exists:
        (root / "docs" / "plans" / "p.md").write_text("plan", encoding="utf-8")
    (root / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (root / "state" / "sizings" / "s.yaml").write_text(
        f"plan: {sizing_plan}\n" if sizing_plan else "route: x\n", encoding="utf-8",
    )
    d = root / "state" / "warp" / RID
    (d / "halted.json").write_text(
        json.dumps({"status": "halted", "halted": "verify-overlay-red", "sizing_rel": "state/sizings/s.yaml"}), encoding="utf-8",
    )
    return _git(root, "rev-parse", "HEAD")


def test_plan_less_commit_halt_queues_close_for_the_sizing_plan(tmp_path, launches):
    _git(tmp_path, "init", "-q")
    (tmp_path / "one").write_text("one", encoding="utf-8")
    _git(tmp_path, "add", "--", "one")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "one")
    (tmp_path / "state" / "warp" / RID).mkdir(parents=True)
    sha = _plan_less_halt(tmp_path, "docs/plans/p.md")
    close = op._handler({"run_id": RID, "sha": sha, "session_id": UUID}, tmp_path)["close"]
    assert close.get("detail") != "halt record names no plan"
    assert close["launched"] is True and "plan-reconciliation" in close["queued"]
    assert launches == [RID]


@pytest.mark.parametrize("sizing_plan, exists", [(None, True), ("docs/plans/p.md", False), ("../p.md", True)])
def test_plan_less_halt_without_a_resolvable_sizing_plan_stays_not_applicable(tmp_path, launches, sizing_plan, exists):
    _git(tmp_path, "init", "-q")
    (tmp_path / "one").write_text("one", encoding="utf-8")
    _git(tmp_path, "add", "--", "one")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "one")
    (tmp_path / "state" / "warp" / RID).mkdir(parents=True)
    sha = _plan_less_halt(tmp_path, sizing_plan, exists)
    close = op._handler({"run_id": RID, "sha": sha, "session_id": UUID}, tmp_path)["close"]
    assert close == {"status": "not-applicable", "detail": "halt record names no plan"}
    assert launches == []
