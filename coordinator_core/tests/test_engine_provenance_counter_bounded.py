"""Provenance counter stays bounded: match lines stop at a small cap, hard cap stops all."""
from coordinator_core import engine_provenance_counter as epc


def _rec(tmp_path, monkeypatch, verdict):
    monkeypatch.setattr(epc, "resolve_git_root_cheap", lambda cwd=None: str(tmp_path))
    monkeypatch.setattr(epc, "_may_write", lambda root: True)
    epc.record_engine_provenance("w", "dispatch", verdict, None, None)
    return tmp_path / "state" / epc._COUNTS_FILENAME


def test_match_stops_past_soft_cap_but_divergent_kept(tmp_path, monkeypatch):
    p = _rec(tmp_path, monkeypatch, "match")
    assert p.read_text().count("\n") == 1
    p.write_bytes(b"x" * epc._MATCH_CAP_BYTES)
    _rec(tmp_path, monkeypatch, "match")
    assert p.stat().st_size == epc._MATCH_CAP_BYTES
    _rec(tmp_path, monkeypatch, "divergent")
    assert p.stat().st_size > epc._MATCH_CAP_BYTES


def test_hard_cap_stops_everything(tmp_path, monkeypatch):
    p = _rec(tmp_path, monkeypatch, "match")
    p.write_bytes(b"x" * epc._HARD_CAP_BYTES)
    _rec(tmp_path, monkeypatch, "divergent")
    assert p.stat().st_size == epc._HARD_CAP_BYTES
