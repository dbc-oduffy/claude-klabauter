"""A terminal commit whose landed file reads a name from a modified file a halted sibling just stopped
holding lands that file with it; a target still held, or never halted-held, keeps refusing lost-definition."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_withheld_index, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = reviewed()
_INPUT = "pkg/likeness_input.py"
_ROUTE = "pkg/likeness_route.py"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / _INPUT).write_text("def old():\n    return 1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / _INPUT).write_text("def old():\n    return 1\n\n\ndef new_fn():\n    return 2\n", encoding="utf-8")
    (root / _ROUTE).write_text(
        "def run():\n    from pkg import likeness_input\n    return likeness_input.new_fn()\n", encoding="utf-8",
    )
    return root


@pytest.fixture(autouse=True)
def _review_verdict_present(monkeypatch):
    from coordinator_core.ops.dispatch_emit import review_floor

    monkeypatch.setattr(review_floor, "verdict_count", lambda *_a, **_k: 1)


def _fire(repo: Path) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(_ROUTE,)),))
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    params = {"inline_review": _REVIEWED, "script_path": "run.mjs", "incomplete_chunks": []}
    return terminal_commit._handler(params, repo_root=repo / ".git")


def _committed_files(repo: Path) -> set:
    return set(_git(["show", "--name-only", "--format=", "HEAD"], repo).split())


def test_a_target_a_halted_sibling_stopped_holding_lands_with_its_importer(repo, monkeypatch):
    monkeypatch.setattr(halted_withheld_index, "withheld_by_path", lambda _root: {_INPUT: "206de0"})
    monkeypatch.setattr(terminal_commit, "foreign_halted_withheld", lambda *_a, **_k: {})

    out = _fire(repo)

    assert out["committed"] is True, out
    assert {_INPUT, _ROUTE} <= _committed_files(repo)


def test_a_target_a_halted_sibling_still_holds_keeps_refusing(repo, monkeypatch):
    monkeypatch.setattr(halted_withheld_index, "withheld_by_path", lambda _root: {_INPUT: "206de0"})
    monkeypatch.setattr(terminal_commit, "foreign_halted_withheld", lambda *_a, **_k: {_INPUT: "206de0"})
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo)

    assert out["committed"] is False
    assert out["refused"] == "lost-definition", out
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_a_target_no_halted_run_ever_held_keeps_refusing(repo, monkeypatch):
    monkeypatch.setattr(halted_withheld_index, "withheld_by_path", lambda _root: {})
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo)

    assert out["committed"] is False
    assert out["refused"] == "lost-definition", out
    assert _git(["rev-parse", "HEAD"], repo) == head
