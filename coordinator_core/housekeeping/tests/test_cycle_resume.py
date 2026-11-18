"""
Tests for the `housekeeping.cycle` resume cursor: `remaining`, `more` and
`archived_none_reason`, driven through the op handler at default args.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from coordinator_core.housekeeping import cycle
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_N = 40


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True,
        **no_console_creationflags(),
    )


def _handoff(path: Path, state: str, hid: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nhandoff_id: {hid}\ndeployment_state: {state}\n---\n\nFixture body.\n",
        encoding="utf-8",
    )


def _repo(tmp_path: Path, n: int = _N, state: str = "closed") -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "resume-test@example.invalid")
    _git(root, "config", "user.name", "resume-test")
    for i in range(n):
        _handoff(root / "state" / "handoffs" / f"2026-06-01_{i:05d}_t.md", state, f"hnd-{i}")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")
    return root


def _call(root: Path, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return cycle._handler(params or {}, root)


def _live(root: Path) -> int:
    return len(list((root / "state" / "handoffs").glob("*.md")))


def test_default_call_reports_cursor_and_drains_fixture_under_budget(tmp_path):
    root = _repo(tmp_path)
    start = time.process_time()
    result = _call(root)
    elapsed_ms = (time.process_time() - start) * 1000

    assert result["exit_code"] == 0
    assert result["remaining"] == 0
    assert result["more"] is False
    assert len(result["archived"]) == _N
    assert result["archived_none_reason"] is None
    assert elapsed_ms < 500, f"default call took {elapsed_ms:.0f}ms process time"


def test_repeated_default_calls_drain_when_cap_defers_work(tmp_path, monkeypatch):
    monkeypatch.setattr(cycle, "DEFAULT_CAP", 15)
    root = _repo(tmp_path)

    seen = []
    for _ in range(5):
        result = _call(root)
        seen.append((len(result["archived"]), result["remaining"], result["more"]))
        if not result["more"]:
            break

    assert seen == [(15, 25, True), (15, 10, True), (10, 0, False)]
    assert _live(root) == 0


def test_call_aborted_mid_batch_resumes_without_duplicate_moves(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    real = cycle.archive_and_commit
    moves_seen = []

    async def _flaky(worktree_root, moves, subject):
        moves_seen.append([m.candidate_id for m in moves])
        if len(moves_seen) == 1:
            raise TimeoutError("interrupted before any move landed")
        return await real(worktree_root, moves, subject)

    monkeypatch.setattr(cycle, "archive_and_commit", _flaky)

    with pytest.raises(TimeoutError):
        _call(root)
    assert _live(root) == _N

    result = _call(root)

    assert len(result["archived"]) == _N
    assert len(set(result["archived"])) == _N
    assert result["remaining"] == 0 and result["more"] is False
    assert moves_seen[1] == moves_seen[0]
    assert _live(root) == 0

    again = _call(root)
    assert again["archived"] == []
    assert again["remaining"] == 0


def test_none_terminal_reason(tmp_path):
    root = _repo(tmp_path, n=3, state="active")
    result = _call(root)
    assert result["archived"] == []
    assert result["archived_none_reason"] == "none-terminal"
    assert result["more"] is False


def test_all_excluded_reason_when_every_terminal_candidate_is_dirty(tmp_path):
    root = _repo(tmp_path, n=3)
    for path in (root / "state" / "handoffs").glob("*.md"):
        path.write_text(path.read_text(encoding="utf-8") + "dirty\n", encoding="utf-8")

    result = _call(root)

    assert result["archived"] == []
    assert result["archived_none_reason"] == "all-excluded"
    assert result["remaining"] == 0


def test_all_failed_and_lock_held_reasons(tmp_path, monkeypatch):
    root = _repo(tmp_path, n=3)

    def _fail_with(reason: str):
        async def _fail(worktree_root, moves, subject):
            return [], [{"id": m.candidate_id, "reason": reason} for m in moves]
        return _fail

    monkeypatch.setattr(cycle, "archive_and_commit", _fail_with("dst-exists"))
    result = _call(root)
    assert result["archived"] == [] and len(result["failed"]) == 3
    assert result["archived_none_reason"] == "all-failed"

    monkeypatch.setattr(
        cycle, "archive_and_commit",
        _fail_with("fatal: Unable to create '.git/index.lock': File exists."),
    )
    result = _call(root)
    assert result["archived_none_reason"] == "lock-held"
