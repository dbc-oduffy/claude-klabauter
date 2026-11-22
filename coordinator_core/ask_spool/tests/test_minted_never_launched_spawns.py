"""The never-launched sweep over thousands of ask dirs: process time under the 500ms bar, no spawn but the injected fire."""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from coordinator_core.ask_spool import drain
from coordinator_core.group_em import gem_home
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT

pytestmark = pytest.mark.spawns_process

NOW = 1_800_000_000.0
TOTAL = 3000
IN_WINDOW = 5
BAR_S = 0.5


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    monkeypatch.setattr(gem_home, "gem_holder", lambda: {"session_id": "gem-sess", "live": True})


def _populate(root, in_window=IN_WINDOW):
    warp = root / RECORD_DIR_ROOT
    scripts = root / RUN_DIR_ROOT
    scripts.mkdir(parents=True, exist_ok=True)
    for n in range(TOTAL):
        run_id = f"ask-20260101T{n // 60 % 24:02d}{n % 60:02d}00-{n:06x}"
        rd = warp / run_id
        rd.mkdir(parents=True)
        (rd / "run-stage.json").write_text(
            json.dumps({"stage": "minted", "session_id": "owner-sess", "started_at": "2026-01-01T00:00:00Z"}), encoding="utf-8"
        )
        recent = n < in_window
        if recent:
            (scripts / f"{run_id}.workflow.mjs").write_text("// script", encoding="utf-8")
        age = 1000 if recent else drain.MINTED_SWEEP_LOOKBACK_SECONDS + 1000
        os.utime(rd, (NOW - age, NOW - age))


class _NoSpawn:
    def __init__(self, monkeypatch):
        self.spawned = []
        for name in ("Popen", "run", "check_output", "check_call"):
            monkeypatch.setattr(subprocess, name, self._deny(name))

    def _deny(self, name):
        def refuse(*args, **kwargs):
            self.spawned.append((name, args[:1]))
            raise AssertionError(f"subprocess.{name} spawned by the sweep")

        return refuse


def _timed(root, fire):
    start = time.process_time()
    counts = drain._sweep_minted_never_launched(str(root), NOW, fire)
    return counts, time.process_time() - start


def test_a_done_backlog_cursor_sweeps_3000_dirs_under_the_bar_with_only_the_fire(tmp_path, monkeypatch):
    _populate(tmp_path)
    cursor = tmp_path / ".coordinator-local" / "ask-spool" / "never-launched-backlog.cursor"
    cursor.parent.mkdir(parents=True)
    cursor.write_text(json.dumps({"cursor": "", "done": True}), encoding="utf-8")
    guard, fired = _NoSpawn(monkeypatch), []
    counts, took = _timed(tmp_path, lambda root, rid: fired.append(rid) or {"fire_id": "f", "state": "running"})
    assert counts == {"never_launched": IN_WINDOW, "refired": IN_WINDOW, "closed": 0}
    assert len(fired) == IN_WINDOW and guard.spawned == []
    assert took < BAR_S


def test_one_full_backlog_batch_stays_under_the_bar_with_only_the_fire(tmp_path, monkeypatch):
    _populate(tmp_path)
    guard, fired = _NoSpawn(monkeypatch), []
    counts, took = _timed(tmp_path, lambda root, rid: fired.append(rid) or {"fire_id": "f", "state": "running"})
    assert counts == {"never_launched": IN_WINDOW, "refired": IN_WINDOW, "closed": drain.NEVER_LAUNCHED_BACKLOG_BATCH}
    assert guard.spawned == []
    assert took < BAR_S
