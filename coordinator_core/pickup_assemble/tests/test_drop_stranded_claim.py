"""
coordinator_core.pickup_assemble.tests.test_drop_stranded_claim — `drop`
releases an apply-stage ledger claim whose frontmatter stamp never landed.

Purpose: a crash between the ledger grant and the frontmatter stamp leaves
`status: open`, no `claimed_by`, and a held ledger claim. `drop` must release
the ledger for every `deployment_state` without touching the frontmatter
bytes or committing, and a stamped claim must still go through
`cs_unclaim_handoff`.

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_drop_stranded_claim.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.pickup_assemble.apply as pa_apply
from coordinator_core.pickup_assemble.tests._git_harness import (
    git as _git,
    init_repo as _init_repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_STATES = [
    "ready_to_fire",
    "in_flight",
    "awaiting_gate",
    "shipped",
    "abandoned",
    "continued",
    "closed",
]


_TERMINAL = {"shipped", "abandoned", "continued", "closed"}


def _seed_unstamped(repo: Path, name: str, deployment_state: str) -> Path:
    fm = (
        'title: "Test Handoff"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        f"deployment_state: {deployment_state}\n"
    )
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _write_ledger_claim(repo: Path, basename: str, holder_sid: str) -> None:
    cdir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "session_id").write_text(f"{holder_sid}\n", encoding="utf-8")
    (cdir / "claimed_at").write_text("2026-01-01T00:00:00Z\n", encoding="utf-8")
    (cdir / "stage").write_text("apply\n", encoding="utf-8")


def _spy_release(monkeypatch) -> list:
    calls: list = []
    real = pa_apply.release_artifact

    def _spy(class_, basename, **kwargs):
        calls.append((class_, basename))
        return real(class_, basename, **kwargs)

    monkeypatch.setattr(pa_apply, "release_artifact", _spy)
    return calls


@pytest.mark.parametrize("state", _STATES)
def test_unstamped_stranded_claim_is_released_and_frontmatter_untouched(
    tmp_path, monkeypatch, state
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_unstamped(repo, "h-strand.md", state)
    _write_ledger_claim(repo, "h-strand.md", "sid-holder")
    monkeypatch.chdir(repo)
    release_calls = _spy_release(monkeypatch)
    unclaim_calls: list = []
    monkeypatch.setattr(
        pa_apply,
        "cs_unclaim_handoff",
        lambda *a, **k: unclaim_calls.append(a) or True,
    )
    before = handoff.read_bytes()
    rev_before = _git(repo, "rev-list", "--count", "HEAD").stdout.strip()

    exit_code, report = pa_apply.drop(
        "state/handoffs/h-strand.md", session_id="sid-holder", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_OK, report
    assert report["released"] is True
    expected = "skipped-terminal" if state in _TERMINAL else "skipped-unstamped"
    assert report["frontmatter_revert"] == expected
    assert report["commit_sha"] is None
    assert release_calls == [("handoff", "h-strand.md")]
    assert unclaim_calls == []
    assert handoff.read_bytes() == before
    assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == rev_before


def test_stamped_claim_still_goes_through_cs_unclaim_handoff(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_unstamped(repo, "h-stamped.md", "in_flight")
    text = handoff.read_text(encoding="utf-8").replace(
        "status: open\n", "status: claimed\nclaimed_by: sid-holder\n"
    )
    handoff.write_text(text, encoding="utf-8")
    _git(repo, "commit", "-am", "stamp")
    _write_ledger_claim(repo, "h-stamped.md", "sid-holder")
    monkeypatch.chdir(repo)

    exit_code, report = pa_apply.drop(
        "state/handoffs/h-stamped.md", session_id="sid-holder", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_OK, report
    assert report["unclaimed"] is True
    assert report.get("frontmatter_revert") is None
    assert "status: open" in handoff.read_text(encoding="utf-8")
