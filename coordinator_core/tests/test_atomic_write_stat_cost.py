"""atomic_write_bytes: one stat of the target, no per-write scandir, dead-temp sweep still reaps."""

import os
import stat as stat_mod
from pathlib import Path

from coordinator_core import atomic_replace
from coordinator_core.atomic_replace import atomic_write_bytes


def _count(monkeypatch, name):
    calls = []
    real = getattr(os, name)

    def spy(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(os, name, spy)
    return calls


def test_warm_write_one_target_stat_no_scandir(tmp_path, monkeypatch):
    target = tmp_path / "out.txt"
    atomic_write_bytes(target, b"1")
    stats = _count(monkeypatch, "stat")
    scans = _count(monkeypatch, "scandir")
    atomic_write_bytes(target, b"2")
    assert target.read_bytes() == b"2"
    assert sum(1 for a in stats if Path(str(a[0])) == target) <= 1
    assert not scans


def test_missing_target_and_missing_dir_still_written(tmp_path):
    target = tmp_path / "a" / "b" / "out.txt"
    atomic_write_bytes(target, b"x")
    assert target.read_bytes() == b"x"


def test_directory_target_mode_not_captured(tmp_path):
    (tmp_path / "d").mkdir()
    try:
        atomic_write_bytes(tmp_path / "d", b"x")
    except OSError:
        pass
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".atomic-write.")]


def test_mode_preserved_on_posix(tmp_path):
    if os.name == "nt":
        return
    target = tmp_path / "out.txt"
    target.write_bytes(b"1")
    target.chmod(0o640)
    atomic_write_bytes(target, b"2")
    assert stat_mod.S_IMODE(target.stat().st_mode) == 0o640


def test_sweep_reaps_dead_temp_after_interval_and_spares_live(tmp_path, monkeypatch):
    dead = tmp_path / ".atomic-write.other.txt.999999.abc"
    live = tmp_path / f".atomic-write.other.txt.{os.getpid()}.abc"
    dead.write_bytes(b"d")
    live.write_bytes(b"l")
    monkeypatch.setattr(atomic_replace, "_pid_alive", lambda pid: pid == os.getpid())
    atomic_replace._last_sweep.clear()
    atomic_write_bytes(tmp_path / "out.txt", b"1")
    assert not dead.exists() and live.exists()

    later = tmp_path / ".atomic-write.other.txt.999998.abc"
    later.write_bytes(b"d")
    atomic_write_bytes(tmp_path / "out.txt", b"2")
    assert later.exists()
    atomic_replace._last_sweep[str(tmp_path)] -= atomic_replace._SWEEP_INTERVAL_SECS + 1
    atomic_write_bytes(tmp_path / "out.txt", b"3")
    assert not later.exists()
