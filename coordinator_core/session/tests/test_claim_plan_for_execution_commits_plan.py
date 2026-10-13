"""``claim_plan(for_execution=True)`` first-commits an untracked plan so the
stamp-executing flip lands in HEAD instead of being left uncommitted."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import claims
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SLUG = "2026-10-13-untracked-plan"
_REL = f"docs/plans/{_SLUG}.md"


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


def _write_plan(repo):
    p = Path(repo) / _REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\nstatus: reviewed\ndeliverable_id: dlv-untracked-plan-abc123\n---\n# plan\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def me(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "me-sid")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def test_untracked_plan_is_first_committed_and_flip_lands_in_head(tmp_path, me):
    repo = _repo(tmp_path)
    _write_plan(repo)
    assert claims.claim_plan(_SLUG, cwd=str(repo), for_execution=True) is True
    head_copy = _git(repo, "show", f"HEAD:{_REL}").stdout
    assert "status: executing" in head_copy
    assert _git(repo, "status", "--porcelain", "--", _REL).stdout.strip() == ""


def test_tracked_plan_takes_no_extra_first_commit(tmp_path, me):
    repo = _repo(tmp_path)
    _write_plan(repo)
    _git(repo, "add", _REL)
    _git(repo, "commit", "-q", "-m", "plan")
    before = _git(repo, "rev-list", "--count", "HEAD").stdout.strip()
    assert claims.claim_plan(_SLUG, cwd=str(repo), for_execution=True) is True
    after = _git(repo, "rev-list", "--count", "HEAD").stdout.strip()
    assert int(after) - int(before) <= 1
    assert "status: executing" in _git(repo, "show", f"HEAD:{_REL}").stdout


def test_first_commit_failure_releases_claim_and_returns_false(tmp_path, me, monkeypatch):
    from coordinator_core.ops.ceremony import git_native

    repo = _repo(tmp_path)
    _write_plan(repo)
    monkeypatch.setattr(
        git_native,
        "commit_authored_new_file",
        lambda *a, **k: git_native.GitResult(returncode=1, stdout="", stderr="boom"),
    )
    assert claims.claim_plan(_SLUG, cwd=str(repo), for_execution=True) is False
    assert "status: reviewed" in (repo / _REL).read_text(encoding="utf-8")
    assert claims.list_claims_by_session("me-sid", cwd=str(repo)) == []
