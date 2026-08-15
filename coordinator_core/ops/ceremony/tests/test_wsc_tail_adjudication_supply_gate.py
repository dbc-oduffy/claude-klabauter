"""
coordinator_core.ops.ceremony.tests.test_wsc_tail_adjudication_supply_gate

`ceremony.wsc_tail` asserting adjudication without a complete `review_trail`
record must refuse BEFORE the ceremony commit, not after it.

`tail_ops.write_review_trail`'s `b_adjudication_present` gate has always
turned that input into a `failed_critical[]` entry -- but `failed_critical`
only becomes an exit code at the bottom of `_handler`, by which point the
commit has landed. The observed shape (2026-08-14): a close-out exiting
non-zero on `b_adjudication present but review_trail missing required
fields: sha_range` with its own bookkeeping commit already made -- a
green-looking ceremony carrying no trail record, and nothing left to re-run.
The breach is decidable from params alone, so it is decided before the
roadmap render, the coverage gate, and the commit.

Deliberately a NEW file with a local fixture, mirroring
`test_wsc_tail_outer_empty_resolve.py`'s own stated rationale for not
importing the parity file's `WscTailRepo`: a sibling executor/reviewer may be
concurrently touching that file.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

import coordinator_core.ops.ceremony.wsc_tail as wsc_tail_mod

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _head_sha(repo: Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


def _unique_session_id() -> str:
    return f"test-session-{uuid.uuid4().hex[:8]}"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def gate_repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-b", "main"], root)
    _git(["config", "user.email", "wsc-tail-adjudication-gate@claude-klabauter.test"], root)
    _git(["config", "user.name", "WSC Tail Adjudication Gate Test"], root)
    _git(["config", "commit.gpgsign", "false"], root)
    (root / "state" / "handoffs").mkdir(parents=True)
    (root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "chore: initial skeleton"], root)
    (root / "tasks" / "feature").mkdir(parents=True)
    (root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")
    return root


def _params(sid: str, **extra) -> dict:
    return {
        "sid": sid,
        "subject": "workstream-complete: feature",
        "stage_paths": ["tasks/feature/todo.md"],
        "caller_paths": ["tasks/feature/todo.md"],
        **extra,
    }


def test_adjudication_without_review_trail_refuses_before_commit(gate_repo):
    sid = _unique_session_id()
    before = _head_sha(gate_repo)

    result = _run(
        wsc_tail_mod._handler(
            _params(sid, b_adjudication_present=True),
            repo_root=(gate_repo / ".git").resolve(),
        )
    )

    assert result["exit_code"] == 1, result
    assert "b_adjudication_present" in result["error"]
    assert "sha_range" in result["error"]
    # The load-bearing half: nothing landed, so re-running with complete
    # metadata is still the whole remedy.
    assert _head_sha(gate_repo) == before


def test_adjudication_with_incomplete_review_trail_refuses_before_commit(gate_repo):
    """A partial dict is the same breach as an absent one -- `write_review_
    trail` requires all five fields, so four of them writes no record."""
    sid = _unique_session_id()
    before = _head_sha(gate_repo)

    result = _run(
        wsc_tail_mod._handler(
            _params(
                sid,
                b_adjudication_present=True,
                review_trail={"reviewer": "code-reviewer", "scope": "session", "verdict": "ok"},
            ),
            repo_root=(gate_repo / ".git").resolve(),
        )
    )

    assert result["exit_code"] == 1, result
    assert _head_sha(gate_repo) == before


def test_adjudication_with_complete_review_trail_reaches_the_commit(gate_repo):
    """Control: complete metadata never trips the gate. The write itself may
    still be refused downstream (foreign-session range, reviewer evidence) --
    that surfaces as a tail item on a LANDED commit, which is the pre-existing
    contract this gate deliberately leaves alone."""
    sid = _unique_session_id()
    before = _head_sha(gate_repo)

    result = _run(
        wsc_tail_mod._handler(
            _params(
                sid,
                b_adjudication_present=True,
                review_trail={
                    "sha_range": f"{before}^..{before}",
                    "reviewer": "code-reviewer",
                    "scope": "session",
                    "verdict": "ok",
                    "diff_loc": "1",
                },
            ),
            repo_root=(gate_repo / ".git").resolve(),
        )
    )

    assert "b_adjudication_present with no complete review_trail" not in str(
        result.get("error", "")
    ), result
    assert result["committed_sha"] is not None, result
    assert _head_sha(gate_repo) != before


def test_no_adjudication_assertion_leaves_the_missing_trail_a_clean_skip(gate_repo):
    """Control: without the adjudication assertion, absent review metadata is
    the ordinary 'no review this session' skip -- unchanged, still commits."""
    sid = _unique_session_id()
    before = _head_sha(gate_repo)

    result = _run(
        wsc_tail_mod._handler(
            _params(sid), repo_root=(gate_repo / ".git").resolve()
        )
    )

    assert result["exit_code"] != 1, result
    assert result["committed_sha"] is not None
    assert _head_sha(gate_repo) != before
