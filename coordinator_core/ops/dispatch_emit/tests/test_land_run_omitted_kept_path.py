"""A kept path a halted run holds lands when the measured-rev record carries its worktree blob, and still refuses when the bytes differ."""

from __future__ import annotations

import json

import pytest

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, measured_rev_record_path
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401  (fixture)
from coordinator_core.ops.dispatch_emit.tests.test_land_run_refire_inherits_withheld_scope import (  # noqa: F401
    FOREIGN,
    HALT_REASON,
    INHERITED,
    OWN,
    PRED,
    RUN,
    _ledger_open,
    _predecessor_halt,
    _resume,
    _sessions_live,
    _stage,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


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


def _stage_held(root):
    """OWN is a tracked file the run modified, INHERITED an untracked new file; a foreign halted run holds both."""
    prod._write(root, OWN, "# seed\n")
    prod._git(["add", OWN], root)
    prod._git(["commit", "-q", "-m", "seed own"], root)
    _stage(root)
    held = {INHERITED: HALT_REASON, OWN: HALT_REASON}
    _predecessor_halt(root, PRED, held)
    _predecessor_halt(root, FOREIGN, held)
    prod._write(root, f"{RECORD_DIR_ROOT}/{FOREIGN}/manifest.json", json.dumps({"rows": [{"id": "F1", "writes": list(held)}]}))
    prod._write(root, f"{RECORD_DIR_ROOT}/{FOREIGN}/run.json", json.dumps({"sizing_rel": "state/sizings/other.yaml"}))


def test_kept_paths_a_halted_run_holds_land_when_the_record_carries_their_blobs(repo):  # noqa: F811
    _stage_held(repo)
    _record(repo, [OWN, INHERITED])

    out = prod._land(repo, resume_ledger=_resume())

    assert out["committed"] is True and out.get("refused") is None, out
    assert {OWN, INHERITED} <= set(prod._tree(repo))


def test_a_held_path_whose_bytes_differ_from_the_record_still_refuses(repo):  # noqa: F811
    _stage_held(repo)
    _record(repo, [OWN, INHERITED], blobs={INHERITED: "a" * 40})
    head = prod._git(["rev-parse", "HEAD"], repo)

    out = prod._land(repo, resume_ledger=_resume())

    assert out["committed"] is False and out["refused"] == land_run.LANDING_TREE_DIFFERS, out
    assert out["paths"] == [INHERITED]
    assert prod._git(["rev-parse", "HEAD"], repo) == head


def test_no_record_keeps_the_halted_hold(repo):  # noqa: F811
    _stage_held(repo)

    out = prod._land(repo, resume_ledger=_resume())

    assert out["committed"] is True and out.get("refused") is None, out
    assert INHERITED not in prod._tree(repo) and OWN not in prod._tree(repo)
