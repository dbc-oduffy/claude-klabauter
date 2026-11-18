"""The hot-path FS probe emits its contract keys, counts calling-thread FS calls only, and pins the isolation spike."""
import json
import os
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.benchmarks import hot_path_fs_probe as probe

_CONTRACT_KEYS = {
    "inline_rotation_stats",
    "realpath_calls",
    "distinct_roots",
    "process_time_s",
    "spawns",
    "peer_ops_submitted",
    "peer_ops_completed_while_blocked",
}


@pytest.fixture(scope="module")
def record() -> dict:
    return probe.measure(envelopes=30, peer_ops=4, spike_secs=0.6)


def test_record_carries_the_contract_keys_with_sane_values(record):
    assert _CONTRACT_KEYS <= set(record)
    assert record["distinct_roots"] == probe.DISTINCT_ROOTS
    assert record["calling_thread"]["envelopes"] == 30
    assert record["realpath_calls"] >= 0 and record["inline_rotation_stats"] >= 0
    assert record["peer_ops_submitted"] == 4
    assert record["spawns"] == 0


def test_one_held_worker_leaves_a_healthy_peer_to_answer_every_op(record):
    assert record["pool"]["held_confirmed"] is True
    assert record["peer_ops_completed_while_blocked"] == record["peer_ops_submitted"]


def test_isolation_spike_pins_both_sides_of_the_decision(record):
    spike = record["isolation_spike"]
    assert spike["bare"]["held_confirmed"] is True
    assert spike["bare_saturated_peer_completed"] is False
    assert spike["run_bounded_saturated_peer_completed"] is True
    assert spike["mechanism_run_bounded_on_request_thread_fs_call"] == "holds"


def test_counters_count_the_calling_thread_and_ignore_a_peer_thread(tmp_path):
    sink = tmp_path / "op-latency.jsonl"
    sink.write_bytes(b"x")
    counters = probe._Counters(threading.get_ident())
    mp = pytest.MonkeyPatch()
    try:
        probe._install_counters(mp, counters, frozenset({"op-latency.jsonl"}))
        os.stat(sink)
        Path(tmp_path).resolve()
        os.path.realpath(tmp_path)
        peer = threading.Thread(target=lambda: (os.stat(sink), Path(tmp_path).resolve()))
        peer.start()
        peer.join()
    finally:
        mp.undo()
    assert counters.sink_stats == 1
    assert counters.resolve_calls == 1
    assert counters.bare_realpath_calls == 1


def test_main_json_prints_the_record(monkeypatch, capsys):
    fake = {k: 0 for k in _CONTRACT_KEYS}
    from coordinator_core import benchmarks

    monkeypatch.setattr(benchmarks, "declare_benchmark_origin", lambda: None)
    monkeypatch.setattr(probe, "measure", lambda **_k: fake)
    assert probe.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out) == fake
