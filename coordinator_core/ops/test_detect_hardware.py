"""Characterization + parity tests for coordinator_core.ops.detect_hardware.

Port source: coordinator/lib/detect-hardware.sh (DoE-claude, 179 lines, retained as
a polyglot trampoline over this module on cutover).
Spec backlink: docs/plans/2026-06-23-coordinator-install-surface-dogfood-hardening.md §C4
"""
from __future__ import annotations

import os
import stat
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import detect_hardware as dh


def _write_fake_exe(bin_dir: Path, name: str, body: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / name
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _mock_tools(monkeypatch, outputs: dict[tuple, str], which_names: set[str] = frozenset()) -> None:
    """Stand in for a real fake-executable-on-PATH: patch dh.shutil.which and
    dh._run directly rather than spawning a POSIX `#!/bin/sh` shebang script.

    A bare-named `#!/bin/sh` script written to a temp PATH dir (the technique
    used elsewhere in this file for tests that only need *presence on PATH*,
    e.g. the `machine-local` set-call logger) is not runnable via
    subprocess on Windows -- there is no shebang interpreter and
    `shutil.which` only resolves names carrying a PATHEXT extension
    (.exe/.bat/.cmd/...). `_detect_cores`/`_detect_ram_gb`/`_detect_gpu`
    already take the target platform as an explicit, injectable parameter
    (not a `sys.platform` branch), so the honest cross-platform fix is to
    mock the two seams they call through -- `shutil.which` (tool presence)
    and `_run` (tool output) -- rather than to weaken or skip the assertion.

    `outputs` maps an exact `tuple(cmd)` to the raw stdout `_run` would have
    returned for that command; `which_names` is the set of tool basenames
    that should report as present on PATH.
    """
    which_set = set(which_names) | {cmd[0] for cmd in outputs}

    def fake_which(name, *a, **kw):
        return f"/fake/bin/{name}" if name in which_set else None

    def fake_run(cmd):
        return outputs.get(tuple(cmd))

    monkeypatch.setattr(dh.shutil, "which", fake_which)
    monkeypatch.setattr(dh, "_run", fake_run)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Every test gets its own HOME/PATH sandbox; no test touches the real machine."""
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("OSTYPE", raising=False)
    monkeypatch.delenv("OS", raising=False)
    monkeypatch.delenv("NUMBER_OF_PROCESSORS", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "")
    return home


# ---------------------------------------------------------------------------
# _platform classification
# ---------------------------------------------------------------------------


def test_platform_darwin(monkeypatch):
    monkeypatch.setattr(dh.sys, "platform", "darwin")
    assert dh._platform() == "macos"


def test_platform_windows_via_sys_platform(monkeypatch):
    monkeypatch.setattr(dh.sys, "platform", "win32")
    assert dh._platform() == "windows"


def test_platform_windows_via_os_var(monkeypatch):
    monkeypatch.setattr(dh.sys, "platform", "linux")
    monkeypatch.setenv("OS", "Windows_NT")
    assert dh._platform() == "windows"


def test_platform_linux_default(monkeypatch):
    monkeypatch.setattr(dh.sys, "platform", "linux")
    assert dh._platform() == "linux"


def test_platform_ignores_ostype_bash_builtin(monkeypatch):
    """Parity regression: OSTYPE is a bash-internal var, invisible to the
    exec'd Python process behind the polyglot trampoline — must NOT be read."""
    monkeypatch.setattr(dh.sys, "platform", "linux")
    monkeypatch.setenv("OSTYPE", "darwin23")  # would misclassify if read
    assert dh._platform() == "linux"


# ---------------------------------------------------------------------------
# _detect_cores
# ---------------------------------------------------------------------------


def test_detect_cores_macos_sysctl(tmp_path, monkeypatch):
    _mock_tools(monkeypatch, {("sysctl", "-n", "hw.ncpu"): "8\n"})
    assert dh._detect_cores("macos") == 8


def test_detect_cores_linux_nproc(tmp_path, monkeypatch):
    _mock_tools(monkeypatch, {("nproc",): "4\n"})
    assert dh._detect_cores("linux") == 4


def test_detect_cores_linux_proc_cpuinfo_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text("processor\t: 0\nprocessor\t: 1\nprocessor\t: 2\n")
    real_isfile = os.path.isfile
    real_open = open
    monkeypatch.setattr(os.path, "isfile", lambda p: True if p == "/proc/cpuinfo" else real_isfile(p))
    monkeypatch.setattr(
        "builtins.open",
        lambda p, *a, **kw: real_open(cpuinfo, *a, **kw) if p == "/proc/cpuinfo" else real_open(p, *a, **kw),
    )
    assert dh._detect_cores("linux") == 3


def test_detect_cores_windows_uses_psutil(monkeypatch):
    class _FakePsutil:
        @staticmethod
        def cpu_count(logical=True):
            return 32

    monkeypatch.setattr(dh, "psutil", _FakePsutil)
    assert dh._detect_cores("windows") == 32


def test_detect_cores_windows_env_fallback_when_psutil_absent(monkeypatch):
    monkeypatch.setattr(dh, "psutil", None)
    monkeypatch.setenv("NUMBER_OF_PROCESSORS", "16")
    assert dh._detect_cores("windows") == 16


def test_detect_cores_windows_env_fallback_when_psutil_returns_nothing(monkeypatch):
    class _FakePsutil:
        @staticmethod
        def cpu_count(logical=True):
            return None

    monkeypatch.setattr(dh, "psutil", _FakePsutil)
    monkeypatch.setenv("NUMBER_OF_PROCESSORS", "16")
    assert dh._detect_cores("windows") == 16


def test_detect_cores_windows_undetectable_returns_none(monkeypatch):
    monkeypatch.setattr(dh, "psutil", None)
    assert dh._detect_cores("windows") is None


def test_detect_cores_undetectable_returns_none(monkeypatch):
    monkeypatch.setenv("PATH", "")
    assert dh._detect_cores("macos") is None
    assert dh._detect_cores("linux") is None


# ---------------------------------------------------------------------------
# _detect_ram_gb (rounding parity with the bash oracle's integer arithmetic)
# ---------------------------------------------------------------------------


def test_detect_ram_gb_macos_rounds_nearest(tmp_path, monkeypatch):
    # 17179869184 bytes = exactly 16 GB
    _mock_tools(monkeypatch, {("sysctl", "-n", "hw.memsize"): "17179869184\n"})
    assert dh._detect_ram_gb("macos") == 16


def test_detect_ram_gb_linux_meminfo_kb_to_gb(tmp_path, monkeypatch):
    meminfo = tmp_path / "meminfo"
    # 25165824 kB ~= 24 GB
    meminfo.write_text("MemTotal:       25165824 kB\nMemFree:        1000 kB\n")
    real_isfile = os.path.isfile
    monkeypatch.setattr(os.path, "isfile", lambda p: p == "/proc/meminfo" or real_isfile(p))
    real_open = open
    monkeypatch.setattr("builtins.open", lambda p, *a, **kw: real_open(meminfo, *a, **kw) if p == "/proc/meminfo" else real_open(p, *a, **kw))
    assert dh._detect_ram_gb("linux") == 24


def test_detect_ram_gb_undetectable_returns_none(monkeypatch):
    monkeypatch.setenv("PATH", "")
    assert dh._detect_ram_gb("macos") is None
    assert dh._detect_ram_gb("linux") is None


def test_detect_ram_gb_windows_uses_psutil(monkeypatch):
    class _FakeVirtualMemory:
        total = 17179869184  # exactly 16 GB

    class _FakePsutil:
        @staticmethod
        def virtual_memory():
            return _FakeVirtualMemory

    monkeypatch.setattr(dh, "psutil", _FakePsutil)
    assert dh._detect_ram_gb("windows") == 16


def test_detect_ram_gb_windows_undetectable_when_psutil_absent(monkeypatch):
    monkeypatch.setattr(dh, "psutil", None)
    assert dh._detect_ram_gb("windows") is None


# ---------------------------------------------------------------------------
# _detect_gpu (best-effort, never raises)
# ---------------------------------------------------------------------------


def test_detect_gpu_macos_full_value_after_label(tmp_path, monkeypatch):
    """Negative-spec parity: 'Chipset Model:' captures the FULL value, not just
    the last word — reproduces the bash oracle's documented-correct behavior."""
    _mock_tools(
        monkeypatch,
        {
            ("system_profiler", "SPDisplaysDataType"): (
                "Graphics/Displays:\n"
                "    Apple M5 Pro:\n"
                "      Chipset Model: Apple M5 Pro\n"
                "      Type: GPU\n"
            ),
        },
    )
    gpu, vram = dh._detect_gpu("macos")
    assert gpu == "Apple M5 Pro"
    assert vram is None  # macOS never detects VRAM (parity with the bash oracle)


def test_detect_gpu_linux_lspci_strips_paren_suffix(tmp_path, monkeypatch):
    _mock_tools(
        monkeypatch,
        {
            ("lspci",): "00:02.0 VGA compatible controller: Intel Corporation UHD Graphics (rev 05)\n",
        },
    )
    gpu, vram = dh._detect_gpu("linux")
    assert gpu == "Intel Corporation UHD Graphics"
    assert vram is None


def test_detect_gpu_no_tool_available_is_silent(monkeypatch):
    monkeypatch.setenv("PATH", "")
    gpu, vram = dh._detect_gpu("linux")
    assert gpu is None
    assert vram is None


# ---------------------------------------------------------------------------
# _detect_gpu_windows (registry-based, no shell spawn) — mock-verified only;
# this box cannot execute a real Windows registry read (see run report).
# ---------------------------------------------------------------------------


def _install_fake_winreg(monkeypatch, root_node):
    """Install a fake `winreg` module in sys.modules so `import winreg` inside
    `_detect_gpu_windows` resolves to it instead of raising ImportError (the
    real module is Windows-only stdlib, unavailable on this box). `root_node`
    is `None` to simulate the Display class key itself being absent, or
    `{"subkeys": {"0000": {"values": {...}}, ...}}` to simulate adapter
    subkeys with their registry values."""

    class _FakeKeyHandle:
        def __init__(self, node):
            self.node = node

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    class _FakeWinreg:
        HKEY_LOCAL_MACHINE = object()

        @staticmethod
        def OpenKey(parent, path):
            if isinstance(parent, _FakeKeyHandle):
                node = parent.node.get("subkeys", {}).get(path)
            else:
                node = root_node
            if node is None:
                raise OSError(f"fake winreg: key not found: {path}")
            return _FakeKeyHandle(node)

        @staticmethod
        def EnumKey(key, index):
            # Review: coordinator:code-reviewer P3 — this sorted() ordering is
            # an artifact of the fake, not a verified property of the real
            # Windows API (real registry enumeration order is typically
            # insertion/creation order, not alphabetical/numeric-sorted). The
            # subkey names used in these tests are zero-padded numeric
            # strings that happen to sort correctly, so this fake stays a
            # faithful-enough stand-in for the "first adapter with a
            # DriverDesc" best-effort semantics under test.
            names = sorted(key.node.get("subkeys", {}).keys())
            if index >= len(names):
                raise OSError("fake winreg: no more subkeys")
            return names[index]

        @staticmethod
        def QueryValueEx(key, name):
            values = key.node.get("values", {})
            if name not in values:
                raise FileNotFoundError(name)
            return values[name], 1

    monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg)


def test_detect_gpu_windows_picks_discrete_over_integrated(monkeypatch):
    """Regression net for the first-found-adapter defect: the reference box
    enumerated `Intel(R) Graphics` (2GB, index 0001) ahead of an
    `NVIDIA GeForce RTX 5070 Ti` (16GB, index 0002), so `hardware.vram_gb`
    landed at 2 on a machine with 16 — and example-retrieval-repo's embed sidecar sizes
    itself off that value. Enumeration order is not a capability ranking."""
    _install_fake_winreg(
        monkeypatch,
        {
            "subkeys": {
                # index 0000: a driver entry with no DriverDesc at all
                "0000": {"values": {}},
                "0001": {
                    "values": {
                        "DriverDesc": "Intel(R) Graphics",
                        "HardwareInformation.MemorySize": 2147479552,  # ~2 GB
                    }
                },
                "0002": {
                    "values": {
                        "DriverDesc": "NVIDIA GeForce RTX 5070 Ti",
                        "HardwareInformation.qwMemorySize": 17094934528,  # ~16 GB
                    }
                },
            }
        },
    )
    name, vram_gb = dh._detect_gpu_windows()
    assert name == "NVIDIA GeForce RTX 5070 Ti"
    assert vram_gb == 16


def test_detect_gpu_windows_no_adapter_reports_vram_falls_back_to_first(monkeypatch):
    """Degraded shape, unchanged: with no VRAM value anywhere, the first
    adapter carrying a DriverDesc is reported with vram_gb=None."""
    _install_fake_winreg(
        monkeypatch,
        {
            "subkeys": {
                "0001": {"values": {"DriverDesc": "Basic Display Adapter"}},
                "0002": {"values": {"DriverDesc": "Second Adapter"}},
            }
        },
    )
    name, vram_gb = dh._detect_gpu_windows()
    assert name == "Basic Display Adapter"
    assert vram_gb is None


def test_detect_gpu_windows_registry_qwmemorysize_preferred(monkeypatch):
    _install_fake_winreg(
        monkeypatch,
        {
            "subkeys": {
                "0000": {
                    "values": {
                        "DriverDesc": "NVIDIA GeForce RTX 5090",
                        "HardwareInformation.qwMemorySize": 25769803776,  # 24 GB
                        "HardwareInformation.MemorySize": 4294967295,  # 32-bit-truncated decoy
                    }
                },
            }
        },
    )
    name, vram_gb = dh._detect_gpu_windows()
    assert name == "NVIDIA GeForce RTX 5090"
    assert vram_gb == 24  # confirms the 64-bit value wins over the 32-bit decoy


def test_detect_gpu_windows_registry_qwmemorysize_reg_binary(monkeypatch):
    """Review: coordinator:code-reviewer P2 — regression net for the P1 fix:
    HardwareInformation.qwMemorySize is frequently REG_BINARY (an 8-byte
    little-endian blob) on real drivers, not REG_QWORD. This would have
    raised TypeError on `int(bytes)` before the P1 fix, breaking the
    documented never-raises contract; asserts it now decodes cleanly."""
    _install_fake_winreg(
        monkeypatch,
        {
            "subkeys": {
                "0000": {
                    "values": {
                        "DriverDesc": "NVIDIA GeForce RTX 5090",
                        "HardwareInformation.qwMemorySize": struct.pack("<Q", 25769803776),  # 24 GB
                    }
                },
            }
        },
    )
    name, vram_gb = dh._detect_gpu_windows()
    assert name == "NVIDIA GeForce RTX 5090"
    assert vram_gb == 24


def test_detect_gpu_windows_registry_memorysize_reg_binary_4byte(monkeypatch):
    """4-byte REG_BINARY shape for the older 32-bit HardwareInformation.MemorySize."""
    _install_fake_winreg(
        monkeypatch,
        {
            "subkeys": {
                "0000": {
                    "values": {
                        "DriverDesc": "Intel UHD Graphics",
                        "HardwareInformation.MemorySize": struct.pack("<I", 1073741824),  # 1 GB
                    }
                },
            }
        },
    )
    name, vram_gb = dh._detect_gpu_windows()
    assert name == "Intel UHD Graphics"
    assert vram_gb == 1


def test_detect_gpu_windows_registry_memorysize_fallback(monkeypatch):
    _install_fake_winreg(
        monkeypatch,
        {
            "subkeys": {
                "0000": {
                    "values": {
                        "DriverDesc": "Intel UHD Graphics",
                        "HardwareInformation.MemorySize": 1073741824,  # 1 GB
                    }
                },
            }
        },
    )
    name, vram_gb = dh._detect_gpu_windows()
    assert name == "Intel UHD Graphics"
    assert vram_gb == 1


def test_detect_gpu_windows_registry_skips_subkey_without_driverdesc(monkeypatch):
    _install_fake_winreg(
        monkeypatch,
        {
            "subkeys": {
                "0000": {"values": {}},  # e.g. a placeholder/removed adapter entry
                "0001": {"values": {"DriverDesc": "AMD Radeon RX 9080 XT"}},
            }
        },
    )
    name, vram_gb = dh._detect_gpu_windows()
    assert name == "AMD Radeon RX 9080 XT"
    assert vram_gb is None


def test_detect_gpu_windows_registry_class_key_missing_is_silent(monkeypatch):
    _install_fake_winreg(monkeypatch, None)
    name, vram_gb = dh._detect_gpu_windows()
    assert name is None
    assert vram_gb is None


def test_detect_gpu_windows_end_to_end_via_detect_gpu(monkeypatch):
    """Confirms _detect_gpu('windows') wires _detect_gpu_windows through the
    same space-collapsing/first-line normalization the CIM path used to."""
    _install_fake_winreg(
        monkeypatch,
        {
            "subkeys": {
                "0000": {
                    "values": {
                        "DriverDesc": "NVIDIA  GeForce   RTX 5090",
                        "HardwareInformation.qwMemorySize": 25769803776,
                    }
                },
            }
        },
    )
    gpu, vram = dh._detect_gpu("windows")
    assert gpu == "NVIDIA GeForce RTX 5090"
    assert vram == 24


# ---------------------------------------------------------------------------
# main() — end-to-end against a fake `machine-local` CLI
# ---------------------------------------------------------------------------


def _write_fake_machine_local(bin_dir: Path, calls_log: Path) -> None:
    """A fake machine-local that logs every `set --concern hardware <key> <value>` call."""
    _write_fake_exe(
        bin_dir,
        "machine-local",
        f'if [ "$1" = "set" ]; then echo "$@" >> "{calls_log}"; exit 0; fi\nexit 9\n',
    )


def test_main_missing_machine_local_exits_1(capsys):
    rc = dh.main([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "machine-local not found" in captured.err


def test_main_success_writes_cores_and_ram(tmp_path, monkeypatch, capsys):
    # `machine-local` is invoked as a genuine child process by `_ml_set` (a
    # separate seam from the `shutil.which`/`_run` pair `_mock_tools`
    # patches for the CPU/RAM/GPU probes) -- it doesn't need real
    # subprocess execution to prove main()'s own control flow, so it is
    # mocked directly here rather than via a POSIX `#!/bin/sh` fake-exe
    # (which is not runnable via subprocess on Windows; see _mock_tools).
    calls_log = tmp_path / "calls.log"
    _mock_tools(
        monkeypatch,
        {
            ("sysctl", "-n", "hw.ncpu"): "8\n",
            ("sysctl", "-n", "hw.memsize"): "17179869184\n",
        },
    )

    def fake_ml_set(ml_bin, key, value):
        with open(calls_log, "a", encoding="utf-8") as f:
            f.write(f"{ml_bin} set --concern hardware {key} {value}\n")
        return 0

    monkeypatch.setattr(dh, "_resolve_machine_local", lambda: "machine-local")
    monkeypatch.setattr(dh, "_ml_set", fake_ml_set)
    monkeypatch.setattr(dh.sys, "platform", "darwin")

    rc = dh.main([])

    assert rc == 0
    log_text = calls_log.read_text()
    assert "hardware.cores 8" in log_text
    assert "hardware.ram_gb 16" in log_text
    out = capsys.readouterr().out
    assert "cores=8 ram_gb=16" in out
    assert "hardware audit complete" in out


def test_main_cores_undetectable_exits_1_before_writing(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    calls_log = tmp_path / "calls.log"
    _write_fake_machine_local(bin_dir, calls_log)
    # no sysctl/nproc on PATH -> cores undetectable
    monkeypatch.setattr(dh.sys, "platform", "darwin")
    monkeypatch.setenv("PATH", str(bin_dir))

    rc = dh.main([])

    assert rc == 1
    assert not calls_log.exists()
