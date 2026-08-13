"""
coordinator_core.ops.tests.test_handoff_normalize_carry_scope — break-class fix
(2026-08-12): the batch `handoff.normalize` sweep must not stamp its own
session's claimed-plan `deliverable_id` onto an unrelated handoff.

Provenance: archive/specs/2026-08/2026-08-01-deliverable-id-carry-onto-executing-handoff.md
("Do NOT backfill existing handoffs" — the carry is scoped to the session's OWN
executing handoff, discriminated by `claimed_by`).

Covers:
  1. batch normalize, session holds a claimed plan -> a key-absent handoff
     claimed by a DIFFERENT session (or unclaimed) is NOT stamped.
  2. same run DOES insert it into a key-absent handoff claimed by the
     CURRENT session.
  3. unresolvable session id -> no file is stamped, regardless of claimed_by.

Spawns a real `git init` because `locked_rmw`'s write path resolves the git
common dir via a real git subprocess (see `coordinator_core.lifecycle.
git_common_dir`) — no fixture stands in for that. Follows the
`test_sizing_decline.py` fixture pattern for this reason.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.frontmatter.primitives import read_fm_field
from coordinator_core.ops.fleet._common import plan_claim_dir
from coordinator_core.ops.handoff_normalize import _handler
from coordinator_core.session import core as session_core

pytestmark = [pytest.mark.spawns_process]

_GIT_ENV = {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env={**os.environ, **_GIT_ENV},
        timeout=15, stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # popup-safe-env-suppressed
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _run(params: dict, repo_root: Path) -> dict:
    return asyncio.run(_handler(params, repo_root=repo_root))


def _write_plan(worktree_root: Path, slug: str, deliverable_id: str) -> None:
    plan_path = worktree_root / "docs" / "plans" / f"{slug}.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        f"---\ndeliverable_id: {deliverable_id}\n---\n\n# Plan\n\nBody.\n",
        encoding="utf-8",
    )


def _seed_plan_claim(worktree_root: Path, session_id: str, plan_slug: str, claimed_at: str) -> None:
    common_dir = worktree_root / ".git"
    claim_dir = plan_claim_dir(common_dir, Path(f"{plan_slug}.md"))
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    (claim_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")


def _handoff_body(*, claimed_by: str | None) -> str:
    lines = ["---", "title: Test handoff", "created: 2026-08-12"]
    if claimed_by is not None:
        lines.append(f"claimed_by: {claimed_by}")
    lines += ["---", "", "# Test handoff", "", "Body.", ""]
    return "\n".join(lines)


def _write_handoff(worktree_root: Path, slug: str, *, claimed_by: str | None) -> Path:
    path = worktree_root / "state" / "handoffs" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_handoff_body(claimed_by=claimed_by), encoding="utf-8")
    return path


def _setup(tmp_path: Path, monkeypatch, *, session_id: str | None) -> Path:
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setattr(
        session_core, "sessions_dir", lambda cwd=None: str(repo / ".git" / "coordinator-sessions")
    )
    if session_id is not None:
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)
    else:
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    plan_slug = "2026-08-12-carry-scope-plan"
    _write_plan(repo, plan_slug, "dlv-carry-scope-plan-abc123")
    if session_id is not None:
        _seed_plan_claim(repo, session_id, plan_slug, "2026-08-12T09:00:00Z")
    return repo


@pytest.mark.parametrize("write", [False, True])
def test_key_absent_handoff_claimed_by_different_session_not_stamped(tmp_path, monkeypatch, write):
    session_id = "sid-carry-scope-self"
    repo = _setup(tmp_path, monkeypatch, session_id=session_id)
    other = _write_handoff(repo, "2026-08-12-other-session-handoff", claimed_by="sid-someone-else")
    unclaimed = _write_handoff(repo, "2026-08-12-unclaimed-handoff", claimed_by=None)

    result = _run({"write": write}, repo_root=repo / ".git")

    assert result["errors"] == []
    for path in (other, unclaimed):
        content = path.read_text(encoding="utf-8")
        assert read_fm_field(content, "deliverable_id") is None or not read_fm_field(
            content, "deliverable_id"
        ).startswith("dlv-carry-scope-plan-")


def test_key_absent_handoff_claimed_by_current_session_is_stamped(tmp_path, monkeypatch):
    session_id = "sid-carry-scope-self2"
    repo = _setup(tmp_path, monkeypatch, session_id=session_id)
    mine = _write_handoff(repo, "2026-08-12-my-own-handoff", claimed_by=session_id)

    result = _run({"write": True}, repo_root=repo / ".git")

    assert result["errors"] == []
    content = mine.read_text(encoding="utf-8")
    assert read_fm_field(content, "deliverable_id") == "dlv-carry-scope-plan-abc123"


@pytest.mark.parametrize("write", [False, True])
def test_batch_sweep_discriminates_per_file_within_one_invocation(tmp_path, monkeypatch, write):
    # Review: coordinator:code-reviewer (05907de0) WARN #4 — the two
    # single-file-scoped tests above don't prove the discrimination fires
    # per-file *within one sweep*, which is the actual shape of the original
    # defect (one session-derived value resolved once and threaded across
    # every globbed file in the loop). This combines mine/other's/unclaimed
    # into a single `_handler` call and asserts all three outcomes at once.
    session_id = "sid-carry-scope-batch-self"
    repo = _setup(tmp_path, monkeypatch, session_id=session_id)
    mine = _write_handoff(repo, "2026-08-12-batch-my-own-handoff", claimed_by=session_id)
    other = _write_handoff(repo, "2026-08-12-batch-other-session-handoff", claimed_by="sid-someone-else")
    unclaimed = _write_handoff(repo, "2026-08-12-batch-unclaimed-handoff", claimed_by=None)

    result = _run({"write": write}, repo_root=repo / ".git")

    assert result["errors"] == []
    for path in (other, unclaimed):
        content = path.read_text(encoding="utf-8")
        assert read_fm_field(content, "deliverable_id") is None or not read_fm_field(
            content, "deliverable_id"
        ).startswith("dlv-carry-scope-plan-")

    mine_content = mine.read_text(encoding="utf-8")
    if write:
        assert read_fm_field(mine_content, "deliverable_id") == "dlv-carry-scope-plan-abc123"
    else:
        # dry-run must not mutate disk, but the report should still name the
        # file it would have stamped with the CARRIED value specifically —
        # other/unclaimed may still report unrelated normalize changes (e.g.
        # a freshly minted, non-carried deliverable_id), so the assertion is
        # on the carry-specific changes entry, not on "changed" membership.
        assert read_fm_field(mine_content, "deliverable_id") is None
        mine_entries = [e for e in result["changed"] if e["file"].endswith(mine.name)]
        assert mine_entries and any(
            "dlv-carry-scope-plan-abc123" in c for c in mine_entries[0]["changes"]
        )
        for other_path in (other, unclaimed):
            entries = [e for e in result["changed"] if e["file"].endswith(other_path.name)]
            for entry in entries:
                assert not any("dlv-carry-scope-plan-abc123" in c for c in entry["changes"])


def test_unresolvable_session_id_stamps_nothing(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch, session_id=None)
    # Review: code-reviewer (05907de0) finding #5 — reworded for WHAT over WHY.
    # session_id=None skips the claim seed, so carried_deliverable_id resolves
    # to None; asserts no deliverable_id carrying happens anywhere in the sweep.
    unclaimed = _write_handoff(repo, "2026-08-12-nobody-claims-me", claimed_by=None)

    result = _run({"write": True}, repo_root=repo / ".git")

    assert result["errors"] == []
    content = unclaimed.read_text(encoding="utf-8")
    minted = read_fm_field(content, "deliverable_id")
    assert minted is not None
    assert not minted.startswith("dlv-carry-scope-plan-")
