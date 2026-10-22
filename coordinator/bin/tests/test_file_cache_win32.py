"""Tests for coordinator/bin/lib/file_cache_win32.py.

Mapping and fake-PDH tests run on every OS; the ctypes tests run on Windows only. No test enters
quota_privilege or calls trim_working_set.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BIN / "lib"))

import file_cache_win32 as w32  # noqa: E402
from file_cache_contract import CacheLimits, CounterError  # noqa: E402

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Win32 API exists only on Windows")

_GIB = 1024**3


def _fixture(**overrides: object) -> dict:
    data = {
        "CacheBytes": 40 * _GIB,
        "AvailableBytes": 10 * _GIB,
        "StandbyCacheCoreBytes": 1 * _GIB,
        "StandbyCacheNormalPriorityBytes": 2 * _GIB,
        "StandbyCacheReserveBytes": 3 * _GIB,
        "ModifiedPageListBytes": 512,
    }
    data.update(overrides)
    return data


class _FakePdh:
    """Stands in for pdh.dll: returns `fail_at` as the status of the named call."""

    def __init__(self, fail_at: str | None = None, status: int = 0x800007D0) -> None:
        self.fail_at, self.status = fail_at, status
        self.closed = False
        self.added: list[str] = []

    def _rc(self, name: str) -> int:
        return self.status if self.fail_at == name else 0

    def PdhOpenQueryW(self, *_args: object) -> int:
        return self._rc("PdhOpenQueryW")

    def PdhAddEnglishCounterW(self, _query: object, path: str, *_rest: object) -> int:
        self.added.append(path)
        return self._rc("PdhAddEnglishCounterW")

    def PdhCollectQueryData(self, *_args: object) -> int:
        return self._rc("PdhCollectQueryData")

    def PdhGetFormattedCounterValue(self, _handle: object, _fmt: int, _t: object, ref: object) -> int:
        out = ref._obj
        out.CStatus = 0
        out.largeValue = 7
        return self._rc("PdhGetFormattedCounterValue")

    def PdhCloseQuery(self, *_args: object) -> int:
        self.closed = True
        return 0


def test_snapshot_sums_standby_fields() -> None:
    snap = w32.snapshot_from_values(_fixture())
    assert snap.cache_bytes == 40 * _GIB
    assert snap.available_bytes == 10 * _GIB
    assert snap.standby_bytes == 6 * _GIB
    assert snap.modified_bytes == 512


@pytest.mark.parametrize("field", w32.PERF_FIELDS)
def test_snapshot_missing_field_raises(field: str) -> None:
    data = _fixture()
    del data[field]
    with pytest.raises(CounterError, match=field):
        w32.snapshot_from_values(data)


def test_snapshot_rejects_negative() -> None:
    with pytest.raises(CounterError):
        w32.snapshot_from_values(_fixture(CacheBytes=-1))


def test_every_perf_field_has_a_memory_counter_path() -> None:
    assert set(w32.PDH_COUNTER_PATHS) == set(w32.PERF_FIELDS)
    assert all(p.startswith("\\Memory\\") for p in w32.PDH_COUNTER_PATHS.values())


def test_read_pdh_counters_reads_every_field_and_closes() -> None:
    pdh = _FakePdh()
    values = w32.read_pdh_counters(pdh)
    assert values == {name: 7 for name in w32.PERF_FIELDS}
    assert len(pdh.added) == len(w32.PERF_FIELDS)
    assert pdh.closed


@pytest.mark.parametrize(
    "call",
    ["PdhOpenQueryW", "PdhAddEnglishCounterW", "PdhCollectQueryData", "PdhGetFormattedCounterValue"],
)
def test_pdh_error_status_raises_counter_error(call: str) -> None:
    pdh = _FakePdh(fail_at=call)
    with pytest.raises(CounterError, match=call):
        w32.read_pdh_counters(pdh)
    assert pdh.closed == (call != "PdhOpenQueryW")


def test_module_has_no_powershell_reference() -> None:
    assert Path(w32.__file__).read_text(encoding="utf-8").lower().count("powershell") == 0


@windows_only
def test_struct_sizes() -> None:
    assert ctypes.sizeof(w32.LUID) == 8
    assert ctypes.sizeof(w32.LUID_AND_ATTRIBUTES) == 12
    assert ctypes.sizeof(w32.TOKEN_PRIVILEGES) == 16


@windows_only
def test_set_system_file_cache_size_uses_size_t() -> None:
    host = w32.Win32FileCacheHost()
    argtypes = host._kernel32.SetSystemFileCacheSize.argtypes
    assert argtypes[0] is ctypes.c_size_t and argtypes[1] is ctypes.c_size_t


@windows_only
def test_is_elevated_returns_bool() -> None:
    assert isinstance(w32.Win32FileCacheHost().is_elevated(), bool)


@windows_only
def test_read_limits_returns_cache_limits() -> None:
    limits = w32.Win32FileCacheHost().read_limits()
    assert isinstance(limits, CacheLimits)
    assert limits.maximum >= limits.minimum >= 0
