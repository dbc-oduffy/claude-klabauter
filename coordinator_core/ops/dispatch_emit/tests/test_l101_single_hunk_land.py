"""dispatch.land_run at default arguments lands only its own hunks of a source file a same-parent sibling also holds."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401
from coordinator_core.session import core as session_core

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence, pytest.mark.designed_red]

SRC = "pkg/shared.py"
SIBLING = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-5b11d1"
PARENT = "parent-1"
PEER_SESSION = "5e1f0000-0000-4000-8000-0000000000b3"
BASE = "".join(f"line{i}\n" for i in range(1, 21))
OWN_WORKTREE = BASE.replace("line2\n", "own_hunk\n")
BOTH_WORKTREE = OWN_WORKTREE.replace("line19\n", "peer_hunk\n")


def _blob(root, text):
    return git_objects.write_object(resolve_git_common_dir(root), b"blob", text.encode())


def _sibling(root):
    rdir = root / "state" / "warp" / SIBLING
    rdir.mkdir(parents=True)
    (rdir / "run.json").write_text(json.dumps({"run_id": SIBLING}), encoding="utf-8")
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [SRC]}], "doc_writes": []}), encoding="utf-8"
    )
    (rdir / "parent.json").write_text(json.dumps({"parent_id": PARENT}), encoding="utf-8")
    session_core.init(PEER_SESSION, cwd=str(root))
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=(SRC,)),), plan_path=None)
    prod._write(root, "run-b.mjs", "// emitted\n" + render_marker(request) + "\n")
    run_token.mint(root, SIBLING, "run-b.mjs", session_id=PEER_SESSION)
    own = root / "state" / "warp" / prod.RUN
    own.mkdir(parents=True, exist_ok=True)
    (own / "parent.json").write_text(json.dumps({"parent_id": PARENT}), encoding="utf-8")


def test_land_run_commits_only_its_own_hunks_of_a_co_held_source_file(repo, monkeypatch):
    prod._write(repo, SRC, BASE)
    prod._git(["add", SRC], repo)
    prod._git(["commit", "-q", "-m", "seed"], repo)
    _sibling(repo)
    baselines = {prod.RUN: {SRC: _blob(repo, BASE)}, SIBLING: {SRC: _blob(repo, OWN_WORKTREE)}}
    monkeypatch.setattr(hha, "_baseline", lambda r, run: baselines.get(run))
    prod._author(repo, SRC, BOTH_WORKTREE)
    prod._declare(repo, SRC)
    prod._record_dir(repo)

    out = prod._land(repo, inline_review=reviewed())

    assert out.get("committed") is True, out
    assert not out.get("peer_held_left"), out
    assert prod._git(["show", f"HEAD:{SRC}"], repo) == OWN_WORKTREE
    assert (repo / SRC).read_text() == BOTH_WORKTREE
    assert SRC in prod._dirty(repo)
