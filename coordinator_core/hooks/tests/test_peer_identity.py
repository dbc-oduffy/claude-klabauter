"""peer_identity leg: name and box from the roster, same-box / other-box split, advisory on failure."""

from __future__ import annotations

import asyncio
import json
import socket
import time
from types import SimpleNamespace

import pytest

from coordinator_core.hooks import peer_identity as pi
from coordinator_core.hooks import sessionstart_dispatch as sd

SELF = "8776ca63-188a-43e0-8f76-92df6a7d7513"


def _entry(sid, name, box=None):
    e = {"sessionId": sid, "name": name, "pid": 1}
    if box:
        e["unimatrixBox"] = box
    return e


@pytest.fixture
def roster(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "machine-a")
    monkeypatch.setattr(pi, "_sessions_dir", lambda: str(tmp_path))
    state = {"entries": []}

    def write(entries):
        for old in tmp_path.glob("*.json"):
            old.unlink()
        for i, e in enumerate(entries):
            (tmp_path / f"{i}.json").write_text(json.dumps(e), encoding="utf-8")
        state["entries"] = entries
        cands = [SimpleNamespace(session_id=e["sessionId"], name=e["name"]) for e in entries]
        monkeypatch.setattr(pi, "_roster_candidates", lambda: cands)

    return write


def test_names_self_and_splits_peers(roster):
    roster([
        _entry(SELF, "claude-klabauter-a1", "machine-a"),
        _entry("s2", "claude-klabauter-c2", "machine-a"),
        _entry("s3", "coordinator-content-repo-a1", "macmini"),
    ])
    out = pi.identity_text({"session_id": SELF})
    assert "You are claude-klabauter-a1 on machine-a." in out
    assert "Peers on this box: claude-klabauter-c2 (machine-a)" in out
    assert "Peers on other boxes: coordinator-content-repo-a1 (macmini)" in out
    assert "claude-klabauter-a1 (" not in out


def test_unstamped_entry_falls_back_to_local_box_not_a_suffix(roster):
    roster([_entry(SELF, "claude-klabauter-a1.macmini"), _entry("s2", "x-a1")])
    out = pi.identity_text({"session_id": SELF})
    assert "on machine-a." in out
    assert "x-a1 (machine-a)" in out


def test_session_absent_from_roster_never_prints_id_prefix(roster):
    roster([_entry("s2", "x-a1", "machine-a")])
    out = pi.identity_text({"session_id": SELF})
    assert "name unrecorded in roster" in out
    assert "8776ca63" not in out
    assert "You are" not in out


def test_empty_local_box_prints_box_unrecorded(roster, monkeypatch):
    roster([_entry(SELF, "a1")])
    monkeypatch.setattr(socket, "gethostname", lambda: "")
    assert "box unrecorded" in pi.identity_text({"session_id": SELF})


def test_failure_yields_no_line(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(pi, "_sibling", boom)
    assert pi.identity_text({"session_id": SELF}) is None


def test_roster_name_exact_match_only(roster):
    roster([_entry(SELF, "a1")])
    assert pi.roster_name(SELF) == "a1"
    assert pi.roster_name(SELF[:8]) is None


def test_compact_dispatch_yields_identity_and_both_headings(roster, monkeypatch):
    roster([_entry(SELF, "claude-klabauter-a1", "machine-a"), _entry("s2", "peer", "macmini")])
    for _k, _s, fn in sd._LEGS:
        if fn != "_leg_peer_identity":
            monkeypatch.setattr(sd, fn, lambda payload: None)
    result = asyncio.run(sd._handler({"payload": {"source": "compact", "session_id": SELF}}))
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "You are claude-klabauter-a1 on machine-a" in ctx
    assert "Peers on this box:" in ctx and "Peers on other boxes:" in ctx


def test_forty_entry_roster_process_time_under_200ms(roster):
    roster([_entry(SELF, "me", "machine-a")] + [_entry(f"s{i}", f"p{i}", "machine-a" if i % 2 else "mac") for i in range(39)])
    t0 = time.process_time()
    out = pi.identity_text({"session_id": SELF})
    assert out and "You are me on machine-a." in out
    assert time.process_time() - t0 < 0.2
