"""Tests for the newer-index marker contract."""
import pytest

from coordinator_core.index_distribution import newer_index_marker as m


def test_roundtrip_and_path(tmp_path):
    p = m.write_marker(tmp_path, {"kind": "structural", "checked_at": 100.0, "newer_available": True})
    assert p == tmp_path / ".coordinator-local" / "newer-index-marker.json"
    got = m.read_marker(tmp_path)
    assert got["kind"] == "structural" and m.newer_available(got) is True


def test_absent_and_malformed_read_none(tmp_path):
    assert m.read_marker(tmp_path) is None
    p = tmp_path / m.MARKER_RELPATH
    p.parent.mkdir(parents=True)
    for bad in ("{not json", "[]", '{"kind": "bogus", "checked_at": 1}', '{"kind": "semantic"}',
                '{"kind": "semantic", "checked_at": true}'):
        p.write_text(bad, encoding="utf-8")
        assert m.read_marker(tmp_path) is None


def test_write_rejects_bad_marker(tmp_path):
    with pytest.raises(ValueError):
        m.write_marker(tmp_path, {"kind": "x", "checked_at": 1})
    with pytest.raises(ValueError):
        m.write_marker(tmp_path, {"kind": "semantic"})


def test_is_fresh_both_sides():
    mk = {"kind": "semantic", "checked_at": 1000.0}
    assert m.is_fresh(mk, 1000.0 + m.TTL_SECONDS - 1) is True
    assert m.is_fresh(mk, 1000.0 + m.TTL_SECONDS) is False
    assert m.is_fresh(None, 5.0) is False
    assert m.is_fresh({"kind": "semantic"}, 5.0) is False


def test_newer_available_only_on_explicit_true():
    assert m.newer_available(None) is False
    assert m.newer_available({"kind": "structural", "checked_at": 1}) is False
    assert m.newer_available({"newer_available": "yes"}) is False
    assert m.newer_available({"newer_available": True}) is True
