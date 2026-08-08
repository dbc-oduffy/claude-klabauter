"""
coordinator_core.ops.ceremony.tests.test_wsc_tail_sha_unverified

W3 (docs/plans/2026-08-08-a-landed-commit-reported-as-failed.md): a commit
that LANDED but whose sha could not be resolved
(`commit_pipeline.PipelineResult.sha_unverified=True`, `commit_failed=False`,
`committed_sha=None`) must not render as `commit_pipeline:nothing-to-commit`
in `ceremony.wsc_tail`'s own `tail_results` -- the exact binary
`committed_sha or commit_failed` collapse this plan's W3 item 1 names as the
worst-case defect.

Deliberately a NEW file, not an addition to `test_wsc_tail_parity.py` --
that file is out of this chunk's scope (a sibling executor/reviewer is
already working it in the same dispatch wave). Fixture setup mirrors that
file's own `WscTailRepo`/`wsc_tail_repo` shape, kept minimal and local here
rather than imported (no exported name in that module to import from without
touching it).
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

import coordinator_core.ops.ceremony.wsc_tail as wsc_tail_mod
from coordinator_core.ops.ceremony import commit_pipeline as commit_pipeline_mod


def _git(args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _unique_session_id() -> str:
    return f"test-session-{uuid.uuid4().hex[:8]}"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def sha_unverified_repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-b", "main"], root)
    _git(["config", "user.email", "wsc-tail-sha-unverified@claude-klabauter.test"], root)
    _git(["config", "user.name", "WSC Tail Sha Unverified Test"], root)
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


def _landed_sha_unverified_outcome() -> commit_pipeline_mod.PipelineResult:
    """The exact shape `run_commit_pipeline` (W2) returns for a commit that
    landed with an unresolvable sha: `commit_failed=False`,
    `committed_sha=None`, `sha_unverified=True`, pushed proceeds."""
    return commit_pipeline_mod.PipelineResult(
        stage=None,
        deletion_gate=None,
        dirty_gate=None,
        commit=None,
        push=None,
        committed_sha=None,
        pushed=None,
        commit_failed=False,
        integrity_breach=False,
        sha_unverified=True,
        diagnostics=[
            "commit_pipeline: commit landed but its sha is unresolvable "
            "(W2 sha_unverified) -- crash sentinel did not fire",
        ],
    )


def test_landed_sha_unverified_not_rendered_as_nothing_to_commit(
    sha_unverified_repo, monkeypatch
):
    """W3 item 1 (the worst case): `tail_results["commit_pipeline"]` must
    not read `nothing-to-commit` (nor `commit-failed`) on a landed-but-
    unverified commit -- both would misreport durable history as a no-op.

    Mechanism check (red-proof): reverting `_commit_gap_reason` to the old
    `'commit-failed' if commit_failed else 'nothing-to-commit'` binary --
    equivalently, reverting `tail_results["commit_pipeline"]`'s `skipped`/
    `acted` construction to `committed_sha or commit_failed` -- makes this
    test's assertions fail: `skipped` would read
    `["commit_pipeline:nothing-to-commit"]` instead of `[]`. Verified by
    hand: temporarily restoring the pre-W3 expressions at both call sites
    and re-running this test reproduces exactly that failure; restored
    immediately after.
    """
    repo = sha_unverified_repo
    sid = _unique_session_id()

    (repo / "tasks" / "feature").mkdir(parents=True)
    (repo / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    monkeypatch.setattr(
        wsc_tail_mod, "run_commit_pipeline",
        lambda *_a, **_kw: _landed_sha_unverified_outcome(),
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

    assert result["committed_sha"] is None
    assert result["commit_failed"] is False

    commit_pipeline_result = result["tail_results"]["commit_pipeline"]
    assert commit_pipeline_result["skipped"] == []
    assert commit_pipeline_result["failed"] == []
    assert commit_pipeline_result["acted"] == [
        "commit_pipeline:landed-sha-unverified"
    ]

    # The other tail nodes this same binary repeated at (archive-sweeps,
    # consumed-handoff-stamp, origin-stub-close, deliverable-cascade) must
    # also read the three-way reason, never the collapsed two-way one.
    archive = result["tail_results"][wsc_tail_mod.tail_ops.OP_ARCHIVE_SWEEPS_DETACHED]
    assert archive["skipped"] == [
        f"{wsc_tail_mod.tail_ops.OP_ARCHIVE_SWEEPS_DETACHED}:landed-sha-unverified"
    ]

    origin_stub = result["tail_results"][wsc_tail_mod.OP_CLOSE_ORIGIN_STUB]
    assert origin_stub["skipped"] == [
        f"{wsc_tail_mod.OP_CLOSE_ORIGIN_STUB}:landed-sha-unverified"
    ]


def test_commit_gap_reason_distinguishes_all_three_states():
    """Direct unit coverage of the extracted discriminator -- the load-
    bearing predicate every `tail_results[...]` site above threads through.
    """
    assert wsc_tail_mod._commit_gap_reason(False, False) == "nothing-to-commit"
    assert wsc_tail_mod._commit_gap_reason(True, False) == "commit-failed"
    assert wsc_tail_mod._commit_gap_reason(False, True) == "landed-sha-unverified"
