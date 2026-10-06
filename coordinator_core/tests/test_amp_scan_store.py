"""Unit tests for the amplification-scan cache store."""

from __future__ import annotations

from coordinator_core.tests import _amp_scan_store as s

D = "digest-1"


def test_content_key_is_sha1_hex():
    assert s.content_key(b"") == "da39a3ee5e6b4b0d3255bfef95601890afd80709"


def test_same_size_edit_inside_racy_window_rehashes():
    m = s.StatMemo()
    mt = 10_000_000_000
    m.record("a.py", 5, mt, "k", now_ns=mt + 1_000_000)
    assert m.trust("a.py", 5, mt) is None


def test_old_unchanged_file_is_trusted_and_size_change_rehashes():
    m = s.StatMemo()
    mt = 10_000_000_000
    m.record("a.py", 5, mt, "k", now_ns=mt + s.RACY_WINDOW_NS)
    assert m.trust("a.py", 5, mt) == "k"
    assert m.trust("a.py", 6, mt) is None
    assert m.trust("a.py", 5, mt + 1) is None
    assert m.trust("b.py", 5, mt) is None


def test_roundtrip(tmp_path):
    s.save(tmp_path, {"x": 1}, {"k": [1, "a"]}, D)
    head = s.load_head(tmp_path, D)
    assert head["x"] == 1
    assert s.load_body(tmp_path, head) == {"k": [1, "a"]}


def test_absent_and_digest_mismatch_are_none(tmp_path):
    assert s.load_head(tmp_path, D) is None
    assert s.load_head(tmp_path / "nope", D) is None
    s.save(tmp_path, {}, {}, D)
    assert s.load_head(tmp_path, "other") is None


def test_corrupt_blobs_read_none(tmp_path):
    s.save(tmp_path, {}, {"a": 1}, D)
    head = s.load_head(tmp_path, D)
    body = next(tmp_path.glob("body-*.bin"))
    body.write_bytes(body.read_bytes()[:-3])
    assert s.load_body(tmp_path, head) is None
    (tmp_path / "head.bin").write_bytes(b"\x00garbage")
    assert s.load_head(tmp_path, D) is None
    assert s.load_body(tmp_path, None) is None


def test_format_version_bump_reads_none(tmp_path, monkeypatch):
    s.save(tmp_path, {}, {"a": 1}, D)
    head = s.load_head(tmp_path, D)
    monkeypatch.setattr(s, "FORMAT_VERSION", s.FORMAT_VERSION + 1)
    assert s.load_head(tmp_path, D) is None
    assert s.load_body(tmp_path, head) is None


def test_interleaved_writers_leave_consistent_pair(tmp_path):
    # Writer A's body lands, then B's whole save, then A's head.
    s.save(tmp_path, {"w": "B"}, {"w": "B"}, D)
    raw_a = s.marshal.dumps((s.FORMAT_VERSION, {"w": "A"}))
    a_id = s.content_key(raw_a)
    s.atomic_write_bytes(s._body_path(tmp_path, a_id), raw_a)
    s.save(tmp_path, {"w": "B2"}, {"w": "B2"}, D)
    s.save(tmp_path, {"w": "A"}, {"w": "A"}, D)
    head = s.load_head(tmp_path, D)
    assert head is not None
    assert s.load_body(tmp_path, head) == {"w": head["w"]}


def test_unwritable_directory_is_noop(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_bytes(b"x")
    s.save(blocker / "sub", {}, {}, D)
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "sub" in err
    assert s.load_head(blocker / "sub", D) is None


def test_analyzer_digest_tracks_source_and_extra(tmp_path):
    f = tmp_path / "a.py"
    f.write_bytes(b"1")
    d1 = s.analyzer_digest([f], b"e")
    assert d1 == s.analyzer_digest([f], b"e")
    assert d1 != s.analyzer_digest([f], b"f")
    f.write_bytes(b"2")
    assert d1 != s.analyzer_digest([f], b"e")
