"""review.reverify revert mode leaves another run's uncommitted edit byte-identical and stashes every revert."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ops.review_reverify import review_reverify
from coordinator_core.ops.tests.test_review_reverify_head_clamp import BASE_FILES, PREFIX, SESSION, Repo

FOREIGN = "tests/test_foreign.py"
FOREIGN_COMMITTED = b"def test_f():\r\n    assert True\r\n"
FOREIGN_DIRT = b"def test_f():\r\n    assert False  # another run's WIP\r\n"
OWN = "pkg/reply_sidecar.py"
OWN_APPLIED = b"def old():\n    return 9\n"
ROW = {"id": "F1", "file": OWN, "before": "    return 1\n", "after": "    return 9\n"}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return Repo(tmp_path, monkeypatch)


def _world(repo: Repo) -> str:
    repo.write(BASE_FILES)
    base = repo.commit("base")
    repo.snapshot(base, base, BASE_FILES)
    repo.write({FOREIGN: FOREIGN_COMMITTED})
    repo.commit("another run checkpoints a new test")
    repo.write({FOREIGN: FOREIGN_DIRT, OWN: OWN_APPLIED})
    repo.arm({FOREIGN: "a2"})
    repo.members("a2", FOREIGN)
    return base


def test_foreign_uncommitted_edit_survives_while_own_edit_is_reverted_and_stashed(repo):
    base = _world(repo)
    out = repo.revert(base, [f"{FOREIGN}::test_f", "tests/test_rs.py::test_x"], [ROW])
    assert out["error"] is None
    assert (repo.root / FOREIGN).read_bytes() == FOREIGN_DIRT
    assert f"{FOREIGN}::test_f" in out["unattributed"]
    assert [(e["path"], e["action"]) for e in out["reverted"]] == [(OWN, "reverted-findings")]
    assert (repo.root / OWN).read_bytes() == b"def old():\n    return 1\n"
    stash = repo.root / out["reverted"][0]["kept_at"]
    assert stash.read_bytes() == OWN_APPLIED


def test_no_sidecars_leaves_foreign_created_tracked_dirt_untouched_and_unattributed(repo):
    base = _world(repo)
    out = repo.revert(base, [f"{FOREIGN}::test_f"])
    assert out["error"] is None
    assert out["reverted"] == []
    assert (repo.root / FOREIGN).read_bytes() == FOREIGN_DIRT
    assert f"{FOREIGN}::test_f" in out["unattributed"]
    assert [k["path"] for k in out["kept"]] == [FOREIGN]


def test_run_finding_naming_a_created_tracked_path_still_restores_it_with_a_stash(repo):
    base = _world(repo)
    row = {"id": "F2", "file": FOREIGN, "before": "assert True", "after": "assert False"}
    out = repo.revert(base, [f"{FOREIGN}::test_f"], [row])
    assert [(e["path"], e["action"], e["source"]) for e in out["reverted"]] == [(FOREIGN, "restored", "head")]
    assert (repo.root / FOREIGN).read_bytes().replace(b"\r\n", b"\n") == FOREIGN_COMMITTED.replace(b"\r\n", b"\n")
    assert (repo.root / out["reverted"][0]["kept_at"]).read_bytes() == FOREIGN_DIRT


def test_probe_open_without_sidecars_leaves_foreign_created_tracked_dirt_untouched(repo):
    base = _world(repo)
    out = review_reverify(
        repo.root, base=base, slice_prefix=PREFIX, session_id=SESSION,
        failing=[f"{FOREIGN}::test_f"], probe="open",
    )
    assert out["error"] is None
    assert out["probed"] == []
    assert (repo.root / FOREIGN).read_bytes() == FOREIGN_DIRT
