"""ask_stage_receipt: match, mismatch, missing receipt, edited manifest, and the partial window."""

from __future__ import annotations

import hashlib
import json
import os
import time

from coordinator_core.ops.dispatch_emit import ask_stage_receipt as r


def _stage(tmp_path, text='{"a":1}'):
    raw = text.encode("ascii")
    (tmp_path / "manifest.json").write_bytes(raw)
    return {"path": "state/warp/x/manifest.json", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def test_match_returns_handle(tmp_path):
    h = _stage(tmp_path)
    r.write(tmp_path, "d1", h)
    assert r.landed(tmp_path, "d1") == h
    assert r.partial(tmp_path) is False
    raw = (tmp_path / r.RECEIPT_NAME).read_bytes()
    assert raw.endswith(b"\n") and raw.isascii()


def test_digest_mismatch_is_none_and_not_partial(tmp_path):
    h = _stage(tmp_path)
    r.write(tmp_path, "d1", h)
    assert r.landed(tmp_path, "d2") is None
    assert r.partial(tmp_path) is False


def test_missing_receipt_is_none_and_partial(tmp_path):
    _stage(tmp_path)
    assert r.landed(tmp_path, "d1") is None
    assert r.partial(tmp_path) is True


def test_halted_run_without_receipt_is_not_partial(tmp_path):
    # A run that halted before receipts existed (691c75) must stay refireable.
    _stage(tmp_path)
    (tmp_path / "halted.json").write_text("{}", encoding="utf-8")
    assert r.partial(tmp_path) is False


def test_manifest_edited_after_receipt(tmp_path):
    h = _stage(tmp_path)
    r.write(tmp_path, "d1", h)
    (tmp_path / "manifest.json").write_bytes(b'{"a":2}')
    assert r.landed(tmp_path, "d1") is None
    assert r.partial(tmp_path) is True


def test_no_manifest_is_neither(tmp_path):
    assert r.landed(tmp_path, "d1") is None
    assert r.partial(tmp_path) is False


def test_corrupt_receipt_is_none(tmp_path):
    _stage(tmp_path)
    (tmp_path / r.RECEIPT_NAME).write_bytes(b"not json")
    assert r.landed(tmp_path, "d1") is None
    assert r.partial(tmp_path) is True


def test_digest_excludes_session_id_and_is_order_free():
    a = r.params_digest({"run_id": "x", "writes": ["p"], "session_id": "s1"})
    b = r.params_digest({"writes": ["p"], "session_id": "s2", "run_id": "x"})
    assert a == b
    assert a != r.params_digest({"run_id": "x", "writes": ["q"]})
    json.dumps(a)


def test_reply_key():
    assert r.ALREADY_STAGED_KEY == "already_staged"
    assert r.STAGE_IN_FLIGHT_KEY == "stage_in_flight"


def _write_claim(tmp_path, digest="d1", pid=None, age=0.0):
    body = {"digest": digest, "owner_pid": os.getpid() if pid is None else pid, "started_at": time.time() - age}
    (tmp_path / r.CLAIM_NAME).write_bytes(json.dumps(body).encode("ascii"))


def test_claim_then_in_flight_live_owner(tmp_path):
    assert r.claim(tmp_path, "d1") is True
    got = r.in_flight(tmp_path, "d1")
    assert got["digest"] == "d1" and got["owner_pid"] == os.getpid()
    assert (tmp_path / r.CLAIM_NAME).read_bytes().isascii()


def test_second_claim_against_live_claim_is_false(tmp_path):
    assert r.claim(tmp_path, "d1") is True
    assert r.claim(tmp_path, "d1") is False
    assert r.claim(tmp_path, "d2") is False


def test_dead_owner_is_not_in_flight_and_claim_retakes(tmp_path, monkeypatch):
    _write_claim(tmp_path, pid=999999)
    monkeypatch.setattr(r.session_registry, "pid_alive", lambda pid: False)
    assert r.in_flight(tmp_path, "d1") is None
    assert r.claim(tmp_path, "d1") is True
    monkeypatch.setattr(r.session_registry, "pid_alive", lambda pid: True)
    assert r.in_flight(tmp_path, "d1") is not None


def test_digest_mismatch_is_not_in_flight(tmp_path):
    r.claim(tmp_path, "d1")
    assert r.in_flight(tmp_path, "d2") is None


def test_stale_claim_is_dead_even_with_live_pid(tmp_path):
    _write_claim(tmp_path, age=r.CLAIM_STALE_SECS + 5)
    assert r.in_flight(tmp_path, "d1") is None
    assert r.claim(tmp_path, "d1") is True


def test_receipt_after_claim_is_not_in_flight(tmp_path):
    h = _stage(tmp_path)
    r.claim(tmp_path, "d1")
    r.write(tmp_path, "d1", h)
    assert r.in_flight(tmp_path, "d1") is None


def test_release_drops_claim_and_tolerates_absence(tmp_path):
    r.claim(tmp_path, "d1")
    r.release(tmp_path)
    assert not (tmp_path / r.CLAIM_NAME).exists()
    r.release(tmp_path)
    assert r.claim(tmp_path, "d1") is True


def test_corrupt_old_claim_is_retaken(tmp_path):
    p = tmp_path / r.CLAIM_NAME
    p.write_bytes(b"garbage")
    os.utime(p, (time.time() - 60, time.time() - 60))
    assert r.claim(tmp_path, "d1") is True


def test_partial_with_claim_is_false_without_claim_unchanged(tmp_path):
    _stage(tmp_path)
    assert r.partial(tmp_path) is True
    r.claim(tmp_path, "d1")
    assert r.partial(tmp_path) is False


def test_staleness_ceiling_clears_observed_stall():
    from coordinator_core.warm.server import _POOL_RESULT_DEADLINE_SECS

    assert r.CLAIM_STALE_SECS > 5 * 60
    assert r.CLAIM_STALE_SECS > 2 * 60 + 12
    assert r.CLAIM_STALE_SECS > _POOL_RESULT_DEADLINE_SECS


def test_stalled_create_raises_within_deadline_and_late_winner_leaves_no_claim(tmp_path, monkeypatch):
    import threading

    import pytest

    gate = threading.Event()
    real_open = os.open

    def slow_open(path, flags, mode=0o777):
        if str(path).endswith(r.CLAIM_NAME):
            gate.wait(10)
        return real_open(path, flags, mode)

    monkeypatch.setattr(r.os, "open", slow_open)
    monkeypatch.setattr(r, "CLAIM_CREATE_DEADLINE_SECS", 0.2)
    t0 = time.perf_counter()
    with pytest.raises(r.ClaimCreateStalled):
        r.claim(tmp_path, "d1")
    assert time.perf_counter() - t0 < 2.0
    gate.set()
    deadline = time.monotonic() + 3
    while any(t.name == "ask-stage-claim-create" for t in threading.enumerate()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not (tmp_path / r.CLAIM_NAME).exists()
    monkeypatch.undo()
    assert r.claim(tmp_path, "d1") is True


def test_handler_answers_a_stalled_claim_with_error(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_stage

    def stalled(record_dir, digest):
        raise r.ClaimCreateStalled("stalled")

    monkeypatch.setattr(r, "claim", stalled)
    monkeypatch.setattr(ask_stage, "main_worktree_root", lambda p: tmp_path)
    monkeypatch.setattr(ask_stage, "validate_params", lambda *a, **k: None)
    monkeypatch.setattr(ask_stage, "_consume_park_marker", lambda *a: False)
    monkeypatch.setattr(ask_stage, "_escaping_write_refusal", lambda *a, **k: None)
    monkeypatch.setattr(ask_stage, "record_dir", lambda root, run_id: tmp_path / run_id)
    reply = ask_stage._handler({"run_id": "r1", "plan_path": "p.md"}, repo_root=tmp_path)
    assert reply["error"].startswith(ask_stage.STAGE_CLAIM_STALLED)
