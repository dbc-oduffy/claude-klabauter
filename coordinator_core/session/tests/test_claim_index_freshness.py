"""Freshness contract for ``claim_index.lookup``: any lookup implementation must keep these green.

Written against the public surface (``lookup`` plus the touch_record writers); the one exception is the
unreadable-sink case, which patches ``Path.read_bytes`` and so assumes the sink read goes through it.
"""

import os
import shutil
from pathlib import Path

from coordinator_core.session import claim_index, touch_record

T0 = 1_780_000_000.0
W = touch_record.KIND_WRITE
R = touch_record.KIND_READ


def _sink(base, sid):
    return os.path.join(base, sid, "touch-record.jsonl")


def _ev(base, sid, verb, path, kind=W, ts=T0):
    touch_record.append_event(
        _sink(base, sid),
        session_id=sid,
        agent_id=None,
        verb=verb,
        path=path,
        timestamp=ts,
        kind=kind,
        name=None,
    )


def _agent(base, agent_id, owner_sid, verb, path, kind=W):
    agent_dir = os.path.join(base, ".agents", agent_id)
    os.makedirs(agent_dir, exist_ok=True)
    with open(os.path.join(agent_dir, "em-session-id.txt"), "w", encoding="utf-8") as fh:
        fh.write(owner_sid + "\n")
    touch_record.append_event(
        os.path.join(agent_dir, "touch-record.jsonl"),
        session_id=owner_sid,
        agent_id=agent_id,
        verb=verb,
        path=path,
        timestamp=T0,
        kind=kind,
        name=None,
    )


def test_second_claim_by_existing_session_seen_by_next_lookup(tmp_path):
    base = str(tmp_path)
    _ev(base, "peer", "T", "a.py")
    assert claim_index.lookup(["a.py", "b.py"], sessions_dir=base) == {
        "a.py": ["peer"],
        "b.py": [],
    }

    _ev(base, "peer", "T", "b.py", ts=T0 + 1)

    assert claim_index.lookup(["a.py", "b.py"], sessions_dir=base) == {
        "a.py": ["peer"],
        "b.py": ["peer"],
    }


def test_second_claim_via_append_touch_claims_seen_by_next_lookup(tmp_path):
    root = str(tmp_path)
    base = os.path.join(root, ".git", "coordinator-sessions")
    touch_record.append_touch_claims(["a.py"], "peer", root, kind=W)
    assert claim_index.lookup(["a.py", "b.py"], sessions_dir=base)["b.py"] == []

    touch_record.append_touch_claims(["b.py"], "peer", root, kind=W)

    assert claim_index.lookup(["b.py"], sessions_dir=base) == {"b.py": ["peer"]}


def test_brand_new_session_first_claim_seen_by_next_lookup(tmp_path):
    base = str(tmp_path)
    _ev(base, "old", "T", "a.py")
    assert claim_index.lookup(["new.py"], sessions_dir=base) == {"new.py": []}

    _ev(base, "fresh", "T", "new.py")

    assert claim_index.lookup(["new.py"], sessions_dir=base) == {"new.py": ["fresh"]}


def test_new_agent_dir_attributed_to_owner_sid(tmp_path):
    base = str(tmp_path)
    _ev(base, "owner", "T", "a.py")
    assert claim_index.lookup(["agent.py"], sessions_dir=base) == {"agent.py": []}

    _agent(base, "agent-1", "owner", "T", "agent.py")

    result = claim_index.lookup(["agent.py"], sessions_dir=base)
    assert result == {"agent.py": ["owner"]}
    assert "agent-1" not in result["agent.py"]


def test_release_after_claim_removes_claimant(tmp_path):
    base = str(tmp_path)
    _ev(base, "peer", "T", "a.py", ts=T0)
    assert claim_index.lookup(["a.py"], sessions_dir=base) == {"a.py": ["peer"]}

    _ev(base, "peer", "R", "a.py", kind=None, ts=T0 + 1)

    result = claim_index.lookup(["a.py"], sessions_dir=base)
    assert result == {"a.py": []}
    assert result.complete is True


def test_unreadable_sink_makes_unclaimed_paths_unanswerable(tmp_path, monkeypatch):
    base = str(tmp_path)
    _ev(base, "peer", "T", "a.py")
    bad_name = Path(_sink(base, "peer")).name
    real_read_bytes = Path.read_bytes

    def flaky_read_bytes(self):
        if self.name == bad_name:
            raise OSError("simulated unreadable sink")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", flaky_read_bytes)

    result = claim_index.lookup(["a.py", "other.py"], sessions_dir=base)

    assert result.complete is False
    assert result.abort_cause == claim_index.ABORT_CAUSE_IO_ERROR
    assert result["other.py"] == [claim_index.UNANSWERABLE]
    assert result["a.py"] != []


def test_reaped_sink_dir_never_yields_its_claimant(tmp_path):
    base = str(tmp_path)
    _ev(base, "gone", "T", "a.py")
    _ev(base, "stays", "T", "b.py")
    assert claim_index.lookup(["a.py", "b.py"], sessions_dir=base) == {
        "a.py": ["gone"],
        "b.py": ["stays"],
    }

    shutil.rmtree(os.path.join(base, "gone"))

    result = claim_index.lookup(["a.py", "b.py"], sessions_dir=base)
    assert result == {"a.py": [], "b.py": ["stays"]}
    assert result.held_by == {"stays": ["b.py"]}


def _claimants(result):
    return {path: sorted(sids) for path, sids in result.items()}


def test_forced_rotation_keeps_claimant_set(tmp_path):
    base = str(tmp_path)
    _ev(base, "peer", "T", "a.py", ts=T0)
    _ev(base, "peer", "T", "t.py", ts=T0)
    _ev(base, "peer", "R", "t.py", kind=None, ts=T0 + 1)
    _ev(base, "other", "T", "b.py", ts=T0)
    paths = ["a.py", "t.py", "b.py"]
    before = _claimants(claim_index.lookup(paths, sessions_dir=base))
    assert before == {"a.py": ["peer"], "t.py": [], "b.py": ["other"]}

    touch_record._rotate_oversized(Path(_sink(base, "peer")))

    assert not os.path.exists(_sink(base, "peer"))
    assert _claimants(claim_index.lookup(paths, sessions_dir=base)) == before

    _ev(base, "peer", "T", "c.py", ts=T0 + 2)
    assert claim_index.lookup(["c.py", "a.py"], sessions_dir=base) == {
        "c.py": ["peer"],
        "a.py": ["peer"],
    }


def test_compaction_keeps_claimant_set(tmp_path):
    base = str(tmp_path)
    _ev(base, "peer", "T", "a.py", ts=T0)
    _ev(base, "peer", "T", "t.py", ts=T0)
    _ev(base, "peer", "R", "t.py", kind=None, ts=T0 + 1)
    _ev(base, "peer", "T", "t.py", ts=T0 + 2)
    _ev(base, "peer", "R", "a.py", kind=None, ts=T0 + 3)
    _ev(base, "peer", "T", "a.py", ts=T0 + 4)
    paths = ["a.py", "t.py"]
    before = _claimants(claim_index.lookup(paths, sessions_dir=base))
    assert before == {"a.py": ["peer"], "t.py": ["peer"]}

    touch_record.compact_record(_sink(base, "peer"))

    assert _claimants(claim_index.lookup(paths, sessions_dir=base)) == before


def test_held_by_lists_write_paths_and_excludes_read_paths(tmp_path):
    base = str(tmp_path)
    _ev(base, "peer", "T", "a.py", W)
    _ev(base, "peer", "T", "t.py", W)
    _ev(base, "peer", "T", "r.py", R)

    result = claim_index.lookup(["a.py", "r.py"], sessions_dir=base)

    assert result["a.py"] == ["peer"]
    assert result.held_by == {"peer": ["a.py", "t.py"]}


def test_repeat_lookup_with_no_write_between_is_equal(tmp_path):
    base = str(tmp_path)
    _ev(base, "peer", "T", "a.py")
    _ev(base, "other", "T", "a.py", ts=T0 + 1)
    paths = ["a.py", "none.py"]

    first = claim_index.lookup(paths, sessions_dir=base)
    second = claim_index.lookup(paths, sessions_dir=base)

    assert first == second
    assert first.complete == second.complete is True
    assert first.abort_cause == second.abort_cause is None
    assert first.held_by == second.held_by
    assert first.edit_ts == second.edit_ts
