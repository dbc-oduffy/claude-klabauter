"""landing_hold: set/is_held/defer/pending/clear round trip and both sides of each refusal."""

import json
import os

import pytest

from coordinator_core.session import landing_hold as lh


def test_not_held_by_default(tmp_path):
    assert lh.is_held(str(tmp_path), "s1") is False
    assert lh.pending(str(tmp_path), "s1") == []


def test_set_then_held_and_file_shape(tmp_path):
    assert lh.set_hold(str(tmp_path), "s1") is True
    assert lh.is_held(str(tmp_path), "s1") is True
    p = tmp_path / ".coordinator-local" / "landing-hold" / "s1.json"
    rec = json.loads(p.read_text(encoding="utf-8"))
    assert rec["session_id"] == "s1" and rec["deferred"] == [] and "set_at" in rec


def test_hold_is_session_scoped(tmp_path):
    lh.set_hold(str(tmp_path), "s1")
    assert lh.is_held(str(tmp_path), "s2") is False
    assert lh.defer(str(tmp_path), "s2", "halted_record", {}) is False


@pytest.mark.parametrize("sid", [None, "", "..", ".", "a/b", "a\\b", "x y"])
def test_unsafe_sid_is_never_held(tmp_path, sid):
    assert lh.is_held(str(tmp_path), sid) is False
    assert lh.pending(str(tmp_path), sid) == []
    assert lh.clear_hold(str(tmp_path), sid) == []
    if sid:
        assert lh.set_hold(str(tmp_path), sid) is False
    assert not (tmp_path / ".coordinator-local").exists()


def test_defer_defers_never_drops_and_clear_returns_in_order(tmp_path):
    r = str(tmp_path)
    lh.set_hold(r, "s1")
    assert lh.defer(r, "s1", "halted_record", {"run": "a"}) is True
    assert lh.defer(r, "s1", "coded_stamp", {"rows": ["1"]}) is True
    assert lh.defer(r, "s1", "checkpoint_stamp", {"n": 2}) is True
    assert [d["kind"] for d in lh.pending(r, "s1")] == [
        "halted_record", "coded_stamp", "checkpoint_stamp",
    ]
    out = lh.clear_hold(r, "s1")
    assert out[0] == {"kind": "halted_record", "params": {"run": "a"}}
    assert len(out) == 3
    assert lh.is_held(r, "s1") is False
    assert lh.pending(r, "s1") == []


def test_defer_refused_when_not_held_or_unknown_kind(tmp_path):
    r = str(tmp_path)
    assert lh.defer(r, "s1", "halted_record", {}) is False
    lh.set_hold(r, "s1")
    assert lh.defer(r, "s1", "bogus", {}) is False
    assert lh.pending(r, "s1") == []


def test_set_hold_again_keeps_deferred(tmp_path):
    r = str(tmp_path)
    lh.set_hold(r, "s1")
    lh.defer(r, "s1", "halted_record", {"x": 1})
    lh.set_hold(r, "s1")
    assert len(lh.pending(r, "s1")) == 1


def test_clear_when_absent_is_empty(tmp_path):
    assert lh.clear_hold(str(tmp_path), "s1") == []


def test_malformed_record_is_held_but_pending_empty(tmp_path):
    d = tmp_path / ".coordinator-local" / "landing-hold"
    d.mkdir(parents=True)
    (d / "s1.json").write_text("{not json", encoding="utf-8")
    assert lh.is_held(str(tmp_path), "s1") is True
    assert lh.pending(str(tmp_path), "s1") == []
    assert lh.clear_hold(str(tmp_path), "s1") == []
    assert not os.path.exists(d / "s1.json")
