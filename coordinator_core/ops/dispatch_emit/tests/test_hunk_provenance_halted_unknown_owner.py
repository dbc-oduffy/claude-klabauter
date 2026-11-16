"""hunk_provenance.unproduced_hunks names a halted holder or an unknown owner and settles its hunks out of
the candidate; a hunk the run itself recorded stays its own even when a peer recorded it too (case 8)."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.git import git_dir, git_objects, git_state
from coordinator_core.ops.dispatch_emit import co_declared_settle
from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
from coordinator_core.ops.dispatch_emit import hunk_provenance as hp
from coordinator_core.ops.dispatch_emit import kept_work, peer_hunk_gate

PATH = "coordinator_core/ops/dispatch_emit/deferred_emit_script.py"
ME = "warp-2026-11-14-61e845-1"
PEER = "ask-20261024T000000-bbbbbb"
HALTED = "warp-2026-11-14-aaaaaa-1"
LINES = 200


def _text(edits: dict[int, str]) -> bytes:
    return "".join(f"{edits.get(i, f'line{i}')}\n" for i in range(1, LINES + 1)).encode()


@pytest.fixture
def world(tmp_path, monkeypatch):
    blobs: dict[str, bytes] = {}
    state: dict = {"baseline": None, "head": None, "owner": [], "halted": None, "settled_hunks": None}

    def no_spawn(*a, **k):
        raise AssertionError("unproduced_hunks must not spawn")

    def put(data: bytes) -> str:
        sha = f"{len(blobs) + 1:040x}"
        blobs[sha] = data
        return sha

    def fake_blob(root, path, head_bytes, hunks):
        state["settled_hunks"] = [(h.old_start, h.old_len, h.new_start, h.new_len) for h in hunks]
        return "b" * 40

    monkeypatch.setattr(subprocess, "run", no_spawn)
    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    monkeypatch.setattr(git_dir, "resolve_git_common_dir", lambda root: tmp_path / ".git")
    monkeypatch.setattr(git_objects, "read_object", lambda cd, sha: ("blob", blobs[sha]) if sha in blobs else None)
    monkeypatch.setattr(
        git_state, "head_blobs", lambda root, paths: {PATH: (0o100644, state["head"])} if state["head"] else {}
    )
    monkeypatch.setattr(hha, "_baseline", lambda root, run: state["baseline"])
    monkeypatch.setattr(kept_work, "checkin_form", lambda root, rel, data: data.replace(b"\r\n", b"\n"))
    monkeypatch.setattr(
        peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [(PATH, o) for o in state["owner"]]
    )
    monkeypatch.setattr(hp, "_halted_holder", lambda root, run, plan, path: state["halted"])
    monkeypatch.setattr(co_declared_settle, "settled_blob", fake_blob)

    class World:
        root = tmp_path
        st = state

        @staticmethod
        def tree(data: bytes):
            (tmp_path / PATH).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / PATH).write_bytes(data)

        @staticmethod
        def head(data: bytes):
            state["head"] = put(data)

        @staticmethod
        def baseline(data: bytes):
            state["baseline"] = {PATH: put(data)}

        @staticmethod
        def store(data: bytes) -> str:
            return put(data)

        @staticmethod
        def kept(raw_sha: str, run: str = ME):
            d = tmp_path / "state" / "warp" / run
            d.mkdir(parents=True, exist_ok=True)
            (d / "kept.json").write_text(json.dumps({"post_checkpoint": {PATH: raw_sha}}), encoding="utf-8")

    return World


def _foreign_at_2(world):
    world.head(_text({}))
    world.baseline(_text({2: "PEER"}))
    world.tree(_text({2: "PEER", 8: "OWN"}))


def _call(world, settled=None):
    return hp.unproduced_hunks(world.root, ME, [PATH], own_plan="plan-a", settled=settled)


def test_a_halted_non_co_declaring_holder_is_named_and_its_hunk_settles_out(world):
    _foreign_at_2(world)
    world.st["halted"] = HALTED
    settled: dict = {}
    assert _call(world, settled) == []
    assert settled == {PATH: {"blob": "b" * 40, "owner": HALTED, "lines": "2"}}
    assert world.st["settled_hunks"] == [(8, 1, 8, 1)]


def test_an_unknown_owner_settles_out_naming_unknown_and_the_lines(world):
    _foreign_at_2(world)
    settled: dict = {}
    assert _call(world, settled) == []
    assert settled == {PATH: {"blob": "b" * 40, "owner": hp.UNKNOWN_OWNER, "lines": "2"}}


def test_without_settled_a_halted_holder_is_named_in_the_finding(world):
    _foreign_at_2(world)
    world.st["halted"] = HALTED
    found = _call(world)
    assert [f.reason for f in found] == [hp.FOREIGN_HUNK_UNPRODUCED]
    assert HALTED in found[0].detail and "2" in found[0].detail


def test_without_settled_an_unknown_owner_stays_a_finding(world):
    _foreign_at_2(world)
    found = _call(world)
    assert [f.reason for f in found] == [hp.FOREIGN_HUNK_UNPRODUCED]
    assert hp.UNKNOWN_OWNER in found[0].detail


def test_a_live_co_declared_owner_still_keeps_the_finding(world, monkeypatch):
    _foreign_at_2(world)
    world.st["owner"] = [PEER]
    monkeypatch.setattr(hp, "_non_live", lambda root, owner: False)
    settled: dict = {}
    found = _call(world, settled)
    assert [f.reason for f in found] == [hp.FOREIGN_HUNK_UNPRODUCED]
    assert PEER in found[0].detail
    assert settled == {}


def test_case8_a_two_line_own_hunk_a_peer_also_recorded_is_own_never_unknown(world, monkeypatch):
    own = {189: "    emit_error(own_a)", 190: "    emit_error(own_b)"}
    world.head(_text({}))
    world.baseline(_text(own))
    world.tree(_text(own))
    world.st["owner"] = [PEER]
    monkeypatch.setattr(hp, "_non_live", lambda root, owner: False)
    world.kept(world.store(_text({**own, 5: "KEPT"})))
    world.kept(world.store(_text({**own, 6: "THEIRS"})), run=PEER)
    settled: dict = {}
    assert _call(world, settled) == []
    assert settled == {}


def test_case8_the_same_hunk_without_the_runs_own_record_is_not_credited(world):
    own = {189: "    emit_error(own_a)", 190: "    emit_error(own_b)"}
    world.head(_text({}))
    world.baseline(_text(own))
    world.tree(_text(own))
    found = _call(world)
    assert [f.reason for f in found] == [hp.FOREIGN_HUNK_UNPRODUCED]
    assert found[0].detail.startswith("committed lines 189-190 ")


def test_case8_a_hunk_only_a_peer_recorded_stays_foreign_naming_that_peer(world, monkeypatch):
    peer_edit = {189: "    emit_error(peer_a)", 190: "    emit_error(peer_b)"}
    world.head(_text({}))
    world.baseline(_text(peer_edit))
    world.tree(_text(peer_edit))
    world.st["owner"] = [PEER]
    monkeypatch.setattr(hp, "_non_live", lambda root, owner: False)
    world.kept(world.store(_text({5: "KEPT"})))
    world.kept(world.store(_text(peer_edit)), run=PEER)
    found = _call(world)
    assert [f.reason for f in found] == [hp.FOREIGN_HUNK_UNPRODUCED]
    assert PEER in found[0].detail and "189-190" in found[0].detail
