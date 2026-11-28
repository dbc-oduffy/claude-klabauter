"""warp_live.live_run_for_session: the hook's lookup cost is independent of the session's launch-record
and token counts, and a launch record is read once per change."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from coordinator_core import launch_records, warp_live
from coordinator_core.ops.dispatch_emit import run_token

SESSION = "s1"
RECORDS = 2000
TOKENS = 300
HOOK_BAR_S = 0.05
PAST = time.time() - 3600


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    warp_live._NOT_LIVE.clear()
    warp_live._SESSION_INDEX.clear()
    yield
    warp_live._NOT_LIVE.clear()
    warp_live._SESSION_INDEX.clear()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture
def managed(tmp_path, monkeypatch):
    managed_dir = tmp_path / "managed"
    legacy = tmp_path / "legacy"
    managed_dir.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed_dir)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: legacy / name)
    return managed_dir


@pytest.fixture
def launch_reads(managed, monkeypatch):
    """Paths `_read_json` is asked for under the launch dir."""
    seen: list[str] = []
    real = warp_live._read_json

    def spy(path):
        if str(path).startswith(str(managed)):
            seen.append(str(path))
        return real(path)

    monkeypatch.setattr(warp_live, "_read_json", spy)
    return seen


def _launch(managed, task_id, script, *, age=True):
    path = Path(launch_records.record_path(str(managed), SESSION, task_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"scriptPath": script, "fired_at": 1.0}), encoding="utf-8")
    if age:
        os.utime(path, (PAST, PAST))
    return path


def _age_dir(managed, bump=0):
    """Stamp the session dir an hour old; `bump` seconds apart from an earlier stamp so its stat differs."""
    directory = launch_records.session_dir(str(managed), SESSION)
    os.utime(directory, (PAST + bump, PAST + bump))


def _session_with_history(repo, managed, *, records=RECORDS, tokens=TOKENS):
    for n in range(records):
        _launch(managed, f"t{n}", f"scratch/other/{n % 50}.mjs")
    _age_dir(managed)
    for n in range(tokens):
        run_token.mint(repo, f"r{n}", f"scratch/warp/t{n}.mjs", session_id=SESSION, sizing_rel=None)


def test_lookup_is_bounded_by_neither_records_nor_tokens(repo, managed, launch_reads):
    _session_with_history(repo, managed)
    assert warp_live.live_run_for_session(repo, SESSION) is None
    assert len(launch_reads) <= RECORDS

    _launch(managed, "tnew", "scratch/other/new.mjs")
    _age_dir(managed, bump=10)
    launch_reads.clear()
    assert warp_live.live_run_for_session(repo, SESSION) is None
    assert launch_reads == [launch_records.record_path(str(managed), SESSION, "tnew")]

    launch_reads.clear()
    timings = []
    for _ in range(3):
        started = time.process_time()
        assert warp_live.live_run_for_session(repo, SESSION) is None
        timings.append(time.process_time() - started)
    assert launch_reads == []
    assert min(timings) < HOOK_BAR_S


def test_a_new_launch_costs_one_record_read_not_tokens_times_records(repo, managed, launch_reads):
    _session_with_history(repo, managed, records=300, tokens=60)
    warp_live.live_run_for_session(repo, SESSION)
    launch_reads.clear()
    _launch(managed, "tfresh", "scratch/other/fresh.mjs")
    _age_dir(managed, bump=10)
    warp_live.live_run_for_session(repo, SESSION)
    assert len(launch_reads) == 1


def test_the_index_answers_what_a_full_read_answers(repo, managed):
    script = "scratch/warp/x.mjs"
    _launch(managed, "a", script)
    _launch(managed, "b", "scratch/warp/./x.mjs")
    _launch(managed, "c", "scratch/warp/y.mjs")
    directory = launch_records.session_dir(str(managed), SESSION)
    Path(directory, "junk.json").write_text("not json", encoding="utf-8")
    dirs = warp_live._launch_dirs(None)
    snapshot = warp_live.LaunchSnapshot(SESSION, dirs)
    assert warp_live._launch_records(SESSION, script, dirs) == warp_live._launch_records(
        SESSION, script, dirs, snapshot
    )
    assert [t for t, _ in warp_live._launch_records(SESSION, script, dirs)] == ["a", "b"]
    assert warp_live._launch_records(SESSION, "", dirs) == []


def test_a_record_rewritten_under_the_same_name_is_seen(repo, managed):
    script = "scratch/warp/x.mjs"
    path = _launch(managed, "a", "scratch/warp/other.mjs")
    _age_dir(managed)
    dirs = warp_live._launch_dirs(None)
    assert warp_live._launch_records(SESSION, script, dirs) == []
    path.write_text(json.dumps({"scriptPath": script, "fired_at": 2.0, "x": 1}), encoding="utf-8")
    os.utime(path, (PAST + 5, PAST + 5))
    os.utime(path.parent, (PAST + 5, PAST + 5))
    assert [t for t, _ in warp_live._launch_records(SESSION, script, dirs)] == ["a"]


def test_a_record_written_in_the_same_tick_as_the_scan_is_not_missed(repo, managed):
    script = "scratch/warp/x.mjs"
    first = Path(launch_records.record_path(str(managed), SESSION, "a"))
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text(json.dumps({"scriptPath": script}), encoding="utf-8")
    dirs = warp_live._launch_dirs(None)
    assert [t for t, _ in warp_live._launch_records(SESSION, script, dirs)] == ["a"]
    stamp = first.parent.stat().st_mtime_ns
    _launch(managed, "b", script, age=False)
    os.utime(first.parent, ns=(stamp, stamp))
    assert [t for t, _ in warp_live._launch_records(SESSION, script, dirs)] == ["a", "b"]


def test_a_removed_launch_dir_empties_the_answer(repo, managed):
    script = "scratch/warp/x.mjs"
    path = _launch(managed, "a", script)
    dirs = warp_live._launch_dirs(None)
    assert warp_live._launch_records(SESSION, script, dirs)
    path.unlink()
    path.parent.rmdir()
    assert warp_live._launch_records(SESSION, script, dirs) == []
    assert not warp_live._SESSION_INDEX


def test_the_index_is_bounded(repo, managed, monkeypatch):
    monkeypatch.setattr(warp_live, "_SESSION_INDEX_MAX", 2)
    for n in range(4):
        path = Path(launch_records.record_path(str(managed), f"s{n}", "t"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        warp_live._launch_records(f"s{n}", "x", [str(managed)])
    assert len(warp_live._SESSION_INDEX) == 2
