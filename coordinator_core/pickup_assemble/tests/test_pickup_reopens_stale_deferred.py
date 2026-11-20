"""`pickup_brief.brief` re-opens a stale-deferred plan through `stamp-unstaled`
on a claiming single-path brief; a `--no-claim` brief and every non-stale
deferral stay untouched."""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.pickup_brief as pb
from coordinator_core.ops import plan_status_transition as pst
from coordinator_core.pickup_assemble.tests._git_harness import (
    git as _git,
    init_repo as _init_repo,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REL = "docs/plans/2026-01-01-example.md"
_STALE = "stale: no progress 30+ days, unverified; re-opens on pickup"


def _plan(repo: Path, status: str, reason: str | None = None) -> Path:
    path = repo / _REL
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = f"status_reason: {reason}\n" if reason else ""
    path.write_text(
        f'---\ntitle: "Example"\ncreated: 2026-01-01\nstatus: {status}\n{extra}---\n\n# Plan\n',
        encoding="utf-8",
    )
    _git(repo, "add", _REL)
    _git(repo, "commit", "-m", "add plan")
    return path


def _stale_deferred(repo: Path, prior: str) -> Path:
    path = _plan(repo, prior)
    assert pst.main(["stamp-deferred", "--plan", str(path), "--reason", _STALE]) == 0
    assert "status: deferred" in path.read_text(encoding="utf-8")
    return path


@pytest.mark.parametrize("prior", ["draft", "reviewed"])
def test_claiming_brief_reopens_stale_deferred_to_prior_status(tmp_path, monkeypatch, prior):
    repo = tmp_path / "repo"
    _init_repo(repo)
    path = _stale_deferred(repo, prior)
    monkeypatch.chdir(repo)
    res = pb.brief(_REL, claim_at_brief=True, repo_root=repo)
    text = path.read_text(encoding="utf-8")
    assert f"status: {prior}" in text
    assert "status_reason" not in text
    assert res.decision_object["artifact"]["frontmatter"]["status"] == prior


def test_no_claim_brief_does_not_reopen(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    path = _stale_deferred(repo, "draft")
    before = path.read_text(encoding="utf-8")
    monkeypatch.chdir(repo)
    res = pb.brief(_REL, claim_at_brief=False, repo_root=repo)
    assert path.read_text(encoding="utf-8") == before
    assert res.decision_object["artifact"]["frontmatter"]["status"] == "deferred"


@pytest.mark.parametrize(
    "status,reason",
    [
        ("deferred", "paused for X"),
        ("deferred", None),
        ("abandoned", _STALE),
        ("implemented", None),
    ],
)
def test_non_stale_or_terminal_plan_is_untouched(tmp_path, monkeypatch, status, reason):
    repo = tmp_path / "repo"
    _init_repo(repo)
    path = _plan(repo, status, reason)
    before = path.read_text(encoding="utf-8")
    monkeypatch.chdir(repo)
    pb.brief(_REL, claim_at_brief=True, repo_root=repo)
    assert path.read_text(encoding="utf-8") == before
