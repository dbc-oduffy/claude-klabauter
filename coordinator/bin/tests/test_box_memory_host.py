"""Tests for coordinator/bin/lib/box_memory_host.py (no process spawn, no real governor)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_HOST = Path(__file__).resolve().parents[1] / "lib" / "box_memory_host.py"
_spec = importlib.util.spec_from_file_location("box_memory_host_under_test", _HOST)
host = importlib.util.module_from_spec(_spec)
# dataclass resolves its module through sys.modules, so register before exec.
sys.modules[_spec.name] = host
_spec.loader.exec_module(host)

GIB = host.GIB


def _status(avail=100 * GIB, total=128 * GIB, used=10 * GIB, limit=200 * GIB):
    return host.MemoryStatus(avail, total, used, limit)


def test_constants():
    assert host.GIB == 1024**3
    assert host.UNSUPPORTED_HOST_EXIT == 6


def test_cannot_detach_exit_is_free_family_code():
    assert host.CANNOT_DETACH_EXIT == 5
    assert host.CANNOT_DETACH_EXIT not in (0, 1, 2, 3, 4, host.UNSUPPORTED_HOST_EXIT)


def test_admit_cap_binds_for_large_peak():
    peak = 64 * GIB  # 25% = 16 GiB, capped at 8 GiB
    v = host.admit(peak, _status(avail=peak + 8 * GIB))
    assert v.need_avail == peak + 8 * GIB
    assert v.avail_ok and v.commit_ok and v.admit
    v2 = host.admit(peak, _status(avail=peak + 8 * GIB - 1))
    assert not v2.avail_ok and not v2.admit


def test_admit_pct_binds_for_small_peak():
    peak = 4 * GIB  # 25% = 1 GiB < cap
    v = host.admit(peak, _status(avail=5 * GIB))
    assert v.need_avail == 5 * GIB
    assert v.admit
    assert not host.admit(peak, _status(avail=5 * GIB - 1)).avail_ok


def test_admit_commit_fails_alone():
    st = _status(avail=100 * GIB, used=195 * GIB, limit=200 * GIB)
    v = host.admit(10 * GIB, st)
    assert v.avail_ok and not v.commit_ok and not v.admit


def test_admit_avail_fails_alone():
    st = _status(avail=1 * GIB, used=0, limit=200 * GIB)
    v = host.admit(10 * GIB, st)
    assert not v.avail_ok and v.commit_ok and not v.admit


def test_admit_cushion_cap_parameter():
    v = host.admit(64 * GIB, _status(), cushion_cap=2 * GIB)
    assert v.need_avail == 66 * GIB


def test_admit_commit_limit_override():
    st = _status(used=10 * GIB, limit=200 * GIB)
    assert not host.admit(10 * GIB, st, commit_limit=15 * GIB).commit_ok


def test_unsupported_output_shape(capsys):
    rc = host.unsupported("prog", "linux", "needs Windows")
    out = capsys.readouterr()
    assert rc == 6
    assert out.out == ""
    assert out.err == "prog: unsupported host (linux): needs Windows\n"


def test_require_psutil_missing(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert host.require_psutil("prog") == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and err.startswith("prog:")


def test_require_psutil_present(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", object())
    assert host.require_psutil("prog") is None


def test_load_governor_fake_path(tmp_path):
    fake = tmp_path / "group-em-memory-governor.py"
    fake.write_text(
        "def descendants(): return 'd'\n"
        "def list_procs(): return 'l'\n"
        "def signal_tree(): return 's'\n"
        "def is_protected(): return 'p'\n"
        "def footprints(): return 'f'\n",
        encoding="utf-8",
    )
    mod = host.load_governor(path=fake)
    assert [getattr(mod, n)() for n in
            ("descendants", "list_procs", "signal_tree", "is_protected", "footprints")] == list("dlspf")


def test_load_governor_default_is_sibling_path(monkeypatch):
    seen = {}

    def fake_spec(name, path):
        seen["path"] = Path(path)
        return None

    monkeypatch.setattr(host.importlib.util, "spec_from_file_location", fake_spec)
    with pytest.raises(ImportError):
        host.load_governor()
    assert seen["path"] == _HOST.parents[1] / "group-em-memory-governor.py"


def test_memory_status_darwin_from_injected_readers():
    st = host.memory_status_darwin(
        avail_mb_fn=lambda: (8192, 16384),
        vm_fn=lambda: 16 * GIB,
        swap_fn=lambda: (4 * GIB, 1 * GIB),
    )
    assert st.avail_phys == 8 * GIB and st.total_phys == 16 * GIB
    assert st.commit_used == 9 * GIB and st.commit_limit == 20 * GIB


def test_memory_status_darwin_unreadable_available_raises():
    with pytest.raises(OSError):
        host.memory_status_darwin(
            avail_mb_fn=lambda: (None, None), vm_fn=lambda: GIB, swap_fn=lambda: (0, 0)
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 only")
def test_read_memory_status_win32():
    st = host.read_memory_status()
    assert st.commit_used <= st.commit_limit
    assert st.avail_phys <= st.total_phys


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 only")
def test_nonpaged_pool_bytes_win32():
    assert host.nonpaged_pool_bytes() > 0


def _spi_entry(next_off, pid, ppid, create, ws, private, name_ptr=0, name_len=0):
    import struct

    raw = bytearray(256)
    struct.pack_into("<I", raw, 0, next_off)
    struct.pack_into("<q", raw, 32, create)
    struct.pack_into("<HHxxxxQ", raw, 56, name_len, name_len, name_ptr)
    struct.pack_into("<QQ", raw, 80, pid, ppid)
    struct.pack_into("<Q", raw, 144, ws)
    struct.pack_into("<Q", raw, 200, private)
    return bytes(raw)


def test_parse_process_information_two_entries_and_terminator():
    name = "a.exe".encode("utf-16-le")
    ft = (1_700_000_000 + 11644473600) * 10_000_000
    first = _spi_entry(256, 10, 4, ft, 111, 222, name_ptr=1000 + 512, name_len=len(name))
    second = _spi_entry(0, 20, 10, 0, 333, 444)
    buf = first + second + name
    rows = host.parse_process_information(buf, base=1000)
    assert [r.pid for r in rows] == [10, 20]
    assert rows[0] == host.ProcRow(10, 4, 1_700_000_000.0, 111, 222, "a.exe")
    assert (rows[1].ppid, rows[1].create_time, rows[1].private_bytes, rows[1].image) == (10, 0.0, 444, "")


def test_parse_process_information_no_base_leaves_image_empty():
    name = "a.exe".encode("utf-16-le")
    buf = _spi_entry(0, 1, 0, 0, 1, 2, name_ptr=1256, name_len=len(name)) + name
    assert host.parse_process_information(buf)[0].image == ""


def test_parse_process_information_truncated_and_empty():
    assert host.parse_process_information(b"") == []
    full = _spi_entry(256, 1, 0, 0, 1, 2)
    assert [r.pid for r in host.parse_process_information(full)] == [1]
    assert [r.pid for r in host.parse_process_information(full[:207])] == []


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 only")
def test_proc_table_contains_self_with_true_parent():
    import os

    rows = {r.pid: r for r in host.proc_table()}
    me = rows[os.getpid()]
    assert me.ppid == os.getppid()
    assert me.private_bytes > 0 and me.image.lower().startswith("python")


def test_proc_cmdline_nonexistent_pid_is_empty():
    pytest.importorskip("psutil")
    assert host.proc_cmdline(2**22 + 1) == ""


def test_admit_need_is_max_of_cushion_and_floor_plus_peak():
    status = host.MemoryStatus(40 * host.GIB, 64 * host.GIB, 10 * host.GIB, 100 * host.GIB)
    assert host.admit(31 * host.GIB, status, floor=0).admit
    refused = host.admit(31 * host.GIB, status, floor=10 * host.GIB)
    assert not refused.admit and refused.need_avail == 41 * host.GIB and not refused.avail_ok


def test_peak_store_keeps_largest(tmp_path):
    adm = tmp_path / "admission.jsonl"
    assert host.read_recorded_peak(adm, "k") is None
    host.record_peak(adm, "k", 2.0)
    assert host.record_peak(adm, "k", 1.0) == 2.0
    assert host.read_recorded_peak(adm, "k") == 2.0


def test_register_and_release_drive_reserved(tmp_path):
    adm = tmp_path / "admission.jsonl"
    host.register_command(adm, "k", 5, 4.0)
    kw = dict(rss_fn=lambda p: 1024.0, alive_fn=lambda p: True)
    assert host.reserved_bytes(adm, **kw) == 3 * host.GIB
    host.release_command(adm, "k", 5)
    assert host.reserved_bytes(adm, **kw) == 0
