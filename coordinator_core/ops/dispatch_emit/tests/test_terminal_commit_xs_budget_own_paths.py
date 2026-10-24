"""The XS budget prices only the run's own declared paths, never peer commits or peer dirt."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_xs_over_budget import (  # noqa: F401
    _call,
    _git,
    root,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _own_paths(root, count):
    names = []
    for i in range(count):
        rel = f"own_{i}.py"
        (root / rel).write_text("x = 1\n", encoding="utf-8")
        names.append(rel)
    return names


def test_forty_peer_files_committed_and_dirty_do_not_count_against_nine_own_paths(root):
    base = _git(root, "rev-parse", "HEAD")
    for i in range(40):
        (root / f"peer_{i}.py").write_text("p = 1\n" * 25, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "peer work\n\nSession-Id: peer-session")
    for i in range(5):
        (root / f"peer_{i}.py").write_text("p = 2\n" * 25, encoding="utf-8")
    (root / "peer_untracked.py").write_text("u = 1\n" * 30, encoding="utf-8")
    names = _own_paths(root, 2) + ["keep.txt", "tracked.txt"]
    reply, _ = _call(root, names, base=base)
    assert reply.get("refused") != "xs-over-budget", reply


def test_own_paths_over_budget_still_refuse_beside_forty_peer_files(root):
    base = _git(root, "rev-parse", "HEAD")
    for i in range(40):
        (root / f"peer_{i}.py").write_text("p = 1\n" * 25, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "peer work")
    names = _own_paths(root, 3)
    reply, _ = _call(root, names, base=base)
    assert reply["refused"] == "xs-over-budget"
    assert reply["measured"] == {"files": 3, "lines": 3}
