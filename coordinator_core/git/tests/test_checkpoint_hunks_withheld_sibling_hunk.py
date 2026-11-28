"""A wave checkpoint through `ceremony.commit_v2` holds back a test that names a definition only a withheld hunk of a shared file carries, and still lands one that names only the run's own hunk."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import checkpoint_hunks
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import peer_hunk_gate, run_hunk_claims, run_write_prints
from coordinator_core.ops.dispatch_emit.run_hunk_claims import HunkSplit
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "cks-self"
PLAN = "state/mise-inventory/run.spine.md"
SHARED = "pkg/shared.py"
TEST = "pkg/tests/test_shared.py"
BASE_LINES = [f"V{i:02d} = {i}\n" for i in range(1, 21)]


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _params(repo: Path, paths: list) -> dict:
    base = _git(["rev-parse", "HEAD"], repo).strip()
    return {
        "paths": paths, "skip_missing": True, "gone_tracked_as_deleted": True,
        "message": f"checkpoint(wave 1): 1 rows — r1\n\nCheckpoint-Plan: {PLAN}\nCheckpoint-Base: {base}\n",
    }


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "pkg/tests").mkdir(parents=True)
    (root / "pkg/__init__.py").write_text("", encoding="utf-8")
    (root / "pkg/tests/__init__.py").write_text("", encoding="utf-8")
    (root / SHARED).write_text("".join(BASE_LINES), encoding="utf-8", newline="")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(checkpoint_hunks, "checkpoint_run_id", lambda root, message, sid: "run1")
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
    monkeypatch.setattr(run_write_prints, "read", lambda r, run: {SHARED: "recorded", TEST: "recorded"})
    monkeypatch.setattr(
        run_hunk_claims, "own_hunks",
        lambda r, run, path, hunks, holders: HunkSplit(own=[h for h in hunks if h.old_start <= 4]),
    )
    return root


def _edit_shared(repo: Path) -> None:
    lines = list(BASE_LINES)
    lines[1] = "def own_fn():\n    return 1\n"
    lines[18] = "class ForeignThing:\n    pass\n"
    (repo / SHARED).write_text("".join(lines), encoding="utf-8", newline="")


def test_a_test_naming_a_class_only_a_withheld_hunk_defines_is_held_back_with_that_hunk(repo):
    _edit_shared(repo)
    (repo / TEST).write_text(
        "from pkg.shared import ForeignThing\n\n\ndef test_foreign():\n    assert ForeignThing()\n", encoding="utf-8",
    )

    result = _call(repo, [SHARED, TEST])

    assert TEST in result.get("checkpoint_withheld_paths", []), result
    assert "test exercises withheld code" in result["checkpoint_withheld_reasons"][TEST], result
    assert TEST not in _git(["show", "--name-only", "--format=", "HEAD"], repo).split()


def test_a_test_naming_only_the_runs_own_hunk_definition_lands_with_it(repo):
    _edit_shared(repo)
    (repo / TEST).write_text(
        "from pkg.shared import own_fn\n\n\ndef test_own():\n    assert own_fn() == 1\n", encoding="utf-8",
    )

    result = _call(repo, [SHARED, TEST])

    assert result["committed"] is True, result
    assert set(_git(["show", "--name-only", "--format=", "HEAD"], repo).split()) == {SHARED, TEST}
    assert TEST not in result.get("checkpoint_withheld_paths", []), result
    assert "ForeignThing" in (repo / SHARED).read_text(encoding="utf-8")


def _call(repo: Path, paths: list) -> dict:
    return commit_v2._handler(_params(repo, paths), repo_root=repo / ".git")
