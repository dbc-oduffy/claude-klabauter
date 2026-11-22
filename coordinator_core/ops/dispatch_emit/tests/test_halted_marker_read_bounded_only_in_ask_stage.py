"""A stalled halt-marker read fails only `dispatch.ask_stage`, which replies indeterminate; every other
reader waits it out. Witness: land_run (59a3f5) and review prep (250289) failed -32603 on 2026-11-22
because one unrelated run's halted.json read stalled past 250ms."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from coordinator_core import bounded_io
from coordinator_core.ops.dispatch_emit import halted_run


@pytest.fixture
def slow_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A halted.json whose bounded read stalls past the bound, then completes."""
    rec = tmp_path / "run"
    rec.mkdir()
    (rec / halted_run.HALTED_FILE).write_text(json.dumps({"halted": "commit"}), encoding="utf-8")
    real = bounded_io._read_registered
    release = threading.Event()

    def slow(path: str):
        release.wait(bounded_io.READ_DEADLINE_SECS * 3)
        return real(path)

    monkeypatch.setattr(bounded_io, "_read_registered", slow)
    yield rec
    release.set()


def test_a_slow_foreign_marker_is_read_not_failed_outside_ask_stage(slow_marker: Path) -> None:
    assert halted_run.read_strict(slow_marker) == {"halted": "commit"}


def test_inside_bounded_marker_reads_a_stall_is_indeterminate(slow_marker: Path) -> None:
    with halted_run.bounded_marker_reads(), pytest.raises(bounded_io.IoIndeterminate):
        halted_run.read_strict(slow_marker)
    assert halted_run._BOUNDED_MARKER_READS.get() is False
