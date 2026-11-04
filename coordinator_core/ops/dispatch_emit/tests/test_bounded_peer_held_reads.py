"""Stalled or oversize engine reads fail fast to a safe default on the peer-held and orphan-gate paths."""
import time

from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import _read_json_object
from coordinator_core.session import claim_index, touch_record


def test_peer_held_answers_no_hold_when_claim_lookup_stalls(tmp_path, monkeypatch):
    monkeypatch.setattr(land_run, "PEER_CLAIM_LOOKUP_DEADLINE_S", 0.1)
    monkeypatch.setattr(claim_index, "lookup", lambda *a, **k: time.sleep(2) or {})
    t0 = time.monotonic()
    held = land_run._peer_held(tmp_path, ["a.py"], "me", "run", None)
    assert time.monotonic() - t0 < 1.0
    assert held == {}


def test_bounded_claim_lookup_passes_a_fast_answer_through(monkeypatch):
    monkeypatch.setattr(claim_index, "lookup", lambda paths, cwd=None: {"a": ["s"]})
    assert land_run._bounded_claim_lookup(claim_index, ["a"], "x") == {"a": ["s"]}


def test_read_json_object_refuses_oversize_and_stalled(tmp_path, monkeypatch):
    big = tmp_path / "run.json"
    big.write_text('{"k": "' + "x" * 200 + '"}', encoding="utf-8")
    assert _read_json_object(big) == {"k": "x" * 200}
    assert hrr.bounded_json_object(big, max_bytes=50) is None
    monkeypatch.setattr(hrr, "_read_file", lambda p: time.sleep(2) or b"{}")
    t0 = time.monotonic()
    assert hrr.bounded_json_object(tmp_path / "slow.json", deadline_ms=50) is None
    assert time.monotonic() - t0 < 1.0


def test_walk_stops_at_wall_deadline(tmp_path, monkeypatch):
    for i in range(5):
        d = tmp_path / f"sess{i}"
        d.mkdir()
        (d / "touch-record.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(claim_index, "REBUILD_WALL_CAP_SECS", -1.0)
    state = claim_index._walk(str(tmp_path))
    assert state.complete is False
    assert state.abort_cause == claim_index.ABORT_CAUSE_CAP_EXCEEDED


def test_oversize_sink_degrades_not_hangs(tmp_path, monkeypatch):
    monkeypatch.setattr(touch_record, "_MAX_SINK_BYTES", 10)
    sink = tmp_path / "touch-record.jsonl"
    sink.write_bytes(b"x" * 100)
    _claims, _held, degraded, reasons = touch_record.read_stream_claims_and_held_kind(sink)
    assert degraded and reasons


def test_agent_owner_sid_caps_first_line(tmp_path):
    (tmp_path / "em-session-id.txt").write_text("s" * 100000, encoding="utf-8")
    sid, ok = touch_record.agent_owner_sid(str(tmp_path))
    assert ok and len(sid) == touch_record._AGENT_OWNER_MAX_CHARS


def test_hook_state_reads_return_default_when_open_stalls(tmp_path, monkeypatch):
    from coordinator_core.hooks import postuse_advisory_dispatch as pad

    monkeypatch.setattr(hrr, "_read_file", lambda p: time.sleep(2) or b"{}")
    t0 = time.monotonic()
    assert pad._read_workflow_run_record(str(tmp_path / "r.json")) is None
    assert pad._load_advisory_state(str(tmp_path), "sid") == {}
    assert time.monotonic() - t0 < 1.0


def test_hook_state_reads_unchanged_for_normal_files(tmp_path):
    from coordinator_core.hooks import postuse_advisory_dispatch as pad

    f = tmp_path / "r.json"
    f.write_text('{"a": 1}', encoding="utf-8")
    assert pad._read_workflow_run_record(str(f)) == {"a": 1}


def test_token_dir_scan_opens_nothing_when_unchanged(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_token

    for i in range(20):
        (tmp_path / f"r{i}.json").write_text('{"run_id": "r%d"}' % i, encoding="utf-8")
    run_token._RECORD_CACHE.clear()
    assert len(run_token._cached_records(tmp_path)) == 20
    calls = []
    with monkeypatch.context() as m:
        m.setattr(run_token, "_read", lambda p: calls.append(p))
        assert len(run_token._cached_records(tmp_path)) == 20
    assert calls == []
    (tmp_path / "r3.json").write_text('{"run_id": "r3", "x": 11111}', encoding="utf-8")
    got = dict(run_token._cached_records(tmp_path))
    assert got["r3"]["x"] == 11111
