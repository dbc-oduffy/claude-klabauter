"""
C10 (plan ``2026-08-27-a-pathspec-is-not-a-scope``): tests for the
whole-file content-hash fingerprint recorded alongside a TOUCH --
``touch_record.py::compute_content_hash``, ``TouchEvent.content_hash``, and
the encode/decode round trip that carries it. See
``docs/research/2026-08-27-hunk-level-ownership-spike.md`` for why a content
hash (not ``size+mtime``) is the mechanism.

This chunk records only -- no commit-time consumer here (C11's job); these
tests cover the recording mechanism itself: computing the hash, carrying it
through encode/decode, folding it through last-verb-wins, and degrading
(never silently) when it cannot be computed or is absent.
"""

from __future__ import annotations

import hashlib

from coordinator_core.session.touch_record import (
    VERB_RELEASE,
    VERB_TOUCH,
    compute_content_hash,
    decode_line,
    encode_line,
    _last_verb_wins,
)


def test_compute_content_hash_matches_hashlib_sha256(tmp_path):
    target = tmp_path / "file.txt"
    target.write_bytes(b"hello world")
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert compute_content_hash(target) == expected


def test_compute_content_hash_returns_none_for_missing_file(tmp_path):
    assert compute_content_hash(tmp_path / "does-not-exist.txt") is None


def test_compute_content_hash_differs_for_different_content(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_bytes(b"same size a")
    b.write_bytes(b"same size b")
    assert len(b"same size a") == len(b"same size b")
    assert compute_content_hash(a) != compute_content_hash(b)


def test_encode_decode_round_trips_content_hash():
    digest = hashlib.sha256(b"payload").hexdigest()
    encoded = encode_line(
        session_id="sess-1",
        agent_id=None,
        verb=VERB_TOUCH,
        path="coordinator_core/session/touch_record.py",
        content_hash=digest,
    )
    event = decode_line(encoded)
    assert event.content_hash == digest


def test_encode_omits_hash_field_when_none():
    encoded = encode_line(
        session_id="sess-1",
        agent_id=None,
        verb=VERB_TOUCH,
        path="coordinator_core/session/touch_record.py",
    )
    assert b'"hash"' not in encoded


def test_decode_line_content_hash_defaults_to_none_when_absent():
    encoded = encode_line(
        session_id="sess-1",
        agent_id=None,
        verb=VERB_RELEASE,
        path="coordinator_core/session/touch_record.py",
    )
    event = decode_line(encoded)
    assert event.content_hash is None


def test_decode_line_rejects_non_string_hash():
    import json

    record = {
        "v": 1,
        "verb": VERB_TOUCH,
        "ts": 1.0,
        "sid": "sess-1",
        "agent": None,
        "path": "a.txt",
        "hash": 12345,
    }
    line = (json.dumps(record) + "\n").encode("utf-8")
    import pytest
    from coordinator_core.session.touch_record import MalformedRecordLine

    with pytest.raises(MalformedRecordLine):
        decode_line(line)


def test_last_verb_wins_supersedes_earlier_hash_for_same_path():
    """A later own-write's hash supersedes an earlier one for the same
    path -- the fold C11 will read from, per the brief's instruction to
    reuse the existing last-verb-wins projection rather than invent a
    second one."""
    h1 = hashlib.sha256(b"v1").hexdigest()
    h3 = hashlib.sha256(b"v3").hexdigest()
    event_v1 = decode_line(
        encode_line(
            session_id="sess-A",
            agent_id=None,
            verb=VERB_TOUCH,
            path="a.txt",
            timestamp=1.0,
            content_hash=h1,
        )
    )
    event_v3 = decode_line(
        encode_line(
            session_id="sess-A",
            agent_id=None,
            verb=VERB_TOUCH,
            path="a.txt",
            timestamp=3.0,
            content_hash=h3,
        )
    )
    folded = _last_verb_wins([event_v1, event_v3])
    assert len(folded) == 1
    assert folded[0].content_hash == h3
