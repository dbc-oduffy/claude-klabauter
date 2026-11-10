"""retry_past_stall: one latch-bypassing retry of a read-only call, then PackReadStalled; the halt kind is mechanical."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from coordinator_core.git import git_objects as go
from coordinator_core.ops.dispatch_emit import halt_remedy_class as hrc


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def stall_env(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(go, "_pack_clock", clock)
    monkeypatch.setattr(time, "sleep", lambda *_: pytest.fail("retry_past_stall slept"))
    go._PACK_STALL_UNTIL.clear()
    walls: list[bool] = []
    script: list[bool] = []  # True = the next wall stalls

    def fake_run_bounded(key, work, deadline, default):
        stalls = script.pop(0) if script else False
        walls.append(stalls)
        return default if stalls else work()

    monkeypatch.setattr(go, "run_bounded", fake_run_bounded)
    yield clock, walls, script
    go._PACK_STALL_UNTIL.clear()


def _read(common: Path):
    return go._under_read_wall("pack-read", common, "a" * 40, lambda: "payload")


def test_single_stall_is_absorbed_and_latch_stays_armed(stall_env, tmp_path: Path) -> None:
    _, walls, script = stall_env
    script.append(True)
    assert go.retry_past_stall(tmp_path, lambda: _read(tmp_path)) == "payload"
    assert walls == [True, False]
    with pytest.raises(go.PackReadDeadline):  # another caller still fails fast
        _read(tmp_path)
    assert walls == [True, False]


def test_persisting_stall_raises_pack_read_stalled(stall_env, tmp_path: Path) -> None:
    _, walls, script = stall_env
    script.extend([True, True])
    calls = []

    def fn():
        calls.append(1)
        return _read(tmp_path)

    with pytest.raises(go.PackReadStalled):
        go.retry_past_stall(tmp_path, fn)
    assert len(calls) == 2 and walls == [True, True]
    assert issubclass(go.PackReadStalled, go.PackReadDeadline)


def test_bypass_is_scoped_to_the_retrying_call_and_common_dir(stall_env, tmp_path: Path) -> None:
    clock, walls, script = stall_env
    script.append(True)
    other = tmp_path / "other"
    seen = []

    def fn():
        try:
            return _read(tmp_path)
        except go.PackReadDeadline:
            seen.append("first")
            raise

    go.retry_past_stall(tmp_path, fn)
    assert go._PACK_LATCH_BYPASS.get() == frozenset()
    go._PACK_STALL_UNTIL[str(other)] = clock.now + 10
    with pytest.raises(go.PackReadDeadline):
        go.retry_past_stall(tmp_path, lambda: (_ for _ in ()).throw(go.PackReadDeadline("x")))
    with pytest.raises(go.PackReadDeadline):
        _read(other)


def test_nested_retry_does_not_retry_again(stall_env, tmp_path: Path) -> None:
    _, _, script = stall_env
    script.extend([True, True, True, True])
    with pytest.raises(go.PackReadStalled):
        go.retry_past_stall(tmp_path, lambda: go.retry_past_stall(tmp_path, lambda: _read(tmp_path)))


def test_healthy_call_runs_once(tmp_path: Path) -> None:
    calls = []
    assert go.retry_past_stall(tmp_path, lambda: calls.append(1) or "ok") == "ok"
    assert calls == [1]


def test_non_deadline_error_propagates_unretried(tmp_path: Path) -> None:
    calls = []

    def fn():
        calls.append(1)
        raise ValueError("boom")

    with pytest.raises(ValueError):
        go.retry_past_stall(tmp_path, fn)
    assert calls == [1]


def test_pack_read_stall_kind_is_mechanical_refire() -> None:
    assert hrc.PACK_READ_STALL_HALT_KIND == "pack-read-stall"
    assert hrc.HALT_REMEDY["pack-read-stall"] == (hrc.MECHANICAL, "refire-after-pack-read-stall")
    assert hrc.is_mechanical("pack-read-stall")
