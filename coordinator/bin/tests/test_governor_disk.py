"""Tests for the pure layer of governor_disk."""
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import governor_disk as gd  # noqa: E402

MB = 1024 * 1024


def test_saturated_needs_low_idle_and_high_queue():
    assert gd.saturated(gd.DiskStat("0 data", 1.0, 10), 5, 4)
    assert not gd.saturated(gd.DiskStat("0 data", 50.0, 10), 5, 4)
    assert not gd.saturated(gd.DiskStat("0 data", 1.0, 1), 5, 4)


def test_episode_trips_after_n_ticks_once_and_rearms():
    ep = gd.DiskEpisode(ticks=3)
    assert [ep.observe(True) for _ in range(2)] == [False, False]
    assert ep.observe(True) is True
    assert ep.observe(True) is False  # flag-once
    assert ep.observe(False) is False  # recovery
    assert [ep.observe(True) for _ in range(3)] == [False, False, True]


def test_episode_blip_resets_streak():
    ep = gd.DiskEpisode(ticks=2)
    ep.observe(True)
    ep.observe(False)
    assert ep.observe(True) is False


def test_top_by_bytes_ranks_by_delta_not_total_and_skips_new_pids():
    prev = {1: 100 * MB, 2: 0, 3: 5 * MB}
    cur = {1: 101 * MB, 2: 50 * MB, 3: 5 * MB, 4: 900 * MB}
    assert gd.top_by_bytes(prev, cur, 3) == [(2, 50 * MB), (1, MB)]


def test_format_names_pid_and_full_cmdline_and_one_stat_per_disk():
    stats = [gd.DiskStat("1 sys", 90.0, 0), gd.DiskStat("0 data", 0.0, 12)]
    top = [(77, 300 * MB), (5, 2 * MB)]
    cmds = {77: "python writer.py --out /x y"}
    lines = gd.format_disk_lines(stats, top, cmds)
    assert lines[0] == ("GOVERNOR DISK pid=77 io=300MB disk=0 data idle=0% queue=12 "
                        "cmd=python writer.py --out /x y")
    stat_lines = [ln for ln in lines if ln.startswith("GOVERNOR DISK-STAT")]
    assert len(stat_lines) == 2
    assert stat_lines[1] == "GOVERNOR DISK-STAT disk=0 data idle=0% queue=12 top=77:300MB,5:2MB"


def test_format_no_disk_line_when_nothing_saturated():
    lines = gd.format_disk_lines([gd.DiskStat("0", 80.0, 0)], [(1, MB)], {})
    assert all(ln.startswith("GOVERNOR DISK-STAT") for ln in lines)


def test_format_without_top_writer_still_reports():
    lines = gd.format_disk_lines([gd.DiskStat("0", 0.0, 9)], [], {})
    assert lines[0].startswith("GOVERNOR DISK pid=none")


# ---- Windows reader ----


def _pdh_buffer(items, base=0x9000):
    """A fixture PdhGetFormattedCounterArrayW buffer: [(name, status, value)]."""
    head = 24 * len(items)
    names = bytearray()
    buf = bytearray(head)
    for i, (name, status, value) in enumerate(items):
        raw = name.encode("utf-16-le") + b"\x00\x00"
        struct.pack_into("<QI4xd", buf, i * 24, base + head + len(names), status, value)
        names += raw
    return bytes(buf + names), base


def test_parse_pdh_items_reads_names_values_and_skips_total_and_bad_status():
    buf, base = _pdh_buffer([("0 C: E:", 0, 3.5), ("_Total", 0, 9.0), ("1 D:", 0x800007D8, 1.0),
                             ("2 F:", 1, 77.0)])
    assert gd.parse_pdh_items(buf, base, 4) == {"0 C: E:": 3.5, "2 F:": 77.0}


def test_build_disk_stats_joins_idle_and_queue_clamps_and_defaults_queue():
    stats = gd.build_disk_stats({"1 D:": 100.4, "0 C:": -0.2}, {"0 C:": 12.0})
    assert stats == [gd.DiskStat("0 C:", 0.0, 12.0), gd.DiskStat("1 D:", 100.0, 0.0)]


def _nt_buffer(entries):
    entry_len = 288
    buf = bytearray(entry_len * len(entries))
    for i, (pid, read, write) in enumerate(entries):
        off = i * entry_len
        struct.pack_into("<I", buf, off, entry_len if i < len(entries) - 1 else 0)
        struct.pack_into("<Q", buf, off + 80, pid)
        struct.pack_into("<QQQ", buf, off + 232, read, write, 555)
    return bytes(buf)


def test_parse_io_bytes_sums_read_and_write_only_and_skips_pid_zero():
    assert gd.parse_io_bytes(_nt_buffer([(0, 5, 5), (4, 10, 20), (9, 0, 2 ** 41)])) == {4: 30, 9: 2 ** 41}


def test_disk_sample_is_none_on_macos(monkeypatch):
    monkeypatch.setattr(gd.sys, "platform", "darwin")
    assert gd.disk_sample() is None


# ---- Linux reader ----

DISKSTATS = """\
   7       0 loop0 10 0 80 5 0 0 0 0 0 5 5
   1       0 ram0 0 0 0 0 0 0 0 0 0 0 0
   8       0 sda 100 0 8000 50 200 0 16000 90 7 1000 140
   8       1 sda1 100 0 8000 50 200 0 16000 90 7 1000 140
 259       0 nvme0n1 5 0 40 1 5 0 40 1 0 20 2
 259       1 nvme0n1p1 5 0 40 1 5 0 40 1 0 20 2
 253       0 dm-0 1 0 8 1 1 0 8 1 0 1 1
   8      16 sdb 1 0 8 1 1 0 8 1 3 400 2
garbage line
"""


def test_parse_diskstats_keeps_whole_disks_with_ticks_and_inflight():
    assert gd.parse_diskstats(DISKSTATS) == {"sda": (1000, 7), "nvme0n1": (20, 0), "sdb": (400, 3)}


def test_diskstats_to_stats_idle_from_io_ticks_delta_and_queue_from_inflight():
    prev = {"sda": (0, 0), "sdb": (0, 0)}
    cur = {"sda": (990, 9), "sdb": (400, 3), "sdc": (5, 1)}
    stats = gd.diskstats_to_stats(prev, cur, 1000.0)
    assert [s.name for s in stats] == ["sda", "sdb"]  # sdc has no baseline
    assert stats[0].idle_pct == pytest.approx(1.0) and stats[0].queue == 9
    assert stats[1].idle_pct == pytest.approx(60.0) and stats[1].queue == 3


def test_diskstats_idle_is_clamped_and_zero_interval_yields_nothing():
    stats = gd.diskstats_to_stats({"sda": (0, 0)}, {"sda": (5000, 1)}, 1000.0)
    assert stats[0].idle_pct == 0.0
    assert gd.diskstats_to_stats({"sda": (0, 0)}, {"sda": (5, 1)}, 0.0) == []


def test_psi_gate_holds_saturation_off_and_on():
    prev, cur = {"sda": (0, 0)}, {"sda": (1000, 20)}
    hot = gd.diskstats_to_stats(prev, cur, 1000.0, pressured=True)[0]
    cold = gd.diskstats_to_stats(prev, cur, 1000.0, pressured=False)[0]
    assert gd.saturated(hot, 5, 4)
    assert not gd.saturated(cold, 5, 4)


def test_parse_psi_full_avg10():
    text = "some avg10=1.50 avg60=0.00 avg300=0.00 total=1\nfull avg10=12.25 avg60=3.00 avg300=1.00 total=9\n"
    assert gd.parse_psi_full_avg10(text) == 12.25
    assert gd.parse_psi_full_avg10("some avg10=9.00 avg60=0 avg300=0 total=1\n") == 0.0


def test_parse_proc_io_sums_read_and_write_bytes_only():
    text = "rchar: 999\nwchar: 999\nsyscr: 5\nsyscw: 5\nread_bytes: 4096\nwrite_bytes: 8192\ncancelled_write_bytes: 77\n"
    assert gd.parse_proc_io(text) == 12288


def _fake_proc(root, with_psi=0.0, sda_ticks=0, sda_inflight=0):
    (root / "pressure").mkdir(parents=True, exist_ok=True)
    (root / "pressure" / "io").write_text(f"some avg10=0.00 avg60=0 avg300=0 total=0\n"
                                          f"full avg10={with_psi} avg60=0 avg300=0 total=0\n")
    (root / "diskstats").write_text(f"   8       0 sda 1 0 8 1 1 0 8 1 {sda_inflight} {sda_ticks} 2\n")
    (root / "42").mkdir(exist_ok=True)
    (root / "42" / "io").write_text("read_bytes: 100\nwrite_bytes: 200\n")
    (root / "43").mkdir(exist_ok=True)  # no io file: permission-denied stand-in
    (root / "self").mkdir(exist_ok=True)
    (root / "self" / "io").write_text("read_bytes: 1\nwrite_bytes: 1\n")


def test_proc_io_bytes_skips_unreadable_and_non_numeric_entries(tmp_path):
    _fake_proc(tmp_path)
    assert gd.proc_io_bytes(tmp_path) == {42: 300}


def test_linux_sample_baseline_then_saturated_stat_gated_by_psi(tmp_path, monkeypatch):
    monkeypatch.setattr(gd, "_linux_prev", None)
    clock = iter([100.0, 101.0, 102.0])
    _fake_proc(tmp_path)
    stats, io = gd._linux_sample(tmp_path, now=lambda: next(clock))
    assert stats == [] and io == {42: 300}
    _fake_proc(tmp_path, with_psi=30.0, sda_ticks=1000, sda_inflight=12)
    stats, _ = gd._linux_sample(tmp_path, now=lambda: next(clock))
    assert len(stats) == 1 and gd.saturated(stats[0], 5, 4)
    _fake_proc(tmp_path, with_psi=0.5, sda_ticks=2000, sda_inflight=12)
    stats, _ = gd._linux_sample(tmp_path, now=lambda: next(clock))
    assert stats[0].idle_pct == 0.0 and not gd.saturated(stats[0], 5, 4)


def test_linux_sample_without_psi_file_does_not_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(gd, "_linux_prev", None)
    _fake_proc(tmp_path)
    (tmp_path / "pressure" / "io").unlink()
    clock = iter([0.0, 1.0])
    gd._linux_sample(tmp_path, now=lambda: next(clock))
    (tmp_path / "diskstats").write_text("   8       0 sda 1 0 8 1 1 0 8 1 9 1000 2\n")
    stats, _ = gd._linux_sample(tmp_path, now=lambda: next(clock))
    assert gd.saturated(stats[0], 5, 4)


def test_disk_sample_routes_linux_to_the_proc_reader(monkeypatch):
    monkeypatch.setattr(gd.sys, "platform", "linux")
    monkeypatch.setattr(gd, "_linux_sample", lambda: ([], {1: 2}))
    assert gd.disk_sample() == ([], {1: 2})


def test_cmdline_of_returns_the_full_untruncated_line(monkeypatch):
    long_arg = "x" * 5000

    class Pr:
        def __init__(self, pid):
            assert pid == 42

        def cmdline(self):
            return ["python", "writer.py", long_arg]

    monkeypatch.setattr(gd, "psutil", type("Ps", (), {"Process": Pr}))
    assert gd.cmdline_of(42) == f"python writer.py {long_arg}"


def test_cmdline_of_is_empty_when_the_process_is_gone(monkeypatch):
    class Pr:
        def __init__(self, pid):
            raise OSError("gone")

    monkeypatch.setattr(gd, "psutil", type("Ps", (), {"Process": Pr}))
    assert gd.cmdline_of(1) == ""


def test_windows_sample_closes_the_reader_it_drops_on_a_fault(monkeypatch):
    closed = []

    class Reader:
        def read(self):
            raise OSError("pdh fault")

        def close(self):
            closed.append(1)

    monkeypatch.setattr(gd, "_reader", Reader())
    with pytest.raises(OSError):
        gd._windows_sample()
    assert closed == [1] and gd._reader is None


@pytest.mark.skipif(sys.platform != "win32", reason="pdh.dll and ntdll")
def test_live_windows_sample_reports_disks_and_this_pids_io():
    import os
    assert gd.disk_sample() is not None
    stats, io = gd.disk_sample()
    assert stats and all(0.0 <= s.idle_pct <= 100.0 and s.queue >= 0.0 for s in stats)
    assert os.getpid() in io
