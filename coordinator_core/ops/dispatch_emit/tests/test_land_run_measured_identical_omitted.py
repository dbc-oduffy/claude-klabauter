"""A measured-record path whose worktree bytes equal its measured blob never refuses the landing and lands unless a
hold withholds it; a path whose bytes differ from the record still refuses `landing-tree-differs-from-measured`."""

from __future__ import annotations

import json

import pytest

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RECORD_DIR_ROOT,
    RUN_DIR_ROOT,
    StageManifest,
    measured_rev_record_path,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests import test_land_run_freed_note_agrees_withhold as fr
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests import test_land_run_refire_inherits_withheld_scope as rf
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import RUN, SELF, repo  # noqa: F401  (fixture)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PATHS = ("src/a.py", "src/b.py", "src/c.py")


@pytest.fixture(autouse=True)
def _ledger_open(monkeypatch):
    monkeypatch.setattr(land_run, "_ledger_gate", lambda *a, **k: (None, None, []))


def _record(root, paths, blobs=None):
    rec = measured_rev_record_path(root, RUN)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(
        json.dumps({
            "sha": "0" * 40, "base_sha": "0" * 40, "paths": list(paths), "removed": [], "tree": "0" * 40,
            "blobs": {**{p: commit.checkin_blob_sha(root, p) for p in paths}, **(blobs or {})},
        }),
        encoding="utf-8",
    )


def _peer_hold(root, *paths):
    prod._write(root, "state/warp/run-b/manifest.json", json.dumps({"rows": [{"id": "C1", "writes": list(paths)}]}))
    run_token.mint(root, "run-b", "b.mjs", session_id=SELF)


def _ask_stage(root, paths, text="a = 1\n"):
    for p in paths:
        prod._author(root, p, text)
    marker = f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt"
    request = CommitRequest(chunks=(ChunkCommit(id="X1", title="x1", paths=tuple(paths)),), plan_path=None)
    prod._write(root, marker, render_marker(request) + "\n")
    manifest = StageManifest(run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(), marker_path=marker)
    prod._write(root, f"{RECORD_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    prod._write(root, prod.SCRIPT, f"{ASK_MANIFEST_MARKER}{RECORD_DIR_ROOT}/{RUN}/manifest.json\n")
    prod._write(root, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"sizing_rel": rf.SIZING}))


def test_several_measured_paths_in_the_commit_with_identical_bytes_are_all_committed(repo):  # noqa: F811
    _ask_stage(repo, PATHS)
    _record(repo, PATHS)

    out = prod._land(repo)

    assert out["committed"] is True and out.get("refused") is None, out
    assert set(PATHS) <= set(prod._tree(repo))


def test_one_drifted_path_in_the_commit_among_identical_ones_refuses_alone(repo):  # noqa: F811
    _ask_stage(repo, PATHS)
    prod._write(repo, PATHS[1], "foreign = 1\n")
    _record(repo, PATHS, blobs={PATHS[1]: "a" * 40})
    head = prod._git(["rev-parse", "HEAD"], repo)

    out = prod._land(repo)

    assert out["committed"] is False and out["refused"] == land_run.LANDING_TREE_DIFFERS, out
    assert out["paths"] == [PATHS[1]]
    assert prod._git(["rev-parse", "HEAD"], repo) == head


def test_held_measured_paths_with_identical_bytes_stay_out_without_refusing(repo):  # noqa: F811
    _ask_stage(repo, PATHS)
    _peer_hold(repo, *PATHS)
    _record(repo, PATHS)

    out = prod._land(repo)

    assert out["committed"] is True and out.get("refused") is None, out
    assert not set(PATHS) & set(prod._tree(repo))
    assert set(PATHS) <= {ln[3:] for ln in prod._git(["status", "--porcelain", "-uall"], repo).splitlines()}


def test_held_measured_path_whose_bytes_differ_from_the_record_refuses(repo):  # noqa: F811
    _ask_stage(repo, PATHS)
    _peer_hold(repo, *PATHS)
    _record(repo, PATHS, blobs={PATHS[0]: "a" * 40})

    out = prod._land(repo)

    assert out["committed"] is False and out["refused"] == land_run.LANDING_TREE_DIFFERS, out
    assert out["paths"] == [PATHS[0]]


@pytest.mark.parametrize("kept_in_head", [True, False])
def test_path_freed_from_a_halted_sibling_lands_when_the_record_carries_its_blob(repo, kept_in_head):  # noqa: F811
    fr._halted_spine_only(repo, kept_in_head=kept_in_head)
    _ask_stage(repo, [fr.SRC], fr.OWN)
    _record(repo, [fr.SRC])

    out = prod._land(repo)

    assert out["committed"] is True and out.get("refused") is None, out
    assert fr.SRC in prod._tree(repo)


def test_path_a_halted_sibling_holds_refuses_when_its_bytes_differ_from_the_record(repo):  # noqa: F811
    fr._halted_spine_only(repo, kept_in_head=False)
    _ask_stage(repo, [fr.SRC], fr.OWN)
    _record(repo, [fr.SRC], blobs={fr.SRC: "a" * 40})

    out = prod._land(repo)

    assert out["committed"] is False and out["refused"] == land_run.LANDING_TREE_DIFFERS, out
    assert out["paths"] == [fr.SRC]


def test_mismatch_accepts_several_omitted_identical_paths_and_flags_only_the_drifted(repo):  # noqa: F811
    for p in PATHS:
        prod._write(repo, p, f"{p} = 1\n")
    record = {"blobs": {p: commit.checkin_blob_sha(repo, p) for p in PATHS}, "removed": []}

    assert land_run._measured_mismatch(repo, record, [], [], {}) == []
    prod._write(repo, PATHS[2], "drift = 1\n")
    assert land_run._measured_mismatch(repo, record, [], [], {}) == [PATHS[2]]
