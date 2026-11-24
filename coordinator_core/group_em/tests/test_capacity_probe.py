"""capacity_probe.sample: FIELDS contract, per-OS commit memory, never-raises, spawn count, real host."""

import json
import subprocess
import time

import pytest

from coordinator_core.group_em import capacity_probe, fleet_live

LINUX_MEMINFO = """MemTotal:       16384000 kB
MemAvailable:    8192000 kB
SwapTotal:       4096000 kB
SwapFree:        3072000 kB
CommitLimit:    12288000 kB
Committed_AS:    9216000 kB
"""

READERS = {
    "box": lambda: "box-1",
    "alias": lambda: "gipsy",
    "os": lambda: "Linux",
    "cores": lambda: 24,
    "cpu_model": lambda: "Test CPU",
    "memory": lambda: (98304, 40000, 120000, 70000),
    "cpu_pct": lambda: 12.5,
    "load5": lambda: 1.5,
    "gpus": lambda: [
        {"index": 0, "name": "G1", "vram_total_mib": 24000, "vram_used_mib": 100, "util_pct": 3},
        {"index": 1, "name": "G2", "vram_total_mib": 24000, "vram_used_mib": 50, "util_pct": 9},
    ],
    "disks": lambda: [{"mount": "/data", "total_gb": 1.0, "free_gb": 0.5, "floor_gb": 50.0}],
    "sessions": lambda: 7,
    "jobs": lambda: [{"run": "r1", "job_type": "t", "gpu_index": 0}],
    "gpu_slot_holder": lambda: "s1",
    "editor_slot_holder": lambda: None,
    "top_cpu": lambda: [{"pid": 1, "comm": "a", "pcpu": 9.0}],
    "top_ram": lambda: [{"pid": 2, "comm": "b", "rss_mb": 5}],
}


def test_sample_keys_equal_fleet_live_fields_and_fill_from_readers():
    row = capacity_probe.sample(now=100.0, readers=READERS)
    assert tuple(row) == tuple(fleet_live.FIELDS)
    assert row["state"] == "ok" and row["maxima_source"] == "live"
    assert (row["box"], row["alias"], row["os"]) == ("box-1", "gipsy", "Linux")
    assert (row["ram_total_mb"], row["ram_free_mb"]) == (98304, 40000)
    assert (row["commit_limit_mb"], row["commit_used_mb"]) == (120000, 70000)
    assert row["vram_used_mib"] == 150 and row["gpu_util_pct"] == 9
    assert row["sampled_at"] == 100.0 and row["last_seen"] == 100.0
    assert row["jobs"][0]["run"] == "r1"
    assert row["gpu_slot_holder"] == "s1" and row["editor_slot_holder"] is None
    assert row["top_ram"][0]["rss_mb"] == 5


def test_sample_never_raises_when_every_reader_fails():
    def boom():
        raise RuntimeError("x")

    row = capacity_probe.sample(readers={k: boom for k in capacity_probe._DEFAULT_READERS})
    assert tuple(row) == tuple(fleet_live.FIELDS)
    assert row["ram_total_mb"] is None and row["gpus"] == [] and row["top_cpu"] == [] and row["jobs"] == []
    assert row["vram_used_mib"] is None and row["gpu_util_pct"] is None


def test_malformed_readers_degrade_to_none_or_empty():
    row = capacity_probe.sample(readers={**READERS, "memory": lambda: "bad", "gpus": lambda: "x",
                                         "top_cpu": lambda: None})
    assert row["commit_used_mb"] is None and row["gpus"] == [] and row["top_cpu"] == []


def test_windows_commit_charge_and_limit_from_pagefile_totals():
    row_mem = capacity_probe.read_memory(
        system="Windows", ram=(1000, 4000), windows_pagefile=lambda: (200000, 150000))
    assert row_mem == (4000, 1000, 200000, 50000)


def test_linux_commit_uses_commitlimit_and_committed_as():
    assert capacity_probe.read_memory(system="Linux", meminfo_text=LINUX_MEMINFO, ram=(8000, 16000)) == (
        16000, 8000, 12000, 9000)


def test_linux_commit_falls_back_to_ram_plus_swap_without_commit_keys():
    text = "".join(line + "\n" for line in LINUX_MEMINFO.splitlines() if not line.startswith("Commit"))
    limit, used = capacity_probe.linux_commit(capacity_probe.parse_meminfo(text))
    assert limit == 16000 + 4000
    assert used == (16000 - 8000) + (4000 - 3000)


def test_darwin_commit_is_ram_plus_swap():
    got = capacity_probe.read_memory(system="Darwin", ram=(5000, 16000), darwin_swap=lambda: (2048, 512))
    assert got == (16000, 5000, 16000 + 2048, (16000 - 5000) + 512)


def test_darwin_without_swap_reading_leaves_commit_none():
    got = capacity_probe.read_memory(system="Darwin", ram=(5000, 16000), darwin_swap=lambda: None)
    assert got[2:] == (None, None)


def test_read_memory_never_raises_on_reader_failure():
    def boom():
        raise OSError("x")

    assert capacity_probe.read_memory(system="Windows", ram=(1, 2), windows_pagefile=boom) == (2, 1, None, None)


def test_disk_floor_env_overrides_named_default(monkeypatch):
    monkeypatch.delenv("COORDINATOR_DISK_FLOOR_GB", raising=False)
    assert capacity_probe.disk_floor_gb() == capacity_probe.DEFAULT_DISK_FLOOR_GB
    monkeypatch.setenv("COORDINATOR_DISK_FLOOR_GB", "12.5")
    assert capacity_probe.disk_floor_gb() == 12.5
    monkeypatch.setenv("COORDINATOR_DISK_FLOOR_GB", "junk")
    assert capacity_probe.disk_floor_gb() == capacity_probe.DEFAULT_DISK_FLOOR_GB


def test_read_disks_carries_floor_and_skips_unreadable(tmp_path):
    rows = capacity_probe.read_disks(mounts=[str(tmp_path), str(tmp_path / "missing")], floor=7.0)
    assert [r["mount"] for r in rows] == [str(tmp_path)]
    assert rows[0]["floor_gb"] == 7.0 and rows[0]["total_gb"] > 0


def test_slot_holder_reads_record_under_settings_home(monkeypatch, tmp_path):
    box = tmp_path / "state" / "group-em" / "box"
    box.mkdir(parents=True)
    (box / "gpu-slot.json").write_text(json.dumps({"session_id": "abc"}), encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    assert capacity_probe._slot_holder("gpu", []) == "abc"
    assert capacity_probe._slot_holder("editor", []) is None


def test_gpu_slot_holder_falls_back_to_grant_pinning_a_gpu(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    jobs = [{"run": "cpu-run", "gpu_index": None}, {"run": "gpu-run", "gpu_index": 1}]
    assert capacity_probe._slot_holder("gpu", jobs) == "gpu-run"
    assert capacity_probe._slot_holder("editor", jobs) is None


def test_jobs_are_the_live_admission_grants(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    ledger = tmp_path / "state" / "group-em" / "box" / "capacity-grants.jsonl"
    ledger.parent.mkdir(parents=True)
    now = time.time()
    rows = [{"run": "live", "job_type": "t", "granted_at": now, "ttl_s": 600, "ram_mb": 10},
            {"run": "old", "job_type": "t", "granted_at": now - 99999, "ttl_s": 600}]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert [j["run"] for j in capacity_probe.read_jobs()] == ["live"]


def test_real_host_sample_fills_maxima_commit_and_spawns_at_most_one_child(monkeypatch):
    spawns = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def counting_run(*a, **k):
        spawns.append(a[0] if a else k.get("args"))
        return real_run(*a, **k)

    class CountingPopen(real_popen):
        def __init__(self, *a, **k):
            spawns.append(a[0] if a else k.get("args"))
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "run", counting_run)
    monkeypatch.setattr(subprocess, "Popen", CountingPopen)
    started = time.process_time()
    row = capacity_probe.sample()
    cost = time.process_time() - started
    assert tuple(row) == tuple(fleet_live.FIELDS)
    assert row["state"] == "ok" and row["os"] and row["cores"] >= 1
    assert row["ram_total_mb"] > 0 and row["disks"]
    assert row["commit_limit_mb"] and row["commit_used_mb"] is not None
    assert row["sampled_at"] and row["top_ram"]
    # top_cpu and top_ram share one ps on POSIX; Windows spawns none.
    assert len(spawns) <= 1, spawns
    assert cost < 0.5, cost
    json.dumps(row)
