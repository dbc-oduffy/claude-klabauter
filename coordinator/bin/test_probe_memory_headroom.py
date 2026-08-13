"""test_probe_memory_headroom.py — test suite for probe-memory-headroom.py.

Port of: probe-memory-headroom.test.sh (coordinator-claude 71e76370, 2026-07-21). Proves the probe's CONTRACT
(stable output shape + graceful degradation), not a specific machine's numbers: every field is
always present, every value is an integer or the literal `unknown`, exit is always 0 on
success, and --human never crashes. On Linux (/proc/meminfo present) it additionally
proves ram_available_mb is a real number — the path the fan-out memory-pressure signal
depends on.

Spec backlink: docs/plans/2026-05-30-organic-ramp-concurrency-doctrine.md § 111-114 (successor signal)
Spec backlink: docs/plans/2026-07-19-debash-coordinator-windows.md (Plan C, Wave E3-d)

Converted from a hand-rolled runner (`probe-memory-headroom.test.py`) to a pytest-collectable
module; the PASS/FAIL tally is replaced by plain `assert` per check.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys

import pytest
from coordinator_core.win_portability import no_console_creationflags

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUBJECT = os.path.join(SCRIPT_DIR, "probe-memory-headroom.py")


def _is_int_or_unknown(v: str) -> bool:
    return v == "unknown" or bool(re.match(r"^\d+$", v))


def _field(out: str, key: str) -> str:
    for line in out.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return ""


@pytest.fixture(scope="module")
def key_value_output() -> str:
    result = subprocess.run(
        [sys.executable, SUBJECT], capture_output=True, text=True,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, f"rc={result.returncode}"
    return result.stdout


def test_key_value_mode_all_keys_present(key_value_output: str) -> None:
    out = key_value_output
    for key in ("ram_available_mb", "ram_total_mb", "vram_free_mb", "vram_total_mb"):
        assert re.search(rf"^{key}=", out, re.MULTILINE), f"key '{key}' not present, out={out}"


def test_every_value_is_int_or_unknown(key_value_output: str) -> None:
    for key in ("ram_available_mb", "ram_total_mb", "vram_free_mb", "vram_total_mb"):
        v = _field(key_value_output, key)
        assert _is_int_or_unknown(v), f"{key} expected int|unknown, got='{v}'"


def test_human_mode_prints_sentence_exit_zero() -> None:
    hresult = subprocess.run(
        [sys.executable, SUBJECT, "--human"], capture_output=True, text=True,
        **no_console_creationflags(),
    )
    assert hresult.returncode == 0, f"rc={hresult.returncode}"
    assert "memory headroom" in hresult.stdout, f"out={hresult.stdout}"


def test_bad_argument_usage_error_exit_two() -> None:
    dresult = subprocess.run(
        [sys.executable, SUBJECT, "--bogus"], capture_output=True, text=True,
        **no_console_creationflags(),
    )
    assert dresult.returncode == 2, f"rc={dresult.returncode}"


def test_linux_substrate_real_ram_number(key_value_output: str) -> None:
    if not (os.path.exists("/proc/meminfo") and os.access("/proc/meminfo", os.R_OK)):
        pytest.skip("no /proc/meminfo — not a Linux substrate")
    v = _field(key_value_output, "ram_available_mb")
    assert re.match(r"^\d+$", v), f"expected numeric RAM on Linux, got='{v}'"
    assert int(v) > 0, f"expected ram_available_mb > 0, got='{v}'"


def test_fmt_mb_helper() -> None:
    spec = importlib.util.spec_from_file_location("probe_memory_headroom_under_test", SUBJECT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    assert mod._fmt_mb(2048) == "~2 GB", f"got={mod._fmt_mb(2048)!r}"
    assert mod._fmt_mb(512) == "~512 MB", f"got={mod._fmt_mb(512)!r} (no zero-GB truncation)"


def _load_subject_module():
    spec = importlib.util.spec_from_file_location("probe_memory_headroom_under_test", SUBJECT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# --- _ram_from_windows: mock-only, this box is not Windows -----------------
# No real psutil-on-Windows path is exercised here; these tests mock a fake
# psutil module and prove the function's own contract (shape, MB conversion,
# never-raises). Genuinely live behavior on a Windows box is unverified.


def test_ram_from_windows_uses_psutil(monkeypatch) -> None:
    mod = _load_subject_module()

    class _FakeVirtualMemory:
        available = 4 * 1024 * 1024 * 1024  # 4 GiB
        total = 16 * 1024 * 1024 * 1024  # 16 GiB

    class _FakePsutil:
        @staticmethod
        def virtual_memory():
            return _FakeVirtualMemory

    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil)
    avail, total = mod._ram_from_windows()
    assert avail == 4096, f"expected 4096 MB available, got={avail!r}"
    assert total == 16384, f"expected 16384 MB total, got={total!r}"


def test_ram_from_windows_psutil_import_error_returns_none_none(monkeypatch) -> None:
    mod = _load_subject_module()
    monkeypatch.setitem(sys.modules, "psutil", None)
    avail, total = mod._ram_from_windows()
    assert (avail, total) == (None, None)


def test_ram_from_windows_psutil_exception_returns_none_none(monkeypatch) -> None:
    mod = _load_subject_module()

    class _RaisingPsutil:
        @staticmethod
        def virtual_memory():
            raise RuntimeError("simulated psutil internals failure")

    monkeypatch.setitem(sys.modules, "psutil", _RaisingPsutil)
    avail, total = mod._ram_from_windows()
    assert (avail, total) == (None, None)
