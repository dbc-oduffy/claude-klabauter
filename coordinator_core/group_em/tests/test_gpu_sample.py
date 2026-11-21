"""gpu_sample: NVML through a mocked ctypes library on each OS branch, unified memory on Darwin, zero spawn."""

from __future__ import annotations

import ctypes
import subprocess

import pytest

from coordinator_core.group_em import gpu_sample

MIB = 1024 * 1024
NOT_AVAILABLE = 0xFFFFFFFFFFFFFFFF


class FakeNvml:
    """Stands in for the ctypes library: each entry takes the same args the real one does."""

    def __init__(self, devices, procs=None, init_status=0, proc_symbol="nvmlDeviceGetComputeRunningProcesses_v3"):
        self.devices = devices
        self.procs = procs or {}
        self.init_status = init_status
        self.calls = []
        if proc_symbol:
            setattr(self, proc_symbol, self._processes)

    def nvmlInit_v2(self):
        self.calls.append("init")
        return self.init_status

    def nvmlShutdown(self):
        self.calls.append("shutdown")
        return 0

    def nvmlDeviceGetCount_v2(self, ref):
        ref._obj.value = len(self.devices)
        return 0

    def nvmlDeviceGetHandleByIndex_v2(self, index, ref):
        ref._obj.value = index.value + 1
        return 0

    def _dev(self, handle):
        return self.devices[handle.value - 1]

    def nvmlDeviceGetMemoryInfo(self, handle, ref):
        dev = self._dev(handle)
        ref._obj.total = dev["total_mib"] * MIB
        ref._obj.used = dev["used_mib"] * MIB
        ref._obj.free = (dev["total_mib"] - dev["used_mib"]) * MIB
        return 0

    def nvmlDeviceGetUtilizationRates(self, handle, ref):
        ref._obj.gpu = self._dev(handle)["util"]
        return 0

    def nvmlDeviceGetName(self, handle, buf, length):
        buf.value = self._dev(handle)["name"].encode()
        return 0

    def _processes(self, handle, count_ref, infos):
        rows = self.procs.get(handle.value, [])
        if infos is None:
            count_ref._obj.value = len(rows)
            return 0 if not rows else 7
        for i, (pid, used) in enumerate(rows):
            infos[i].pid = pid
            infos[i].usedGpuMemory = used
        count_ref._obj.value = len(rows)
        return 0


RTX = {"name": "NVIDIA GeForce RTX 5090", "total_mib": 32768, "used_mib": 6144, "util": 1}


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a, **k):
        raise AssertionError("gpu_sample must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)


def test_windows_branch_reads_nvml_and_shuts_down(monkeypatch: pytest.MonkeyPatch) -> None:
    lib = FakeNvml([RTX], procs={1: [(4242, 3000 * MIB), (77, NOT_AVAILABLE)]})
    tried = []

    def loader(path):
        tried.append(path)
        return lib

    monkeypatch.setenv("SystemRoot", "SYSROOT")
    out = gpu_sample.sample(system="Windows", loader=loader)
    assert tried[0].endswith("nvml.dll") and "System32" in tried[0]
    assert out["gpus"] == [
        {"index": 0, "name": RTX["name"], "vram_total_mib": 32768, "vram_used_mib": 6144, "util_pct": 1}
    ]
    assert out["vram_by_pid"] == {4242: 3000}
    assert lib.calls == ["init", "shutdown"]


def test_windows_falls_back_to_the_nvsmi_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    lib = FakeNvml([RTX])
    tried = []

    def loader(path):
        tried.append(path)
        if "System32" in path:
            raise OSError("not found")
        return lib

    monkeypatch.setenv("SystemRoot", "SYSROOT")
    monkeypatch.setenv("ProgramFiles", "PF")
    assert "gpus" in gpu_sample.sample(system="Windows", loader=loader)
    assert "NVSMI" in tried[-1]


def test_linux_branch_loads_the_soname_and_reads_two_devices() -> None:
    second = {"name": "GPU B", "total_mib": 24576, "used_mib": 100, "util": 40}
    lib = FakeNvml([RTX, second], proc_symbol="nvmlDeviceGetComputeRunningProcesses")
    tried = []

    def loader(path):
        tried.append(path)
        return lib

    out = gpu_sample.sample(system="Linux", loader=loader)
    assert tried == ["libnvidia-ml.so.1"]
    assert [g["index"] for g in out["gpus"]] == [0, 1]
    assert out["gpus"][1]["util_pct"] == 40 and out["gpus"][1]["vram_total_mib"] == 24576


@pytest.mark.parametrize("system", ["Windows", "Linux"])
def test_unloadable_library_reports_nvml_absent(system: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SystemRoot", "SYSROOT")
    monkeypatch.setenv("ProgramFiles", "PF")

    def loader(path):
        raise OSError("missing")

    assert gpu_sample.sample(system=system, loader=loader) == {"gpu_unavailable": "nvml-absent"}


def test_init_failure_and_zero_devices_name_their_reason() -> None:
    assert gpu_sample.sample(system="Linux", loader=lambda p: FakeNvml([RTX], init_status=9)) == {
        "gpu_unavailable": "nvml-init-failed"
    }
    lib = FakeNvml([])
    assert gpu_sample.sample(system="Linux", loader=lambda p: lib) == {"gpu_unavailable": "no-gpu"}
    assert lib.calls == ["init", "shutdown"]


def test_a_raising_library_never_raises_out_of_sample() -> None:
    class Boom(FakeNvml):
        def nvmlDeviceGetCount_v2(self, ref):
            raise ctypes.ArgumentError("bad")

    lib = Boom([RTX])
    out = gpu_sample.sample(system="Linux", loader=lambda p: lib)
    assert out == {"gpu_unavailable": "nvml-error:ArgumentError"}
    assert lib.calls == ["init", "shutdown"]


def test_darwin_arm64_is_one_unified_row_sized_by_ram() -> None:
    out = gpu_sample.sample(system="Darwin", machine="arm64", ram_total_mb=lambda: 24576)
    row = out["gpus"][0]
    assert out["gpus"] == [row] and out["vram_by_pid"] == {}
    assert row["vram_total_mib"] == 24576 and row["util_pct"] is None and row["unified"] is True


def test_darwin_intel_and_other_systems_are_vendor_unsupported() -> None:
    assert gpu_sample.sample(system="Darwin", machine="x86_64") == {"gpu_unavailable": "vendor-unsupported"}
    assert gpu_sample.sample(system="FreeBSD") == {"gpu_unavailable": "vendor-unsupported"}


def test_static_rows_drop_live_figures_and_keep_the_reason() -> None:
    reading = gpu_sample.sample(system="Linux", loader=lambda p: FakeNvml([RTX]))
    assert gpu_sample.static_rows(reading) == {
        "devices": [{"name": RTX["name"], "vram_total_mib": 32768, "unified": False}]
    }
    assert gpu_sample.static_rows({"gpu_unavailable": "nvml-absent"}) == {"unavailable": "nvml-absent"}


def test_real_host_sample_returns_rows_or_a_reason() -> None:
    out = gpu_sample.sample()
    assert isinstance(out.get("gpus"), list) or isinstance(out.get("gpu_unavailable"), str)
