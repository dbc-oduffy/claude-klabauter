"""A claimless abandoned draft still carries its deliverable_id.

Plan authorship takes no plan claim, so the draft's id must reach the commit
trailer and the handoff carry cascade from the artifact itself, not from a
session-keyed claim. In-process; no subprocess, no git.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.git.commit_trailers import _resolve_deliverable_id_from_paths
from coordinator_core.ops.deliverable_carry import resolve_deliverable_and_initiative
from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field
from coordinator_core.session import claimed_plan

DRAFT_ID = "dlv-abandoned-draft-000000"
DRAFT_REL = "docs/plans/2026-09-30-abandoned-draft.md"


def _write_plan_frontmatter(path: Path, **fields: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("# body")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _setup(tmp_path: Path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    sessions_dir = tmp_path / "coordinator-sessions"
    (sessions_dir / "plan-claims").mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setattr(
        claimed_plan.core, "sessions_dir", lambda cwd=None: str(sessions_dir)
    )
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "abandoned-draft-sid")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    _write_plan_frontmatter(
        repo / DRAFT_REL,
        status="draft",
        deliverable_id=f'"{DRAFT_ID}"',
    )
    return repo


def _carry_only_mint(*, deliverable_id=None, slug=None):
    assert slug is None and deliverable_id, "mint-from-slug fired: the carry dropped"
    return deliverable_id, "carry"


def test_no_claim_is_held(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    assert claimed_plan.list_held_plan_claims(str(repo)) == []
    assert claimed_plan.resolve_claimed_plan_path(str(repo)) is None


def test_commit_trailer_carries_draft_id_without_claim(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    assert claimed_plan.list_held_plan_claims(str(repo)) == []
    assert _resolve_deliverable_id_from_paths([DRAFT_REL], repo) == DRAFT_ID


def test_handoff_cascade_carries_draft_id_without_claim(tmp_path, monkeypatch):
    repo = _setup(tmp_path, monkeypatch)
    assert claimed_plan.list_held_plan_claims(str(repo)) == []
    deliverable_id, _initiative = resolve_deliverable_and_initiative(
        read_frontmatter_field,
        _carry_only_mint,
        plan_file=None,
        predecessor=str(repo / DRAFT_REL),
        predecessor_is_plan_input=True,
    )
    assert deliverable_id == DRAFT_ID
