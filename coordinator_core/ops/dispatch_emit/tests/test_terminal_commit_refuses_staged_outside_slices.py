"""dispatch.terminal_commit refuses, before any git write, a landing that stages a declared code path
absent from every reviewed slice (``inline_review.prep.slice_files``), and lands one fully covered."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.terminal_commit_refusals import (
    REFUSED_UNREVIEWED,
    _staged_outside_slices_refusal,
)
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_landing_integrity import (  # noqa: F401
    _git,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _review(slice_files: list) -> dict:
    out = dict(reviewed())
    out["prep"] = {"product_files": len(slice_files), "slice_files": slice_files}
    return out


def _fire(repo, paths: tuple, inline_review: dict) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=paths),))
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    params = {"inline_review": inline_review, "script_path": "run.mjs", "incomplete_chunks": []}
    return terminal_commit._handler(params, repo_root=repo / ".git")


def _write_two(repo) -> None:
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("b = 1\n", encoding="utf-8")


def test_a_staged_path_in_no_slice_refuses_and_commits_nothing(repo):
    _write_two(repo)
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, ("a.py", "b.py"), _review(["a.py"]))

    assert out["committed"] is False
    assert out["refused"] == REFUSED_UNREVIEWED
    assert out["unreviewed_paths"] == ["b.py"]
    assert "b.py" in out["error"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_a_fully_covered_staged_set_still_lands(repo):
    _write_two(repo)

    out = _fire(repo, ("a.py", "b.py"), _review(["a.py", "./b.py"]))

    assert out.get("refused") != REFUSED_UNREVIEWED
    assert out["committed"] is True
    assert set(_git(["show", "--name-only", "--format=", "HEAD"], repo).split()) >= {"a.py", "b.py"}


def test_an_empty_slice_list_defers_to_the_floor_note():
    assert _staged_outside_slices_refusal(_review([]), ["a.py"]) is None
    assert _staged_outside_slices_refusal({"prep": None}, ["a.py"]) is None


def test_bookkeeping_and_non_code_paths_are_not_review_owed():
    review = _review(["a.py"])
    assert _staged_outside_slices_refusal(review, ["a.py", "state/x.yaml", "notes.md"]) is None
