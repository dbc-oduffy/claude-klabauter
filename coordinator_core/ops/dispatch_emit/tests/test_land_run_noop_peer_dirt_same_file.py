"""dispatch.land_run on a halted run that changed nothing, where a peer dirtied a file the run declared:
the run has no product change of its own, so the peer's dirt is never a landing-tree mismatch."""

from __future__ import annotations

import json

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path
from coordinator_core.ops.dispatch_emit.tests.test_land_run_unclaimed_peer_dirt_own_hunks import (  # noqa: F401
    PEER,
    REPORT,
    RUN,
    SEED,
    TARGET,
    _declare,
    _git,
    _land,
    _write,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _record(root, blobs=None):
    rec = measured_rev_record_path(root, RUN)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(
        json.dumps({
            "sha": "0" * 40, "base_sha": "0" * 40, "paths": sorted(blobs or {}), "removed": [], "tree": "0" * 40,
            "blobs": dict(blobs or {}), "reach_dirt": {},
        }),
        encoding="utf-8",
    )


def _stored_blob(root, rel):
    """The check-in blob of *rel*, written to the object store as the measure's export does."""
    return git_objects.write_object(resolve_git_common_dir(root), b"blob", (root / rel).read_bytes())


def _stage_noop(root, *, record_blobs=None):
    """A peer's dirt sits in TARGET at the run's baseline; the run edits nothing."""
    _write(root, TARGET, PEER)
    baseline = wb.capture(root, [TARGET])
    _write(root, REPORT, "PARTIAL: X1\nNot done: nothing edited, the run changed no file\n")
    token = _declare(root, baseline)
    _record(root, record_blobs)
    return token


def test_noop_run_with_the_record_carrying_nothing_lands_without_tree_differs(repo):  # noqa: F811
    token = _stage_noop(repo)

    out = _land(repo, token)

    assert out.get("refused") != land_run.LANDING_TREE_DIFFERS, out
    assert (repo / TARGET).read_text(encoding="utf-8") == PEER


def test_noop_run_with_the_record_carrying_the_peer_bytes_lands_without_tree_differs(repo):  # noqa: F811
    _write(repo, TARGET, PEER)
    blob = _stored_blob(repo, TARGET)
    token = _stage_noop(repo, record_blobs={TARGET: blob})

    out = _land(repo, token)

    assert out.get("refused") != land_run.LANDING_TREE_DIFFERS, out


def test_noop_run_peer_edits_the_declared_file_after_the_measure_lands_without_tree_differs(repo):  # noqa: F811
    _write(repo, TARGET, PEER)
    blob = _stored_blob(repo, TARGET)
    token = _stage_noop(repo, record_blobs={TARGET: blob})
    _write(repo, TARGET, PEER.replace("line5\n", "late-peer-five\n"))

    out = _land(repo, token)

    assert out.get("refused") != land_run.LANDING_TREE_DIFFERS, out


def test_noop_run_peer_dirties_a_clean_declared_file_after_the_baseline_lands_without_tree_differs(repo):  # noqa: F811
    baseline = wb.capture(repo, [TARGET])
    _write(repo, REPORT, "PARTIAL: X1\nNot done: nothing edited, the run changed no file\n")
    token = _declare(repo, baseline)
    _write(repo, TARGET, PEER)
    _record(repo)

    out = _land(repo, token)

    assert out.get("refused") != land_run.LANDING_TREE_DIFFERS, out


def test_noop_run_never_commits_the_peers_dirt(repo):  # noqa: F811
    token = _stage_noop(repo)

    out = _land(repo, token)

    assert out.get("refused") != land_run.LANDING_TREE_DIFFERS, out
    assert _git(repo, "show", f"HEAD:{TARGET}") == SEED
    assert (repo / TARGET).read_text(encoding="utf-8") == PEER
