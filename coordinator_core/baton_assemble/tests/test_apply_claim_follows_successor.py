"""Handoff apply: the session's handoff claim follows the baton the run mints.

Entrypoint test: drives `apply` (brief -> directives) and executes only the
claim directives through the real `session-claim-cli` `main`, in-process.
"""

from __future__ import annotations


from coordinator_core.session_hub_path import hub_for_common
from pathlib import Path

import pytest

import coordinator_core.session.claims as claims

SESSION = "sess-current-0001"
OTHER = "sess-other-0002"
_CLI = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "session-claim-cli.py"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def _seed_claim(repo: Path, sid: str, basename: str) -> None:
    d = hub_for_common(repo / ".git") / "handoff-claims" / basename
    d.mkdir(parents=True)
    (d / "session_id").write_text(sid + "\n", encoding="utf-8")
    (d / "stage").write_text("apply\n", encoding="utf-8")


def _held(repo: Path, sid: str) -> list[str]:
    return sorted(
        b for c, b in claims.list_claims_by_session(sid, cwd=str(repo)) if c == "handoff-claims"
    )


def test_follow_is_convergent_when_the_claim_already_moved(tmp_path):
    repo = _repo(tmp_path)
    _seed_claim(repo, SESSION, "new.md")
    assert claims.follow_handoff_claim(SESSION, "old.md", "new.md", cwd=str(repo)) is True
    assert _held(repo, SESSION) == ["new.md"]


def test_follow_still_refuses_when_nothing_is_held(tmp_path):
    repo = _repo(tmp_path)
    _seed_claim(repo, OTHER, "new.md")
    assert claims.follow_handoff_claim(SESSION, "old.md", "new.md", cwd=str(repo)) is False
