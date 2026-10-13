"""Tests for the bounded scratch read."""

from __future__ import annotations

import inspect
import os

import pytest

from coordinator_core.hooks import scratch_size_probe as probe
from coordinator_core.hooks.scratch_size_probe import ScratchRead, read_scratch


class FakeClock:
    """Returns 0.0 until call number ``past_on`` (1-based), then a time past any deadline."""

    def __init__(self, past_on: int) -> None:
        self.calls = 0
        self.past_on = past_on

    def __call__(self) -> float:
        self.calls += 1
        return 0.0 if self.calls < self.past_on else 1000.0


def _tree(base, files=3, sub="sub"):
    base.mkdir(parents=True, exist_ok=True)
    total = 0
    n = 0
    for i in range(files):
        data = b"x" * (10 + i)
        (base / f"f{i}.txt").write_bytes(data)
        total += len(data)
        n += 1
    d = base / sub
    d.mkdir()
    n += 1
    (d / "g.txt").write_bytes(b"yyyy")
    return n + 1, total + 4


def _can_symlink(tmp_path) -> bool:
    try:
        os.symlink(tmp_path, tmp_path / "probe-link", target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    os.unlink(tmp_path / "probe-link")
    return True


def test_small_tree_finishes_with_own_totals(tmp_path):
    entries, size = _tree(tmp_path / "a")
    assert read_scratch([tmp_path / "a"]) == ScratchRead(True, entries, size)


def test_missing_root_is_empty_finished(tmp_path):
    assert read_scratch([tmp_path / "nope"]) == ScratchRead(True, 0, 0)


def test_two_roots_sum_and_missing_between(tmp_path):
    e1, s1 = _tree(tmp_path / "a")
    e2, s2 = _tree(tmp_path / "b", files=2)
    want = ScratchRead(True, e1 + e2, s1 + s2)
    assert read_scratch([tmp_path / "a", tmp_path / "b"]) == want
    assert read_scratch([tmp_path / "a", tmp_path / "missing", tmp_path / "b"]) == want


def test_deadline_between_roots_keeps_first_only(tmp_path):
    e1, s1 = _tree(tmp_path / "a")
    _tree(tmp_path / "b")
    # Calls: 1 entry; 2 and 3 before root a's two directories; 4 before root b's directory.
    got = read_scratch([tmp_path / "a", tmp_path / "b"], clock=FakeClock(4))
    assert got == ScratchRead(False, e1, s1)


def test_deadline_after_first_directory_is_partial(tmp_path):
    entries, size = _tree(tmp_path / "a")
    got = read_scratch([tmp_path / "a"], clock=FakeClock(3))
    assert got.finished is False
    assert got.entries < entries
    assert got.bytes < size


def test_in_directory_deadline_stops_flat_directory(tmp_path, monkeypatch):
    flat = tmp_path / "flat"
    flat.mkdir()
    for i in range(20):
        (flat / f"f{i}").write_bytes(b"z")
    monkeypatch.setattr(probe, "DEADLINE_STRIDE_ENTRIES", 4)
    got = read_scratch([flat], clock=FakeClock(4))
    assert got.finished is False
    assert 0 < got.entries < 20


def test_clock_already_past_on_second_call_reads_nothing(tmp_path):
    _tree(tmp_path / "a")
    assert read_scratch([tmp_path / "a"], clock=FakeClock(2)) == ScratchRead(False, 0, 0)


def test_symlink_to_outside_directory_adds_nothing(tmp_path):
    if not _can_symlink(tmp_path):
        pytest.skip("host cannot create symlinks")
    outside = tmp_path / "outside"
    _tree(outside)
    root = tmp_path / "root"
    root.mkdir()
    (root / "f").write_bytes(b"12345")
    os.symlink(outside, root / "link", target_is_directory=True)
    assert read_scratch([root]) == ScratchRead(True, 1, 5)


def test_root_symlink_is_read_through(tmp_path):
    if not _can_symlink(tmp_path):
        pytest.skip("host cannot create symlinks")
    entries, size = _tree(tmp_path / "real")
    os.symlink(tmp_path / "real", tmp_path / "rootlink", target_is_directory=True)
    assert read_scratch([tmp_path / "rootlink"]) == ScratchRead(True, entries, size)


def _flat(base, n):
    base.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (base / f"f{i}").write_bytes(b"ab")


def test_tree_of_exactly_the_limit_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "WALK_ENTRY_LIMIT", 10)
    _flat(tmp_path / "a", 10)
    got = read_scratch([tmp_path / "a"])
    assert got.finished is True and got.over_entries is False and got.entries == 10


def test_tree_one_past_the_limit_stops_on_count(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "WALK_ENTRY_LIMIT", 10)
    _flat(tmp_path / "a", 11)
    got = read_scratch([tmp_path / "a"])
    assert got.finished is False and got.over_entries is True and got.entries == 10
    assert got.bytes <= 22


def test_over_limit_tree_read_twice_is_equal(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "WALK_ENTRY_LIMIT", 10)
    _flat(tmp_path / "a", 25)
    assert read_scratch([tmp_path / "a"]) == read_scratch([tmp_path / "a"])


def test_limit_is_one_count_across_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "WALK_ENTRY_LIMIT", 10)
    _flat(tmp_path / "a", 6)
    _flat(tmp_path / "b", 6)
    got = read_scratch([tmp_path / "a", tmp_path / "b"])
    assert got.finished is False and got.over_entries is True and got.entries == 10


def test_deadline_before_limit_is_not_over_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "WALK_ENTRY_LIMIT", 1000)
    monkeypatch.setattr(probe, "DEADLINE_STRIDE_ENTRIES", 4)
    _flat(tmp_path / "a", 50)
    got = read_scratch([tmp_path / "a"], clock=FakeClock(4))
    assert got.finished is False and got.over_entries is False


def test_default_clock_is_thread_time_and_source_has_no_monotonic():
    import time

    assert inspect.signature(read_scratch).parameters["clock"].default is time.thread_time
    assert "monotonic" not in inspect.getsource(probe)


def test_entry_limit_constant_is_the_plans_number():
    assert probe.WALK_ENTRY_LIMIT == 4000


def test_module_source_spawns_nothing():
    src = inspect.getsource(probe)
    for token in ("subprocess", "os.system", "os.popen", "os.spawn"):
        assert token not in src
