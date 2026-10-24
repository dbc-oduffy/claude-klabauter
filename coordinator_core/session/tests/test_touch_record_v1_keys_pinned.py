"""Pins the key set touch_record's writer emits against the v1 producer contract."""

import json

from coordinator_core.session import touch_record

CONTRACT = "coordinator_core/contract/touch-record-v1-producer-contract.md"
ALWAYS = {"v", "verb", "ts", "sid", "agent", "path"}
OPTIONAL = {"hash", "name", "kind"}


def _read(sink):
    return [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]


def _msg(keys):
    return f"touch-record key set drifted to {sorted(keys)}; update {CONTRACT} and its version policy"


def test_minimal_line_emits_exactly_the_always_keys(tmp_path):
    sink = tmp_path / "sid1" / touch_record.RECORD_FILENAME
    touch_record.append_event(
        sink, session_id="sid1", agent_id=None, verb="T", path="a/b.py",
        timestamp=1.0, name=None,
    )
    (rec,) = _read(sink)
    assert set(rec) == ALWAYS, _msg(rec)
    assert rec["v"] == 1 and rec["agent"] is None


def test_full_line_emits_always_plus_optional_keys(tmp_path):
    sink = tmp_path / ".agents" / "ag1" / touch_record.RECORD_FILENAME
    touch_record.append_event(
        sink, session_id="sid1", agent_id="ag1", verb="T", path="a/b.py",
        timestamp=1.0, content_hash="ab" * 32, name="nm", kind="w",
    )
    (rec,) = _read(sink)
    assert set(rec) == ALWAYS | OPTIONAL, _msg(rec)
    assert rec["sid"] == "sid1" and rec["agent"] == "ag1"


def test_release_line_and_filename_pinned(tmp_path):
    assert touch_record.RECORD_FILENAME == "touch-record.jsonl", CONTRACT
    sink = tmp_path / "sid1" / touch_record.RECORD_FILENAME
    touch_record.append_event(
        sink, session_id="sid1", agent_id=None, verb="R", path="a/b.py",
        timestamp=1.0, name=None,
    )
    (rec,) = _read(sink)
    assert set(rec) == ALWAYS and rec["verb"] == "R", _msg(rec)
