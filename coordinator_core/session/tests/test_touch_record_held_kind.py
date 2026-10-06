"""Held-kind fold beside last-verb-wins, read in one family pass."""

from __future__ import annotations

from coordinator_core.session import touch_record as tr
from coordinator_core.session.touch_record import (
    KIND_READ,
    KIND_WRITE,
    VERB_RELEASE,
    VERB_TOUCH,
    _read_stream_claims,
    append_event,
    read_stream_claims_and_held_kind,
)


def _put(sink, verb, path, kind=None, ts=1.0):
    append_event(sink, session_id="s1", agent_id=None, verb=verb, path=path,
                 kind=kind, timestamp=ts, name=None)


def _held(tmp_path, steps):
    sink = tmp_path / "s1" / "record"
    for i, (verb, kind) in enumerate(steps):
        _put(sink, verb, "a.py", kind, ts=1.0 + i)
    return read_stream_claims_and_held_kind(sink)[1]


T, R = VERB_TOUCH, VERB_RELEASE


def test_write_then_read_is_write(tmp_path):
    assert _held(tmp_path, [(T, "w"), (T, "r")]) == {"a.py": KIND_WRITE}


def test_read_read_is_read(tmp_path):
    assert _held(tmp_path, [(T, "r"), (T, "r")]) == {"a.py": KIND_READ}


def test_write_release_read_is_read(tmp_path):
    assert _held(tmp_path, [(T, "w"), (R, None), (T, "r")]) == {"a.py": KIND_READ}


def test_kindless_then_read_is_none(tmp_path):
    assert _held(tmp_path, [(T, None), (T, "r")]) == {"a.py": None}


def test_write_release_is_absent(tmp_path):
    assert _held(tmp_path, [(T, "w"), (R, None)]) == {}


def test_legacy_wrapper_unchanged_over_mixed_fixture(tmp_path):
    sink = tmp_path / "s1" / "record"
    _put(sink, T, "a.py", "w", 1.0)
    _put(sink, T, "b.py", "r", 2.0)
    _put(sink, T, "c.py", None, 3.0)
    _put(sink, R, "a.py", None, 4.0)
    claims, degraded, reasons = _read_stream_claims(sink)
    full = read_stream_claims_and_held_kind(sink)
    assert (claims, degraded, reasons) == (full[0], full[2], full[3])
    assert set(claims) == {"a.py", "b.py", "c.py"}
    assert claims["a.py"].verb == R
    assert not degraded


def test_degraded_member_flags_both_projections(tmp_path):
    sink = tmp_path / "s1" / "record"
    _put(sink, T, "a.py", "w")
    with open(sink, "ab") as fh:
        fh.write(b"garbage-not-a-record\n")
    claims, held, degraded, reasons = read_stream_claims_and_held_kind(sink)
    assert degraded and reasons
    assert held == {"a.py": KIND_WRITE}
    assert _read_stream_claims(sink)[1] is True
    assert tr.degrade_counts()
