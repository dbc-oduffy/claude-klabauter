"""Tests for idle_compact.watcher.tick / loop: logging, dedupe, arming, zero-spawn, persistence."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from coordinator_core.idle_compact import contract, eligibility, watcher


class Recorder:
    def __init__(self, ok=True, reason="injected"):
        self.pids = []
        self._result = contract.InjectResult(ok, reason)

    def inject(self, pid):
        self.pids.append(pid)
        return self._result


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(b"x" * 10)
    verdicts = {}
    monkeypatch.setattr(
        eligibility, "decide",
        lambda sid, rec, tp, now, armed: verdicts.get(sid, eligibility.Decision("skip", "not-allowlisted")),
    )
    monkeypatch.setattr(watcher, "_transcript_for", lambda sid, cwd: str(transcript))
    sessions = {"s1": SimpleNamespace(pid=111, cwd="c"), "s2": SimpleNamespace(pid=222, cwd="c")}
    return SimpleNamespace(verdicts=verdicts, sessions=sessions, log=tmp_path / "log.jsonl",
                           transcript=transcript)


def _tick(e, injector, armed=None, last=None):
    return watcher.tick("run-x", injector, armed={} if armed is None else armed,
                        last_logged={} if last is None else last, now=1.0,
                        log_path=e.log, snapshot=lambda: e.sessions)


def _lines(e):
    return [json.loads(x) for x in e.log.read_text().splitlines()]


def test_inject_logs_run_id_pid_action_and_arms(env):
    env.verdicts["s1"] = eligibility.Decision("inject", "eligible")
    inj, armed = Recorder(), {}
    _tick(env, inj, armed)
    assert inj.pids == [111]
    lines = _lines(env)
    by_sid = {x["session_id"]: x for x in lines}
    assert by_sid["s1"]["action"] == "injected" and by_sid["s1"]["run_id"] == "run-x"
    assert by_sid["s1"]["pid"] == 111 and by_sid["s1"]["reason"] == "eligible"
    assert by_sid["s2"]["action"] == "skipped" and by_sid["s2"]["reason"] == "not-allowlisted"
    assert armed["s1"] == {"size": 10}


def test_unchanged_skip_logged_once_but_changed_reason_logged(env):
    inj, last = Recorder(), {}
    _tick(env, inj, last=last)
    _tick(env, inj, last=last)
    assert len(_lines(env)) == 2
    env.verdicts["s1"] = eligibility.Decision("skip", "not-idle")
    _tick(env, inj, last=last)
    assert [x["reason"] for x in _lines(env)][-1] == "not-idle"
    assert len(_lines(env)) == 3
    assert inj.pids == []


def test_failed_injection_is_a_logged_skip_and_still_arms(env):
    env.verdicts["s1"] = eligibility.Decision("inject", "eligible")
    armed = {}
    _tick(env, Recorder(False, "injector-unavailable"), armed)
    rec = next(x for x in _lines(env) if x["session_id"] == "s1")
    assert (rec["action"], rec["reason"]) == ("skipped", "injector-unavailable")
    assert "s1" in armed


def test_tick_with_no_injection_spawns_nothing(env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    _tick(env, Recorder())
    assert len(_lines(env)) == 2


def test_session_without_int_pid_is_ignored(env):
    env.sessions["s3"] = SimpleNamespace(pid=None, cwd="c")
    _tick(env, Recorder())
    assert "s3" not in {x["session_id"] for x in _lines(env)}


def test_armed_ledger_roundtrips_and_missing_is_empty(env):
    assert watcher.load_armed() == {}
    watcher.save_armed({"s1": {"size": 5}})
    assert watcher.load_armed() == {"s1": {"size": 5}}


def test_loop_survives_a_failing_tick_and_persists_armed(env, monkeypatch):
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("bad record")
        k["armed"]["s1"] = {"size": 1}
        return []

    monkeypatch.setattr(watcher, "tick", flaky)
    watcher.loop("r", Recorder(), interval=0, sleep=lambda s: None, max_ticks=2)
    assert calls["n"] == 2
    assert watcher.load_armed() == {"s1": {"size": 1}}
