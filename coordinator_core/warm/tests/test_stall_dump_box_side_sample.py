"""The full warm stall record carries a bounded box-side sample; the stub record and a failing sampler do not lose the dump."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.warm import box_sample, server

pytestmark = pytest.mark.complete

WINDOWS = sys.platform == "win32"


@pytest.fixture(autouse=True)
def _fresh_sample_key():
    """An overrun test leaves `box-sample` stalled in `bounded_io` until its worker returns; start each test clean."""
    from coordinator_core import bounded_io

    with bounded_io._lock:
        bounded_io._stalled.pop("box-sample", None)
        bounded_io._inflight.pop("box-sample", None)


def _header(record: Path) -> dict:
    head = record.read_text(encoding="utf-8").partition("--- threads ---")[0]
    return dict(line.split(": ", 1) for line in head.strip().split("\n"))


def _full_record(tmp_path) -> dict:
    record = server._write_stall_record(tmp_path / "stall-dumps", None, key="k", full=True)
    assert record is not None
    return _header(record)


@pytest.mark.skipif(not WINDOWS, reason="live sample reads the Windows process table")
@pytest.mark.deliberate_wall_clock(reason="the sample window plus margin is a wall-clock bound on the dump; that bound is the behaviour under test")
def test_full_record_at_default_arguments_carries_the_windows_box_side_lines(tmp_path):
    t0 = time.perf_counter()
    header = _full_record(tmp_path)
    assert time.perf_counter() - t0 < box_sample.WINDOW_SECS + box_sample.BOUND_MARGIN_SECS + 2
    assert header["box-sample"].startswith("ok window-s=2.")
    top = header[f"box-io-read-top{box_sample.TOP_N}"].split(" | ")
    assert len(top) == box_sample.TOP_N
    assert all(" pid=" in entry and entry.endswith("MB/s") for entry in top)
    assert "box-msmpeng-cpu-pct" in header
    avail, _, total = header["box-available-memory-mb"].partition(" of ")
    assert 0 < float(avail) <= float(total)
    assert float(header["box-file-cache-mb"]) >= 0


def test_non_windows_writes_one_explicit_unavailable_line(tmp_path, monkeypatch):
    monkeypatch.setattr(box_sample, "_platform", lambda: "darwin")
    header = _full_record(tmp_path)
    assert header["box-sample"] == "unavailable (platform=darwin)"
    assert not any(key.startswith("box-io-read") for key in header)


def test_a_raising_sampler_leaves_the_record_with_an_error_value(tmp_path, monkeypatch):
    def boom(_window):
        raise RuntimeError("no counters")

    monkeypatch.setattr(box_sample, "sample_lines", boom)
    header = _full_record(tmp_path)
    assert header["box-sample"] == "error:RuntimeError"
    assert "fs-probe-ms" in header


@pytest.mark.deliberate_wall_clock(reason="the sampler timeout bound is the behaviour under test")
def test_an_overrunning_sampler_times_out_inside_the_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(box_sample, "WINDOW_SECS", 0.2)
    monkeypatch.setattr(box_sample, "BOUND_MARGIN_SECS", 0.2)
    monkeypatch.setattr(box_sample, "sample_lines", lambda _w: time.sleep(3) or "box-sample: ok\n")
    t0 = time.perf_counter()
    header = _full_record(tmp_path)
    assert time.perf_counter() - t0 < 2.0
    assert header["box-sample"] == "error:timeout"


def test_the_stub_record_takes_no_sample(tmp_path, monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("stub record must not sample")

    monkeypatch.setattr(box_sample, "box_side_lines", forbidden)
    stall_dir = tmp_path / "stall-dumps"
    record = server._write_stall_record(stall_dir, None, key="k", full=False)
    assert record is not None and "box-sample" not in record.read_text(encoding="utf-8")


def test_the_sampler_spawns_no_subprocess():
    source = Path(box_sample.__file__).read_text(encoding="utf-8")
    for banned in ("subprocess", "os.system", "powershell", "wmic", "typeperf"):
        assert banned not in source.lower()


def test_the_overdue_request_path_dump_never_takes_the_box_sample(tmp_path, monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("request-thread dump must not sample")

    monkeypatch.setattr(box_sample, "box_side_lines", forbidden)
    stall_dir = tmp_path / "stall-dumps"
    note = server._ensure_overdue_stall_dump({}, stall_dir, "k1", 3.0)
    assert note.startswith("Stall dump: ")
    record = Path(note.removeprefix("Stall dump: ").rstrip("."))
    assert "box-sample" not in record.read_text(encoding="utf-8")
