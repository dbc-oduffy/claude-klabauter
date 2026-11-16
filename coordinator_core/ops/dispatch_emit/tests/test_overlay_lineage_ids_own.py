"""Hunks a refired run's prior lineage ids wrote are the run's own in `peer_split` (the clean-overlay attribution)."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.git import git_dir, git_objects, git_state
from coordinator_core.ops.dispatch_emit import co_declared_settle
from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
from coordinator_core.ops.dispatch_emit import hunk_provenance as hp
from coordinator_core.ops.dispatch_emit import kept_work, peer_hunk_gate
from pathlib import Path

PATH = "src/target.py"
OLD_SIZING = "state/sizings/2026-11/2026-11-15-head-bake-resolution-53a9c1.yaml"
NEW_SIZING = "state/sizings/2026-11/2026-11-15-head-bake-resolution-one-resolver-dd077e.yaml"
REFIRE = "warp-2026-11-15-head-bake-resolution-one-resolver-dd077e-20261115T081024-dd077e"
FIRST_FIRE = "ask-20261115T073026-53a9c1"
UNRELATED = "ask-20261115T070000-bbbbbb"


def _text(edits: dict) -> bytes:
    return "".join(f"{edits.get(i, f'line{i}')}\n" for i in range(1, 11)).encode()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    blobs: dict = {}

    def put(data: bytes) -> str:
        sha = f"{len(blobs) + 1:040x}"
        blobs[sha] = data
        return sha

    def no_spawn(*a, **k):
        raise AssertionError("peer_split must not spawn")

    head = put(_text({}))
    baseline = put(_text({2: "FIRST"}))
    monkeypatch.setattr(subprocess, "run", no_spawn)
    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    monkeypatch.setattr(git_dir, "resolve_git_common_dir", lambda root: tmp_path / ".git")
    monkeypatch.setattr(git_objects, "read_object", lambda cd, sha: ("blob", blobs[sha]) if sha in blobs else None)
    monkeypatch.setattr(git_state, "head_blobs", lambda root, paths: {PATH: (0o100644, head)})
    monkeypatch.setattr(hha, "_baseline", lambda root, run: {PATH: baseline})
    monkeypatch.setattr(kept_work, "checkin_form", lambda root, rel, data: data)
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
    monkeypatch.setattr(co_declared_settle, "settled_blob", lambda *a, **k: "b" * 40)

    (tmp_path / "src").mkdir()
    (tmp_path / PATH).write_bytes(_text({2: "FIRST", 8: "OWN"}))

    class World:
        root = tmp_path

        @staticmethod
        def run(run_id: str, **record) -> None:
            d = tmp_path / "state" / "warp" / run_id
            d.mkdir(parents=True, exist_ok=True)
            if record:
                (d / "run.json").write_text(json.dumps(record), encoding="utf-8")

        @staticmethod
        def kept(run_id: str, data: bytes) -> None:
            d = tmp_path / "state" / "warp" / run_id
            d.mkdir(parents=True, exist_ok=True)
            (d / "kept.json").write_text(json.dumps({"post_checkpoint": {PATH: put(data)}}), encoding="utf-8")

    return World


def _refire(world) -> None:
    world.run(REFIRE, sizing_rel=NEW_SIZING, earlier_sizings=[OLD_SIZING])


def test_first_fire_ask_bound_to_an_earlier_sizing_is_own(world) -> None:
    _refire(world)
    world.run(FIRST_FIRE, sizing_rel=OLD_SIZING)
    world.kept(FIRST_FIRE, _text({2: "FIRST"}))
    assert hp.peer_split(world.root, REFIRE, [PATH]) == ({}, {})


def test_first_fire_ask_with_no_recorded_sizing_is_own_by_its_hex(world) -> None:
    _refire(world)
    world.run(FIRST_FIRE)
    world.kept(FIRST_FIRE, _text({2: "FIRST"}))
    assert hp.peer_split(world.root, REFIRE, [PATH]) == ({}, {})


def test_resumed_from_warp_id_with_its_own_sizing_is_own(world) -> None:
    prior = "warp-2026-11-15-something-else-aaaaaa-20261115T075000-aaaaaa"
    world.run(REFIRE, sizing_rel=NEW_SIZING, resumed_from=prior)
    world.run(prior, sizing_rel="state/sizings/2026-11/unrelated-name.yaml")
    world.kept(prior, _text({2: "FIRST"}))
    assert hp.peer_split(world.root, REFIRE, [PATH]) == ({}, {})


def test_unrelated_ask_hunks_still_split(world) -> None:
    _refire(world)
    world.run(UNRELATED, sizing_rel="state/sizings/2026-11/2026-11-15-other-work-bbbbbb.yaml")
    world.kept(UNRELATED, _text({2: "FIRST"}))
    split, unsettled = hp.peer_split(world.root, REFIRE, [PATH])
    assert set(split) | set(unsettled) == {PATH}


def test_unrecorded_ask_with_a_foreign_hex_is_not_kin(world) -> None:
    _refire(world)
    world.run(UNRELATED)
    world.kept(UNRELATED, _text({2: "FIRST"}))
    split, unsettled = hp.peer_split(world.root, REFIRE, [PATH])
    assert set(split) | set(unsettled) == {PATH}


def test_first_fire_hunks_split_without_the_lineage_record(world) -> None:
    _refire(world)
    split, unsettled = hp.peer_split(world.root, REFIRE, [PATH])
    assert set(split) | set(unsettled) == {PATH}
