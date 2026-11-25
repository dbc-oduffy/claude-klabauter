"""The anchor check memoises a successful realpath per (target, lock_dir) and never a stall."""

from __future__ import annotations

import os
import threading

import pytest

from coordinator_core import locked_write
from coordinator_core.locked_write import LockAnchorError, _assert_anchor_outside_target


@pytest.fixture(autouse=True)
def _clean_memo():
    locked_write._anchor_memo.clear()
    yield
    locked_write._anchor_memo.clear()


def _counting_realpath(monkeypatch):
    calls = []
    real = os.path.realpath

    def counting(p, *a, **k):
        calls.append(str(p))
        return real(p, *a, **k)

    monkeypatch.setattr(os.path, "realpath", counting)
    return calls


def test_second_check_for_same_pair_does_not_call_realpath(tmp_path, monkeypatch):
    calls = _counting_realpath(monkeypatch)
    target, lock_dir = tmp_path / "t", tmp_path / "locks"
    _assert_anchor_outside_target(target, lock_dir)
    first = len(calls)
    assert first == 2
    _assert_anchor_outside_target(target, lock_dir)
    assert len(calls) == first


def test_memoised_pair_still_refuses_nesting(tmp_path):
    target = tmp_path / "t"
    for _ in range(2):
        with pytest.raises(LockAnchorError):
            _assert_anchor_outside_target(target, target / "locks")


def test_distinct_pair_resolves_again(tmp_path, monkeypatch):
    calls = _counting_realpath(monkeypatch)
    _assert_anchor_outside_target(tmp_path / "a", tmp_path / "locks")
    n = len(calls)
    _assert_anchor_outside_target(tmp_path / "b", tmp_path / "locks")
    assert len(calls) > n


def test_memo_is_bounded_lru(tmp_path, monkeypatch):
    monkeypatch.setattr(locked_write, "_ANCHOR_MEMO_MAX", 2)
    lock_dir = tmp_path / "locks"
    for name in ("a", "b", "c"):
        _assert_anchor_outside_target(tmp_path / name, lock_dir)
    assert len(locked_write._anchor_memo) == 2
    keys = {k[0] for k in locked_write._anchor_memo}
    assert os.path.abspath(str(tmp_path / "a")) not in keys


@pytest.mark.deliberate_wall_clock(reason="bounds the caller's wait on a realpath blocked for seconds")
def test_stalled_realpath_stays_bounded_and_is_not_memoised(tmp_path, monkeypatch, capsys):
    release = threading.Event()
    monkeypatch.setattr(os.path, "realpath", lambda p, *a, **k: release.wait(10) or p)
    target, lock_dir = tmp_path / "t", tmp_path / "locks"
    try:
        _assert_anchor_outside_target(target, lock_dir)
        assert not release.is_set()
        assert "anchor check degraded" in capsys.readouterr().err
        assert locked_write._anchor_memo == {}
    finally:
        release.set()
