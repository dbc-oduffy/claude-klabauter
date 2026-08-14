"""
coordinator_core.ops.ceremony.tests.test_wsc_tail_outer_empty_resolve

AC5 (docs/plans/2026-08-10-commit-event-5s-cap-and-the-silent-tail.md § C3):
`ceremony.wsc_tail` must never exit 0 having committed while flipping no
`deployment_state`. The `chain_terminal is False` path (step-1 resolve found
no consumed handoff for this sid at all) must escalate to `exit_code=2` with
a non-empty diagnostic ONLY when the resolve that produced the empty result
was itself DEGRADED (Detector A hit an enumeration gap, or Detector B could
not fully compute its hit-list) -- never for an ordinary, cleanly-resolved
single-session close, which must stay silent-exit-0 (see
`test_wsc_tail_parity.py::test_single_session_close_lands_but_names_no_flip_due_in_diagnostics`,
unchanged by this fix).

This is the OUTER empty case, distinct from the INNER empty case
(`chain_terminal` True but `post_commit_stamp_and_ship`'s post-commit
re-derive comes back empty -- already `empty_consumed_set=True`, exit_code=2,
covered by `test_wsc_tail_parity.py::
test_chain_terminal_stamp_all_skipped_surfaces_tail_item_not_silent_exit_0`
and unchanged here) -- this file does not touch that path at all, only
proves it is unaffected by re-asserting a minimal chain-terminal-True
degrade-nothing-to-stamp shape resolves the same as before.

Deliberately a NEW file, local fixture (mirrors
`test_wsc_tail_sha_unverified.py`'s own stated rationale for not importing
`WscTailRepo` from the parity file -- a sibling executor/reviewer may be
concurrently touching that file in the same dispatch wave).
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
def outer_empty_repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-b", "main"], root)
    _git(["config", "user.email", "wsc-tail-outer-empty@claude-klabauter.test"], root)
    _git(["config", "user.name", "WSC Tail Outer Empty Test"], root)
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


def test_outer_empty_degraded_resolve_escalates_to_exit_2(outer_empty_repo, monkeypatch):
    """Detector A hits an enumeration gap (scan_errors non-empty) on an
    otherwise-empty resolve -- the discriminator (`resolve_degraded`) must
    flip this from silent-exit-0 to a named exit_code=2 tail item, since an
    enumeration gap is never the same fact as 'sid genuinely consumed
    nothing' (see `resolver.find_all_consumed_handoffs`'s own docstring)."""
    repo = outer_empty_repo
    sid = _unique_session_id()

    (repo / "tasks" / "feature").mkdir(parents=True)
    (repo / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    def _degraded_find_all_consumed_handoffs(worktree_root, sid_, *, scan_errors=None):
        if scan_errors is not None:
            scan_errors.append(
                "test: simulated enumeration gap under state/handoffs/"
            )
        return []

    monkeypatch.setattr(
        wsc_tail_mod, "find_all_consumed_handoffs", _degraded_find_all_consumed_handoffs
    )
    # Detector B still runs (initial_consumed is empty) -- let it return
    # cleanly so the ONLY degradation signal is Detector A's scan_errors.
    monkeypatch.setattr(
        wsc_tail_mod, "detect_git_provenance_consumed", lambda *_a, **_kw: ([], [])
    )

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
        "outer-empty-degraded" in e and sid in e for e in stamp_result["failed"]
    ), stamp_result

    diagnostics = result["diagnostics"]
    assert any(
        "chain_terminal=False" in d and "resolve_degraded=True" in d
        for d in diagnostics
    ), diagnostics


def test_outer_empty_clean_resolve_stays_exit_0(outer_empty_repo):
    """Control: a genuinely clean empty resolve (no scan_errors, no Detector
    B warnings -- the ordinary single-session close) must NOT gain a
    `consumed_handoff_stamp["failed"]` entry and must stay `exit_code=0`,
    same as `test_wsc_tail_parity.py::
    test_single_session_close_lands_but_names_no_flip_due_in_diagnostics`."""
    repo = outer_empty_repo
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
    assert not any("outer-empty-degraded" in e for e in stamp_result["failed"]), stamp_result
    assert "consumed_handoff_stamp:no-consumed-handoff-resolved" in stamp_result["skipped"], (
        stamp_result
    )

    diagnostics = result["diagnostics"]
    assert any(
        "chain_terminal=False" in d and "resolve_degraded=False" in d
        for d in diagnostics
    ), diagnostics
