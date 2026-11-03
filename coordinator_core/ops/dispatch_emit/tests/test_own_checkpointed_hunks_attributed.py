"""A run's own hunks left after its checkpoint attribute as own when the only other claimant is a
halted run that stopped before it started and staged against the same bytes (the 6dc8fa shape).

Records are real files under `state/warp/<run_id>/`; `own_hunks` reads them through the default path."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.git import git_dir, git_objects
from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha

PATH = "src/target.py"
ME = "ask-20261103T092804-6dc8fa"
HALTED_PEER = "warp-2026-11-03-listener-pid-status-tripwire-426cfb-20261103T005621-bd4c96"
LIVE_PEER = "ask-20261103T100000-aaaaaa"


def _text(edits: dict[int, str]) -> bytes:
    return "".join(f"{edits.get(i, f'line{i}')}\n" for i in range(1, 21)).encode()


def _h(line: int) -> rhc.Hunk:
    return rhc.Hunk(PATH, line, 1, line, 1)


def _record(root, run_id: str, *, baseline: bytes, stage: dict, kept: dict | None = None) -> None:
    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True)
    (rdir / "manifest.json").write_text(json.dumps({"write_baseline": {PATH: blob_sha(baseline)}}), encoding="utf-8")
    (rdir / "run-stage.json").write_text(json.dumps(stage), encoding="utf-8")
    if kept is not None:
        (rdir / "kept.json").write_text(json.dumps({"chunks": {"C1": {"paths": kept}}}), encoding="utf-8")


@pytest.fixture
def world(tmp_path, monkeypatch):
    blobs: dict[str, bytes] = {}

    def no_spawn(*a, **k):
        raise AssertionError("own_hunks must not spawn")

    monkeypatch.setattr(subprocess, "run", no_spawn)
    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    monkeypatch.setattr(git_dir, "resolve_git_common_dir", lambda root: tmp_path / ".git")
    monkeypatch.setattr(git_objects, "read_object", lambda cd, sha: ("blob", blobs[sha]) if sha in blobs else None)

    class World:
        root = tmp_path
        base = _text({})
        # The run's checkpoint committed line 2; line 9 is the edit left after it.
        tree = _text({2: "CHECKPOINTED", 9: "REMAINDER"})

        @classmethod
        def stage_run(cls, run_id, *, baseline=None, stage, kept=None):
            data = cls.base if baseline is None else baseline
            blobs[blob_sha(data)] = data
            _record(tmp_path, run_id, baseline=data, stage=stage, kept=kept)

    (tmp_path / "src").mkdir()
    (tmp_path / PATH).write_bytes(World.tree)
    return World


_LANDER = {"started_at": "2026-11-03T09:28:00Z"}
_HALTED_BEFORE = {"stage": "halted", "last_activity_at": "2026-11-03T01:00:00Z"}


def test_halted_holder_with_the_same_baseline_does_not_claim_the_remaining_hunks(world):
    world.stage_run(ME, stage=_LANDER)
    world.stage_run(HALTED_PEER, stage=_HALTED_BEFORE)
    split = rhc.own_hunks(world.root, ME, PATH, [_h(9)], {HALTED_PEER: True})
    assert split.own == [_h(9)]
    assert split.unattributed == [] and split.overlapping == {}


def test_live_holder_with_the_same_baseline_still_leaves_the_hunks_unattributed(world):
    world.stage_run(ME, stage=_LANDER)
    world.stage_run(LIVE_PEER, stage={"started_at": "2026-11-03T09:00:00Z"})
    split = rhc.own_hunks(world.root, ME, PATH, [_h(9)], {LIVE_PEER: True})
    assert split.own == [] and split.unattributed == [_h(9)]


def test_halted_holder_whose_kept_print_matches_the_tree_still_claims(world):
    world.stage_run(ME, stage=_LANDER)
    world.stage_run(HALTED_PEER, stage=_HALTED_BEFORE, kept={PATH: blob_sha(world.tree)})
    split = rhc.own_hunks(world.root, ME, PATH, [_h(9)], {HALTED_PEER: True})
    assert split.own == [] and split.unattributed == [_h(9)]


def test_halted_holder_that_started_after_the_lander_still_claims(world):
    world.stage_run(ME, stage=_LANDER)
    world.stage_run(HALTED_PEER, stage={"stage": "halted", "last_activity_at": "2026-11-03T10:00:00Z"})
    split = rhc.own_hunks(world.root, ME, PATH, [_h(9)], {HALTED_PEER: True})
    assert split.own == [] and split.unattributed == [_h(9)]


def _drop_from_baseline(world, run_id):
    manifest = world.root / "state" / "warp" / run_id / "manifest.json"
    manifest.write_text(json.dumps({"write_baseline": {"other/file.py": None}}), encoding="utf-8")


def test_revision_added_path_absent_from_the_baseline_attributes_own(world):
    world.stage_run(ME, stage=_LANDER)
    _drop_from_baseline(world, ME)
    split = rhc.own_hunks(world.root, ME, PATH, [_h(2), _h(9)], {})
    assert split.own == [_h(2), _h(9)] and split.unattributed == []


def test_revision_added_path_with_a_live_claimant_still_holds(world):
    world.stage_run(ME, stage=_LANDER)
    _drop_from_baseline(world, ME)
    world.stage_run(LIVE_PEER, stage={"started_at": "2026-11-03T09:00:00Z"})
    split = rhc.own_hunks(world.root, ME, PATH, [_h(9)], {LIVE_PEER: True})
    assert split.own == [] and split.unattributed == [_h(9)]


def test_unreadable_manifest_still_holds(world):
    # No record for ME at all: unknown is never own.
    split = rhc.own_hunks(world.root, ME, PATH, [_h(9)], {})
    assert split.own == [] and split.unattributed == [_h(9)]


def test_halted_holder_with_a_different_baseline_stays_a_claimant_and_its_hunk_is_never_own(world):
    # The run staged over a line-15 edit already in the tree; the halted peer staged before it.
    world.stage_run(ME, baseline=_text({15: "PEER"}), stage=_LANDER)
    world.stage_run(HALTED_PEER, stage=_HALTED_BEFORE)
    (world.root / PATH).write_bytes(_text({2: "CHECKPOINTED", 9: "REMAINDER", 15: "PEER"}))
    split = rhc.own_hunks(world.root, ME, PATH, [_h(9), _h(15)], {HALTED_PEER: True})
    assert _h(15) not in split.own
    assert split.own == []
