"""The tracked stand-down sink takes one line per distinct record.

A stand-down fires on every tool call of a cloud session; a line per call keeps
the tree dirty forever. Repeats differing only in timestamp are dropped, a first
line and a distinct line still land, and the untracked overrides.log is untouched.
"""
from __future__ import annotations

from coordinator_core import environment
from coordinator_core.bash_guards import _write_bump_stand_down as sd

_SINK = "ownership-leg.log"


def _call(root, session="s1", evidence="ev"):
    sd._mirror_to_durable_sink(
        root,
        "%s | %s | MARK | repo | %s\n" % ("T", session, evidence),
        _SINK,
    )


def _lines(root):
    return (root / "state" / "stand-downs" / _SINK).read_text().splitlines()


def _durable(monkeypatch, value=True):
    monkeypatch.setattr(
        environment, "capability",
        lambda name, env=None: environment.Capability(name, value, "stub"),
    )


def test_first_line_lands(tmp_path, monkeypatch):
    _durable(monkeypatch)
    _call(tmp_path)
    assert len(_lines(tmp_path)) == 1


def test_repeat_differing_only_in_timestamp_is_suppressed(tmp_path, monkeypatch):
    _durable(monkeypatch)
    sd._mirror_to_durable_sink(tmp_path, "T1 | s1 | MARK | repo | ev\n", _SINK)
    sd._mirror_to_durable_sink(tmp_path, "T2 | s1 | MARK | repo | ev\n", _SINK)
    assert _lines(tmp_path) == ["T1 | s1 | MARK | repo | ev"]


def test_distinct_session_or_evidence_still_lands(tmp_path, monkeypatch):
    _durable(monkeypatch)
    _call(tmp_path)
    _call(tmp_path, session="s2")
    _call(tmp_path, evidence="other")
    assert len(_lines(tmp_path)) == 3


def test_record_older_than_the_tail_is_rewritten_not_lost(tmp_path, monkeypatch):
    _durable(monkeypatch)
    monkeypatch.setattr(sd, "_SINK_TAIL_BYTES", 64)
    _call(tmp_path)
    for i in range(5):
        _call(tmp_path, evidence="filler-%d" % i)
    _call(tmp_path)
    assert len(_lines(tmp_path)) == 7


def test_non_durable_host_writes_nothing(tmp_path, monkeypatch):
    _durable(monkeypatch, value=False)
    _call(tmp_path)
    assert not (tmp_path / "state" / "stand-downs").exists()
