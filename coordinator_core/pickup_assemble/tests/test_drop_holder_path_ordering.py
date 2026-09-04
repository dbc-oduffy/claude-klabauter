"""
coordinator_core.pickup_assemble.tests.test_drop_holder_path_ordering — C5's
THIRD test (docs/plans/2026-08-30-drop-releases-a-claim-it-never-held.md),
the holder path neither falsifier leg exercises.

Purpose: the common path — drop as the RECORDED holder — is the one path the
ORDERING CONTRACT reorder and the `APPLY_EXIT_PARTIAL_MUTATION` report fix
actually change behaviour on. Without an oracle here, a reorder that
silently breaks the holder drop would read green on every other test this
plan adds (LEG 1 exercises the non-holder refusal; LEG 2 exercises the
unification precondition — neither touches this path).

Drives the REAL `drop()` end to end against a real git repo and asserts:
  - the frontmatter is reverted (status: open, deployment_state:
    ready_to_fire, `claimed_by`/`claimed_at` stripped)
  - the claim ledger dir is gone
  - `released` and `unclaimed` both read `True`
  - exactly one commit lands
  - the actual ORDERING CONTRACT: the frontmatter revert
    (`cs_unclaim_handoff`) precedes `release_artifact`, proven with a
    call-order recorder that wraps the REAL primitives (each still runs its
    real effect) rather than mocking either one's effect away.

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_drop_holder_path_ordering.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.pickup_assemble.apply as pa_apply

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


from coordinator_core.pickup_assemble.tests._git_harness import (
    git as _git,
    init_repo as _init_repo,
)


HOLDER_SID = "sid-holder"

_HANDOFF_FM = (
    'title: "Test Handoff"\n'
    "created: 2026-01-01\n"
    "branch: work/test/2026-01-01\n"
    "status: claimed\n"
    'predecessor: "none"\n'
    "deployment_state: in_flight\n"
    f"claimed_by: {HOLDER_SID}\n"
    "claimed_at: 2026-01-01T00:00:00Z\n"
)


def _seed_claimed_handoff(repo: Path, name: str) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{_HANDOFF_FM}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _write_ledger_claim(repo: Path, basename: str, holder_sid: str) -> Path:
    cdir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "session_id").write_text(f"{holder_sid}\n", encoding="utf-8")
    (cdir / "claimed_at").write_text("2026-01-01T00:00:00Z\n", encoding="utf-8")
    (cdir / "stage").write_text("apply\n", encoding="utf-8")
    return cdir


def _rev_count(repo: Path) -> str:
    return _git(repo, "rev-list", "--count", "HEAD").stdout.strip()


def test_holder_drop_reverts_frontmatter_releases_claim_one_commit_correct_order(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_claimed_handoff(repo, "h1.md")
    cdir = _write_ledger_claim(repo, "h1.md", HOLDER_SID)

    call_order: list[str] = []
    real_unclaim = pa_apply.cs_unclaim_handoff
    real_release = pa_apply.release_artifact

    def _recording_unclaim(*args, **kwargs):
        call_order.append("cs_unclaim_handoff")
        return real_unclaim(*args, **kwargs)

    def _recording_release(*args, **kwargs):
        call_order.append("release_artifact")
        return real_release(*args, **kwargs)

    monkeypatch.setattr(pa_apply, "cs_unclaim_handoff", _recording_unclaim)
    monkeypatch.setattr(pa_apply, "release_artifact", _recording_release)
    # `release_artifact` resolves `my_sid` ambiently (`core.resolve_session_id`),
    # not from `drop`'s own `--session-id` — the ambient env must agree with
    # the explicit id passed below for the release half to see itself as
    # the holder.
    monkeypatch.setenv("COORDINATOR_SESSION_ID", HOLDER_SID)

    before_rev_count = int(_rev_count(repo))

    exit_code, report = pa_apply.drop(
        "state/handoffs/h1.md", session_id=HOLDER_SID, repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_OK
    assert report["released"] is True
    assert report["unclaimed"] is True

    after_text = handoff.read_text(encoding="utf-8")
    assert "status: open" in after_text
    assert "deployment_state: ready_to_fire" in after_text
    assert "claimed_by" not in after_text
    assert "claimed_at" not in after_text

    assert not cdir.is_dir(), "the claim ledger dir must be gone after a holder drop"

    after_rev_count = int(_rev_count(repo))
    assert after_rev_count == before_rev_count + 1, "exactly one commit must land"

    assert call_order == ["cs_unclaim_handoff", "release_artifact"], (
        "ORDERING CONTRACT: the frontmatter revert must precede "
        "release_artifact, proven via a call-order recorder wrapping the "
        "real primitives, not a mock of either one's effect"
    )


def test_holder_drop_scoped_commit_failure_reports_partial_mutation_truthfully(
    tmp_path, monkeypatch
):
    """`_scoped_commit` failing on the tail must not escape as a raised
    `RuntimeError` — the frontmatter revert and ledger release have both
    already landed by that point, so `drop` must report
    `APPLY_EXIT_PARTIAL_MUTATION` with `released`/`unclaimed` describing
    what actually happened (both True), not the earlier `released: False`
    shape used above `release_artifact`.

    `_scoped_commit` itself is patched to raise — not `release_artifact` or
    either class-inverse transition primitive, which is the false-green
    shape this test family exists to avoid. Everything up to the commit
    call runs for real against a real git repo.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_claimed_handoff(repo, "h2.md")
    cdir = _write_ledger_claim(repo, "h2.md", HOLDER_SID)

    monkeypatch.setenv("COORDINATOR_SESSION_ID", HOLDER_SID)

    def _raising_scoped_commit(*args, **kwargs):
        raise RuntimeError("simulated git commit failure")

    monkeypatch.setattr(pa_apply, "_scoped_commit", _raising_scoped_commit)

    exit_code, report = pa_apply.drop(
        "state/handoffs/h2.md", session_id=HOLDER_SID, repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_PARTIAL_MUTATION
    assert "simulated git commit failure" in report["error"]
    assert report["released"] is True
    assert report["unclaimed"] is True
    assert report["commit_sha"] is None

    # The frontmatter revert and ledger release genuinely landed even
    # though the commit did not — the report tells the truth about both.
    after_text = handoff.read_text(encoding="utf-8")
    assert "status: open" in after_text
    assert "deployment_state: ready_to_fire" in after_text
    assert "claimed_by" not in after_text
    assert not cdir.is_dir(), "the claim ledger dir must be gone despite the commit failure"
