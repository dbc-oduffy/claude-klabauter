"""live_write_guard.check_live_write: refuse a write that drops a peer's unlanded hunk."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import live_write_guard, run_hunk_claims, sibling_claims

PATH = "pkg/mod.py"
HEAD = [b"a\n", b"b\n", b"c\n", b"d\n"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / "pkg").mkdir()
    state = {"claims": {PATH: "peer-run"}, "own": False, "unattributed": False}

    monkeypatch.setattr(live_write_guard, "_head_lines", lambda root, path: list(HEAD))
    monkeypatch.setattr(
        sibling_claims, "sibling_run_claims", lambda root, run_id, cands, **kw: dict(state["claims"])
    )

    def fake_own(root, run_id, path, hunks, holders):
        if state["own"]:
            return run_hunk_claims.HunkSplit(own=list(hunks))
        if state["unattributed"]:
            return run_hunk_claims.HunkSplit(unattributed=list(hunks))
        return run_hunk_claims.HunkSplit()

    monkeypatch.setattr(run_hunk_claims, "own_hunks", fake_own)

    def write_live(lines):
        (tmp_path / PATH).write_bytes(b"".join(lines))

    state["write_live"] = write_live
    state["root"] = tmp_path
    return state


def test_drop_of_peer_insertion_is_refused_naming_holder(world):
    world["write_live"]([b"a\n", b"PEER\n", b"b\n", b"c\n", b"d\n"])
    out = live_write_guard.check_live_write(world["root"], "me", PATH, b"".join(HEAD))
    assert out is not None and "peer-run" in out and PATH in out


def test_write_preserving_peer_hunk_passes(world):
    world["write_live"]([b"a\n", b"PEER\n", b"b\n", b"c\n", b"d\n"])
    new = b"a\nPEER\nb\nc\nd\nmine\n"
    assert live_write_guard.check_live_write(world["root"], "me", PATH, new) is None


def test_drop_of_peer_deletion_is_refused(world):
    world["write_live"]([b"a\n", b"c\n", b"d\n"])
    assert live_write_guard.check_live_write(world["root"], "me", PATH, b"".join(HEAD)) is not None


def test_drop_of_peer_replacement_is_refused(world):
    world["write_live"]([b"a\n", b"B2\n", b"c\n", b"d\n"])
    assert live_write_guard.check_live_write(world["root"], "me", PATH, b"".join(HEAD)) is not None


def test_own_hunks_may_be_dropped(world):
    world["own"] = True
    world["write_live"]([b"a\n", b"MINE\n", b"b\n", b"c\n", b"d\n"])
    assert live_write_guard.check_live_write(world["root"], "me", PATH, b"".join(HEAD)) is None


def test_unattributed_hunk_drop_refuses_as_unattributed(world):
    world["unattributed"] = True
    world["write_live"]([b"a\n", b"???\n", b"b\n", b"c\n", b"d\n"])
    out = live_write_guard.check_live_write(world["root"], "me", PATH, b"".join(HEAD))
    assert out is not None and live_write_guard.UNATTRIBUTED in out


def test_unheld_path_passes(world):
    world["claims"].clear()
    world["write_live"]([b"a\n", b"PEER\n", b"b\n", b"c\n", b"d\n"])
    assert live_write_guard.check_live_write(world["root"], "me", PATH, b"".join(HEAD)) is None


def test_head_clean_live_passes(world):
    world["write_live"](HEAD)
    assert live_write_guard.check_live_write(world["root"], "me", PATH, b"x\n") is None


def test_never_raises(world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(sibling_claims, "sibling_run_claims", boom)
    assert live_write_guard.check_live_write(world["root"], "me", PATH, b"") is None
