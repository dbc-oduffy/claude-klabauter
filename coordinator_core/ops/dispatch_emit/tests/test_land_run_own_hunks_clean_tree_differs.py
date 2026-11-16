"""dispatch.land_run on a halted run whose only product change is its own clean hunks, with peers holding
reach_dirt in its measured-rev record (one measured path, many reach_dirt paths): never
`landing-tree-differs-from-measured`."""

from __future__ import annotations

import json

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path
from coordinator_core.ops.dispatch_emit.tests.test_land_run_unclaimed_peer_dirt_own_hunks import (  # noqa: F401
    BOTH,
    PEER,
    RUN,
    SEED,
    TARGET,
    _git,
    _land,
    _stage,
    _write,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PEER_PATHS = [f"peerpkg/peer_{i:02d}.py" for i in range(25)]


def _stored_blob(root, rel):
    """The check-in blob of *rel*, written to the object store as the measure's export does."""
    return git_objects.write_object(resolve_git_common_dir(root), b"blob", (root / rel).read_bytes())


def _record(root, *, reach_dirt=None):
    rec = measured_rev_record_path(root, RUN)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(
        json.dumps({
            "sha": "0" * 40, "base_sha": "0" * 40, "paths": [TARGET], "removed": [], "tree": "0" * 40,
            "blobs": {TARGET: _stored_blob(root, TARGET)}, "reach_dirt": dict(reach_dirt or {}),
        }),
        encoding="utf-8",
    )


def _peer_reach_dirt(root):
    for p in PEER_PATHS:
        _write(root, p, "peer = 0\n")
    _git(root, "add", "-f", *PEER_PATHS)
    _git(root, "commit", "-q", "-m", "peer files")
    for p in PEER_PATHS:
        _write(root, p, "peer = 1\n")
    return {p: _stored_blob(root, p) for p in PEER_PATHS}


def test_own_clean_hunks_over_baseline_peer_dirt_land_as_measured(repo):  # noqa: F811
    token = _stage(repo, PEER, BOTH)
    _record(repo)

    out = _land(repo, token)

    assert out.get("refused") != land_run.LANDING_TREE_DIFFERS, out
    assert out.get("committed") is True, out


def test_own_clean_hunks_with_peer_hunk_added_after_measure_land(repo):  # noqa: F811
    token = _stage(repo, PEER, BOTH)
    _record(repo)
    _write(repo, TARGET, BOTH.replace("line5\n", "late-peer-five\n"))

    out = _land(repo, token)

    assert out.get("refused") != land_run.LANDING_TREE_DIFFERS, out
    assert out.get("committed") is True, out


def test_own_clean_hunks_beside_peer_reach_dirt_land(repo):  # noqa: F811
    token = _stage(repo, PEER, BOTH)
    reach = _peer_reach_dirt(repo)
    _record(repo, reach_dirt=reach)

    out = _land(repo, token)

    assert out.get("refused") != land_run.LANDING_TREE_DIFFERS, out


def test_own_hunk_changed_after_measure_still_refuses(repo):  # noqa: F811
    token = _stage(repo, PEER, BOTH)
    _record(repo)
    _write(repo, TARGET, BOTH.replace("own10\n", "own10-changed\n"))

    out = _land(repo, token)

    assert out.get("refused") == land_run.LANDING_TREE_DIFFERS, out
    assert out["paths"] == [TARGET]
