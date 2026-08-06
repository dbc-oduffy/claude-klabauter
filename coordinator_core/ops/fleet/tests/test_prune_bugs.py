"""
Tests for coordinator_core.ops.fleet.prune_bugs — fleet.prune_closed_bugs handler.

Coverage:
  (a) dry_run:true → candidates[] with family:"bug"; no mutation.
  (b) dry_run:false → source gone, dest present, git log exact src+dst (--no-renames),
      git status clean (AC4/AC10).
  (c) closed-only: open bugs untouched (D1 drift skip on act).
  (d) YYYY-MM derivation: filename prefix primary, created: fallback.
  (e) Idempotent replay (AC12): re-dispatch same candidate_ids → skipped "already-archived",
      exit_code:0, mutates nothing.

Import guard (lesson: state/lessons/2026-07-04-universal-registry-completeness-tests-ov.yaml):
  Importing coordinator_core.ops.fleet.prune_bugs fires @register_op("fleet.prune_closed_bugs")
  before any test that relies on registry state.  Floor assertion: ≥1 fleet.* op registered.

Harness: asyncio.run() in sync test fns — no pytest-asyncio dependency.
Handler called directly with repo_root=fleet_repo.common_dir.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

# ---- Import guard: fires @register_op side-effect for fleet.prune_closed_bugs. ----
import coordinator_core.ops.fleet.prune_bugs  # noqa: F401

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.fleet.prune_bugs import _handler


def _run(coro):
    """Run async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------

def _git_log_names_no_renames(fleet_repo, n: int = 1):
    """Return file paths touched in the last n commits with rename-detection disabled.

    Uses --no-renames so that a git-mv rename is shown as BOTH the source (deleted)
    and the destination (added) as separate path entries — unlike the default
    --name-only behaviour which collapses a rename into only the destination path.
    Required to verify the scoped-pathspec contract (AC4 / DR-211 D3): both src AND
    dst must appear in the commit.

    Mirrors the same helper in test_archive_handoffs.py.
    """
    result = subprocess.run(
        ["git", "log", f"-{n}", "--no-renames", "--name-only", "--format="],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    )
    return [
        line
        for line in result.stdout.decode(errors="replace").strip().splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Import-guard floor assertion
# ---------------------------------------------------------------------------


def test_prune_bugs_registered():
    """fleet.prune_closed_bugs must be in _REGISTRY after the import guard fires."""
    fleet_ops = [k for k in _REGISTRY if k.startswith("fleet.")]
    assert len(fleet_ops) >= 1, (
        "At least one fleet.* op must be registered after the import guard; "
        f"registered ops: {sorted(_REGISTRY.keys())}"
    )
    assert "fleet.prune_closed_bugs" in _REGISTRY, (
        "fleet.prune_closed_bugs must be registered"
    )


# ---------------------------------------------------------------------------
# (a) dry_run:true — candidates[] with family:"bug"; no mutation
# ---------------------------------------------------------------------------


def test_dry_run_returns_only_closed_candidates(fleet_repo):
    """dry_run:true with mixed statuses → candidates[] contains only closed bugs."""
    fleet_repo.seed_bug("2026-03-10-closed.yaml", "closed", title="Closed Bug")
    fleet_repo.seed_bug("2026-03-11-open.yaml", "open", title="Open Bug")
    fleet_repo.seed_bug("2026-03-12-wip.yaml", "in-progress", title="WIP Bug")

    result = _run(_handler(
        {"mode": "already-terminal", "dry_run": True},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["dry_run"] is True
    assert result["mode"] == "already-terminal"
    # Only the closed bug should appear
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["id"] == "state/bug-backlog/2026-03-10-closed.yaml"


def test_dry_run_family_is_bug(fleet_repo):
    """Every candidate returned by dry_run:true must have family='bug'."""
    fleet_repo.seed_bug("2026-04-01-bug-a.yaml", "closed")
    fleet_repo.seed_bug("2026-04-02-bug-b.yaml", "closed")

    result = _run(_handler(
        {"mode": "already-terminal", "dry_run": True},
        repo_root=fleet_repo.common_dir,
    ))

    assert len(result["candidates"]) == 2
    for candidate in result["candidates"]:
        assert candidate["family"] == "bug", (
            f"Expected family='bug', got {candidate['family']!r} for {candidate['id']}"
        )


def test_dry_run_no_mutation(fleet_repo):
    """dry_run:true must not move any files or alter git state."""
    bug_path = fleet_repo.seed_bug("2026-03-10-closed.yaml", "closed")
    candidate_id = fleet_repo.repo_rel(bug_path)

    _run(_handler(
        {"mode": "already-terminal", "dry_run": True},
        repo_root=fleet_repo.common_dir,
    ))

    assert fleet_repo.path_exists(candidate_id), "Source must still exist after dry_run"
    assert fleet_repo.git_status_clean(), "git status must be clean after dry_run"


def test_dry_run_empty_when_no_closed_bugs(fleet_repo):
    """dry_run:true with no closed bugs → candidates[] is empty, exit_code:0."""
    fleet_repo.seed_bug("2026-03-10-open.yaml", "open")

    result = _run(_handler(
        {"mode": "already-terminal", "dry_run": True},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["candidates"] == []


def test_dry_run_candidate_fields(fleet_repo):
    """Candidate dict must contain id, title, status, family, terminal_since, note."""
    fleet_repo.seed_bug("2026-04-01-my-bug.yaml", "closed", title="My Bug")

    result = _run(_handler(
        {"mode": "already-terminal", "dry_run": True},
        repo_root=fleet_repo.common_dir,
    ))

    assert len(result["candidates"]) == 1
    c = result["candidates"][0]
    assert "id" in c
    assert "title" in c
    assert "status" in c
    assert c["status"] == "closed"
    assert "family" in c
    assert c["family"] == "bug"
    assert "terminal_since" in c   # may be None — degrade gracefully
    assert "note" in c             # null for bugs (handoff-only field, contract §2.1)
    assert c["note"] is None
    # to_path / from_path MUST NOT appear (contract §2.1)
    assert "to_path" not in c
    assert "from_path" not in c


# ---------------------------------------------------------------------------
# (b) dry_run:false — source gone, dest present, git log exact, git status clean
# ---------------------------------------------------------------------------


def test_act_archives_closed_bug(fleet_repo):
    """dry_run:false archives a closed bug: source gone, dest present, acted[] correct."""
    bug_path = fleet_repo.seed_bug("2026-03-15-closed-bug.yaml", "closed")
    candidate_id = fleet_repo.repo_rel(bug_path)

    result = _run(_handler(
        {"mode": "already-terminal", "dry_run": False, "candidate_ids": [candidate_id]},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["acted"] == [{"id": candidate_id, "archived": True}]
    assert result["skipped"] == []
    assert result["failed"] == []

    # Source must be gone
    assert not fleet_repo.path_exists(candidate_id), "Source file must be absent after archival"
    # Destination must be present under archive/bug-backlog/YYYY-MM/
    dest_rel = f"archive/bug-backlog/2026-03/{bug_path.name}"
    assert fleet_repo.path_exists(dest_rel), f"Dest must exist at {dest_rel}"


def test_act_git_log_contains_both_src_and_dst(fleet_repo):
    """After archival, git log -1 (--no-renames) must list BOTH src AND dst paths.

    Uses --no-renames to disable rename-collapse: git mv is shown as deletion(src) +
    addition(dst) rather than just the destination (the default --name-only behaviour).
    Verifies the scoped-pathspec requirement: both sides in the commit (AC4/DR-211 D3).
    """
    bug_path = fleet_repo.seed_bug("2026-05-20-my-bug.yaml", "closed")
    candidate_id = fleet_repo.repo_rel(bug_path)

    _run(_handler(
        {"mode": "already-terminal", "dry_run": False, "candidate_ids": [candidate_id]},
        repo_root=fleet_repo.common_dir,
    ))

    log_names = _git_log_names_no_renames(fleet_repo, 1)
    dest_rel = f"archive/bug-backlog/2026-05/{bug_path.name}"

    assert candidate_id in log_names, (
        f"src path must appear in git log (--no-renames); got {log_names}"
    )
    assert dest_rel in log_names, (
        f"dst path must appear in git log (--no-renames); got {log_names}"
    )


def test_act_git_status_clean_after_archive(fleet_repo):
    """git status --porcelain must be empty after a successful archival (AC4/AC10)."""
    bug_path = fleet_repo.seed_bug("2026-05-20-clean-bug.yaml", "closed")
    candidate_id = fleet_repo.repo_rel(bug_path)

    _run(_handler(
        {"mode": "already-terminal", "dry_run": False, "candidate_ids": [candidate_id]},
        repo_root=fleet_repo.common_dir,
    ))

    assert fleet_repo.git_status_clean(), (
        "git status --porcelain must be empty after archival (AC4/AC10)"
    )


# ---------------------------------------------------------------------------
# (c) closed-only: open bugs untouched (D1 drift skip on act)
# ---------------------------------------------------------------------------


def test_act_skips_open_bugs(fleet_repo):
    """dry_run:false with an open bug in candidate_ids → it is skipped, not archived."""
    closed_path = fleet_repo.seed_bug("2026-04-01-closed.yaml", "closed")
    open_path = fleet_repo.seed_bug("2026-04-02-open.yaml", "open")

    closed_id = fleet_repo.repo_rel(closed_path)
    open_id = fleet_repo.repo_rel(open_path)

    result = _run(_handler(
        {
            "mode": "already-terminal",
            "dry_run": False,
            "candidate_ids": [closed_id, open_id],
        },
        repo_root=fleet_repo.common_dir,
    ))

    # exit_code:0 — failed[] is empty; open bug is skipped (D1 drift), not failed
    assert result["exit_code"] == 0
    assert len(result["acted"]) == 1
    assert result["acted"][0]["id"] == closed_id
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["id"] == open_id
    assert result["failed"] == []
    # Open bug still on disk
    assert fleet_repo.path_exists(open_id), "Open bug must not be moved"


def test_d1_drift_skip(fleet_repo):
    """D1 re-verify: a bug drifted open between T1 and T3 → skipped on act, not archived."""
    bug_path = fleet_repo.seed_bug("2026-04-05-drifting.yaml", "closed")
    candidate_id = fleet_repo.repo_rel(bug_path)

    # Drift: mutate the bug back to open before the act dispatch
    fleet_repo.update_file_status(bug_path, "open")

    result = _run(_handler(
        {"mode": "already-terminal", "dry_run": False, "candidate_ids": [candidate_id]},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["acted"] == []
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["id"] == candidate_id
    # Reason must reflect the drift (contains "drift" or current status)
    reason = result["skipped"][0]["reason"]
    assert "drift" in reason or "open" in reason, (
        f"Skip reason must reflect drift; got {reason!r}"
    )
    assert result["failed"] == []
    assert fleet_repo.path_exists(candidate_id), "Drifted bug must remain on disk"


# ---------------------------------------------------------------------------
# (d) YYYY-MM derivation from filename prefix (primary) / created: (fallback)
# ---------------------------------------------------------------------------


def test_yyyy_mm_from_filename_prefix(fleet_repo):
    """YYYY-MM derived from filename prefix YYYY-MM-DD-slug.yaml → archive/bug-backlog/YYYY-MM/."""
    bug_path = fleet_repo.seed_bug("2026-05-20-mybug.yaml", "closed")
    candidate_id = fleet_repo.repo_rel(bug_path)

    result = _run(_handler(
        {"mode": "already-terminal", "dry_run": False, "candidate_ids": [candidate_id]},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert fleet_repo.path_exists(f"archive/bug-backlog/2026-05/{bug_path.name}"), (
        "Bug must be archived under archive/bug-backlog/2026-05/ per filename prefix"
    )


def test_yyyy_mm_fallback_from_created_frontmatter(fleet_repo):
    """When filename has no date prefix, YYYY-MM falls back to created: frontmatter."""
    bug_path = fleet_repo.seed_bug(
        "no-date-prefix-bug.yaml", "closed", created="2026-08-01"
    )
    candidate_id = fleet_repo.repo_rel(bug_path)

    result = _run(_handler(
        {"mode": "already-terminal", "dry_run": False, "candidate_ids": [candidate_id]},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert fleet_repo.path_exists(f"archive/bug-backlog/2026-08/{bug_path.name}"), (
        "Bug must be archived under archive/bug-backlog/2026-08/ per created: frontmatter"
    )


def test_yyyy_mm_correct_month_only(fleet_repo):
    """YYYY-MM extraction must take only the YYYY-MM portion (not YYYY-MM-DD)."""
    bug_path = fleet_repo.seed_bug("2026-12-31-year-end.yaml", "closed")
    candidate_id = fleet_repo.repo_rel(bug_path)

    result = _run(_handler(
        {"mode": "already-terminal", "dry_run": False, "candidate_ids": [candidate_id]},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert fleet_repo.path_exists(f"archive/bug-backlog/2026-12/{bug_path.name}"), (
        "Bug dated 2026-12-31 must archive under 2026-12 (month only, not full date)"
    )


# ---------------------------------------------------------------------------
# SECURITY — path traversal containment (CRITICAL 2)
# ---------------------------------------------------------------------------


def test_path_traversal_absolute_rejected(fleet_repo):
    """Absolute-path candidate_id is rejected into failed[] before any file read or git op.

    Path('/x')/'/etc/passwd' resolves to /etc/passwd (absolute override); the
    containment guard must catch this before _read_plain_yaml reads the file
    and before git-mv is attempted.
    """
    # Seed a valid closed bug so the rejection is not due to an empty batch.
    fleet_repo.seed_bug("2026-03-10-legit.yaml", "closed", title="Legit Bug")

    malicious_id = "/etc/passwd"
    result = _run(_handler(
        {"mode": "already-terminal", "dry_run": False, "candidate_ids": [malicious_id]},
        repo_root=fleet_repo.common_dir,
    ))

    # Must land in failed[], not acted[] or skipped[].
    failed_ids = [f["id"] for f in result["failed"]]
    assert malicious_id in failed_ids, (
        f"Absolute-path candidate_id must appear in failed[]; got failed={result['failed']}"
    )
    failed_reasons = {f["id"]: f["reason"] for f in result["failed"]}
    assert "path-traversal" in failed_reasons[malicious_id], (
        f"Failure reason must mention path-traversal; got {failed_reasons[malicious_id]!r}"
    )

    # Must NOT appear in acted[].
    assert malicious_id not in [a["id"] for a in result["acted"]], (
        "Malicious candidate must NOT be archived"
    )

    # git status must be clean — no file was moved.
    assert fleet_repo.git_status_clean(), "git status must be clean after path-traversal rejection"


def test_path_traversal_dotdot_rejected(fleet_repo):
    """../ traversal candidate_id is rejected into failed[] before any file read or git op.

    A candidate_id like 'state/bug-backlog/../../docs/plans/secret.md' resolves outside
    state/bug-backlog/ and must be caught by the containment guard.
    """
    fleet_repo.seed_bug("2026-03-10-legit2.yaml", "closed", title="Legit Bug 2")

    traversal_id = "state/bug-backlog/../../docs/plans/escape.md"
    result = _run(_handler(
        {"mode": "already-terminal", "dry_run": False, "candidate_ids": [traversal_id]},
        repo_root=fleet_repo.common_dir,
    ))

    failed_ids = [f["id"] for f in result["failed"]]
    assert traversal_id in failed_ids, (
        f"../ traversal candidate_id must appear in failed[]; got failed={result['failed']}"
    )
    failed_reasons = {f["id"]: f["reason"] for f in result["failed"]}
    assert "path-traversal" in failed_reasons[traversal_id], (
        f"Failure reason must mention path-traversal; got {failed_reasons[traversal_id]!r}"
    )

    # Must NOT appear in acted[].
    assert traversal_id not in [a["id"] for a in result["acted"]], (
        "../ traversal candidate must NOT be archived"
    )
    assert fleet_repo.git_status_clean(), "git status must be clean after path-traversal rejection"


def test_path_traversal_does_not_read_file(fleet_repo):
    """Traversal candidate_id must NOT cause _read_plain_yaml to read the target file.

    Seeds a valid closed bug alongside the malicious ID and verifies the closed bug
    is still acted on correctly — confirms the guard fires early (continue) without
    swallowing the valid batch.
    """
    closed_path = fleet_repo.seed_bug("2026-05-01-safe.yaml", "closed")
    safe_id = fleet_repo.repo_rel(closed_path)
    malicious_id = "/etc/hostname"

    result = _run(_handler(
        {
            "mode": "already-terminal",
            "dry_run": False,
            "candidate_ids": [safe_id, malicious_id],
        },
        repo_root=fleet_repo.common_dir,
    ))

    # Safe bug must be archived.
    acted_ids = [a["id"] for a in result["acted"]]
    assert safe_id in acted_ids, f"Safe bug must be archived; acted={acted_ids}"

    # Malicious must land in failed[].
    failed_ids = [f["id"] for f in result["failed"]]
    assert malicious_id in failed_ids, (
        f"Malicious candidate must appear in failed[]; got {result['failed']}"
    )


# ---------------------------------------------------------------------------
# (e) Idempotent replay (AC12, DR-211 D2(i))
# ---------------------------------------------------------------------------


def test_idempotent_replay(fleet_repo):
    """Re-dispatch same candidate_ids after act → skipped 'already-archived', exit_code:0.

    AC12: already-archived items (source gone) classify as skipped reason:"already-archived",
    never failed[].  exit_code:0.  Mutates nothing on the second dispatch.
    """
    bug_path = fleet_repo.seed_bug("2026-03-15-closed-bug.yaml", "closed")
    candidate_id = fleet_repo.repo_rel(bug_path)

    params = {
        "mode": "already-terminal",
        "dry_run": False,
        "candidate_ids": [candidate_id],
    }

    # First dispatch — archives the bug
    result1 = _run(_handler(params, repo_root=fleet_repo.common_dir))
    assert result1["exit_code"] == 0
    assert len(result1["acted"]) == 1, "First dispatch must archive the bug"

    # Second dispatch — same candidate_ids (idempotent replay)
    result2 = _run(_handler(params, repo_root=fleet_repo.common_dir))
    assert result2["exit_code"] == 0, (
        f"Idempotent replay must return exit_code:0; got {result2['exit_code']}"
    )
    assert result2["acted"] == [], "No items must be re-acted on idempotent replay"
    assert len(result2["skipped"]) == 1
    assert result2["skipped"][0]["id"] == candidate_id
    assert result2["skipped"][0]["reason"] == "already-archived", (
        f"Idempotent replay must classify as 'already-archived'; "
        f"got {result2['skipped'][0]['reason']!r}"
    )
    assert result2["failed"] == [], "Idempotent replay must not produce failed items"


def test_idempotent_replay_multiple(fleet_repo):
    """All already-archived candidates → exit_code:0, acted empty, all skipped."""
    bug_a = fleet_repo.seed_bug("2026-04-01-bug-a.yaml", "closed")
    bug_b = fleet_repo.seed_bug("2026-04-02-bug-b.yaml", "closed")
    id_a = fleet_repo.repo_rel(bug_a)
    id_b = fleet_repo.repo_rel(bug_b)

    params = {
        "mode": "already-terminal",
        "dry_run": False,
        "candidate_ids": [id_a, id_b],
    }

    # First dispatch
    r1 = _run(_handler(params, repo_root=fleet_repo.common_dir))
    assert r1["exit_code"] == 0
    assert len(r1["acted"]) == 2

    # Re-dispatch
    r2 = _run(_handler(params, repo_root=fleet_repo.common_dir))
    assert r2["exit_code"] == 0
    assert r2["acted"] == []
    assert len(r2["skipped"]) == 2
    skipped_reasons = {s["id"]: s["reason"] for s in r2["skipped"]}
    assert skipped_reasons[id_a] == "already-archived"
    assert skipped_reasons[id_b] == "already-archived"
    assert r2["failed"] == []
