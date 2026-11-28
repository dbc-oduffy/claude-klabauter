"""proc_census: row shape, per-axis failure isolation, Linux /proc and macOS ps parsing, live Windows row."""

from __future__ import annotations

import os
import sys

import pytest

from coordinator_core.group_em import proc_census

NOW = 2_000_000_000.0


def _raw(pid, **kw):
    row = {"pid": pid, "ppid": 1, "comm": "x", "cmdline": "x --go", "start": NOW - 100.0, "cpu_s": 50.0,
           "rss_mb": 10.0, "io_read_bytes": 1000, "io_write_bytes": 1000}
    row.update(kw)
    return row


def test_row_carries_derived_rates_and_vram():
    reply = proc_census.read(
        {"processes": lambda: [_raw(7)], "vram": lambda: {"gpus": [], "vram_by_pid": {7: 2048}}},
        system="Linux", now=NOW)
    row = reply["processes"][0]
    assert row["ppid"] == 1 and row["age_s"] == 100.0 and row["pcpu"] == 50.0
    assert row["io_bytes_per_s"] == 20.0 and row["vram_mb"] == 2048
    assert reply["unavailable"] == {}


def test_failed_process_reader_never_raises_and_is_named():
    def boom():
        raise OSError("denied")

    reply = proc_census.read({"processes": boom, "vram": lambda: {"gpu_unavailable": "nvml-absent"}},
                             system="Linux", now=NOW)
    assert reply["processes"] == []
    assert "denied" in reply["unavailable"]["processes"]


def test_failed_vram_reader_leaves_other_axes():
    def boom():
        raise RuntimeError("x")

    reply = proc_census.read({"processes": lambda: [_raw(7)], "vram": boom}, system="Linux", now=NOW)
    assert reply["processes"][0]["vram_mb"] is None and reply["processes"][0]["rss_mb"] == 10.0
    assert reply["unavailable"]["vram_mb"] == "vram-error:RuntimeError"


def test_null_axis_is_null_not_zero_and_named_when_universal():
    rows = [_raw(7, io_read_bytes=None, io_write_bytes=None, cmdline=None)]
    reply = proc_census.read({"processes": lambda: rows, "vram": lambda: {"gpus": [], "vram_by_pid": {}}},
                             system="Linux", now=NOW)
    row = reply["processes"][0]
    assert row["io_bytes_per_s"] is None and row["cmdline"] is None
    assert reply["unavailable"]["cmdline"] == "axis-unreadable"
    assert "rss_mb" not in reply["unavailable"]


def test_one_unreadable_row_does_not_mark_axis_unavailable():
    rows = [_raw(7, cmdline=None), _raw(8)]
    reply = proc_census.read({"processes": lambda: rows, "vram": lambda: {"gpus": [], "vram_by_pid": {}}},
                             system="Linux", now=NOW)
    assert "cmdline" not in reply["unavailable"]


def test_darwin_io_is_null_with_reason():
    rows = [_raw(7, io_read_bytes=None, io_write_bytes=None)]
    reply = proc_census.read({"processes": lambda: rows, "vram": lambda: {"gpu_unavailable": "vendor-unsupported"}},
                             system="Darwin", now=NOW)
    assert reply["unavailable"]["io_read_bytes"] == "io-unavailable-on-darwin"
    assert reply["unavailable"]["vram_mb"] == "vendor-unsupported"


def test_parse_darwin_ps_line():
    text = "  412     1 Thu Nov 26 13:44:02 2026   2.0  20480 /usr/bin/python3 -m job --flag\n"
    rows = proc_census._parse_darwin_ps(text, now=NOW)
    assert rows[0]["pid"] == 412 and rows[0]["ppid"] == 1
    assert rows[0]["rss_mb"] == 20.0
    assert rows[0]["cmdline"] == "/usr/bin/python3 -m job --flag" and rows[0]["comm"] == "python3"
    assert rows[0]["io_read_bytes"] is None


@pytest.mark.skipif(sys.platform == "win32", reason="needs sysconf and /proc layout")
def test_linux_rows_from_fake_proc(tmp_path):
    (tmp_path / "stat").write_text("cpu 1 2 3\nbtime 1000\n")
    pdir = tmp_path / "55"
    pdir.mkdir()
    tck = os.sysconf("SC_CLK_TCK")
    fields = ["S", "9"] + ["0"] * 9 + [str(2 * tck), str(3 * tck)] + ["0"] * 6 + [str(5 * tck), "0", "256"]
    (pdir / "stat").write_text("55 (my job) " + " ".join(fields) + "\n")
    (pdir / "cmdline").write_bytes(b"python\0-m\0job\0")
    (pdir / "io").write_text("rchar: 1\nwchar: 2\nread_bytes: 4096\nwrite_bytes: 8192\n")
    (tmp_path / "notapid").mkdir()
    rows = proc_census._linux_rows(str(tmp_path))
    assert len(rows) == 1
    row = rows[0]
    assert row["pid"] == 55 and row["ppid"] == 9 and row["comm"] == "my job"
    assert row["cpu_s"] == 5.0 and row["start"] == 1005.0
    assert row["cmdline"] == "python -m job"
    assert row["io_read_bytes"] == 4096 and row["io_write_bytes"] == 8192


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ctypes reader")
def test_windows_live_row_for_this_process():
    reply = proc_census.read({"vram": lambda: {"gpu_unavailable": "no-gpu"}})
    mine = [r for r in reply["processes"] if r["pid"] == os.getpid()]
    assert mine, "own pid missing from census"
    row = mine[0]
    assert row["ppid"] == os.getppid()
    assert "python" in (row["comm"] or "").lower()
    assert row["cmdline"] and row["start"] and row["rss_mb"] and row["cpu_s"] is not None
