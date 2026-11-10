"""Acceptance: `idle-compact-watcher.py main(["--once"])` at default thresholds with a recording injector."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.idle_compact import contract, eligibility, watcher
from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.session import harness_registry
from coordinator_core.session_baton import store
from coordinator_core.session_hub_path import session_dir_for

BIN = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "idle-compact-watcher.py"
NOW = 1_000_000.0
SID = "sid-em"


def _load_bin():
    spec = importlib.util.spec_from_file_location("idle_compact_watcher_bin", BIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Recorder:
    def __init__(self):
        self.pids = []

    def inject(self, pid):
        self.pids.append(pid)
        return contract.InjectResult(True, "injected")


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setenv("PYTHONPATH", "")
    (home / "idle-compact-allowlist.json").write_text(json.dumps(["repo-a"]), encoding="utf-8")
    hub = tmp_path / "hub"
    hub.mkdir()
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(b"x" * 100)
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    ctx = {"epoch": NOW - 400, "pct": 25.0}
    state = {"fire_turn": 3}

    monkeypatch.setattr(contract, "optout_marker_path", lambda sid, cwd=None: hub / "optout")
    monkeypatch.setattr(contract, "compact_hold_marker_path", lambda sid, cwd=None: hub / "hold")
    monkeypatch.setattr(eligibility.em_turn_count, "read_state", lambda r, s, t: state)
    monkeypatch.setattr(eligibility, "transcript_activity_epoch", lambda s, c: (ctx["epoch"], True))
    monkeypatch.setattr(
        eligibility.context_usage_sidecar, "read_usage",
        lambda sid, now, transcript_path=None: SimpleNamespace(
            context_window={"used_percentage": ctx["pct"]}, age_seconds=0),
    )
    monkeypatch.setattr(eligibility, "_sentinel_dir", lambda: str(sentinels))
    monkeypatch.setattr(watcher, "_transcript_for", lambda sid, cwd: str(transcript))
    record = SimpleNamespace(pid=4242, cwd=str(tmp_path / "repo-a"), name="repo-a-1")
    monkeypatch.setattr(harness_registry, "snapshot", lambda: {SID: record})
    monkeypatch.setattr(time, "time", lambda: NOW)
    recorder = Recorder()
    monkeypatch.setattr(watcher, "make_injector", lambda: recorder)

    sdir = session_dir_for(tmp_path, SID)
    sdir.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: session_dir_for(tmp_path, s))
    return SimpleNamespace(hub=hub, transcript=transcript, ctx=ctx, state=state, recorder=recorder,
                           home=home, sdir=sdir, bin=_load_bin())


def _log(e):
    path = contract.injection_log_path()
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_eligible_session_injected_once_per_idle_stretch_and_rearms(env):
    assert env.bin.main(["--once"]) == 0
    assert env.recorder.pids == [4242]
    first = _log(env)[0]
    assert first["action"] == "injected" and first["session_id"] == SID and first["pid"] == 4242
    assert len(first["run_id"]) == 32 and first["reason"] == "eligible"

    assert env.bin.main(["--once"]) == 0
    assert env.recorder.pids == [4242]
    last = _log(env)[-1]
    assert (last["action"], last["reason"]) == ("skipped", "already-injected-this-stretch")

    env.transcript.write_bytes(b"x" * 500)
    (Path(eligibility._sentinel_dir()) / f"compaction-occurred-{SID}").write_text("100")
    assert env.bin.main(["--once"]) == 0
    assert env.recorder.pids == [4242, 4242]


def test_injection_marks_the_pm_turn_and_verify_ignores_it(env):
    env.bin.main(["--once"])
    PT.append_turn("/compact", session_id=SID)
    PT.append_turn("real pm words", session_id=SID)
    lines = [json.loads(x) for x in (env.sdir / PT.PM_TURNS_FILENAME).read_text().splitlines()]
    assert lines[0]["injected"]["by"] == "idle-compact-watcher"
    assert lines[0]["injected"]["run_id"] == _log(env)[0]["run_id"]
    assert "injected" not in lines[1]
    assert [t["prompt"] for t in PT.read_turns(session_id=SID, all_turns=True)["turns"]] == ["real pm words"]


@pytest.mark.parametrize("marker,reason", [("optout", "opted-out"), ("hold", "compact-hold")])
def test_opted_out_and_held_sessions_are_skipped_with_reason(env, marker, reason):
    (env.hub / marker).write_text("")
    env.bin.main(["--once"])
    assert env.recorder.pids == []
    assert (_log(env)[0]["action"], _log(env)[0]["reason"]) == ("skipped", reason)


def test_not_allowlisted_is_skipped(env):
    (env.home / "idle-compact-allowlist.json").unlink()
    env.bin.main(["--once"])
    assert env.recorder.pids == [] and _log(env)[0]["reason"] == "not-allowlisted"


def test_precompact_blocked_after_injection_is_skipped(env):
    env.bin.main(["--once"])
    env.transcript.write_bytes(b"x" * 500)
    env.bin.main(["--once"])
    assert env.recorder.pids == [4242]
    assert _log(env)[-1]["reason"] == "precompact-blocked"


def test_session_that_never_fired_an_engine_ask_is_skipped(env):
    env.state["fire_turn"] = None
    env.bin.main(["--once"])
    assert env.recorder.pids == [] and _log(env)[0]["reason"] == "no-engine-ask"


def test_one_tick_under_500ms_process_time_and_idle_tick_spawns_nothing(env, monkeypatch):
    t0 = time.process_time()
    env.bin.main(["--once"])
    assert time.process_time() - t0 < 0.5

    def boom(*a, **k):
        raise AssertionError("spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    env.ctx["pct"] = 5.0
    (env.hub / "optout").write_text("")
    env.bin.main(["--once"])
