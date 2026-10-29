"""A stalled op leaves a durable record under `<svc_dir>/stall-dumps/` that the
stderr-log prune, the claim prune and the claim release all leave alone."""

from __future__ import annotations

import json
import time

from coordinator_core.warm import server


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _stall(tmp_path, monkeypatch, key="k1"):
    stall_dir = tmp_path / "stall-dumps"
    claim_dir = tmp_path / "dispatch-claims"
    claim_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(server, "_WORKER_STDERR_FILE", None)
    monkeypatch.setattr(server, "_WORKER_STALL_DIR", stall_dir)
    claim = claim_dir / key
    claim.write_text("4242")
    disarm = server._arm_stall_dump(0.1, claim)
    assert _wait_for(lambda: stall_dir.is_dir() and any(stall_dir.glob("*.txt")))
    disarm()
    return stall_dir, claim_dir, claim


def test_record_holds_threads_and_leg_and_survives_prune_and_release(tmp_path, monkeypatch):
    stall_dir, claim_dir, claim = _stall(tmp_path, monkeypatch)
    (record,) = stall_dir.glob("*.txt")
    text = record.read_text(encoding="utf-8")
    assert "--- threads ---" in text and "Thread" in text
    leg_line = text.split("\n")[1]
    assert json.loads(leg_line.partition("leg: ")[2])["leg"]

    stderr_dir = tmp_path / "pool-worker-stderr"
    stderr_dir.mkdir()
    (stderr_dir / "1.log").write_text("x")
    server._prune_worker_stderr_logs(stderr_dir)
    server._release_claim(claim)
    server._prune_dispatch_claims(claim_dir)
    assert record.exists()
    assert not list(stall_dir.glob("*.tmp"))


def test_diagnosis_names_the_record_and_recovers_the_leg_after_release(tmp_path, monkeypatch):
    stall_dir, claim_dir, claim = _stall(tmp_path, monkeypatch)
    (record,) = stall_dir.glob("*.txt")
    # Rename to the pid the claim records, as a real worker's record would carry.
    named = stall_dir / f"20200101T000000Z-4242-{claim.name}.txt"
    record.rename(named)

    live = server._running_phase_diagnosis(claim, tmp_path / "stderr", stall_dir)
    assert live["stall_dump"] == str(named)
    assert live["blocked_leg"]["leg"]

    # Without a durable record the diagnosis keeps naming the stderr log.
    other = server._running_phase_diagnosis(claim, tmp_path / "stderr", tmp_path / "none")
    assert other["stall_dump"].endswith("4242.log")


def test_retention_keeps_the_newest(tmp_path, monkeypatch):
    stall_dir = tmp_path / "stall-dumps"
    stall_dir.mkdir()
    for i in range(server.STALL_DUMP_KEEP + 5):
        (stall_dir / f"2020010{1}T0000{i:02d}Z-1-k.txt").write_text("old")
    server._write_stall_record(stall_dir, None)
    names = sorted(p.name for p in stall_dir.glob("*.txt"))
    assert len(names) == server.STALL_DUMP_KEEP
    assert any("-nokey" in n for n in names)
    assert "20200101T000000Z-1-k.txt" not in names
