"""A DONE row's declared write that a never-started stranded row also declares still lands, and
its importers are not entangled by it."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_entangled import (
    _REVIEWED,
    _git,
    _write,
    repo,  # noqa: F401 -- fixture
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _emit(repo, specs) -> str:
    chunks = []
    for cid, paths, report in specs:
        if report is not None:
            _write(repo, f"report-{cid}.md", report)
        chunks.append(ChunkCommit(
            id=cid, title=cid, paths=tuple(paths), report=f"report-{cid}.md"
        ))
    _write(repo, "run.mjs", "// emitted\n" + render_marker(CommitRequest(chunks=tuple(chunks))) + "\n")
    return "run.mjs"


def _call(repo, script, incomplete):
    return terminal_commit._handler(
        {"script_path": script, "incomplete_chunks": incomplete, "inline_review": _REVIEWED},
        repo_root=repo / ".git",
    )


def _shared_fixture(repo, stranded_report):
    _write(repo, "pkg/contract.py", "VERSION = 1\n")
    _write(repo, "pkg/store.py", "from pkg.contract import VERSION\n")
    return _emit(repo, [
        ("D1", ["pkg/contract.py"], "DONE: ok\n"),
        ("D2", ["pkg/store.py"], "DONE: ok\n"),
        ("U1", ["pkg/contract.py"], stranded_report),
    ])


def test_shared_path_of_an_unstarted_stranded_row_lands_with_its_importers(repo):
    out = _call(repo, _shared_fixture(repo, None), ["U1"])

    assert out["committed"] is True, out
    assert {"D1", "D2"} <= set(out["chunks_committed"])
    assert "entangled" not in out
    assert {"pkg/contract.py", "pkg/store.py"} <= set(_git(["ls-files"], repo).split())


def test_path_only_a_stranded_row_declares_still_entangles_its_importers(repo):
    _write(repo, "pkg/contract.py", "VERSION = 1\n")
    _write(repo, "pkg/store.py", "from pkg.contract import VERSION\n")
    _write(repo, "pkg/plain.py", "X = 2\n")
    script = _emit(repo, [
        ("D2", ["pkg/store.py"], "DONE: ok\n"),
        ("D3", ["pkg/plain.py"], "DONE: ok\n"),
        ("U1", ["pkg/contract.py"], "BLOCKED: half done\n"),
    ])

    out = _call(repo, script, ["U1"])

    assert out["chunks_committed"] == ["D3"]
    assert "pkg/store.py" not in _git(["ls-files"], repo).split()


def test_row_with_part_of_its_footprint_already_at_head_lands_the_rest(repo):
    _write(repo, "pkg/contract.py", "VERSION = 1\n")
    _git(["add", "pkg/contract.py"], repo)
    _git(["commit", "-q", "-m", "partial"], repo)
    _write(repo, "pkg/keying.py", "K = 1\n")
    script = _emit(repo, [("D1", ["pkg/contract.py", "pkg/keying.py"], "DONE: ok\n")])

    out = _call(repo, script, [])

    assert out["committed"] is True, out
    assert out["chunks_committed"] == ["D1"]
    assert "pkg/keying.py" in _git(["show", "--name-only", "--format=", "HEAD"], repo).split()


def test_row_whose_writes_are_all_at_head_lands_without_a_new_commit(repo):
    _write(repo, "pkg/contract.py", "VERSION = 1\n")
    _git(["add", "pkg/contract.py"], repo)
    _git(["commit", "-q", "-m", "all"], repo)
    head = _git(["rev-parse", "HEAD"], repo)
    script = _emit(repo, [("D1", ["pkg/contract.py"], "DONE: ok\n")])

    out = _call(repo, script, [])

    assert out.get("refused") is None, out
    assert _git(["rev-parse", "HEAD"], repo) == head
