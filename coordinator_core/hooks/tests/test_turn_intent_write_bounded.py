"""turn_intent._atomic_write is best-effort: a blocked replace returns False to its caller inside the
write deadline, and an unblocked write lands exactly as before."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from coordinator_core.hooks.support import turn_intent


def _join_workers() -> None:
    for t in threading.enumerate():
        if t.name.startswith("bounded-io:"):
            t.join(5.0)


def test_unblocked_write_lands_as_before(tmp_path: Path) -> None:
    assert turn_intent._atomic_write(str(tmp_path), "w.json", '{"a": 1}') is True
    assert (tmp_path / "w.json").read_text(encoding="utf-8") == '{"a": 1}'
    assert not list(tmp_path.glob(".w.json.*.tmp"))


def test_oserror_still_returns_false(tmp_path: Path) -> None:
    assert turn_intent._atomic_write(str(tmp_path / "missing"), "w.json", "{}") is False


@pytest.mark.deliberate_wall_clock(reason="bounds the caller's wait on a replace blocked for seconds")
def test_blocked_replace_returns_false_within_the_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    real = os.replace

    def blocking(src, dst, *a, **kw):
        if str(dst).startswith(str(tmp_path)):
            gate.wait(10)
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", blocking)
    try:
        assert turn_intent._atomic_write(str(tmp_path), "blocked.json", "{}") is False
        assert not gate.is_set()
        assert turn_intent._atomic_write(str(tmp_path), "blocked.json", "{}") is False
        assert not gate.is_set()
    finally:
        gate.set()
        _join_workers()
    assert (tmp_path / "blocked.json").exists()


@pytest.mark.deliberate_wall_clock(reason="a stalled target must not stall a different target")
def test_a_stalled_target_does_not_stall_another(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    real = os.replace

    def blocking(src, dst, *a, **kw):
        if str(dst).endswith("stuck.json"):
            gate.wait(10)
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", blocking)
    try:
        assert turn_intent._atomic_write(str(tmp_path), "stuck.json", "{}") is False
        assert turn_intent._atomic_write(str(tmp_path), "free.json", "{}") is True
    finally:
        gate.set()
        _join_workers()
