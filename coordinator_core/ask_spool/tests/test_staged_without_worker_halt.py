"""A run staged with no plan start, no phase and no live worker is halted `staged_without_worker` once silent past the bound."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import pytest

from coordinator_core.ask_spool import drain
from coordinator_core.ask_spool.tests.test_drain import _notices
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import PLAN_STARTED_FIELD, RECORD_DIR_ROOT, RUN_DIR_ROOT, write_run_stage_at

BOUND = drain.MINTED_DISPATCH_DEADLINE_SECONDS
RUN = "ask-20261122T100000-aaaaaa"


@pytest.fixture(autouse=True)
def _isolated_settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))


def _staged(root, session="sess-a", *, plan_started=False):
    rec = root / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    write_run_stage_at(rec, "staged", session_id=session)
    if plan_started:
        path = rec / "run-stage.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data[PLAN_STARTED_FIELD] = data["started_at"]
        path.write_text(json.dumps(data), encoding="utf-8")
    return rec


def _marker(root):
    path = root / RECORD_DIR_ROOT / RUN / "halted.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _halts(root, session):
    return [n for n in _notices(root, session) if n.get("status") == "halted"]


def _age(rec, seconds):
    path = rec / "run-stage.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    stamp = datetime.fromtimestamp(time.time() - seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["started_at"] = data["last_activity_at"] = stamp
    path.write_text(json.dumps(data), encoding="utf-8")
    then = time.time() - seconds
    for p in (*rec.iterdir(), rec):
        os.utime(p, (then, then))


def test_staged_no_worker_past_bound_is_halted_and_noticed(tmp_path, monkeypatch):
    rec = _staged(tmp_path)
    _age(rec, BOUND + 60)
    monkeypatch.setattr(run_token, "is_live", lambda root, run_id: False)

    drain.run(str(tmp_path))

    marker = _marker(tmp_path)
    assert marker["status"] == "halted" and marker["halted"] == drain.STAGED_WITHOUT_WORKER
    assert "staged with no live worker" in marker["reason"]
    (halt,) = _halts(tmp_path, "sess-a")
    assert halt["run_id"] == RUN and halt["reason"] == marker["reason"]


def test_a_live_token_leaves_it_untouched(tmp_path, monkeypatch):
    rec = _staged(tmp_path)
    _age(rec, BOUND + 60)
    monkeypatch.setattr(run_token, "is_live", lambda root, run_id: True)

    drain.run(str(tmp_path))

    assert _marker(tmp_path) is None
    assert _halts(tmp_path, "sess-a") == []


def test_activity_inside_the_bound_leaves_it_untouched(tmp_path, monkeypatch):
    rec = _staged(tmp_path)
    _age(rec, BOUND - 120)
    monkeypatch.setattr(run_token, "is_live", lambda root, run_id: False)

    drain.run(str(tmp_path), now=time.time() + 60)

    assert _marker(tmp_path) is None


def test_a_plan_started_run_keeps_the_four_hour_bound(tmp_path, monkeypatch):
    rec = _staged(tmp_path, plan_started=True)
    _age(rec, BOUND + 60)
    monkeypatch.setattr(run_token, "is_live", lambda root, run_id: False)

    drain.run(str(tmp_path))

    assert _marker(tmp_path) is None


def test_a_built_run_with_a_run_dir_and_no_token_is_left_for_the_land(tmp_path, monkeypatch):
    """A `staged` run whose run dir holds artifacts is built and awaiting land, not workerless."""
    rec = _staged(tmp_path)
    run_dir = tmp_path / RUN_DIR_ROOT / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
    _age(rec, BOUND + 60)
    monkeypatch.setattr(run_token, "is_live", lambda root, run_id: False)

    drain.run(str(tmp_path))

    assert _marker(tmp_path) is None
    assert _halts(tmp_path, "sess-a") == []
