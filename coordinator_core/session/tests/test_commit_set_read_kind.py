"""commit_set: a path held only by this session with kind ``r`` is named in
``read_only`` and left out of ``paths``."""

import os
import subprocess

from coordinator_core.session import claim_index, touch_record

T = "2026-08-08T10:00:00.000000Z"


def _hold(base, sid, path, kinds):
    sink = os.path.join(str(base), sid, "touch-record.jsonl")
    for kind in kinds:
        touch_record.append_event(
            sink,
            session_id=sid,
            agent_id=None,
            verb=touch_record.VERB_TOUCH,
            path=path,
            timestamp=1786183200.0,
            kind=kind,
        )


def test_read_only_self_hold_leaves_paths(tmp_path):
    _hold(tmp_path, "me", "r.py", ["r"])
    cs = claim_index.commit_set("me", sessions_dir=str(tmp_path))
    assert cs.paths == []
    assert cs.read_only == ["r.py"]


def test_write_then_read_stays_in_paths(tmp_path):
    _hold(tmp_path, "me", "w.py", ["w", "r"])
    cs = claim_index.commit_set("me", sessions_dir=str(tmp_path))
    assert cs.paths == ["w.py"]
    assert cs.read_only == []


def test_kindless_hold_stays_in_paths(tmp_path):
    _hold(tmp_path, "me", "k.py", [None])
    cs = claim_index.commit_set("me", sessions_dir=str(tmp_path))
    assert cs.paths == ["k.py"]
    assert cs.read_only == []


def test_peer_read_hold_still_contests(tmp_path):
    _hold(tmp_path, "me", "c.py", ["w"])
    _hold(tmp_path, "peer", "c.py", ["r"])
    _hold(tmp_path, "peer", "p.py", ["r"])
    cs = claim_index.commit_set("me", sessions_dir=str(tmp_path))
    assert cs.paths == []
    assert cs.contested == {"c.py": ["peer"]}
    assert cs.peers == {"p.py": ["peer"]}
    assert cs.read_only == []


def test_read_only_sorted_and_zero_spawn(tmp_path, monkeypatch):
    _hold(tmp_path, "me", "b.py", ["r"])
    _hold(tmp_path, "me", "a.py", ["r"])

    def _boom(*a, **k):
        raise AssertionError("commit_set spawned a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    cs = claim_index.commit_set("me", sessions_dir=str(tmp_path))
    assert cs.read_only == ["a.py", "b.py"]
