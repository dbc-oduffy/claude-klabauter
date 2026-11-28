"""`halted_run_to_resume` answers from the sizing's own runs, never a read of every record dir."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run as hr
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

SIZING = "state/sizings/2026-11-20-target.yaml"
OTHER = "state/sizings/2026-11-20-other.yaml"
RUN_DIRS = 2000
BAR_MS = 50.0


def _corpus(root: Path, count: int) -> None:
    """`count` halted `ask-*` runs of another sizing, written straight to disk: no lookup index."""
    base = root / RECORD_DIR_ROOT
    for n in range(count):
        d = base / f"ask-20261120T{n // 3600 % 24:02d}{n // 60 % 60:02d}{n % 60:02d}-{n:06x}"
        d.mkdir(parents=True)
        (d / hr.HALTED_FILE).write_text(json.dumps({"status": "halted", "halted": "x", "sizing_rel": OTHER}), encoding="utf-8")


def _reads(monkeypatch: pytest.MonkeyPatch) -> list:
    seen: list = []
    real = hr._read
    monkeypatch.setattr(hr, "_read", lambda d: seen.append(Path(d).name) or real(d))
    return seen


def _timed(fn):
    started = time.process_time()
    out = fn()
    return out, (time.process_time() - started) * 1000.0


def test_a_warp_halt_resolves_in_a_large_unindexed_corpus_reading_only_its_own_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _corpus(tmp_path, RUN_DIRS)
    warp = tmp_path / RECORD_DIR_ROOT / "warp-2026-11-20-target-20261120T010000-aaaaaa"
    warp.mkdir(parents=True)
    (warp / hr.HALTED_FILE).write_text(json.dumps({"status": "halted", "halted": "x", "sizing_rel": SIZING}), encoding="utf-8")
    assert not (tmp_path / hr.LOOKUP_INDEX_DIR).exists()
    reads = _reads(monkeypatch)
    found, ms = _timed(lambda: hr.halted_run_to_resume(tmp_path, SIZING))
    assert found == warp.name
    assert set(reads) <= {warp.name}
    assert ms < BAR_MS, ms
    assert not (tmp_path / hr.LOOKUP_INDEX_DIR).exists()


def test_a_sizing_with_no_halt_resolves_none_with_no_marker_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _corpus(tmp_path, RUN_DIRS)
    reads = _reads(monkeypatch)
    found, ms = _timed(lambda: hr.halted_run_to_resume(tmp_path, SIZING))
    assert found is None
    assert reads == []
    assert ms < BAR_MS, ms


def test_an_ask_halt_written_through_the_marker_writer_is_found_by_the_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _corpus(tmp_path, RUN_DIRS)
    ask = tmp_path / RECORD_DIR_ROOT / "ask-20261120T120000-bbbbbb"
    hr.write_halt_marker(ask, halted="x", sizing_rel=SIZING)
    reads = _reads(monkeypatch)
    found, ms = _timed(lambda: hr.halted_run_to_resume(tmp_path, SIZING))
    assert found == ask.name
    assert set(reads) <= {ask.name}
    assert ms < BAR_MS, ms


def test_an_ask_halt_with_no_index_entry_is_not_found(tmp_path: Path) -> None:
    """Pre-index legacy `ask-*` halts are not back-filled: the refusal side of the no-scan decision."""
    _corpus(tmp_path, 5)
    ask = tmp_path / RECORD_DIR_ROOT / "ask-20261120T120000-bbbbbb"
    ask.mkdir(parents=True)
    (ask / hr.HALTED_FILE).write_text(json.dumps({"status": "halted", "halted": "x", "sizing_rel": SIZING}), encoding="utf-8")
    assert hr.halted_run_to_resume(tmp_path, SIZING) is None


def test_no_wholesale_build_is_reachable() -> None:
    assert not hasattr(hr, "rebuild_lookup_index")
    assert not hasattr(hr, "LOOKUP_SENTINEL")
