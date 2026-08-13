"""
coordinator_core.ops.tests.test_commit_anchors_claim_state — C6a4: proves
`commit_anchors._resolve_anchor` resolves the Anchor: claim match ledger-first,
not off the tracked-frontmatter mirror directly.

Purpose: on a desynced baton (a live claim-ledger entry with a reverted, empty
frontmatter mirror — the branch-switch-revert incident `claim_state.py`'s module
docstring names, commit 11fe08d51) the Anchor: trailer used to come back omitted
because `_resolve_anchor` read only the mirror's `claimed_by`/`consumed_by`
fields directly. That loss is permanently unrecoverable: unlike most C6a sites,
no later pass reconstructs a missing Anchor: trailer once the commit already
landed. This test seeds exactly that desync and proves the anchor now resolves.

Spec backlink: pln-claim-state-make-the-ledger-th-6641e3
§ Tasks row C6a (this chunk: C6a4, `commit_anchors._resolve_anchor`).

Negative-spec: does NOT re-test `resolve_claim_state`'s own ledger/mirror
resolution logic (see `coordinator_core/tests/test_claim_state_accessor.py`) —
only that THIS site routes the claimed_by/consumed_by hop through it instead of
a raw frontmatter mirror read. Does NOT touch `picked_up_by` matching, which has
no ledger counterpart and is unchanged.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_commit_anchors_claim_state.py -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import coordinator_core.claim_state as claim_state_mod
from coordinator_core.ops.commit_anchors import _resolve_anchor

_LIVE_CLAIMANT_SESSION_ID = "22222222-2222-2222-2222-222222222222"


def _git(args, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        encoding="utf-8",
        check=True,
    )


def _init_repo(path: Path) -> None:
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    (path / ".gitkeep").write_text("")
    _git(["add", ".gitkeep"], path)
    _git(["commit", "-m", "initial"], path)


def _seed_desynced_handoff(repo: Path, name: str) -> Path:
    """A handoff with a LIVE ledger claim but a reverted (unclaimed) mirror —
    the exact desync `resolve_claim_state`'s module docstring names."""
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    return path


def _write_ledger_claim(common_dir: Path, handoff_name: str, session_id: str) -> Path:
    claim_dir = claim_state_mod.handoff_claim_dir(common_dir, Path(handoff_name))
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-08-07T10:00:00Z", encoding="utf-8")
    return claim_dir


def test_desynced_ledger_claim_still_resolves_anchor(tmp_path):
    """A live ledger claim with a reverted mirror must still resolve the
    Anchor: breadcrumb — previously came back empty (permanently unrecoverable
    once the commit lands)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    handoff = _seed_desynced_handoff(repo, "20260101-desync.md")
    assert "claimed_by" not in handoff.read_text(encoding="utf-8")
    assert "consumed_by" not in handoff.read_text(encoding="utf-8")

    _write_ledger_claim(repo / ".git", "20260101-desync.md", _LIVE_CLAIMANT_SESSION_ID)

    with mock.patch.object(claim_state_mod, "cs_claim_holder_live", return_value=True):
        anchor = _resolve_anchor(repo, _LIVE_CLAIMANT_SESSION_ID)

    assert anchor == "handoff/20260101-desync"


def test_dead_ledger_holder_does_not_falsely_resolve(tmp_path):
    """A ledger claim whose holder is not live must NOT be treated as a match
    — resolve_claim_state degrades a dead ledger holder to mirror/none, never
    a false "ledger" source."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _seed_desynced_handoff(repo, "20260101-dead.md")

    _write_ledger_claim(repo / ".git", "20260101-dead.md", _LIVE_CLAIMANT_SESSION_ID)

    with mock.patch.object(claim_state_mod, "cs_claim_holder_live", return_value=False):
        anchor = _resolve_anchor(repo, _LIVE_CLAIMANT_SESSION_ID)

    assert anchor is None


def test_no_claim_no_match_omits_anchor(tmp_path):
    """No matching claim at all → omit (precision over recall), unchanged
    baseline behavior."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _seed_desynced_handoff(repo, "20260101-nomatch.md")

    anchor = _resolve_anchor(repo, _LIVE_CLAIMANT_SESSION_ID)

    assert anchor is None
