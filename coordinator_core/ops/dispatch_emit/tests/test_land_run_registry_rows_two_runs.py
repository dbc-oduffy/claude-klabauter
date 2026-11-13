"""dispatch.land_run (production entry) lands its own appended row of the two shared registry files when an
unrelated unlanded run co-declares them with its own row; a modified registry line still holds."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.session import core as session_core
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REGISTRY_MAP = "coordinator_core/ops/_registry_map.py"
OPS_INIT = "coordinator_core/ops/__init__.py"
FILES = (REGISTRY_MAP, OPS_INIT)
SIBLING = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-5b11d1"
PEER_SESSION = "5e1f0000-0000-4000-8000-0000000000b3"
HEAD = "row_a\nrow_b\n"
OWN, THEIRS = "row_own\n", "row_peer\n"


def _land(root) -> dict:
    return prod._land(root, inline_review=reviewed())


def _blob(root, text):
    return git_objects.write_object(resolve_git_common_dir(root), b"blob", text.encode())


def _sibling(root):
    rdir = root / "state" / "warp" / SIBLING
    rdir.mkdir(parents=True)
    (rdir / "run.json").write_text(json.dumps({"run_id": SIBLING}), encoding="utf-8")
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": list(FILES)}], "doc_writes": []}), encoding="utf-8"
    )
    (rdir / "parent.json").write_text(json.dumps({"parent_id": "an-unrelated-parent"}), encoding="utf-8")
    session_core.init(PEER_SESSION, cwd=str(root))
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=FILES),), plan_path=None)
    prod._write(root, "run-b.mjs", "// emitted\n" + render_marker(request) + "\n")
    run_token.mint(root, SIBLING, "run-b.mjs", session_id=PEER_SESSION)
    own = root / "state" / "warp" / prod.RUN
    own.mkdir(parents=True, exist_ok=True)
    (own / "parent.json").write_text(json.dumps({"parent_id": "own-parent"}), encoding="utf-8")


def _setup(root, monkeypatch, worktree, own_baseline, sibling_baseline):
    for path in FILES:
        prod._write(root, path, HEAD)
    prod._git(["add", *FILES], root)
    prod._git(["commit", "-q", "-m", "registry seed"], root)
    _sibling(root)
    baselines = {
        prod.RUN: {p: _blob(root, own_baseline) for p in FILES},
        SIBLING: {p: _blob(root, sibling_baseline) for p in FILES},
    }
    monkeypatch.setattr(hha, "_baseline", lambda r, run: baselines.get(run))
    for path in FILES:
        prod._author(root, path, worktree)
    prod._declare(root, *FILES)
    prod._record_dir(root)


def test_two_runs_each_appending_a_registry_row_land_own_rows_without_a_whole_file_hold(repo, monkeypatch):
    _setup(repo, monkeypatch, HEAD + OWN + THEIRS, HEAD, HEAD + OWN)

    out = _land(repo)

    assert out.get("committed") is True, out
    for path in FILES:
        assert prod._git(["show", f"HEAD:{path}"], repo) == HEAD + OWN
        assert (repo / path).read_text() == HEAD + OWN + THEIRS
        assert path in prod._dirty(repo)


def test_a_registry_line_modified_with_a_peer_co_declaring_still_holds(repo, monkeypatch):
    _setup(repo, monkeypatch, "row_edited\nrow_b\n" + THEIRS, HEAD, HEAD)

    out = _land(repo)

    assert out.get("committed") is not True or not any(
        prod._git(["show", f"HEAD:{p}"], repo) != HEAD for p in FILES
    ), out
    for path in FILES:
        assert prod._git(["show", f"HEAD:{path}"], repo) == HEAD
