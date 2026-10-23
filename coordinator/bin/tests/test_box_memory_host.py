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
