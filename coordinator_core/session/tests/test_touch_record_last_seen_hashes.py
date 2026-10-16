"""last_seen_hashes is last_seen_hash batched: same answers, one read per family member."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.session import touch_record as tr

SID = "sess-batch"


def _sink(root: Path, sid: str = SID) -> Path:
    return tr.sink_path(root / ".git" / "coordinator-sessions" / sid)


def _ev(root: Path, path: str, h, *, sid: str = SID, ts: float = 1.0) -> None:
    tr.append_event(
        _sink(root, sid),
        session_id=sid,
        agent_id=None,
        verb=tr.VERB_TOUCH,
        path=path,
        content_hash=h,
        name=None,
        kind=tr.KIND_WRITE,
        timestamp=ts,
    )


def _rotate(root: Path, name: str = "touch-record.jsonl.rotated-1000-1-0.jsonl") -> Path:
    live = _sink(root)
    dest = live.with_name(name)
    os.replace(live, dest)
    return dest


def _fixture(root: Path) -> list[str]:
    _ev(root, "old_only.py", "a" * 64, ts=1)
    _ev(root, "reedited.py", "b" * 64, ts=2)
    _rotate(root)
    _ev(root, "reedited.py", "c" * 64, ts=3)
    _ev(root, "hashed.py", "d" * 64, ts=4)
    _ev(root, "hashless.py", "e" * 64, ts=5)
    _ev(root, "hashless.py", None, ts=6)
    return ["old_only.py", "reedited.py", "hashed.py", "hashless.py", "unknown.py"]


def test_parity_with_one_path_calls(tmp_path):
    paths = _fixture(tmp_path)
    batched = tr.last_seen_hashes(SID, paths, tmp_path)
    assert batched == {p: tr.last_seen_hash(SID, p, tmp_path) for p in paths}
    assert batched == {
        "old_only.py": "a" * 64,
        "reedited.py": "c" * 64,
        "hashed.py": "d" * 64,
        "hashless.py": None,
        "unknown.py": None,
    }


def test_missing_session_dir_is_all_none(tmp_path):
    assert tr.last_seen_hashes("nope", ["x.py", "y.py"], tmp_path) == {"x.py": None, "y.py": None}


def test_peer_session_events_never_supply_a_baseline(tmp_path):
    _ev(tmp_path, "p.py", "f" * 64)
    other = _sink(tmp_path, "other")
    other.parent.mkdir(parents=True)
    other.write_bytes(
        tr.encode_line(session_id="other", agent_id=None, verb=tr.VERB_TOUCH, path="q.py", content_hash="9" * 64)
    )
    assert tr.last_seen_hashes(SID, ["p.py", "q.py"], tmp_path) == {"p.py": "f" * 64, "q.py": None}


def test_malformed_line_is_skipped_and_walk_continues(tmp_path):
    _ev(tmp_path, "m.py", "1" * 64)
    with open(_sink(tmp_path), "ab") as fh:
        fh.write(b"not json\n")
    assert tr.last_seen_hashes(SID, ["m.py"], tmp_path) == {"m.py": "1" * 64}


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="chmod unreadable needs POSIX non-root")
def test_unreadable_newer_member_falls_through_to_older_hash(tmp_path):
    _ev(tmp_path, "u.py", "2" * 64)
    _rotate(tmp_path)
    _ev(tmp_path, "u.py", "3" * 64)
    live = _sink(tmp_path)
    live.chmod(0)
    try:
        assert tr.last_seen_hashes(SID, ["u.py"], tmp_path) == {"u.py": "2" * 64}
        assert tr.last_seen_hash(SID, "u.py", tmp_path) == "2" * 64
    finally:
        live.chmod(0o600)


def test_each_family_member_is_read_at_most_once(tmp_path, monkeypatch):
    paths = _fixture(tmp_path)
    reads: list[str] = []
    real = Path.read_bytes

    def spy(self):
        reads.append(self.name)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", spy)
    tr.last_seen_hashes(SID, paths, tmp_path)
    family_reads = [n for n in reads if n.startswith("touch-record.jsonl")]
    assert len(family_reads) == len(set(family_reads)) == 2


def test_walk_stops_once_every_path_is_decided(tmp_path, monkeypatch):
    _fixture(tmp_path)
    reads: list[str] = []
    real = Path.read_bytes

    def spy(self):
        reads.append(self.name)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", spy)
    assert tr.last_seen_hashes(SID, ["hashed.py"], tmp_path) == {"hashed.py": "d" * 64}
    assert [n for n in reads if n.startswith("touch-record.jsonl")] == ["touch-record.jsonl"]


def test_events_newest_decides_with_timestamp(tmp_path):
    paths = _fixture(tmp_path)
    assert tr.last_seen_events(SID, paths, tmp_path) == {
        "old_only.py": ("a" * 64, 1),
        "reedited.py": ("c" * 64, 3),
        "hashed.py": ("d" * 64, 4),
        "hashless.py": (None, 6),
        "unknown.py": None,
    }


def test_events_parity_with_last_seen_hashes(tmp_path):
    paths = _fixture(tmp_path)
    events = tr.last_seen_events(SID, paths, tmp_path)
    assert tr.last_seen_hashes(SID, paths, tmp_path) == {
        p: (ev[0] if ev else None) for p, ev in events.items()
    }


def test_events_missing_session_dir_is_all_none(tmp_path):
    assert tr.last_seen_events("nope", ["x.py", "y.py"], tmp_path) == {"x.py": None, "y.py": None}


def test_empty_and_falsy_inputs(tmp_path):
    assert tr.last_seen_hashes(SID, [], tmp_path) == {}
    assert tr.last_seen_hashes("", ["a.py"], tmp_path) == {"a.py": None}
    assert tr.last_seen_hash(SID, "", tmp_path) is None
