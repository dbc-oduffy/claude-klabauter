"""export_lifecycle: the cap refuses before a build and the sweep spares live and parked runs."""
from __future__ import annotations

import json
import subprocess
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import export_lifecycle as el
from coordinator_core.ops.dispatch_emit.landing_export import LandingExportError

Entry = namedtuple("Entry", "mode size mtime mtime_nsec")


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.delenv(el.CAP_ENV, raising=False)


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def _export(root: Path, run: str, *names: str) -> list[Path]:
    out = []
    for n in names or ("export",):
        d = root / "scratch" / "warp" / run / n
        d.mkdir(parents=True)
        (d / "f.txt").write_text("x")
        out.append(d)
    return out


def _record(root: Path, run: str) -> Path:
    d = root / "state" / "warp" / run
    d.mkdir(parents=True)
    return d


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_sweep_removes_unreferenced_spares_parked_live_and_run_base(tmp_path):
    root = tmp_path
    (orphan,) = _export(root, "orphan")
    landed_export, = _export(root, "landed")
    _write(_record(root, "landed") / "halted.json", {"status": "landed"})
    parked, = _export(root, "parked")
    _write(_record(root, "parked") / "halted.json", {"status": "halted"})
    live, = _export(root, "live")
    _write(root / ".coordinator-local" / "retest" / "live.json",
           {"run_id": "live", "status": "running", "deadline": _iso(timedelta(hours=1))})
    expired, = _export(root, "expired")
    _write(root / ".coordinator-local" / "retest" / "expired.json",
           {"run_id": "expired", "status": "running", "deadline": _iso(-timedelta(hours=1))})
    mine, = _export(root, "mine")
    base = root / "scratch" / "warp" / "orphan" / "run-base"
    base.mkdir()
    (base / "keep").write_text("x")

    removed = el.sweep_orphan_exports(root, keep_run="mine")

    assert not orphan.exists() and not landed_export.exists() and not expired.exists()
    assert parked.exists() and live.exists() and mine.exists()
    assert (base / "keep").exists()
    assert sorted(removed) == sorted(str(p) for p in (orphan, landed_export, expired))


def test_sweep_removes_all_three_export_names(tmp_path):
    dirs = _export(tmp_path, "gone", *el.EXPORT_DIR_NAMES)
    el.sweep_orphan_exports(tmp_path)
    assert not any(d.exists() for d in dirs)


def test_sweep_spares_active_record_dir_and_removes_stale_one(tmp_path):
    fresh, = _export(tmp_path, "fresh")
    _write(_record(tmp_path, "fresh") / "run-stage.json", {"last_activity_at": _iso(-timedelta(minutes=5))})
    stale, = _export(tmp_path, "stale")
    _write(_record(tmp_path, "stale") / "run-stage.json", {"last_activity_at": _iso(-timedelta(days=3))})
    el.sweep_orphan_exports(tmp_path)
    assert fresh.exists() and not stale.exists()


def test_sweep_spares_when_predicate_unreadable(tmp_path):
    bad_marker, = _export(tmp_path, "badmarker")
    (_record(tmp_path, "badmarker") / "halted.json").write_text("{not json")
    bad_retest, = _export(tmp_path, "badretest")
    (tmp_path / ".coordinator-local" / "retest").mkdir(parents=True)
    (tmp_path / ".coordinator-local" / "retest" / "badretest.json").write_text("nope")
    no_stage, = _export(tmp_path, "nostage")
    _record(tmp_path, "nostage")
    el.sweep_orphan_exports(tmp_path)
    assert bad_marker.exists() and bad_retest.exists() and no_stage.exists()


def test_sweep_removes_dead_pid_test_base_only(tmp_path, monkeypatch):
    import coordinator_core.group_em.session_registry as sr

    monkeypatch.setattr(sr, "pid_alive", lambda pid: pid == 111)
    dead = tmp_path / "scratch" / "test-base-abcdef12-222"
    live = tmp_path / "scratch" / "test-base-abcdef12-111"
    other = tmp_path / "scratch" / "keepme"
    for d in (dead, live, other):
        d.mkdir(parents=True)
    el.sweep_orphan_exports(tmp_path)
    assert not dead.exists() and live.exists() and other.exists()


def test_sweep_without_scratch_is_empty(tmp_path):
    assert el.sweep_orphan_exports(tmp_path) == []


def test_cap_refuses_over_cap_and_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("coordinator_core.git.git_index.parse_index_stat",
                        lambda r: {"a": Entry(0, 600, 0, 0), "b": Entry(0, 500, 0, 0)})
    monkeypatch.setenv(el.CAP_ENV, "1000")
    with pytest.raises(LandingExportError, match=r"1100.*1000"):
        el.check_export_cap(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_cap_admits_under_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("coordinator_core.git.git_index.parse_index_stat", lambda r: {"a": Entry(0, 10, 0, 0)})
    monkeypatch.setenv(el.CAP_ENV, "1000")
    el.check_export_cap(tmp_path)


def test_cap_refuses_when_free_disk_under_twice_estimate(tmp_path, monkeypatch):
    monkeypatch.setattr("coordinator_core.git.git_index.parse_index_stat", lambda r: {"a": Entry(0, 400, 0, 0)})
    monkeypatch.setenv(el.CAP_ENV, "1000")
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(el.shutil, "disk_usage", lambda p: Usage(10, 0, 799))
    with pytest.raises(LandingExportError, match="free disk 799"):
        el.check_export_cap(tmp_path)
    monkeypatch.setattr(el.shutil, "disk_usage", lambda p: Usage(10, 0, 800))
    el.check_export_cap(tmp_path)


def test_unreadable_estimate_falls_back_to_disk_free(tmp_path, monkeypatch):
    def bad(r):
        raise ValueError("v4")

    monkeypatch.setattr("coordinator_core.git.git_index.parse_index_stat", bad)
    assert el.estimate_head_bytes(tmp_path) is None
    monkeypatch.setenv(el.CAP_ENV, "1000")
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(el.shutil, "disk_usage", lambda p: Usage(10, 0, 1999))
    with pytest.raises(LandingExportError, match="unknown"):
        el.check_export_cap(tmp_path)
    monkeypatch.setattr(el.shutil, "disk_usage", lambda p: Usage(10, 0, 2000))
    el.check_export_cap(tmp_path)


def test_estimate_none_without_index(tmp_path):
    assert el.estimate_head_bytes(tmp_path) is None


def test_bad_env_cap_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(el.CAP_ENV, "junk")
    assert el._cap_bytes() == el.EXPORT_SIZE_CAP_BYTES == 2 * 1024**3
