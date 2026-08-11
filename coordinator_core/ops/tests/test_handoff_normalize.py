"""
coordinator_core.ops.tests.test_handoff_normalize -- restored 2026-08-10 (C1b).

The prior 43-test module was deleted by commit `1d4e686a9` ("test cull:
delete the spawn-heavy Windows-poison test set from orbit") -- see
`state/audits/2026-08-07-spawn-heavy-test-excision-ledger.md`. That commit's
own PM ruling ("delete now, commit the delete, plan the restoration
separately; the coverage gap is pre-authorized") explicitly anticipates this
restoration.

Scope of THIS restoration (C1b of
docs/plans/2026-08-10-a-commit-trailer-that-names-the-session.md): ONLY the
C1a blast-radius non-regression coverage for
`_resolve_claimed_plan_deliverable_id` -- `ops/handoff_normalize.py`'s sole
caller of `resolve_claimed_plan_path` -- covering single-claim byte-identical
resolution and multi-claim deterministic earliest-`claimed_at` tie-break
(tier (b) only; tier (a)'s precedence is unchanged by C1a). This is NOT a
restoration of the deleted module's full 43-test surface -- that remains a
separately-scoped gap.

WHAT MUST NOT COME BACK: the four per-test real-`git init` conftest fixtures
the deleted module consumed (`norm_repo` among them) -- see the ledger's
"What was cut" section. `resolve_claimed_plan_path` (and the
`list_held_plan_claims` it delegates to for its tier-(b) fallback) reads
DIRECTORIES on disk, never git objects, so this file monkeypatches
`coordinator_core.session.core.sessions_dir` to point directly at a
`tmp_path`-rooted `coordinator-sessions` dir instead -- no `git init`
anywhere in this module.

Spec backlink: docs/plans/2026-08-10-a-commit-trailer-that-names-the-session.md § C1b
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.fleet._common import plan_claim_dir
from coordinator_core.ops.handoff_normalize import _resolve_claimed_plan_deliverable_id
from coordinator_core.session import core


def _write_plan(worktree_root: Path, slug: str, deliverable_id: str) -> Path:
    plan_path = worktree_root / "docs" / "plans" / f"{slug}.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        f"---\ndeliverable_id: {deliverable_id}\n---\n\n# Plan\n\nBody.\n",
        encoding="utf-8",
    )
    return plan_path


def _seed_plan_claim(
    worktree_root: Path, session_id: str, plan_slug: str, claimed_at: str
) -> None:
    common_dir = worktree_root / ".git"
    claim_dir = plan_claim_dir(common_dir, Path(f"{plan_slug}.md"))
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    (claim_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")


def _monkeypatch_sessions_dir(monkeypatch, worktree_root: Path) -> None:
    """Point `core.sessions_dir` at `<worktree_root>/.git/coordinator-sessions`
    directly -- no `git rev-parse`, no `git init`. `resolve_claimed_plan_path`
    (and `list_held_plan_claims`, which it delegates to for tier (b)) reads
    this directory tree only; it never touches git objects, so a real repo
    is unnecessary here."""
    monkeypatch.setattr(
        core,
        "sessions_dir",
        lambda cwd=None: str(worktree_root / ".git" / "coordinator-sessions"),
    )


def test_single_claim_resolution_is_byte_identical_before_and_after_c1a(
    tmp_path, monkeypatch
):
    """N<=1 held claims: C1a's tier-(b) tie-break change is a no-op here --
    the deliverable_id resolved via `resolve_claimed_plan_path` is unchanged
    from pre-C1a behavior."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-hn-single")
    _monkeypatch_sessions_dir(monkeypatch, tmp_path)
    plan_slug = "2026-08-10-hn-single-claim"
    _write_plan(tmp_path, plan_slug, "dlv-hn-single-abc123")
    _seed_plan_claim(tmp_path, "sid-hn-single", plan_slug, "2026-08-10T09:00:00Z")

    resolved = _resolve_claimed_plan_deliverable_id(tmp_path)

    assert resolved == "dlv-hn-single-abc123"


def test_multi_claim_tier_b_only_picks_earliest_claimed_at_not_alphabetical(
    tmp_path, monkeypatch
):
    """N>1 held claims, tier (a) untouched (no `session-shape.json` write at
    all in this test) so this is exercised via tier (b) ONLY: C1a's
    tie-break is deterministic earliest-`claimed_at`, not
    alphabetical-by-slug. Slugs are seeded so alphabetical order DISAGREES
    with claim order -- a regression to the pre-C1a alphabetical tie-break
    would carry the wrong plan's deliverable_id."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-hn-multi")
    _monkeypatch_sessions_dir(monkeypatch, tmp_path)
    earlier_claimed_but_later_alpha = "2026-08-10-zzz-claimed-first"
    later_claimed_but_earlier_alpha = "2026-08-10-aaa-claimed-second"
    _write_plan(tmp_path, earlier_claimed_but_later_alpha, "dlv-hn-first-claimed")
    _write_plan(tmp_path, later_claimed_but_earlier_alpha, "dlv-hn-second-claimed")
    _seed_plan_claim(
        tmp_path,
        "sid-hn-multi",
        earlier_claimed_but_later_alpha,
        "2026-08-10T09:00:00Z",
    )
    _seed_plan_claim(
        tmp_path,
        "sid-hn-multi",
        later_claimed_but_earlier_alpha,
        "2026-08-10T10:00:00Z",
    )

    resolved = _resolve_claimed_plan_deliverable_id(tmp_path)

    assert resolved == "dlv-hn-first-claimed"
