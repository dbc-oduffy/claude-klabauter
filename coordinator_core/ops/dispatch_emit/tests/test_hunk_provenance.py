"""hunk_provenance.unproduced_hunks: foreign, own-only, kept-blob, CRLF, interleaved, zero-spawn."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.git import git_dir, git_objects, git_state
from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
from coordinator_core.ops.dispatch_emit import hunk_provenance as hp
from coordinator_core.ops.dispatch_emit import kept_work, peer_hunk_gate

PATH = "src/target.py"
ME = "ask-20261025T000000-aaaaaa"
PEER = "ask-20261024T000000-bbbbbb"


def _text(edits: dict[int, str]) -> bytes:
    return "".join(f"{edits.get(i, f'line{i}')}\n" for i in range(1, 11)).encode()


@pytest.fixture
def world(tmp_path, monkeypatch):
    blobs: dict[str, bytes] = {}
    state: dict = {"baseline": None, "head": None, "owner": []}

    def no_spawn(*a, **k):
        raise AssertionError("unproduced_hunks must not spawn")

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
    monkeypatch.setattr(hha, "_baseline", lambda root, run: state["baseline"])
    monkeypatch.setattr(kept_work, "checkin_form", lambda root, rel, data: data.replace(b"\r\n", b"\n"))
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [(PATH, owner) for owner in state["owner"]])

    class World:
        root = tmp_path

        @staticmethod
        def tree(data: bytes):
            (tmp_path / "src").mkdir(exist_ok=True)
            (tmp_path / PATH).write_bytes(data)

        @staticmethod
        def head(data: bytes):
            state["head"] = put(data)

        @staticmethod
        def baseline(data: bytes):
            state["baseline"] = {PATH: put(data)}

        @staticmethod
        def owner(*ids: str):
            state["owner"] = list(ids)

        @staticmethod
        def kept(raw_sha: str):
            d = tmp_path / "state" / "warp" / ME
            d.mkdir(parents=True, exist_ok=True)
            (d / "kept.json").write_text(json.dumps({"post_checkpoint": {PATH: raw_sha}}), encoding="utf-8")

    return World


def _run(w):
    return hp.unproduced_hunks(w.root, ME, [PATH], own_plan="plan-a")


def test_peer_hunk_in_baseline_is_refused_naming_path_and_owner(world):
    world.head(_text({}))
    world.baseline(_text({2: "PEER"}))  # peer wrote line 2 before this run staged
    world.tree(_text({2: "PEER", 8: "OWN"}))
    world.owner(PEER)
    found = _run(world)
    assert len(found) == 1
    assert found[0].reason == hp.FOREIGN_HUNK_UNPRODUCED
    assert found[0].path == PATH
    assert PEER in found[0].detail and "2" in found[0].detail


def test_owner_unknown_when_no_co_declarer(world):
    world.head(_text({}))
    world.baseline(_text({2: "PEER"}))
    world.tree(_text({2: "PEER"}))
    found = _run(world)
    assert [f.reason for f in found] == [hp.FOREIGN_HUNK_UNPRODUCED]
    assert hp.UNKNOWN_OWNER in found[0].detail


def test_own_hunks_only_lands(world):
    world.head(_text({}))
    world.baseline(_text({}))
    world.tree(_text({3: "OWN", 9: "OWN2"}))
    assert _run(world) == []


def test_interleaved_peer_hunk_between_own_hunks_is_found_by_content(world):
    world.head(_text({}))
    world.baseline(_text({5: "PEER"}))
    world.tree(_text({2: "OWN", 5: "PEER", 8: "OWN"}))
    found = _run(world)
    assert len(found) == 1
    assert "5" in found[0].detail and "2" not in found[0].detail.split("lines")[1].split("were")[0]


def test_kept_blob_equal_is_produced(world):
    world.head(_text({}))
    world.baseline(_text({2: "PEER"}))
    data = _text({2: "PEER", 8: "OWN"})
    world.tree(data)
    world.kept(kept_work.blob_sha(data))
    assert _run(world) == []


def test_crlf_worktree_compares_in_checkin_form(world):
    world.head(_text({}))
    world.baseline(_text({}))
    world.tree(_text({4: "OWN"}).replace(b"\n", b"\r\n"))
    assert _run(world) == []
    world.baseline(_text({4: "OWN"}))  # same hunk already staged-against: no producing row
    assert len(_run(world)) == 1


def test_unknown_baseline_holds(world):
    world.head(_text({}))
    world.tree(_text({4: "X"}))
    found = _run(world)
    assert [f.reason for f in found] == [hp.FOREIGN_HUNK_UNPRODUCED]
    assert "unreadable" in found[0].detail


def test_unchanged_path_yields_nothing(world):
    world.head(_text({}))
    world.tree(_text({}))
    assert _run(world) == []
