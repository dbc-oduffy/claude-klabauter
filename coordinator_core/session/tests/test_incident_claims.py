"""Unit tests for coordinator_core.session.incident_claims (no real registry read)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from coordinator_core.session import core, incident_claims as ic, liveness, reachability

REPO = "repo-root-token"


@pytest.fixture
def world(tmp_path, monkeypatch):
    hub = tmp_path / "coordinator-sessions"
    hub.mkdir()
    state = {"sid": "sid-a", "live": {"sid-a", "sid-b", "sid-c"}}
    monkeypatch.setattr(core, "sessions_dir", lambda cwd=None: str(hub))
    monkeypatch.setattr(core, "resolve_session_id", lambda cwd=None: state["sid"])
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: sid in state["live"])
    monkeypatch.setattr(
        reachability,
        "resolve_addresses_bulk",
        lambda sids: {s: ("<this session>" if s == state["sid"] else f"addr-{s}") for s in sids},
    )
    state["hub"] = hub
    return state


def _as(world, sid):
    world["sid"] = sid


def test_windows_and_posix_spellings_share_one_hash():
    a = ic.normalize_key("coordinator_core\\ops\\x.py")
    b = ic.normalize_key("./coordinator_core/ops/x.py")
    assert a == b == "coordinator_core/ops/x.py"
    assert ic._key_hash(a) == ic._key_hash(b)


def test_slug_is_trimmed_and_case_preserved():
    assert ic.normalize_key("  Ops-Registry-Failure ") == "Ops-Registry-Failure"


@pytest.mark.parametrize("bad", ["", "   ", "/etc/passwd", "C:\\x\\y.py", "c:/x", "a/../b", "..", "..\\x"])
def test_refused_keys(bad):
    with pytest.raises(ValueError):
        ic.normalize_key(bad)


def test_two_holders_see_each_other_and_neither_is_refused(world):
    _as(world, "sid-a")
    ra = ic.set_claim(REPO, "pkg/mod.py", note="reading the import")
    assert ra.peers == []
    _as(world, "sid-b")
    rb = ic.set_claim(REPO, "pkg\\mod.py", note="same file")
    assert [p.session_id for p in rb.peers] == ["sid-a"]
    assert rb.peers[0].note == "reading the import"
    assert rb.peers[0].address == "addr-sid-a"
    assert rb.own_session_id == "sid-b"
    _as(world, "sid-a")
    ra2 = ic.set_claim(REPO, "pkg/mod.py")
    assert [p.session_id for p in ra2.peers] == ["sid-b"]
    assert all(not p.is_self for p in ra2.peers)


def test_dead_holder_is_omitted_and_pruned(world):
    _as(world, "sid-a")
    ic.set_claim(REPO, "k")
    _as(world, "sid-b")
    ic.set_claim(REPO, "k")
    world["live"].discard("sid-a")
    assert ic.list_peers(REPO, "k")[0].session_id == "sid-b"
    _as(world, "sid-c")
    res = ic.set_claim(REPO, "k")
    assert [p.session_id for p in res.peers] == ["sid-b"]
    key_dir = world["hub"] / ic.INCIDENT_CLAIMS_DIRNAME / ic._key_hash("k")
    assert not (key_dir / "sid-a").exists()
    assert (key_dir / "sid-b").is_dir()


def test_release_removes_only_own_directory(world):
    _as(world, "sid-a")
    ic.set_claim(REPO, "k")
    _as(world, "sid-b")
    ic.set_claim(REPO, "k")
    assert ic.release_claim(REPO, "k") is True
    assert ic.release_claim(REPO, "k") is False
    key_dir = world["hub"] / ic.INCIDENT_CLAIMS_DIRNAME / ic._key_hash("k")
    assert (key_dir / "sid-a").is_dir()
    assert not (key_dir / "sid-b").exists()


def test_keyless_list_returns_every_live_key(world):
    _as(world, "sid-a")
    ic.set_claim(REPO, "one")
    _as(world, "sid-b")
    ic.set_claim(REPO, "two")
    peers = ic.list_peers(REPO)
    assert sorted((p.key, p.session_id) for p in peers) == [("one", "sid-a"), ("two", "sid-b")]
    assert {p.session_id: p.is_self for p in peers} == {"sid-a": False, "sid-b": True}
    assert [p.session_id for p in ic.list_peers(REPO, "one")] == ["sid-a"]


def test_list_peers_on_empty_hub_is_empty(world):
    assert ic.list_peers(REPO) == []
    assert ic.list_peers(REPO, "nothing") == []


def test_unresolvable_sid_raises_and_writes_nothing(world):
    world["sid"] = ""
    with pytest.raises(ValueError):
        ic.set_claim(REPO, "k")
    assert not (world["hub"] / ic.INCIDENT_CLAIMS_DIRNAME).exists()


def test_bad_key_raises_from_set_claim(world):
    with pytest.raises(ValueError):
        ic.set_claim(REPO, "../escape")


def test_note_is_single_line_and_truncated(world):
    ic.set_claim(REPO, "k", note="line1\nline2 " + "x" * 400)
    holder = ic.list_peers(REPO, "k")[0]
    assert "\n" not in holder.note
    assert len(holder.note) == ic.NOTE_MAX_CHARS
    assert holder.note.startswith("line1 line2 ")


def test_holder_files_and_lf_newlines(world):
    ic.set_claim(REPO, "k", note="n")
    hd = world["hub"] / ic.INCIDENT_CLAIMS_DIRNAME / ic._key_hash("k") / "sid-a"
    assert sorted(p.name for p in hd.iterdir()) == ["claimed_at", "key", "note", "pid", "session_id"]
    assert (hd / "session_id").read_bytes() == b"sid-a\n"
    assert not (hd / "stage").exists()


def test_unreachable_holder_has_no_address(world, monkeypatch):
    _as(world, "sid-a")
    ic.set_claim(REPO, "k")
    monkeypatch.setattr(reachability, "resolve_addresses_bulk", lambda sids: {s: "" for s in sids})
    _as(world, "sid-b")
    assert ic.set_claim(REPO, "k").peers[0].address is None


def test_holder_dir_without_session_id_is_skipped(world):
    kd = world["hub"] / ic.INCIDENT_CLAIMS_DIRNAME / ic._key_hash("k") / "ghost"
    kd.mkdir(parents=True)
    assert ic.list_peers(REPO, "k") == []


def test_dataclasses_are_frozen():
    assert dataclasses.is_dataclass(ic.IncidentHolder) and dataclasses.is_dataclass(ic.IncidentClaimResult)
    h = ic.IncidentHolder("k", "s", "", "", None, False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        h.note = "x"  # type: ignore[misc]
