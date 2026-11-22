"""A stalled hot read (halted.json, returns.json) makes dispatch.ask_stage reply a typed indeterminate."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from coordinator_core import bounded_io
from coordinator_core.ops.dispatch_emit import ask_stage, row_returns
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_PARKED,
    STOP_AFTER_KEY,
    STOP_AFTER_PLAN,
    STOP_AFTER_FILE,
    record_dir,
)

RUN = "warp-2026-11-22-x-aaaaaa-20261122T000000-bbbbbb"


@pytest.fixture
def blocked_open(monkeypatch: pytest.MonkeyPatch):
    """Reads of a path ending in a name in `stalled` block until released; others read normally."""
    release = threading.Event()
    stalled: set[str] = set()
    real = bounded_io._read_registered

    def fake(path: str):
        if Path(path).name in stalled:
            release.wait(10)
        return real(path)

    monkeypatch.setattr(bounded_io, "_read_registered", fake)
    yield stalled
    release.set()


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    rec = record_dir(tmp_path, RUN)
    rec.mkdir(parents=True)
    (rec / STOP_AFTER_FILE).write_text(json.dumps({STOP_AFTER_KEY: STOP_AFTER_PLAN}), encoding="utf-8")
    return rec


def _fire(tmp_path: Path) -> dict:
    return ask_stage._handler({"run_id": RUN}, repo_root=tmp_path / ".git")


def test_stalled_halt_marker_replies_typed_indeterminate(tmp_path: Path, blocked_open: set) -> None:
    rec = _repo(tmp_path)
    (rec / "halted.json").write_text(json.dumps({"halted": HALT_PARKED}), encoding="utf-8")
    blocked_open.add("halted.json")
    t0 = time.process_time()
    reply = _fire(tmp_path)
    assert time.process_time() - t0 < 0.5
    assert reply["indeterminate"] is True
    assert reply["path"].endswith("halted.json")
    assert "io-indeterminate" in reply["error"] and "reconcile" in reply
    assert (rec / STOP_AFTER_FILE).exists()


def test_readable_marker_still_decides(tmp_path: Path, blocked_open: set) -> None:
    rec = _repo(tmp_path)
    assert _fire(tmp_path).get("parked") == STOP_AFTER_PLAN
    (rec / "halted.json").write_text(json.dumps({"halted": HALT_PARKED}), encoding="utf-8")
    assert "indeterminate" not in _fire(tmp_path)


def test_unbounded_returns_read_never_raises_indeterminate(tmp_path: Path, blocked_open: set) -> None:
    row_returns.write(tmp_path, {"C1": "DONE"})
    blocked_open.add(row_returns.RETURNS_FILE)
    assert "C1" in row_returns.read(tmp_path)


def test_stalled_returns_read_raises_indeterminate(tmp_path: Path, blocked_open: set) -> None:
    row_returns.write(tmp_path, {"C1": "DONE"})
    blocked_open.add(row_returns.RETURNS_FILE)
    with pytest.raises(bounded_io.IoIndeterminate) as caught:
        row_returns.read(tmp_path, bounded=True)
    assert caught.value.path.endswith(row_returns.RETURNS_FILE)
    reply = ask_stage.indeterminate_reply(caught.value)
    assert reply["indeterminate"] is True and reply["path"] == caught.value.path
