"""A DONE row whose wave commit failed or never ran lands at terminal_commit from its chunk report and
declared writes, with no re-fire; the import closure covers the landed set as a whole."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_entangled import (
    _REVIEWED,
    _emit,
    _git,
    _write,
    repo,  # noqa: F401 -- fixture
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _fixture(repo):
    _write(repo, "pkg/contract.py", "VERSION = 1\n")
    _write(repo, "pkg/persist.py", "from pkg.contract import VERSION\n")
    _write(repo, "pkg/run.py", "from pkg.persist import VERSION\n")
    _write(repo, "pkg/later.py", "from pkg.unfinished import X\n")
    _write(repo, "pkg/unfinished.py", "X = 1\n")
    return _emit(repo, [
        ("W1", ["pkg/contract.py"], [], "DONE: ok\n"),
        ("W2", ["pkg/persist.py"], [], "DONE: ok\n"),
        ("W3", ["pkg/run.py"], [], "DONE: ok\n"),
        ("U1", ["pkg/unfinished.py"], [], "BLOCKED: x\n"),
        ("W4", ["pkg/later.py"], [], "DONE: ok\n"),
    ])


def _call(repo, script, incomplete):
    return terminal_commit._handler(
        {"script_path": script, "incomplete_chunks": incomplete, "inline_review": _REVIEWED},
        repo_root=repo / ".git",
    )


def test_done_rows_left_dirty_by_a_failed_wave_commit_land_together(repo):
    script = _fixture(repo)

    out = _call(repo, script, ["U1"])

    assert out["committed"] is True, out
    assert {"W1", "W2", "W3"} <= set(out["chunks_committed"])
    assert out["entangled"] == {"W4": ["U1"]}
    tracked = set(_git(["ls-files"], repo).split())
    assert {"pkg/contract.py", "pkg/persist.py", "pkg/run.py"} <= tracked
    assert not {"pkg/later.py", "pkg/unfinished.py"} & tracked


def test_done_rows_carried_as_incomplete_drafts_land_from_their_reports(repo):
    script = _fixture(repo)

    out = _call(repo, script, ["W1", "W2", "W3", "U1"])

    assert out["committed"] is True, out
    assert {"W1", "W2", "W3"} <= set(out.get("partial_committed", []))
    assert {"pkg/contract.py", "pkg/persist.py", "pkg/run.py"} <= set(_git(["ls-files"], repo).split())
    assert "pkg/unfinished.py" not in _git(["ls-files"], repo).split()
