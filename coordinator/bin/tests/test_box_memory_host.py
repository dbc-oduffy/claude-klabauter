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


@pytest.mark.parametrize(
    "peak_gib, need_gib",
    [(2, 2.5), (12.3, 15.375), (32, 40), (40, 48), (100, 108)],
)
def test_admit_need_is_peak_plus_cushion_capped_at_8_gib(peak_gib, need_gib):
    status = host.MemoryStatus(200 * host.GIB, 256 * host.GIB, 0, 1000 * host.GIB)
    verdict = host.admit(int(peak_gib * host.GIB), status)
    assert verdict.need_avail == pytest.approx(need_gib * host.GIB, rel=1e-9)


def test_admit_has_no_floor_term():
    status = host.MemoryStatus(3 * host.GIB, 64 * host.GIB, 0, 100 * host.GIB)
    assert host.admit(2 * host.GIB, status).admit
    assert not host.admit(2.5 * host.GIB, status).admit
    assert not hasattr(host, "guard_floor_bytes")


def test_admit_and_register_subtracts_live_reservation(tmp_path):
    adm = tmp_path / "admission.jsonl"
    host.register_command(adm, "other", 5, 4.0)
    status = host.MemoryStatus(10 * host.GIB, 64 * host.GIB, 0, 100 * host.GIB)
    kw = dict(rss_fn=lambda p: 0.0, alive_fn=lambda p: True, image_fn=lambda p: "python.exe")
    # 10 - 4 reserved = 6 available; 4.8 needs 6.0 and admits, 4.9 needs 6.125 and holds.
    verdict, reserved = host.admit_and_register(adm, "k", 6, 4.8, status, **kw)
    assert reserved == 4 * host.GIB and verdict.admit
    adm2 = tmp_path / "admission2.jsonl"
    host.register_command(adm2, "other", 5, 4.0)
    verdict, _ = host.admit_and_register(adm2, "k", 6, 4.9, status, **kw)
    assert not verdict.admit


def test_peak_store_keeps_largest(tmp_path):
    adm = tmp_path / "admission.jsonl"
    assert host.read_recorded_peak(adm, "k") is None
    host.record_peak(adm, "k", 2.0)
    assert host.record_peak(adm, "k", 1.0) == 2.0
    assert host.read_recorded_peak(adm, "k") == 2.0


def test_register_and_release_drive_reserved(tmp_path):
    adm = tmp_path / "admission.jsonl"
    host.register_command(adm, "k", 5, 4.0)
    kw = dict(rss_fn=lambda p: 1024.0, alive_fn=lambda p: True, image_fn=lambda p: "python.exe")
    assert host.reserved_bytes(adm, **kw) == 3 * host.GIB
    host.release_command(adm, "k", 5)
    assert host.reserved_bytes(adm, **kw) == 0


def _kw(**extra):
    extra.setdefault("image_fn", lambda p: "python.exe")
    extra.setdefault("now_fn", lambda: 1000.0)
    return dict(rss_fn=lambda p: 0.0, alive_fn=lambda p: True, **extra)


def _ledger(tmp_path, *rows):
    import json

    adm = tmp_path / "admission.jsonl"
    adm.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return adm


def _start(run, pid=5, **extra):
    return {"phase": "START", "session_id": "s", "run": run, "pids": [pid], "declared_peak_mb": 4096, **extra}


def test_register_stamps_create_time_and_ts(tmp_path):
    import json

    adm = tmp_path / "admission.jsonl"
    host.register_command(adm, "k", 5, 4.0, create_time_fn=lambda p: 1234.5)
    line = json.loads(adm.read_text(encoding="utf-8"))
    assert line["create_time"] == 1234.5 and isinstance(line["ts"], float)


def test_matching_create_time_reserves_and_recycled_pid_does_not(tmp_path):
    adm = tmp_path / "admission.jsonl"
    host.register_command(adm, "k", 5, 4.0, create_time_fn=lambda p: 1000.0)
    assert host.reserved_bytes(adm, create_time_fn=lambda p: 1000.0, **_kw()) == 4 * host.GIB
    assert host.reserved_bytes(adm, create_time_fn=lambda p: 1000.9, **_kw()) == 4 * host.GIB
    assert host.reserved_bytes(adm, create_time_fn=lambda p: 1001.5, **_kw()) == 0
    assert host.reserved_bytes(adm, create_time_fn=lambda p: None, **_kw()) == 0


def test_legacy_start_with_ts_keeps_only_while_create_time_not_after_ts(tmp_path):
    adm = _ledger(tmp_path, _start("a", ts=1000.0))
    assert host.reserved_bytes(adm, create_time_fn=lambda p: 900.0, **_kw()) == 4 * host.GIB
    assert host.reserved_bytes(adm, create_time_fn=lambda p: 1000.0, **_kw()) == 4 * host.GIB
    assert host.reserved_bytes(adm, create_time_fn=lambda p: 1001.0, **_kw()) == 4 * host.GIB
    assert host.reserved_bytes(adm, create_time_fn=lambda p: 1001.1, **_kw()) == 0


def test_legacy_start_without_ts_or_create_time_never_reserves(tmp_path):
    adm = _ledger(tmp_path, _start("a"))
    assert host.reserved_bytes(adm, create_time_fn=lambda p: 5e9, **_kw()) == 0


def test_unstamped_start_reserves_only_within_bounded_age(tmp_path):
    adm = _ledger(tmp_path, _start("a", ts=1000.0))
    ok = host.reserved_bytes(adm, create_time_fn=lambda p: 900.0, **_kw(now_fn=lambda: 1000.0 + 3600))
    stale = host.reserved_bytes(adm, create_time_fn=lambda p: 900.0, **_kw(now_fn=lambda: 1000.0 + 7 * 3600))
    assert ok == 4 * host.GIB and stale == 0


def test_image_not_the_declared_command_does_not_hold(tmp_path):
    adm = _ledger(tmp_path, _start("test-runner-pytest:5", ts=1000.0))
    ct = lambda p: 900.0
    assert host.reserved_bytes(adm, create_time_fn=ct, **_kw(image_fn=lambda p: "conhost.exe")) == 0
    assert host.reserved_bytes(adm, create_time_fn=ct, **_kw(image_fn=lambda p: "head.exe")) == 0
    assert host.reserved_bytes(adm, create_time_fn=ct, **_kw(image_fn=lambda p: None)) == 0
    assert host.reserved_bytes(adm, create_time_fn=ct, **_kw(image_fn=lambda p: "python.exe")) == 4 * host.GIB


def test_forty_recycled_pid_legacy_starts_reserve_zero(tmp_path):
    rows = [
        {"phase": "START", "session_id": "box-memory-snapshot", "run": f"test-runner-pytest:{1000 + i}",
         "pids": [1000 + i], "declared_peak_mb": 1024}
        for i in range(40)
    ]
    adm = _ledger(tmp_path, *rows)
    images = {1000 + i: ("conhost.exe" if i % 2 else "head.exe") for i in range(40)}
    assert host.reserved_bytes(adm, create_time_fn=lambda p: 5e9, **_kw(image_fn=images.get)) == 0
    assert host.reserved_bytes(adm, create_time_fn=lambda p: 5e9, **_kw(image_fn=lambda p: "python.exe")) == 0


def test_top_holders_names_session_run_pid_image_largest_first(tmp_path):
    adm = _ledger(
        tmp_path,
        _start("small", pid=1, create_time=10.0, declared_peak_mb=1024),
        _start("big", pid=2, create_time=10.0, declared_peak_mb=8192),
    )
    rows = host.top_holders(adm, limit=1, create_time_fn=lambda p: 10.0, **_kw(image_fn=lambda p: "python.exe"))
    assert rows == [{"session_id": "s", "run": "big", "pid": 2, "image": "python.exe", "bytes": 8 * host.GIB}]


def test_compaction_drops_dead_and_recycled_and_closed_and_backfills_legacy(tmp_path):
    import json

    end = {"phase": "END", "session_id": "s", "run": "closed", "pids": []}
    adm = _ledger(
        tmp_path,
        _start("dead", pid=1, create_time=10.0),
        _start("recycled", pid=2, create_time=10.0),
        _start("closed", pid=3, create_time=10.0),
        end,
        _start("stamped", pid=4, create_time=10.0),
        _start("legacy-ts", pid=6, ts=500.0),
        _start("legacy-ts-recycled", pid=7, ts=500.0),
        _start("legacy-bare", pid=8),
        _start("legacy-ts-stale", pid=9, ts=-30000.0),
    )
    with open(adm, "a", encoding="utf-8") as fh:
        fh.write("not json\n")
    times = {2: 99.0, 3: 10.0, 4: 10.0, 6: 400.0, 7: 600.0, 8: 250.0, 9: -40000.0}
    liveness = dict(create_time_fn=lambda p: times.get(p), image_fn=lambda p: "python.exe", now_fn=lambda: 1000.0)
    removed = host.compact_admission(adm, alive_fn=lambda p: p != 1, **liveness)
    kept = [l for l in adm.read_text(encoding="utf-8").splitlines()]
    runs = [json.loads(l)["run"] for l in kept if l.startswith("{")]
    assert runs == ["stamped", "legacy-ts"] and "not json" in kept
    assert removed == 7
    by_run = {json.loads(l)["run"]: json.loads(l) for l in kept if l.startswith("{")}
    assert by_run["legacy-ts"]["create_time"] == 400.0
    assert by_run["stamped"]["create_time"] == 10.0
    assert host.compact_admission(adm, alive_fn=lambda p: True, **liveness) == 0


def test_after_compaction_backfilled_legacy_row_is_recycle_safe(tmp_path):
    adm = _ledger(tmp_path, _start("legacy", pid=8, ts=300.0))
    host.compact_admission(
        adm, alive_fn=lambda p: True, create_time_fn=lambda p: 250.0,
        image_fn=lambda p: "python.exe", now_fn=lambda: 400.0,
    )
    assert host.reserved_bytes(adm, create_time_fn=lambda p: 250.0, **_kw()) == 4 * host.GIB
    assert host.reserved_bytes(adm, create_time_fn=lambda p: 9999.0, **_kw()) == 0


def test_admit_and_register_compacts_dead_rows(tmp_path):
    adm = _ledger(tmp_path, _start("dead", pid=1, create_time=10.0))
    status = host.MemoryStatus(avail_phys=100 * host.GIB, total_phys=128 * host.GIB, commit_used=0, commit_limit=200 * host.GIB)
    kw = dict(rss_fn=lambda p: 0.0, alive_fn=lambda p: p != 1, create_time_fn=lambda p: 7.0)
    verdict, reserved = host.admit_and_register(adm, "k", 6, 1.0, status, **kw)
    assert verdict.admit and reserved == 0
    assert '"dead"' not in adm.read_text(encoding="utf-8")


def test_concurrent_admits_serialize_check_and_reserve(tmp_path):
    import threading

    adm = tmp_path / "admission.jsonl"
    status = host.MemoryStatus(10 * GIB, 64 * GIB, 1 * GIB, 100 * GIB)
    results = []
    barrier = threading.Barrier(8)

    def worker(pid):
        barrier.wait()
        verdict, _ = host.admit_and_register(
            adm, "k", pid, 4.0, status, cushion_pct=0.0,
            rss_fn=lambda p: 0.0, alive_fn=lambda p: True, image_fn=lambda p: "python.exe",
        )
        results.append(verdict.admit)

    threads = [threading.Thread(target=worker, args=(100 + i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False] * 6 + [True] * 2


def test_admit_and_register_holds_without_writing_a_reservation(tmp_path):
    adm = tmp_path / "admission.jsonl"
    status = host.MemoryStatus(3 * GIB, 64 * GIB, 1 * GIB, 100 * GIB)
    verdict, _ = host.admit_and_register(adm, "k", 7, 4.0, status, cushion_pct=0.0)
    assert not verdict.admit
    assert not adm.exists()
