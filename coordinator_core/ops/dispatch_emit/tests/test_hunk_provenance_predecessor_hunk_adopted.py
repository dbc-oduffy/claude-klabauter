"""peer_split keeps a declared path's ownerless hunk a same-sizing predecessor recorded as its own
(measured-touched.json, withheld-own-delta.json) in the overlay candidate; a live peer's hunk, an
undeclared path and a different sizing's run stay split out."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from coordinator_core.git import git_dir, git_objects, git_state
from coordinator_core.ops.dispatch_emit import co_declared_settle
from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
from coordinator_core.ops.dispatch_emit import hunk_provenance as hp
from coordinator_core.ops.dispatch_emit import kept_work, peer_hunk_gate, run_lineage

PATH = "coordinator_core/ops/dispatch_emit/landing_integrity.py"
ME = "warp-2026-11-21-13b869-6"
PRED = "ask-20261121T150423-d8c559"
PEER = "ask-20261121T160000-bbbbbb"
LINES = 60


def _text(edits: dict[int, str]) -> bytes:
    return "".join(f"{edits.get(i, f'line{i}')}\n" for i in range(1, LINES + 1)).encode()


@pytest.fixture
def world(tmp_path, monkeypatch):
    blobs: dict[str, bytes] = {}
    state: dict = {"head": None, "baseline": None, "owner": [], "preds": [PRED], "declared": frozenset({PATH})}

    def no_spawn(*a, **k):
        raise AssertionError("peer_split must not spawn")

    def put(data: bytes) -> str:
        sha = f"{len(blobs) + 1:040x}"
        blobs[sha] = data
        return sha

    monkeypatch.setattr(subprocess, "run", no_spawn)
    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    monkeypatch.setattr(git_dir, "resolve_git_common_dir", lambda root: tmp_path / ".git")
    monkeypatch.setattr(git_objects, "read_object", lambda cd, sha: ("blob", blobs[sha]) if sha in blobs else None)
    monkeypatch.setattr(
        git_state, "head_blobs", lambda root, paths: {PATH: (0o100644, state["head"])} if state["head"] else {}
    )
    monkeypatch.setattr(hha, "_baseline", lambda root, run: {PATH: state["baseline"]})
    monkeypatch.setattr(kept_work, "checkin_form", lambda root, rel, data: data.replace(b"\r\n", b"\n"))
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [(PATH, o) for o in state["owner"]])
    monkeypatch.setattr(hp, "_halted_holder", lambda root, run, plan, path: None)
    monkeypatch.setattr(hp, "_non_live", lambda root, owner: False)
    monkeypatch.setattr(co_declared_settle, "settled_blob", lambda root, path, head_bytes, hunks: "b" * 40)
    monkeypatch.setattr(run_lineage, "chain_kin", lambda root, run, cands: set())
    monkeypatch.setattr(run_lineage, "lineage_kin", lambda root, run: ())
    monkeypatch.setattr(
        hp.RunInvariants, "own_artifacts",
        lambda self: SimpleNamespace(predecessors=tuple(state["preds"]), covers=lambda p: False),
    )
    monkeypatch.setattr(hp.RunInvariants, "declared", lambda self: state["declared"])

    class World:
        root = tmp_path
        st = state

        @staticmethod
        def setup_worktree_hunk() -> bytes:
            """HEAD is plain; the run's baseline already carried the predecessor's line-2 hunk, and the run
            itself added line 40. Returns HEAD plus only the line-2 hunk."""
            state["head"] = put(_text({}))
            predecessor_only = _text({2: "PREDECESSOR"})
            state["baseline"] = put(predecessor_only)
            (tmp_path / PATH).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / PATH).write_bytes(_text({2: "PREDECESSOR", 40: "OWN"}))
            return predecessor_only

        @staticmethod
        def store(data: bytes) -> str:
            return put(data)

        @staticmethod
        def record(run: str, name: str, doc) -> None:
            d = tmp_path / "state" / "warp" / run
            d.mkdir(parents=True, exist_ok=True)
            (d / name).write_text(json.dumps(doc), encoding="utf-8")

    return World


def _split(world):
    return hp.peer_split(world.root, ME, [PATH], own_plan="plan-a")


def test_a_hunk_with_no_predecessor_record_is_split_as_unknown(world):
    world.setup_worktree_hunk()
    split, unsettled = _split(world)
    assert split[PATH]["owner"] == hp.UNKNOWN_OWNER and not unsettled


def test_a_hunk_in_a_path_the_predecessor_measured_touched_stays_in_the_candidate(world):
    world.setup_worktree_hunk()
    world.record(PRED, "measured-touched.json", [PATH])
    assert _split(world) == ({}, {})


def test_a_hunk_in_the_predecessors_withheld_own_delta_blob_stays_in_the_candidate(world):
    edited = world.setup_worktree_hunk()
    world.record(PRED, "withheld-own-delta.json", {"paths": {PATH: {"blob": world.store(edited)}}})
    assert _split(world) == ({}, {})


def test_a_withheld_own_delta_blob_carrying_a_different_hunk_does_not_adopt_this_one(world):
    world.setup_worktree_hunk()
    world.record(PRED, "withheld-own-delta.json", {"paths": {PATH: {"blob": world.store(_text({9: "ELSE"}))}}})
    split, _unsettled = _split(world)
    assert split[PATH]["owner"] == hp.UNKNOWN_OWNER


def test_a_live_peers_hunk_is_still_split_though_a_predecessor_measured_the_path(world):
    world.setup_worktree_hunk()
    world.record(PRED, "measured-touched.json", [PATH])
    world.st["owner"] = [PEER]
    split, unsettled = _split(world)
    assert split[PATH]["owner"] == PEER and not unsettled


def test_a_run_outside_the_sizing_chain_is_never_adopted(world):
    world.setup_worktree_hunk()
    world.st["preds"] = []
    world.record(PEER, "measured-touched.json", [PATH])
    split, _unsettled = _split(world)
    assert split[PATH]["owner"] == hp.UNKNOWN_OWNER


def test_a_predecessor_measured_path_the_run_does_not_declare_stays_split(world):
    world.setup_worktree_hunk()
    world.record(PRED, "measured-touched.json", [PATH])
    world.st["declared"] = frozenset()
    split, _unsettled = _split(world)
    assert split[PATH]["owner"] == hp.UNKNOWN_OWNER
