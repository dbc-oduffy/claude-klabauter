"""dispatch.land_run (production entry) lands only its own hunks of a DELIVERY.md a live same-parent sibling co-declares."""

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


def _land(root, **extra) -> dict:
    return _prod_land(root, **{"inline_review": reviewed(), **extra})


_prod_land = prod._land

DELIVERY = "DELIVERY.md"
SIBLING = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-5b11d0"
PARENT = "parent-1"
PEER_SESSION = "5e1f0000-0000-4000-8000-0000000000b2"
OWN, THEIRS = "own1\n", "sib1\n"


def _blob(root, text):
    return git_objects.write_object(resolve_git_common_dir(root), b"blob", text.encode())


def _sibling(root):
    rdir = root / "state" / "warp" / SIBLING
    rdir.mkdir(parents=True)
    (rdir / "run.json").write_text(json.dumps({"run_id": SIBLING}), encoding="utf-8")
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [DELIVERY]}], "doc_writes": []}), encoding="utf-8"
    )
    (rdir / "parent.json").write_text(json.dumps({"parent_id": PARENT}), encoding="utf-8")
    session_core.init(PEER_SESSION, cwd=str(root))
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=(DELIVERY,)),), plan_path=None)
    prod._write(root, "run-b.mjs", "// emitted\n" + render_marker(request) + "\n")
    run_token.mint(root, SIBLING, "run-b.mjs", session_id=PEER_SESSION)
    own = root / "state" / "warp" / prod.RUN
    own.mkdir(parents=True, exist_ok=True)
    (own / "parent.json").write_text(json.dumps({"parent_id": PARENT}), encoding="utf-8")


def _setup(root, monkeypatch):
    prod._write(root, DELIVERY, "head\n")
    prod._git(["add", DELIVERY], root)
    prod._git(["commit", "-q", "-m", "delivery seed"], root)
    _sibling(root)
    baselines = {prod.RUN: {DELIVERY: _blob(root, "head\n")}, SIBLING: {DELIVERY: _blob(root, "head\n" + OWN)}}
    monkeypatch.setattr(hha, "_baseline", lambda r, run: baselines.get(run))
    prod._author(root, DELIVERY, "head\n" + OWN + THEIRS)
    prod._declare(root, DELIVERY)
    prod._record_dir(root)


def test_land_run_commits_its_own_delivery_hunks_and_leaves_the_sibling_lines_dirty(repo, monkeypatch):
    _setup(repo, monkeypatch)

    out = _land(repo)

    assert out.get("committed") is True, out
    assert prod._git(["show", f"HEAD:{DELIVERY}"], repo) == "head\n" + OWN
    assert (repo / DELIVERY).read_text() == "head\n" + OWN + THEIRS
    assert DELIVERY in prod._dirty(repo)


def test_land_run_still_refuses_when_the_sibling_is_not_a_same_parent_peer_and_the_path_is_not_a_run_artifact(repo, monkeypatch):
    prod._write(repo, "pkg/a.py", "head\n")
    prod._git(["add", "pkg/a.py"], repo)
    prod._git(["commit", "-q", "-m", "seed"], repo)
    _sibling(repo)
    rdir = repo / "state" / "warp" / SIBLING
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": ["pkg/a.py"]}], "doc_writes": []}), encoding="utf-8"
    )
    (rdir / "parent.json").write_text(json.dumps({"parent_id": "other"}), encoding="utf-8")
    prod._author(repo, "pkg/a.py", "head\nmine\ntheirs\n")
    prod._declare(repo, "pkg/a.py")
    prod._record_dir(repo)

    out = _land(repo)

    assert out.get("committed") is not True, out


def test_land_run_refuses_a_delivery_path_whose_line_both_runs_changed(repo, monkeypatch):
    _setup(repo, monkeypatch)
    prod._author(repo, DELIVERY, "both\n")
    baselines_run = {prod.RUN: {DELIVERY: _blob(repo, "head\n")}, SIBLING: {DELIVERY: _blob(repo, "head\n")}}
    monkeypatch.setattr(hha, "_baseline", lambda r, run: baselines_run.get(run))

    out = _land(repo)

    assert out.get("committed") is not True or DELIVERY not in prod._tree(repo), out
    assert prod._git(["show", f"HEAD:{DELIVERY}"], repo) == "head\n"
