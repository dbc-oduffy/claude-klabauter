"""Pure pins for ``run_touched.touched_paths``: each source contributes, malformed input
contributes nothing. That session claims never admit a file is pinned through the terminal commit
in ``test_terminal_commit_run_owned_dirt.py``."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest
from coordinator_core.ops.dispatch_emit.run_touched import touched_paths


def _req(*reports: str) -> CommitRequest:
    return CommitRequest(
        chunks=tuple(ChunkCommit(id=f"C{i}", title="t", report=r) for i, r in enumerate(reports))
    )


def test_each_source_contributes():
    review = {"prep": {"slice_files": ["src/a.py", "src/b.py"]}}
    got = touched_paths(_req("rep/C0.md", ""), review, ["art/x.md"])
    assert got == frozenset({"rep/C0.md", "src/a.py", "src/b.py", "art/x.md"})


def test_backslashes_are_normalised():
    review = {"prep": {"slice_files": ["src\\a.py"]}}
    assert touched_paths(_req("rep\\C0.md"), review, ["art\\x.md"]) == frozenset(
        {"rep/C0.md", "src/a.py", "art/x.md"}
    )


def test_malformed_review_contributes_nothing():
    req = _req()
    for bad in (None, "x", [], {"prep": None}, {"prep": []}, {"prep": {"slice_files": "src/a.py"}},
                {"prep": {"slice_files": None}}):
        assert touched_paths(req, bad) == frozenset()


def test_non_str_members_are_dropped():
    review = {"prep": {"slice_files": ["src/a.py", 3, None, ""]}}
    assert touched_paths(_req(), review, [None, 5]) == frozenset({"src/a.py"})


def test_empty_request_is_empty():
    assert touched_paths(_req(), None) == frozenset()
