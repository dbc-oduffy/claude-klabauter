"""
coordinator_core.ops.ceremony.tests.test_ceremony_claim_readers — C4: proves
the ceremony-plane claim readers resolve ledger-first, not off the
tracked-frontmatter mirror directly.

Purpose: two decision sites read a handoff's claimant to decide whether it
belongs to THIS session (a spoof/ownership guard). On a desynced baton (a
live claim-ledger entry with a reverted, empty frontmatter mirror — the
branch-switch-revert incident `claim_state.py`'s module docstring names,
commit 11fe08d51) a raw mirror-only read sees no claimant at all and lets the
guard pass a handoff that is actually claimed by a DIFFERENT live session:

  - `resolver.py::detect_git_provenance_consumed` — the restoration-commit
    spoof guard is disarmed; a foreign session's claimed handoff is adopted
    as this session's own predecessor.
  - `branch_resolution.py::_sanitize_consumed_handoffs` /
    `_resolve_branches` — already ledger-first via C2's migration of
    `coverage._get_handoff_consumed_by` (both call sites import and use it
    unchanged); this file's (c)/(d) tests are a regression proof that C2's
    fix already covers these sites, per this chunk's own re-verify-first
    instruction, not a second independent fix.

Spec backlink: docs/plans/2026-08-07-claim-state-ledger-first-authoritative-read.md
§ Tasks row C4.

Negative-spec: does NOT re-test `resolve_claim_state`'s own ledger/mirror
resolution logic (see `coordinator_core/tests/test_claim_state_accessor.py`)
— only that these two sites route the claimant read through it (or, for the
C2-covered branch_resolution.py sites, through `_get_handoff_consumed_by`,
which itself routes through it) instead of a raw frontmatter mirror read.
Does NOT touch the raw-grep-fallback comments in branch_resolution.py's
module docstring — `_grep_disposition` was found, on inspection, to already
delegate to `resolver.find_all_consumed_handoffs` (itself `get_handoff_consumed_by`
-anchored, C2-covered), not a literal `git grep`; no such literal grep exists
in the current source, so there is nothing left to migrate there.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/ceremony/tests/test_ceremony_claim_readers.py -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import coordinator_core.claim_state as claim_state_mod
from coordinator_core.ops.ceremony.branch_resolution import _sanitize_consumed_handoffs
from coordinator_core.ops.ceremony.resolver import detect_git_provenance_consumed

_SID = "sess-c4-own"
_FOREIGN_LIVE_SID = "sess-c4-foreign-live"


def _git(args, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def _init_repo_with_origin(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-b", "main"], root)
    _git(["config", "user.email", "c4@claude-klabauter.test"], root)
    _git(["config", "user.name", "C4 Test"], root)
    _git(["config", "commit.gpgsign", "false"], root)
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "chore: initial skeleton"], root)

    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True, capture_output=True,
    )
    _git(["remote", "add", "origin", str(bare)], root)
    push = _git(["push", "-u", "origin", "main"], root)
    assert push.returncode == 0, push.stderr
    return root


def _seed_archived_handoff_no_mirror_claim(root: Path, name: str) -> Path:
    """An archived handoff with predecessor: null and NO claimed_by/
    consumed_by in its mirror frontmatter — the reverted-mirror half of the
    desync. A live ledger claim (written separately) is the only signal a
    ledger-first reader can see."""
    path = root / "archive" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = 'title: "Test Archived Handoff"\ncreated: 2026-01-01\nstatus: archived\npredecessor: null\n'
    path.write_text(f"---\n{fm}---\n\n# Handoff Body\n", encoding="utf-8")
    return path


def _write_ledger_claim(common_dir: Path, handoff_name: str, session_id: str) -> Path:
    claim_dir = claim_state_mod.handoff_claim_dir(common_dir, Path(handoff_name))
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-08-07T10:00:00Z", encoding="utf-8")
    return claim_dir


def _commit_unpushed(root: Path, message: str) -> None:
    _git(["add", "-A"], root)
    result = _git(["commit", "-m", message], root)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# resolver.py::detect_git_provenance_consumed
# ---------------------------------------------------------------------------


def test_a_detector_b_spoof_guard_fires_on_ledger_only_foreign_claim(tmp_path):
    """(a) The mirror has NO claimed_by/consumed_by (reverted) but the ledger
    holds a LIVE claim naming a DIFFERENT session. A raw mirror-only read
    would see no claimant and adopt the handoff as sid's own predecessor —
    ledger-first must still reject it."""
    repo = _init_repo_with_origin(tmp_path)
    _seed_archived_handoff_no_mirror_claim(repo, "desync-foreign.md")
    _commit_unpushed(repo, f"restore: recover archived handoff\n\nSession-Id: {_SID}")
    _write_ledger_claim(repo / ".git", "desync-foreign.md", _FOREIGN_LIVE_SID)

    with mock.patch(
        "coordinator_core.claim_state.cs_claim_holder_live", return_value=True
    ):
        hits, warnings = detect_git_provenance_consumed(repo, _SID)

    assert hits == []
    assert any(_FOREIGN_LIVE_SID in w and "desync-foreign.md" in w for w in warnings), warnings


def test_b_detector_b_own_desynced_handoff_still_binds(tmp_path):
    """(b) The mirror has no claimant AND the ledger's live claim names THIS
    session (sid) — the desync must not spuriously reject the session's own
    predecessor."""
    repo = _init_repo_with_origin(tmp_path)
    _seed_archived_handoff_no_mirror_claim(repo, "desync-own.md")
    _commit_unpushed(repo, f"restore: recover archived handoff\n\nSession-Id: {_SID}")
    _write_ledger_claim(repo / ".git", "desync-own.md", _SID)

    with mock.patch(
        "coordinator_core.claim_state.cs_claim_holder_live", return_value=True
    ):
        hits, warnings = detect_git_provenance_consumed(repo, _SID)

    assert [p for p, _fm in hits] == ["archive/handoffs/desync-own.md"]
    assert warnings == []


def test_dead_foreign_ledger_holder_degrades_and_binds(tmp_path):
    """A ledger claim naming a foreign session that is NOT live must degrade
    to 'no ledger claim' (resolve_claim_state's own posture) — with the
    mirror also empty, the candidate binds to sid rather than being
    falsely rejected as foreign."""
    repo = _init_repo_with_origin(tmp_path)
    _seed_archived_handoff_no_mirror_claim(repo, "dead-foreign.md")
    _commit_unpushed(repo, f"archive: ship handoff\n\nSession-Id: {_SID}")
    _write_ledger_claim(repo / ".git", "dead-foreign.md", _FOREIGN_LIVE_SID)

    with mock.patch(
        "coordinator_core.claim_state.cs_claim_holder_live", return_value=False
    ):
        hits, warnings = detect_git_provenance_consumed(repo, _SID)

    assert [p for p, _fm in hits] == ["archive/handoffs/dead-foreign.md"]
    assert warnings == []


# ---------------------------------------------------------------------------
# branch_resolution.py::_sanitize_consumed_handoffs -- regression proof this
# site is already ledger-first via C2 (covered-by-C2, not re-fixed here).
# ---------------------------------------------------------------------------


def test_c_sanitize_consumed_handoffs_covered_by_c2_rejects_foreign_desync(tmp_path):
    """(c) Same desync shape against `_sanitize_consumed_handoffs`'s ownership
    check (line ~1059, `_get_handoff_consumed_by(str(hf_in_repo)) == sid`) --
    proves C2's migration of the shared coverage leaf already makes this site
    ledger-first with no edit needed here."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "c4@claude-klabauter.test"], repo)
    _git(["config", "user.name", "C4 Test"], repo)
    (repo / ".gitkeep").write_text("", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "chore: initial skeleton"], repo)

    handoff = repo / "state" / "handoffs" / "desync-c-foreign.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    fm = 'title: "Test Handoff"\ncreated: 2026-01-01\nstatus: open\npredecessor: null\n'
    handoff.write_text(f"---\n{fm}---\n\n# Handoff\n", encoding="utf-8")
    _write_ledger_claim(repo / ".git", "desync-c-foreign.md", _FOREIGN_LIVE_SID)

    with mock.patch(
        "coordinator_core.claim_state.cs_claim_holder_live", return_value=True
    ):
        kept, rejected = _sanitize_consumed_handoffs(
            repo, _SID, [("state/handoffs/desync-c-foreign.md", {})]
        )

    assert kept == []
    assert rejected == ["state/handoffs/desync-c-foreign.md"]


def test_d_sanitize_consumed_handoffs_covered_by_c2_binds_own_desync(tmp_path):
    """(d) Mirror-mute, ledger-live-own desync -- the session's own handoff
    must still bind (not silently rejected into rejected[])."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "c4@claude-klabauter.test"], repo)
    _git(["config", "user.name", "C4 Test"], repo)
    (repo / ".gitkeep").write_text("", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "chore: initial skeleton"], repo)

    handoff = repo / "state" / "handoffs" / "desync-d-own.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    fm = 'title: "Test Handoff"\ncreated: 2026-01-01\nstatus: open\npredecessor: null\n'
    handoff.write_text(f"---\n{fm}---\n\n# Handoff\n", encoding="utf-8")
    _write_ledger_claim(repo / ".git", "desync-d-own.md", _SID)

    with mock.patch(
        "coordinator_core.claim_state.cs_claim_holder_live", return_value=True
    ):
        kept, rejected = _sanitize_consumed_handoffs(
            repo, _SID, [("state/handoffs/desync-d-own.md", {})]
        )

    assert rejected == []
    assert [p for p, _fm in kept] == ["state/handoffs/desync-d-own.md"]
