"""
coordinator_core.ops.ceremony.tests.test_wsc_tail_outer_empty_claim_contradiction

cross-repo/inbox/2026-08-11-coordinator-claude-em-pickup-claim-never-reaches-frontmatter.md:
a full `/pickup` -> work -> `/workstream-complete` cycle can leave a handoff's
frontmatter never advanced past `ready_to_fire` (a separate, `pickup_assemble`-
side bug) while the closing session's claim-record LEDGER still shows a live
`handoff-claims/` entry for that session (`session.claims.
list_claims_by_session_checked` reads that ledger directly, never the
frontmatter mirror). Before this fix, `wsc_tail`'s OUTER empty case (step-1
resolve found no consumed handoff at all, `chain_terminal=False`) stayed
silent-exit-0 whenever the resolve itself was clean (`resolve_degraded=False`)
-- indistinguishable from a genuine single-session close, exactly the shape
the memo's repro hit.

This file proves the NEW claim-store-contradiction check: a held
`handoff-claims/` entry for `sid`, combined with an otherwise-clean empty
resolve, must escalate to `exit_code=2` with a named
`outer-empty-claim-contradiction` tail item -- and that the check stays quiet
when no such claim is held (the ordinary case, already covered by
`test_wsc_tail_outer_empty_resolve.py::
test_outer_empty_clean_resolve_stays_exit_0`, re-asserted narrowly here too).

Deliberately a NEW file, local fixture (mirrors `test_wsc_tail_outer_empty_
resolve.py`'s own stated rationale -- a sibling executor/reviewer may be
concurrently touching `pickup_assemble/`/`claim_state.py` in the same
dispatch wave; this file touches neither).
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


def _unique_session_id() -> str:
    return f"test-session-{uuid.uuid4().hex[:8]}"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def claim_contradiction_repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-b", "main"], root)
    _git(["config", "user.email", "wsc-tail-claim-contradiction@claude-klabauter.test"], root)
    _git(["config", "user.name", "WSC Tail Claim Contradiction Test"], root)
    _git(["config", "commit.gpgsign", "false"], root)
    (root / "state" / "handoffs").mkdir(parents=True)
    (root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "chore: initial skeleton"], root)

    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )
    _git(["remote", "add", "origin", str(bare)], root)
    push = _git(["push", "-u", "origin", "main"], root)
    assert push.returncode == 0, push.stderr
    return root


def _write_handoff_claim(repo: Path, sid: str, basename: str) -> None:
    """Write a bare `handoff-claims/<basename>/session_id` ledger entry --
    the minimal shape `list_claims_by_session_checked` reads (a plain
    `session_id` file inside the claim dir), independent of any frontmatter
    mutation (exactly the memo's repro: the ledger got written, the
    frontmatter mirror did not)."""
    common_dir = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()
    common_dir_path = (repo / common_dir).resolve() if not Path(common_dir).is_absolute() else Path(common_dir)
    claim_dir = common_dir_path / "coordinator-sessions" / "handoff-claims" / basename
    claim_dir.mkdir(parents=True)
    (claim_dir / "session_id").write_text(sid, encoding="utf-8")


def test_outer_empty_with_held_claim_escalates_to_exit_2(claim_contradiction_repo, monkeypatch):
    """A clean empty resolve (no scan_errors, no Detector B warnings) plus a
    live `handoff-claims/` ledger entry for `sid` is the memo's contradiction
    -- must escalate from silent-exit-0 to a named exit_code=2 tail item."""
    repo = claim_contradiction_repo
    sid = _unique_session_id()

    (repo / "tasks" / "feature").mkdir(parents=True)
    (repo / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    _write_handoff_claim(repo, sid, "2026-08-11-some-predecessor-handoff.md")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=(repo / ".git").resolve(),
        )
    )

    assert result["commit_failed"] is False
    assert result["committed_sha"] is not None
    assert result["exit_code"] == 2, result

    stamp_result = result["tail_results"]["consumed_handoff_stamp"]
    assert any(
        "outer-empty-claim-contradiction" in e and sid in e
        and "2026-08-11-some-predecessor-handoff.md" in e
        for e in stamp_result["failed"]
    ), stamp_result


def test_outer_empty_no_claim_held_stays_exit_0(claim_contradiction_repo):
    """Control: a genuinely clean empty resolve with NO claim-store entry for
    `sid` (the overwhelmingly common single-session close) must not gain the
    new tail item and must stay `exit_code=0`."""
    repo = claim_contradiction_repo
    sid = _unique_session_id()

    (repo / "tasks" / "feature").mkdir(parents=True)
    (repo / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=(repo / ".git").resolve(),
        )
    )

    assert result["commit_failed"] is False
    assert result["committed_sha"] is not None
    assert result["exit_code"] == 0, result

    stamp_result = result["tail_results"]["consumed_handoff_stamp"]
    assert not any(
        "outer-empty-claim-contradiction" in e for e in stamp_result["failed"]
    ), stamp_result


def test_outer_empty_claim_held_by_other_session_stays_exit_0(claim_contradiction_repo):
    """A claim held by a DIFFERENT session must not trip the contradiction --
    the ledger check filters on `session_id == sid`, never "any claim
    exists"."""
    repo = claim_contradiction_repo
    sid = _unique_session_id()
    other_sid = _unique_session_id()

    (repo / "tasks" / "feature").mkdir(parents=True)
    (repo / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    _write_handoff_claim(repo, other_sid, "2026-08-11-unrelated-handoff.md")

    result = _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=(repo / ".git").resolve(),
        )
    )

    assert result["exit_code"] == 0, result
    stamp_result = result["tail_results"]["consumed_handoff_stamp"]
    assert not any(
        "outer-empty-claim-contradiction" in e for e in stamp_result["failed"]
    ), stamp_result
