"""Interface tests for the disk-budget enforcer: Policy shape, spawn_detached, sweep_lock, and the
falsifiers for the nested-workflow prune, unbounded CLI, throttle predicate, child wall cap and the
absence of any OS scheduler entry. Fixture trees live under tmp_path only.

The CLI and hook scripts live in DoE (`coordinator/bin`, `hooks/scripts`); tests that load them skip
where they are absent.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[1]
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_LIB))
import disk_budget  # noqa: E402
from disk_budget import GiB, Policy, resolve_roots  # noqa: E402

_CLI = _ROOT / "bin" / "disk-budget-sweep.py"
_STOP_HOOK = _ROOT / "hooks" / "scripts" / "stop-disk-budget-sweep.py"
_START_HOOK = _ROOT / "hooks" / "scripts" / "session-start-disk-budget.py"

HOUR = 3600.0
MiB = 1024 * 1024


def _needs(path: Path):
    return pytest.mark.skipif(not path.is_file(), reason=f"{path.name} ships from DoE, absent here")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _file(path: Path, size: int, age_hours: float, now: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size)
    t = now - age_hours * HOUR
    os.utime(path, (t, t))
    return path


# --- Policy shape ---------------------------------------------------------------------------

def test_cache_total_cap_is_8_gib():
    assert Policy().cache_total_cap == 8 * GiB


def test_deadline_none_is_unbounded():
    assert Policy(deadline_s=None).deadline_at(100.0) == math.inf
    assert Policy(deadline_s=25).deadline_at(100.0) == 125.0
    assert Policy().deadline_s == 25


def test_from_env_still_overrides_cache_total_cap():
    assert Policy.from_env({"COORDINATOR_DISK_BUDGET_CACHE_TOTAL_CAP": "123"}).cache_total_cap == 123


# --- spawn_detached -------------------------------------------------------------------------

class _FakePopen:
    calls: list = []

    def __init__(self, argv, **kwargs):
        type(self).calls.append((argv, kwargs))
        self.pid = 4242


def test_spawn_detached_posix_starts_new_session(monkeypatch):
    _FakePopen.calls = []
    monkeypatch.setattr(disk_budget.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(disk_budget.sys, "platform", "linux")
    assert disk_budget.spawn_detached(["x", "--apply"]) == 4242
    argv, kw = _FakePopen.calls[0]
    assert argv == ["x", "--apply"]
    assert kw["start_new_session"] is True and "creationflags" not in kw
    assert kw["stdin"] == kw["stdout"] == kw["stderr"] == subprocess.DEVNULL


def test_spawn_detached_windows_uses_detached_windowless_flags(monkeypatch):
    _FakePopen.calls = []
    monkeypatch.setattr(disk_budget.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(disk_budget.sys, "platform", "win32")
    disk_budget.spawn_detached(["x"])
    _, kw = _FakePopen.calls[0]
    assert kw["creationflags"] == 0x00000008 | 0x00000200 | 0x08000000
    assert "start_new_session" not in kw


def test_spawn_detached_child_really_outlives_call(tmp_path):
    marker = tmp_path / "ran"
    pid = disk_budget.spawn_detached(
        [sys.executable, "-c", f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ok')"]
    )
    assert pid > 0
    deadline = time.time() + 15
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert marker.read_text() == "ok"


# --- sweep_lock -----------------------------------------------------------------------------

def test_sweep_lock_is_exclusive_and_released(tmp_path):
    with disk_budget.sweep_lock(tmp_path) as first:
        assert first is True
        info = json.loads((tmp_path / disk_budget.LOCK_NAME).read_text())
        assert info["pid"] == os.getpid() and "started" in info
        with disk_budget.sweep_lock(tmp_path) as second:
            assert second is False
        assert (tmp_path / disk_budget.LOCK_NAME).exists()  # loser never releases the holder's lock
    assert not (tmp_path / disk_budget.LOCK_NAME).exists()
    with disk_budget.sweep_lock(tmp_path) as again:
        assert again is True


def test_sweep_lock_takes_over_when_pid_is_gone(tmp_path):
    (tmp_path / disk_budget.LOCK_NAME).write_text(json.dumps({"pid": 2**22 + 12345, "started": time.time()}))
    with disk_budget.sweep_lock(tmp_path) as got:
        assert got is True


def test_sweep_lock_takes_over_when_older_than_twice_max_sweep(tmp_path):
    old = time.time() - 2 * disk_budget.MAX_SWEEP_S - 60
    (tmp_path / disk_budget.LOCK_NAME).write_text(json.dumps({"pid": os.getpid(), "started": old}))
    with disk_budget.sweep_lock(tmp_path) as got:
        assert got is True


def test_sweep_lock_refuses_live_young_holder_and_corrupt_lock_is_stale(tmp_path):
    (tmp_path / disk_budget.LOCK_NAME).write_text(json.dumps({"pid": os.getpid(), "started": time.time()}))
    with disk_budget.sweep_lock(tmp_path) as got:
        assert got is False
    corrupt = tmp_path / disk_budget.LOCK_NAME
    corrupt.write_text("not json")
    with disk_budget.sweep_lock(tmp_path) as got:
        assert got is False  # fresh and unreadable: the holder may be mid-write
    old = time.time() - 2 * disk_budget.LOCK_WRITE_GRACE_S
    os.utime(corrupt, (old, old))
    with disk_budget.sweep_lock(tmp_path) as got:
        assert got is True


# --- pinned falsifiers -----------------------------------------------------------------------

def test_nested_workflow_subagents_aged_30h_are_pruned(tmp_path):
    now = 1_800_000_000.0
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "-Users-x-proj"
    wf = proj / "sess-old" / "subagents" / "workflows" / "wf_abc123"
    gone = [
        _file(wf / "agent-a1.jsonl", 3 * MiB, 30, now),
        _file(wf / "agent-a1.meta.json", 1, 30, now),
        _file(wf / "agent-b2.jsonl", 3 * MiB, 30, now),
    ]
    fresh = _file(proj / "sess-old" / "subagents" / "workflows" / "wf_fresh" / "agent-c3.jsonl", 3 * MiB, 1, now)
    roots = resolve_roots(home=home)
    policy = Policy(projects_cap=1 * GiB)
    report = disk_budget.prune_projects(policy, time.monotonic() + 60, roots=roots, now=now, apply=True)
    assert report.freed_bytes >= 6 * MiB
    assert not any(p.exists() for p in gone)
    assert fresh.exists()


@_needs(_CLI)
def test_cli_runs_sweep_unbounded(monkeypatch, capsys):
    cli = _load("disk_budget_sweep_cli_under_test", _CLI)
    seen = {}

    class _Hook:
        @staticmethod
        def run_sweep(**kwargs):
            seen.update(kwargs)
            return {"freed_bytes": 0, "per_root": {}, "log": [], "still_over": False}

    monkeypatch.setattr(cli, "_load_hook", lambda: _Hook)
    assert cli.main(["--apply"]) == 0
    assert "deadline_s" in seen and seen["deadline_s"] is None


# --- throttle predicate and child cap --------------------------------------------------------

def test_child_max_wall_is_30_minutes():
    assert disk_budget.CHILD_MAX_WALL_S == 1800


def test_sweep_due_true_on_missing_or_expired_stamp_false_inside_throttle(tmp_path):
    policy = Policy()
    wall = 1_800_000_000.0
    assert disk_budget.sweep_due(tmp_path, policy, wall) is True  # missing
    stamp = tmp_path / disk_budget.STAMP_NAME
    stamp.write_text(json.dumps({"ts": wall - 7 * HOUR}))
    assert disk_budget.sweep_due(tmp_path, policy, wall) is True  # expired
    stamp.write_text(json.dumps({"ts": wall - 5 * HOUR}))
    assert disk_budget.sweep_due(tmp_path, policy, wall) is False  # inside 6 h
    stamp.write_text("not json")
    assert disk_budget.sweep_due(tmp_path, policy, wall) is True  # unreadable counts as missing


def _stop_producer(monkeypatch, tmp_path, stamp_age_s):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("COORDINATOR_DISK_BUDGET_THROTTLE_HOURS", raising=False)
    if stamp_age_s is not None:
        stamp = home / ".claude" / "disk-budget" / disk_budget.STAMP_NAME
        stamp.parent.mkdir(parents=True)
        stamp.write_text(json.dumps({"ts": time.time() - stamp_age_s}))
    calls: list = []
    monkeypatch.setattr(disk_budget, "spawn_detached", lambda argv: calls.append(list(argv)) or 1)
    mod = _load("stop_disk_budget_sweep_pin", _STOP_HOOK)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("{}"))
    assert mod.main() == 0
    return calls


@_needs(_STOP_HOOK)
def test_stop_producer_spawns_child_when_due_and_nothing_inside_throttle(monkeypatch, tmp_path):
    assert len(_stop_producer(monkeypatch, tmp_path / "due", 7 * HOUR)) == 1
    assert _stop_producer(monkeypatch, tmp_path / "fresh", 1 * HOUR) == []


@_needs(_STOP_HOOK)
def test_child_argv_carries_max_wall_s_cap(monkeypatch, tmp_path):
    (argv,) = _stop_producer(monkeypatch, tmp_path / "due", None)
    assert argv[argv.index("--max-wall-s") + 1] == str(disk_budget.CHILD_MAX_WALL_S)
    assert "--child" in argv


@_needs(_START_HOOK)
def test_child_stops_at_the_cap(monkeypatch):
    hook = _load("session_start_disk_budget_pin", _START_HOOK)
    seen = {}
    monkeypatch.setattr(hook, "run_sweep", lambda **kw: seen.update(kw))
    hook._run_child(1800.0)
    assert seen["deadline_s"] == 1800.0 and seen["ignore_throttle"] is True


# --- no OS scheduler -------------------------------------------------------------------------

_SCHEDULER_WORDS = ("launchd", "schtasks", "systemd", "crontab")
_DISK_BUDGET_WORDS = ("disk-budget", "disk_budget")


def test_no_os_scheduler_entry_for_the_sweep_outside_docs():
    repo = _ROOT.parent
    this = Path(__file__).resolve()
    offenders = []
    for top in (repo / "coordinator", repo / "setup"):
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [d for d in dirnames if d not in {"docs", "__pycache__", ".git", "node_modules"}]
            for name in filenames:
                path = Path(dirpath) / name
                if path.resolve() == this:
                    continue
                try:
                    text = path.read_text(encoding="utf-8").lower()
                except (OSError, UnicodeDecodeError):
                    continue
                if any(w in text for w in _SCHEDULER_WORDS) and any(w in text for w in _DISK_BUDGET_WORDS):
                    offenders.append(str(path.relative_to(repo)))
    assert offenders == []
