"""ask_stage_receipt: match, mismatch, missing receipt, edited manifest, and the partial window."""

from __future__ import annotations

import hashlib
import json

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
