"""
coordinator_core.telemetry.tests.test_host_sampler_darwin_memory -- coverage
for coordinator_core.telemetry.host_sampler._darwin_memory_mb, the macOS
memory fix (docs/plans/2026-09-27-load-aware-workflow-admission.md C1).

Purpose: pins the sysctlbyname-based memory read (memorystatus_level path
and its page-count fallback), the (None, None, None) total-failure case,
that _collect_row (the 20-minute sink row) carries real memory on a patched
darwin platform, and a real-host assertion gated to actual macOS.

Spec backlink: docs/plans/2026-09-27-load-aware-workflow-admission.md § C1
"""

from __future__ import annotations

import sys

import pytest

from coordinator_core.telemetry import host_sampler

_GIB = 1024 * 1024 * 1024
_MB = 1024 * 1024


def _fake_sysctl(values: dict):
    def _sysctl(name, _out_ctype):
        return values.get(name)

    return _sysctl


def test_darwin_memory_via_memorystatus_level() -> None:
    total = 24 * _GIB
    fake = _fake_sysctl({"hw.memsize": total, "kern.memorystatus_level": 37})
    used_mb, avail_mb, total_mb = host_sampler._darwin_memory_mb(_sysctl=fake)
    assert total_mb == total // _MB
    expected_avail_mb = (total * 37 // 100) // _MB
    assert avail_mb == expected_avail_mb
    assert used_mb == total_mb - avail_mb


def test_darwin_memory_falls_back_to_page_counts_when_level_unreadable() -> None:
    total = 16 * _GIB
    page_size = 16384
    fake = _fake_sysctl(
        {
            "hw.memsize": total,
            "kern.memorystatus_level": None,
            "vm.page_free_count": 1000,
            "vm.page_speculative_count": 500,
            "hw.pagesize": page_size,
        }
    )
    used_mb, avail_mb, total_mb = host_sampler._darwin_memory_mb(_sysctl=fake)
    assert total_mb == total // _MB
    expected_avail_mb = ((1000 + 500) * page_size) // _MB
    assert avail_mb == expected_avail_mb
    assert used_mb == total_mb - avail_mb


def test_darwin_memory_everything_unreadable_returns_all_none() -> None:
    fake = _fake_sysctl({})
    assert host_sampler._darwin_memory_mb(_sysctl=fake) == (None, None, None)


def test_darwin_memory_page_fallback_partial_failure_returns_all_none() -> None:
    fake = _fake_sysctl(
        {
            "hw.memsize": 16 * _GIB,
            "kern.memorystatus_level": None,
            "vm.page_free_count": 1000,
            # vm.page_speculative_count and hw.pagesize missing
        }
    )
    assert host_sampler._darwin_memory_mb(_sysctl=fake) == (None, None, None)


def test_sysctlbyname_never_raises_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BoomLib:
        def sysctlbyname(self, *args, **kwargs):
            raise OSError("no sysctlbyname here")

    monkeypatch.setattr(host_sampler.ctypes, "CDLL", lambda _x: _BoomLib())
    assert host_sampler._sysctlbyname("hw.memsize", host_sampler.ctypes.c_uint64) is None


def test_collect_row_on_patched_darwin_carries_non_null_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(host_sampler, "_IS_WINDOWS", False)
    fake = _fake_sysctl({"hw.memsize": 24 * _GIB, "kern.memorystatus_level": 50})
    _real_darwin_memory_mb = host_sampler._darwin_memory_mb
    monkeypatch.setattr(host_sampler, "_darwin_memory_mb", lambda: _real_darwin_memory_mb(_sysctl=fake))
    monkeypatch.setattr(host_sampler, "_posix_process_counts", lambda: (10, 1, 5))
    monkeypatch.setattr(host_sampler, "_posix_cpu_pct", lambda: 12.5)

    row = host_sampler._collect_row()
    assert row["mem_avail_mb"] is not None
    assert row["mem_total_mb"] is not None
    assert row["mem_total_mb"] > 0


def test_read_load_on_patched_darwin_carries_non_null_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(host_sampler, "_IS_WINDOWS", False)
    fake = _fake_sysctl({"hw.memsize": 24 * _GIB, "kern.memorystatus_level": 50})
    _real_darwin_memory_mb = host_sampler._darwin_memory_mb
    monkeypatch.setattr(host_sampler, "_darwin_memory_mb", lambda: _real_darwin_memory_mb(_sysctl=fake))
    monkeypatch.setattr(host_sampler, "_posix_cpu_load", lambda: 0.5)

    row = host_sampler.read_load()
    assert row["platform"] == "darwin"
    assert row["mem_avail_mb"] is not None
    assert row["mem_total_mb"] is not None


@pytest.mark.skipif(sys.platform != "darwin", reason="real sysctlbyname only on macOS")
def test_real_darwin_memory_is_non_null() -> None:
    used_mb, avail_mb, total_mb = host_sampler._darwin_memory_mb()
    assert total_mb is not None
    assert total_mb > 0
    assert avail_mb is not None
