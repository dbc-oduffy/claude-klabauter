"""Falsifier: the per-repo stall latch is armed in exactly one place, so a plain retry inside the cooldown fails fast and only `retry_past_stall`'s bypass gets a fresh wall."""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from coordinator_core.git import git_objects as go

_ARM = re.compile(r"_PACK_STALL_UNTIL\[\w+\]\s*=(?!=)")


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def stall_env(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(go, "_pack_clock", clock)
    monkeypatch.setattr(time, "sleep", lambda *_: pytest.fail("slept"))
    go._PACK_STALL_UNTIL.clear()
    walls: list[bool] = []
    script: list[bool] = []

    def fake_run_bounded(key, work, deadline, default):
        stalls = script.pop(0) if script else False
        walls.append(stalls)
        return default if stalls else work()

    monkeypatch.setattr(go, "run_bounded", fake_run_bounded)
    yield clock, walls, script
    go._PACK_STALL_UNTIL.clear()


def _read(common: Path):
    return go._under_read_wall("pack-read", common, "a" * 40, lambda: "payload")


def test_latch_assignment_appears_exactly_once() -> None:
    src = Path(go.__file__).read_text(encoding="utf-8")
    assert len(_ARM.findall(src)) == 1


def test_plain_retry_in_cooldown_fails_fast_but_bypass_gets_fresh_wall(stall_env, tmp_path: Path) -> None:
    clock, walls, script = stall_env
    script.append(True)
    with pytest.raises(go.PackReadDeadline):
        _read(tmp_path)
    assert walls == [True]

    clock.now += go.PACK_STALL_COOLDOWN_SECS - 1
    with pytest.raises(go.PackReadDeadline):
        _read(tmp_path)
    assert walls == [True]  # failed fast: no wall was opened

    calls = []

    def fn():
        calls.append(1)
        if len(calls) == 1:
            raise go.PackReadDeadline("first")
        return _read(tmp_path)

    assert go.retry_past_stall(tmp_path, fn) == "payload"
    assert walls == [True, False]  # the bypassed retry opened a fresh wall
