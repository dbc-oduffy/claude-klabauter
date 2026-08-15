"""
coordinator_core.ops.ceremony.tests.test_wsc_tail_partition_slate_naming

`ceremony.wsc_tail`'s `partition_mandatory` pre-commit refusal
(`_run_precommit_tail`, `if partition_mandatory and not
b_adjudication_present:`) is the one message that reaches an EM at the moment
of decision. Before this chunk it named only `gates.review_scale.commit_slices`
-- the narrower record-write population -- leaving `gates.review_scale.
chain_slices` (the chain-scoped review obligation C3 emits) unreferenced
anywhere an EM would read it. `chain_slices` risked becoming a second
`certifies_review`: a field nothing points at (plan docs/plans/2026-08-15-
chain-scope-review-gets-a-discharging-artifact.md, chunk C6).

This asserts the breach message now names BOTH slates and states their
distinction, and that the existing remediation guidance is unchanged.

Deliberately a NEW file, not an extension of `test_tail_ops.py`: that file is
ANTI-SCOPED here (a peer plan, docs/plans/2026-08-15-the-review-trail-write-
stops-paying-n-wa.md, owns it and has uncommitted edits in it in this shared
tree right now). Mirrors `test_wsc_tail_adjudication_supply_gate.py`'s own
local fixture rather than importing shared fixtures from an anti-scoped file.
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
    _git(["config", "user.email", "wsc-tail-partition-slate-naming@claude-klabauter.test"], root)
    _git(["config", "user.name", "WSC Tail Partition Slate Naming Test"], root)
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


def test_partition_mandatory_breach_names_both_slates_and_their_distinction(gate_repo):
    sid = _unique_session_id()
    before = _head_sha(gate_repo)

    result = _run(
        wsc_tail_mod._handler(
            _params(sid, partition_mandatory=True),
            repo_root=(gate_repo / ".git").resolve(),
        )
    )

    assert result["exit_code"] == 1, result
    error = result["error"]
    assert "gates.review_scale.chain_slices" in error, error
    assert "gates.review_scale.commit_slices" in error, error
    # The distinction itself, not just the two keys in isolation.
    assert "different sets by design" in error, error
    # Nothing landed -- re-running with complete metadata is still the whole
    # remedy, same load-bearing half asserted by the adjudication sibling.
    assert _head_sha(gate_repo) == before


def test_partition_mandatory_breach_remediation_guidance_is_unchanged(gate_repo):
    """The fill-in-and-supply remediation path (decisions['review'], or
    --review-slice / discrete --review-* flags) predates this chunk and must
    survive it byte-exact -- this chunk only adds slate-naming, never changes
    what an EM is told to DO about the breach. Pins the full clause, not
    independently-satisfiable fragments -- a single-word paraphrase fails
    this test."""
    sid = _unique_session_id()

    result = _run(
        wsc_tail_mod._handler(
            _params(sid, partition_mandatory=True),
            repo_root=(gate_repo / ".git").resolve(),
        )
    )

    error = result["error"]
    remediation = (
        "The engine's own workstream-complete brief already computed a "
        "per-commit slice list for this close at gates.review_scale.commit_slices "
        "-- fill in reviewer/scope/verdict per entry and supply the list as "
        "decisions['review'], or wsc-tail.py's --review-slice (repeatable) / "
        "discrete --review-* flags."
    )
    assert remediation in error, error


def test_adjudication_present_breach_is_unaffected(gate_repo):
    """Control: the sibling `b_adjudication_present` branch (no
    `partition_mandatory`) names neither slate -- this chunk's scope is the
    `partition_mandatory` branch only."""
    sid = _unique_session_id()

    result = _run(
        wsc_tail_mod._handler(
            _params(sid, b_adjudication_present=True),
            repo_root=(gate_repo / ".git").resolve(),
        )
    )

    error = result["error"]
    assert "gates.review_scale.chain_slices" not in error, error
    assert "gates.review_scale.commit_slices" not in error, error


def test_partition_mandatory_with_complete_review_trail_reaches_the_commit(gate_repo):
    """Control: complete metadata never trips the gate, same as the
    adjudication sibling's control case."""
    sid = _unique_session_id()
    before = _head_sha(gate_repo)

    result = _run(
        wsc_tail_mod._handler(
            _params(
                sid,
                partition_mandatory=True,
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

    assert "partition_mandatory with no complete review_trail" not in str(
        result.get("error", "")
    ), result
    assert result["committed_sha"] is not None, result
    assert _head_sha(gate_repo) != before
