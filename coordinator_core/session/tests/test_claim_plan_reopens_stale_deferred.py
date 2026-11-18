"""`claim_plan(for_execution=True)` re-opens a stale-deferred plan through
`stamp-unstaled` and then flips it to executing; every other deferral is
still refused as frozen."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import plan_status_transition as pst
from coordinator_core.session import claims
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SLUG = "2026-11-01-stale-plan"
_REL = f"docs/plans/{_SLUG}.md"
_STALE = "stale: no progress 30+ days, unverified; re-opens on pickup"


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        **no_console_creationflags(),
    )


def _repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", "seed.txt")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return tmp_path


def _plan(repo, status, reason=None):
    p = Path(repo) / _REL
    p.parent.mkdir(parents=True, exist_ok=True)
    extra = f"status_reason: {reason}\n" if reason else ""
    p.write_text(
        f"---\nstatus: {status}\ndeliverable_id: dlv-stale-plan-abc123\n{extra}---\n# plan\n",
        encoding="utf-8",
    )
    _git(repo, "add", _REL)
    _git(repo, "commit", "-q", "-m", "plan")
    return p


@pytest.fixture
def me(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "me-sid")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


@pytest.mark.parametrize("prior", ["draft", "reviewed"])
def test_stale_deferred_is_reopened_then_executing(tmp_path, me, prior):
    repo = _repo(tmp_path)
    p = _plan(repo, prior)
    assert pst.main(["stamp-deferred", "--plan", str(p), "--reason", _STALE]) == 0
    assert claims.claim_plan(_SLUG, cwd=str(repo), for_execution=True) is True
    text = p.read_text(encoding="utf-8")
    assert "status: executing" in text
    assert "status_reason" not in text


@pytest.mark.parametrize(
    "status,reason",
    [("deferred", "paused for X"), ("abandoned", _STALE), ("implemented", None)],
)
def test_other_frozen_plans_are_still_refused(tmp_path, me, status, reason):
    repo = _repo(tmp_path)
    p = _plan(repo, status, reason)
    before = p.read_text(encoding="utf-8")
    assert claims.claim_plan(_SLUG, cwd=str(repo), for_execution=True) is False
    assert p.read_text(encoding="utf-8") == before
    assert claims.list_claims_by_session("me-sid", cwd=str(repo)) == []
