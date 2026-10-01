"""`session.safe_commit_offer._commit_group` runs `authoring_leaks.leak_gate` before committing.

A refused group is reported failed with the gate text as its detail and HEAD unmoved; later groups
still commit.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.session import core, scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout


def _make_repo(tmp_path):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@example.com"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "b.py").write_text("def old_fn():\n    pass\n")
    (tmp_path / "README.md").write_text("x")
    _git(["add", "."], tmp_path)
    _git(["commit", "-q", "-m", "init"], tmp_path)
    return tmp_path


def _head(repo) -> str:
    return _git(["rev-parse", "HEAD"], repo).strip()


def _falsifier(repo, sid):
    """a.py imports new_fn from pkg.b, which defines it only in the uncommitted worktree."""
    (repo / "pkg" / "b.py").write_text("def old_fn():\n    pass\n\ndef new_fn():\n    pass\n")
    (repo / "pkg" / "a.py").write_text("from pkg.b import new_fn\n")
    scope.touch(sid, "pkg/a.py", cwd=str(repo))


def test_falsifier_group_refused_with_gate_text(tmp_path):
    repo = _make_repo(tmp_path)
    core.init("mine", cwd=str(repo))
    _falsifier(repo, "mine")
    before = _head(repo)

    report = safe_commit_offer.commit_session_offer(
        "mine", str(repo), [{"paths": ["pkg/a.py"], "message": "uses uncommitted definer"}]
    )

    assert report["outcome"]["committed_paths"] == []
    assert len(report["failed_groups"]) == 1
    failed = report["failed_groups"][0]
    assert failed["committed"] is False
    assert failed["commit_failed"] is True
    assert failed["error"].startswith("leak_gate: ")
    assert "import_closure" in failed["error"]
    assert _head(repo) == before


def test_refused_group_does_not_block_later_group(tmp_path):
    repo = _make_repo(tmp_path)
    core.init("mine", cwd=str(repo))
    _falsifier(repo, "mine")
    (repo / "ok.md").write_text("fine\n")
    scope.touch("mine", "ok.md", cwd=str(repo))

    report = safe_commit_offer.commit_session_offer(
        "mine",
        str(repo),
        [
            {"paths": ["pkg/a.py"], "message": "bad group"},
            {"paths": ["ok.md"], "message": "good group"},
        ],
    )

    assert report["outcome"]["status"] != "empty"
    assert report["outcome"]["committed_paths"] == ["ok.md"]
    assert [g["paths"] for g in report["failed_groups"]] == [["pkg/a.py"]]
    assert report["failed_groups"][0]["error"].startswith("leak_gate: ")
    committed = [g for g in report["groups"] if g["committed"]]
    assert [g["paths"] for g in committed] == [["ok.md"]]
    assert "ok.md" in _git(["show", "--name-only", "--format=", "HEAD"], repo)
