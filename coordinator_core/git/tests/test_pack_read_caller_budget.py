"""read_budget: one absolute caller deadline for every read in scope; expiry never arms the repo-wide stall latch."""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.git import git_objects as go


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    clock = _Clock()
    monkeypatch.setattr(go, "_pack_clock", clock)
    go._PACK_STALL_UNTIL.clear()
    walls: list[float] = []
    stall: list[bool] = []

    def fake_run_bounded(key, work, deadline, default):
        walls.append(deadline)
        return default if (stall.pop(0) if stall else False) else work()

    monkeypatch.setattr(go, "run_bounded", fake_run_bounded)
    yield clock, walls, stall
    go._PACK_STALL_UNTIL.clear()


def _read(common: Path):
    return go._under_read_wall("pack-read", common, "a" * 40, lambda: "payload")


def test_no_budget_wall_is_unchanged(env, tmp_path: Path) -> None:
    _, walls, _ = env
    assert _read(tmp_path) == "payload"
    assert walls == [go.PACK_READ_DEADLINE_SECS]


def test_budget_caps_wall_below_the_constant(env, tmp_path: Path) -> None:
    _, walls, _ = env
    with go.read_budget(0.3):
        _read(tmp_path)
    assert walls == [pytest.approx(0.3)]
    assert go._READ_BUDGET.get() is None


def test_budget_is_one_deadline_for_the_whole_scope(env, tmp_path: Path) -> None:
    clock, walls, _ = env
    with go.read_budget(0.4):
        _read(tmp_path)
        clock.now += 0.3
        _read(tmp_path)
        assert walls == [pytest.approx(0.4), pytest.approx(0.1)]
        clock.now += 0.2
        with pytest.raises(go.PackReadDeadline):
            _read(tmp_path)
    assert len(walls) == 2


def test_larger_budget_never_exceeds_the_constant(env, tmp_path: Path) -> None:
    _, walls, _ = env
    with go.read_budget(60.0):
        _read(tmp_path)
    assert walls == [go.PACK_READ_DEADLINE_SECS]


def test_nested_budget_keeps_the_earlier_deadline(env, tmp_path: Path) -> None:
    _, walls, _ = env
    with go.read_budget(0.2):
        with go.read_budget(5.0):
            _read(tmp_path)
    assert walls == [pytest.approx(0.2)]


def test_budget_expiry_raises_without_arming_the_stall_latch(env, tmp_path: Path) -> None:
    _, walls, stall = env
    stall.append(True)
    with go.read_budget(0.3):
        with pytest.raises(go.PackReadDeadline):
            _read(tmp_path)
    assert go._PACK_STALL_UNTIL == {}
    assert _read(tmp_path) == "payload"  # another op's read is not failed fast
    assert walls == [pytest.approx(0.3), go.PACK_READ_DEADLINE_SECS]


def test_full_wall_stall_still_arms_the_latch_with_or_without_budget(env, tmp_path: Path) -> None:
    _, _, stall = env
    stall.append(True)
    with pytest.raises(go.PackReadDeadline):
        _read(tmp_path)
    assert str(tmp_path) in go._PACK_STALL_UNTIL
    go._PACK_STALL_UNTIL.clear()
    stall.append(True)
    with go.read_budget(60.0):
        with pytest.raises(go.PackReadDeadline):
            _read(tmp_path)
    assert str(tmp_path) in go._PACK_STALL_UNTIL


def test_budgeted_pack_read_has_no_reopen_retry(env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clock, _, _ = env
    reopened: list[int] = []
    attempts: list[float] = []

    def at(*_a):
        attempts.append(go._PACK_DEADLINE.at)
        raise go.PackReadDeadline("slow")

    monkeypatch.setattr(go, "_read_pack_object_at", at)
    monkeypatch.setattr(go, "_read_pack_bytes", lambda p: reopened.append(1) or b"")
    monkeypatch.setattr(go, "_drop_scoped_pack_map", lambda p: None)
    with go.read_budget(0.3):
        with pytest.raises(go.PackReadDeadline):
            go._read_pack_object_deadlined(tmp_path, tmp_path / "p.pack", b"", 0)
    assert attempts == [pytest.approx(clock.now + 0.3)] and reopened == []
    assert go._PACK_DEADLINE.at is None

    attempts.clear()
    with pytest.raises(go.PackReadDeadline):  # no budget: one re-open retry, as before
        go._read_pack_object_deadlined(tmp_path, tmp_path / "p.pack", b"", 0)
    assert len(attempts) == 2 and reopened == [1]
    assert attempts[0] == pytest.approx(clock.now + go.PACK_READ_DEADLINE_SECS)
