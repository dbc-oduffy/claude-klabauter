"""A killed workweek-complete apply, rerun, skips landed directives and still commits the dead run's writes.

The kill is a test-local BaseException raised from inside directive k's dispatch. Each pass opens
a fresh checkpoint the way a new process would.

Run: python -m pytest coordinator_core/workweek_complete/test_apply_resume.py -q
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from coordinator_core.workstream_complete import directives_commit_tail as tail
from coordinator_core.workweek_complete import apply as wwc_apply

KILL_AT = 3
PRE_EXISTING = "notes/preexisting.md"


class _Kill(BaseException):
    """Stands in for the process kill: not an Exception, so no per-directive arm swallows it."""


def _directive(did: str) -> dict[str, Any]:
    return {"id": did, "cli": "fake-cli", "args": [], "depends_on": None, "already_satisfied": False}


def _six() -> list[dict[str, Any]]:
    return [_directive(f"d{i}") for i in range(6)]


class _World:
    """A fake tree: `dirty` is what `_dirty_snapshot` reports, and each dispatch dirties its own path."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
        self.monkeypatch = monkeypatch
        self.root = root
        self.dispatched: list[str] = []
        self.dirty: dict[str, str] = {PRE_EXISTING: " M"}
        self.committed: list[list[str]] = []
        sessions = str(root / ".git" / "coordinator-sessions")
        from coordinator_core.ceremony_common import apply_checkpoint
        from coordinator_core.session import core

        monkeypatch.setattr(core, "sessions_dir", lambda cwd=None: sessions)
        monkeypatch.setattr(apply_checkpoint, "sessions_dir", lambda cwd=None: sessions)
        monkeypatch.setattr(wwc_apply, "_resolve_cli", lambda name: Path(name))
        monkeypatch.setattr(wwc_apply, "flush_composition_record", lambda *_a, **_k: None)
        monkeypatch.setattr(wwc_apply, "_dirty_snapshot", lambda _root: dict(self.dirty))
        monkeypatch.setattr(
            wwc_apply, "_prepare_commit_tail", lambda: (str(root), "sid-resume", dict(self.dirty), None)
        )
        monkeypatch.setattr(tail, "resolve_known_concurrent_paths", lambda *_a, **_k: set())
        monkeypatch.setattr(
            wwc_apply.session_scope, "release_committed_claims", lambda *_a, **_k: None
        )
        monkeypatch.setattr(
            wwc_apply.git_native,
            "status_porcelain",
            lambda *_a, **_k: SimpleNamespace(ok=True, stdout="", stderr=""),
        )

        def close_commit(_root: Any, *, stage_paths: list[str], deleted_paths: list[str], **_kw: Any):
            self.committed.append(sorted([*stage_paths, *deleted_paths]))
            return SimpleNamespace(commit_failed=False, committed_sha="abc123", diagnostics=[])

        monkeypatch.setattr(tail, "run_close_commit", close_commit)

    def install(self, directives: list[dict[str, Any]], kill_at: Optional[str]) -> None:
        def fake(directive: dict[str, Any]) -> dict[str, Any]:
            did = directive["id"]
            self.dispatched.append(did)
            self.dirty[f"out/{did}.md"] = "??"
            if did == kill_at:
                raise _Kill()
            return {"id": did, "cli": directive["cli"], "args": [], "exit_code": 0, "stderr": "", "exit_class": "returned"}

        self.monkeypatch.setattr(wwc_apply, "_dispatch_directive", fake)
        self.monkeypatch.setattr(
            wwc_apply,
            "brief",
            lambda decisions=None: (0, {"directives": directives, "judgment_points": [], "decisions": {}}),
        )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _World:
    (tmp_path / ".git").mkdir()
    return _World(monkeypatch, tmp_path)


def _open(world: _World):
    from coordinator_core.ceremony_common.apply_checkpoint import open_checkpoint

    return open_checkpoint(world.root, "workweek-complete")


def test_rerun_after_a_kill_dispatches_each_landed_directive_once(world: _World) -> None:
    directives = _six()
    world.install(directives, kill_at=f"d{KILL_AT}")
    with pytest.raises(_Kill):
        wwc_apply._execute_directives(directives, [], {}, checkpoint=_open(world))
    world.install(directives, kill_at=None)
    exit_code, report = wwc_apply._execute_directives(directives, [], {}, checkpoint=_open(world))

    counts = {d["id"]: world.dispatched.count(d["id"]) for d in directives}
    assert {counts[f"d{i}"] for i in range(KILL_AT)} == {1}
    assert counts[f"d{KILL_AT}"] == 2
    assert {counts["d4"], counts["d5"]} == {1}
    assert exit_code == int(wwc_apply.WorkweekApplyExitCode.SUCCESS)
    assert report["interrupted"] == [f"d{KILL_AT}"]
    assert report["resumed"] == ["d0", "d1", "d2"]


def test_resumed_pass_commits_the_paths_the_killed_pass_dirtied(world: _World) -> None:
    directives = _six()
    world.install(directives, kill_at=f"d{KILL_AT}")
    with pytest.raises(_Kill):
        wwc_apply.apply()
    world.install(directives, kill_at=None)
    exit_code, report = wwc_apply.apply()

    assert exit_code == int(wwc_apply.WorkweekApplyExitCode.SUCCESS)
    (staged,) = world.committed
    assert staged == sorted(f"out/d{i}.md" for i in range(6))
    assert PRE_EXISTING not in staged
    assert report["commit"]["committed_sha"] == "abc123"
