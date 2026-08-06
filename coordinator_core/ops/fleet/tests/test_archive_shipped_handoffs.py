"""
coordinator_core.ops.fleet.tests.test_archive_shipped_handoffs

Tests for the fleet.archive_shipped_handoffs op.

Import guard: coordinator_core.ops.fleet.archive_shipped_handoffs MUST be imported at
module load time to fire the @register_op("fleet.archive_shipped_handoffs") side-effect
and populate _REGISTRY.  Without this import the decorator has not run and any
completeness test passes vacuously over an empty registry
(lesson: state/lessons/2026-07-04-universal-registry-completeness-tests-ov.yaml).

Coverage:
  (a) dry_run:true  — candidates[] for deployment_state:shipped + resolvable shipped_in SHA
  (b) dry_run:true  — non-shipped and shipped-with-unresolvable-SHA excluded from candidates
  (c) dry_run:false — act: git-mv archives; source gone; dest at archive/handoffs/YYYY-MM/;
                      git log -1 --no-renames shows src+dst; git status clean
  (d) IDEMPOTENT-REPLAY — re-dispatch same candidate_ids after archive
                      → skipped reason "already-archived", exit_code:0, no second commit
  (e) act-time terminality drift → skipped reason "terminality-drift" (not acted, not failed)
  (f) shipped-but-unresolvable-SHA → excluded from preview; at act-time → terminality-drift skip
  (g) partial failure → exit_code:2, clean index (archive_and_commit mocked to inject failure)

Spec backlinks:
  - Plan C6: docs/plans/2026-07-06-strang-11-b8-session-init-op-absorption.md § C6
  - Plan C2: docs/plans/2026-07-06-strang-11-b8-session-init-op-absorption.md § C2
  - Wire contract (FROZEN): coordinator_core/contract/cockpit-invoke-producer-contract.md §2.1, §3.1
  - DR-211: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md D1, D2(i), D2(iv)
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test function so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.fleet  # noqa: F401 — triggers package __init__
import coordinator_core.ops.fleet.archive_shipped_handoffs  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.fleet.archive_shipped_handoffs import _handler

# ---------------------------------------------------------------------------
# Positive floor assertion: the handler must be registered before any test runs.
# import-to-register precedes completeness assertion — otherwise the completeness
# check is vacuously green on an empty registry (lesson 2026-07-04).
# ---------------------------------------------------------------------------
_OP_NAME = "fleet.archive_shipped_handoffs"
_fleet_registered_ops = [k for k in _REGISTRY if k.startswith("fleet.")]
assert len(_fleet_registered_ops) >= 1, (
    "import guard failed: no fleet.* ops in _REGISTRY — "
    "check coordinator_core.ops.fleet.archive_shipped_handoffs import"
)
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY; "
    "coordinator_core.ops.fleet.archive_shipped_handoffs @register_op did not fire"
)


# ---------------------------------------------------------------------------
# (a-0) Named import-guard test — mirrors test_archive_actioned_memos.py pattern
# ---------------------------------------------------------------------------

def test_op_registered():
    """fleet.archive_shipped_handoffs must be in the registry after the import guard fires.

    The module-level assertions already cover this; this function makes the contract
    explicit and produces a named failure in pytest output rather than a collection error.
    Mirrors test_archive_actioned_memos.py:183-195.
    """
    # Review: code-reviewer — mirrors test_archive_actioned_memos.py:test_op_registered()
    # for consistent named-test coverage; module-level assert produces collection errors
    # whereas a named function produces a clean pytest failure row.
    fleet_ops = [k for k in _REGISTRY if k.startswith("fleet.")]
    assert len(fleet_ops) >= 1, (
        f"floor: at least one fleet.* op must be registered; got {sorted(fleet_ops)}"
    )
    assert _OP_NAME in _REGISTRY, (
        f"{_OP_NAME!r} must be registered; present ops: {sorted(fleet_ops)}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _make_params(dry_run: bool, candidate_ids=None) -> dict:
    """Build a minimal valid fleet.archive_shipped_handoffs params dict."""
    p: dict = {"mode": "already-terminal", "dry_run": dry_run}
    if candidate_ids is not None:
        p["candidate_ids"] = candidate_ids
    return p


def _head_sha(fleet_repo) -> str:
    """Return the current HEAD commit SHA from fleet_repo — guaranteed git-reachable."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


def _seed_shipped_handoff(fleet_repo, name: str, sha: str) -> str:
    """Write and commit a handoff with deployment_state:shipped + the given shipped_in SHA.

    Writes the file directly (bypassing conftest.seed_handoff) so that multi-field
    frontmatter is not broken by textwrap.dedent's indentation-stripping on a newline
    inside an extra_frontmatter substitution.

    Returns the repo-relative candidate_id path for use in params.
    """
    path = fleet_repo.root / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        f"title: \"Test Shipped Handoff\"\n"
        "status: open\n"
        "deployment_state: shipped\n"
        f"shipped_in: {sha}\n"
        "created: 2026-01-01\n"
        "---\n"
        "\n"
        "# Handoff\n"
        "\n"
        "Body.\n"
    )
    path.write_text(content, encoding="utf-8")
    fleet_repo._git("add", str(path))
    fleet_repo._git("commit", "-m", f"add shipped handoff {name}")
    return f"state/handoffs/{name}"


def _git_log_names_no_renames(fleet_repo, n: int = 1):
    """Return file paths touched in the last n commits with rename-detection disabled.

    Uses --no-renames so that a git-mv rename shows BOTH source (deleted) and
    destination (added) as separate path entries — verifying the scoped-pathspec
    contract (DR-211 D3) where both src AND dst must appear in the commit.
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


# Patch target for archive_and_commit — bound in archive_shipped_handoffs at import time.
_ARCHIVE_AND_COMMIT_PATCH = (
    "coordinator_core.ops.fleet.archive_shipped_handoffs.archive_and_commit"
)


# ---------------------------------------------------------------------------
# (a) dry_run:true — candidates[] for deployment_state:shipped + resolvable SHA
# ---------------------------------------------------------------------------

def test_dry_run_returns_shipped_candidate_with_note(fleet_repo):
    """dry_run:true enumerates handoffs with deployment_state:shipped + reachable SHA."""
    sha = _head_sha(fleet_repo)
    cid = _seed_shipped_handoff(fleet_repo, "2026-07-01-shipped-alpha.md", sha)
    # Non-shipped handoff — must NOT appear in candidates.
    fleet_repo.seed_handoff("2026-07-01-active.md", "active")

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    assert result["exit_code"] == 0
    assert result["dry_run"] is True
    assert isinstance(result["candidates"], list)

    ids = [c["id"] for c in result["candidates"]]
    assert cid in ids, "shipped+SHA-reachable handoff must appear in dry_run candidates"
    assert "state/handoffs/2026-07-01-active.md" not in ids, (
        "non-shipped handoff must be excluded from candidates"
    )

    candidate = next(c for c in result["candidates"] if "shipped-alpha" in c["id"])
    assert candidate["family"] == "handoff"
    assert candidate["status"] == "shipped"
    assert "note" in candidate
    assert "shipped" in candidate["note"]
    assert sha in candidate["note"], "note must contain the shipped_in SHA"
    # acted/skipped/failed are empty in dry_run mode.
    assert result["acted"] == []
    assert result["skipped"] == []
    assert result["failed"] == []


def test_dry_run_multiple_shipped_all_enumerated(fleet_repo):
    """dry_run:true enumerates all shipped+SHA-reachable handoffs."""
    sha = _head_sha(fleet_repo)
    cid_a = _seed_shipped_handoff(fleet_repo, "2026-07-02-sh-a.md", sha)
    cid_b = _seed_shipped_handoff(fleet_repo, "2026-07-02-sh-b.md", sha)

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid_a in ids
    assert cid_b in ids


# ---------------------------------------------------------------------------
# (b) dry_run:true — non-shipped and unresolvable-SHA excluded
# ---------------------------------------------------------------------------

def _seed_non_shipped_handoff(fleet_repo, name: str, deployment_state: str = "in_flight") -> str:
    """Write and commit a handoff with a non-shipped deployment_state (or no deployment_state).

    Returns the repo-relative candidate_id path.
    """
    path = fleet_repo.root / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        f"title: \"Test Handoff\"\n"
        "status: open\n"
        f"deployment_state: {deployment_state}\n"
        "created: 2026-01-01\n"
        "---\n"
        "\n"
        "# Handoff\n"
        "\n"
        "Body.\n"
    )
    path.write_text(content, encoding="utf-8")
    fleet_repo._git("add", str(path))
    fleet_repo._git("commit", "-m", f"add non-shipped handoff {name}")
    return f"state/handoffs/{name}"


def test_dry_run_excludes_non_shipped_handoff(fleet_repo):
    """A handoff without deployment_state:shipped is not a candidate."""
    _seed_non_shipped_handoff(fleet_repo, "2026-07-01-in-flight.md", "in_flight")
    fleet_repo.seed_handoff("2026-07-01-bare-active.md", "active")

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert "state/handoffs/2026-07-01-in-flight.md" not in ids, (
        "non-shipped deployment_state must be excluded"
    )
    assert "state/handoffs/2026-07-01-bare-active.md" not in ids, (
        "handoff with no deployment_state:shipped must be excluded"
    )


def _seed_shipped_handoff_bogus_sha(fleet_repo, name: str, bogus_sha: str) -> str:
    """Write and commit a handoff with deployment_state:shipped and an unresolvable shipped_in SHA."""
    path = fleet_repo.root / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        f"title: \"Test Shipped Handoff (bogus SHA)\"\n"
        "status: open\n"
        "deployment_state: shipped\n"
        f"shipped_in: {bogus_sha}\n"
        "created: 2026-01-01\n"
        "---\n"
        "\n"
        "# Handoff\n"
        "\n"
        "Body.\n"
    )
    path.write_text(content, encoding="utf-8")
    fleet_repo._git("add", str(path))
    fleet_repo._git("commit", "-m", f"add shipped handoff with bogus SHA {name}")
    return f"state/handoffs/{name}"


def _seed_shipped_handoff_no_sha(fleet_repo, name: str) -> str:
    """Write and commit a handoff with deployment_state:shipped but no shipped_in field."""
    path = fleet_repo.root / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        f"title: \"Test Shipped Handoff (no SHA)\"\n"
        "status: open\n"
        "deployment_state: shipped\n"
        "created: 2026-01-01\n"
        "---\n"
        "\n"
        "# Handoff\n"
        "\n"
        "Body.\n"
    )
    path.write_text(content, encoding="utf-8")
    fleet_repo._git("add", str(path))
    fleet_repo._git("commit", "-m", f"add shipped handoff with no shipped_in {name}")
    return f"state/handoffs/{name}"


def test_dry_run_excludes_shipped_with_unresolvable_sha(fleet_repo):
    """A shipped handoff whose shipped_in SHA is not git-reachable is excluded from candidates.

    This is the 'liveness-guard' equivalent for this op: a shipped_in SHA that does not
    exist in the repo means the workstream never actually landed (orphan/bogus record).
    The SHA-gate closes the false-archive gap (negative-spec: unresolvable SHA → skipped).
    """
    bogus_sha = "deadbeef" * 5  # 40 hex chars but not a real object
    cid = _seed_shipped_handoff_bogus_sha(fleet_repo, "2026-07-01-sh-bogus-sha.md", bogus_sha)

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid not in ids, (
        "shipped handoff with unresolvable shipped_in SHA must be excluded from candidates "
        "(SHA-gate closes orphan-record / bogus-SHA gap)"
    )


def test_dry_run_excludes_shipped_with_absent_sha(fleet_repo):
    """A shipped handoff with no shipped_in field at all is excluded from candidates."""
    cid = _seed_shipped_handoff_no_sha(fleet_repo, "2026-07-01-sh-no-sha.md")

    result = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))

    ids = [c["id"] for c in result["candidates"]]
    assert cid not in ids, "shipped handoff with absent shipped_in must be excluded"


# ---------------------------------------------------------------------------
# (c) dry_run:false — act: git-mv; source gone; dest at YYYY-MM/; log + index clean
# ---------------------------------------------------------------------------

def test_act_archives_shipped_handoff(fleet_repo):
    """dry_run:false: git-mv moves handoff to archive/handoffs/YYYY-MM/; source gone; index clean."""
    sha = _head_sha(fleet_repo)
    cid = _seed_shipped_handoff(fleet_repo, "2026-07-02-sh-target.md", sha)

    result = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid]),
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"unexpected exit_code; result={result!r}"
    assert result["acted"] == [{"id": cid, "archived": True}]
    assert result["skipped"] == []
    assert result["failed"] == []

    # Source path must be gone from disk.
    assert not fleet_repo.path_exists(cid), "source file must be removed by git mv"

    # Destination: archive/handoffs/2026-07/<filename>
    expected_dest = "archive/handoffs/2026-07/2026-07-02-sh-target.md"
    assert fleet_repo.path_exists(expected_dest), (
        f"archived file must exist at {expected_dest!r}"
    )

    # git log -1 (--no-renames) must show BOTH src AND dst paths (scoped-pathspec AC4 / DR-211 D3).
    log_names = _git_log_names_no_renames(fleet_repo, 1)
    assert cid in log_names, (
        f"src path {cid!r} must appear in last commit; got {log_names!r}"
    )
    assert expected_dest in log_names, (
        f"dst path {expected_dest!r} must appear in last commit; got {log_names!r}"
    )

    # git status --porcelain must be empty (no dirty index, no unstaged changes).
    assert fleet_repo.git_status_clean(), "working tree must be clean after archive"


def test_act_archives_multiple_shipped_handoffs(fleet_repo):
    """dry_run:false with multiple candidate_ids: all archived in one commit."""
    sha = _head_sha(fleet_repo)
    cid_a = _seed_shipped_handoff(fleet_repo, "2026-07-02-sh-multi-a.md", sha)
    cid_b = _seed_shipped_handoff(fleet_repo, "2026-07-02-sh-multi-b.md", sha)

    result = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid_a, cid_b]),
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    acted_ids = {a["id"] for a in result["acted"]}
    assert cid_a in acted_ids
    assert cid_b in acted_ids
    assert result["failed"] == []

    assert not fleet_repo.path_exists(cid_a)
    assert not fleet_repo.path_exists(cid_b)
    assert fleet_repo.git_status_clean()

    # Review: code-reviewer — DR-211 D3 both-sides-pathspec check for the multi-file batch.
    # A handler that only pathspec'd the dst (or only the last file's paths) would pass
    # path_exists/git_status_clean while violating D3 for the first item. Mirror the
    # single-file test's --no-renames log check for both cid_a/cid_b src AND dst.
    log_names = _git_log_names_no_renames(fleet_repo, 1)
    expected_dest_a = "archive/handoffs/2026-07/2026-07-02-sh-multi-a.md"
    expected_dest_b = "archive/handoffs/2026-07/2026-07-02-sh-multi-b.md"
    assert cid_a in log_names, (
        f"src path {cid_a!r} must appear in last commit (--no-renames); got {log_names!r}"
    )
    assert cid_b in log_names, (
        f"src path {cid_b!r} must appear in last commit (--no-renames); got {log_names!r}"
    )
    assert expected_dest_a in log_names, (
        f"dst path {expected_dest_a!r} must appear in last commit; got {log_names!r}"
    )
    assert expected_dest_b in log_names, (
        f"dst path {expected_dest_b!r} must appear in last commit; got {log_names!r}"
    )


# ---------------------------------------------------------------------------
# (d) IDEMPOTENT-REPLAY — re-dispatch → already-archived skipped, exit_code:0
# ---------------------------------------------------------------------------

def test_idempotent_replay_skips_already_archived(fleet_repo):
    """Re-dispatch same candidate_ids after archive → skipped 'already-archived', exit_code:0."""
    sha = _head_sha(fleet_repo)
    cid = _seed_shipped_handoff(fleet_repo, "2026-07-04-sh-idem.md", sha)

    # --- First dispatch: archives the handoff ---
    first = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid]),
        repo_root=fleet_repo.common_dir,
    ))
    assert first["exit_code"] == 0, f"first dispatch must succeed; got {first!r}"
    assert first["acted"] == [{"id": cid, "archived": True}]
    assert not fleet_repo.path_exists(cid), "source must be gone after first dispatch"
    assert fleet_repo.git_status_clean()

    # --- Second dispatch with the same candidate_ids: source is gone ---
    second = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid]),
        repo_root=fleet_repo.common_dir,
    ))

    # exit_code:0 — already-archived is a clean skip, not a failure.
    assert second["exit_code"] == 0, (
        f"idempotent replay must return exit_code:0; got {second!r}"
    )
    assert second["acted"] == [], "nothing new must be acted on replay"
    assert second["failed"] == [], "already-archived must never appear in failed[]"

    skip_map = {s["id"]: s["reason"] for s in second["skipped"]}
    assert cid in skip_map, (
        f"{cid!r} must appear in skipped[] on replay; got {second['skipped']!r}"
    )
    assert skip_map[cid] == "already-archived", (
        f"skip reason must be 'already-archived'; got {skip_map[cid]!r}"
    )

    # Index must remain clean after idempotent replay.
    assert fleet_repo.git_status_clean(), "index must be clean after idempotent replay"


def test_idempotent_replay_mixed_fresh_and_archived(fleet_repo):
    """Mix: one already-archived + one fresh → fresh acted, archived skipped."""
    sha = _head_sha(fleet_repo)
    cid_archived = _seed_shipped_handoff(fleet_repo, "2026-07-04-sh-archived.md", sha)
    cid_fresh = _seed_shipped_handoff(fleet_repo, "2026-07-04-sh-fresh.md", sha)

    # Archive only cid_archived first.
    first = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid_archived]),
        repo_root=fleet_repo.common_dir,
    ))
    assert first["exit_code"] == 0
    assert not fleet_repo.path_exists(cid_archived)

    # Re-dispatch with BOTH: cid_archived (already gone) + cid_fresh (still live).
    second = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid_archived, cid_fresh]),
        repo_root=fleet_repo.common_dir,
    ))

    assert second["exit_code"] == 0
    acted_ids = {a["id"] for a in second["acted"]}
    assert cid_fresh in acted_ids, "fresh candidate must be acted on second dispatch"
    skip_map = {s["id"]: s["reason"] for s in second["skipped"]}
    assert cid_archived in skip_map
    assert skip_map[cid_archived] == "already-archived"
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (e) act-time terminality drift → skipped "terminality-drift"
# ---------------------------------------------------------------------------

def test_act_time_terminality_drift_skipped(fleet_repo):
    """Handoff was terminal at preview but deployment_state changed before act → skipped.

    Simulates D2(iv) act-time terminality re-verify: a handoff that passes the preview
    predicate (deployment_state:shipped + resolvable SHA) but then has its deployment_state
    mutated to something else before the act phase fires should be skipped with a reason
    containing 'terminality-drift'.
    """
    sha = _head_sha(fleet_repo)
    cid = _seed_shipped_handoff(fleet_repo, "2026-07-05-sh-drift.md", sha)

    # --- PREVIEW: handoff is terminal, appears in candidates ---
    preview = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))
    assert cid in [c["id"] for c in preview["candidates"]], (
        "shipped+SHA-reachable handoff must appear in preview"
    )

    # --- Simulate drift: overwrite deployment_state with something non-shipped ---
    path = fleet_repo.root / "state" / "handoffs" / "2026-07-05-sh-drift.md"
    text = path.read_text(encoding="utf-8")
    text = text.replace("deployment_state: shipped", "deployment_state: in_flight")
    path.write_text(text, encoding="utf-8")
    fleet_repo._git("add", str(path))
    fleet_repo._git("commit", "-m", "drift: deployment_state → in_flight")

    # --- ACT with candidate from preview — should be skipped (terminality-drift) ---
    result = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid]),
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0, (
        f"terminality-drift skip must not set exit_code:1 or 2; got {result!r}"
    )
    assert result["acted"] == [], "drifted handoff must NOT be moved"

    skip_map = {s["id"]: s["reason"] for s in result["skipped"]}
    assert cid in skip_map, f"{cid!r} must be in skipped[]; got {result['skipped']!r}"
    assert "terminality-drift" in skip_map[cid], (
        f"skip reason must contain 'terminality-drift'; got {skip_map[cid]!r}"
    )

    # Source file must still exist — nothing was moved.
    assert fleet_repo.path_exists(cid), "drifted handoff source must not have been moved"
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (f) shipped-but-unresolvable-SHA at act-time → terminality-drift skip
# ---------------------------------------------------------------------------

def test_act_shipped_unresolvable_sha_skipped(fleet_repo):
    """Act-time: shipped handoff with unresolvable SHA is terminality-drift-skipped.

    This is the liveness-guard skip for this op: the SHA-gate re-verifies at T3
    (act time) that the shipped_in SHA is still reachable.  An unresolvable SHA at
    act time classifies as a terminality-drift skip (not a failure).
    """
    bogus_sha = "cafebabe" * 5  # 40 hex chars — never a real object in the test repo
    cid = _seed_shipped_handoff_bogus_sha(fleet_repo, "2026-07-05-sh-bogus-act.md", bogus_sha)

    # Pass cid directly to the act handler (bypassing preview — valid because the
    # op contract accepts any candidate_ids list; the re-verify at T3 is the safety net).
    result = _run(_handler(
        _make_params(dry_run=False, candidate_ids=[cid]),
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0, (
        f"unresolvable-SHA skip must not set exit_code:2; got {result!r}"
    )
    assert result["acted"] == [], "unresolvable-SHA handoff must NOT be moved"

    skip_map = {s["id"]: s["reason"] for s in result["skipped"]}
    assert cid in skip_map, f"{cid!r} must be in skipped[]; got {result['skipped']!r}"
    # The skip reason is "terminality-drift: ..." wrapping the SHA-gate failure detail.
    assert "terminality-drift" in skip_map[cid], (
        f"skip reason must contain 'terminality-drift'; got {skip_map[cid]!r}"
    )
    assert any(kw in skip_map[cid] for kw in ("SHA", "sha", "reachable", bogus_sha[:8])), (
        f"skip reason must reference the SHA issue; got {skip_map[cid]!r}"
    )

    assert fleet_repo.path_exists(cid), "unresolvable-SHA handoff source must not be moved"
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (g) partial failure → exit_code:2, clean index (archive_and_commit mocked)
# ---------------------------------------------------------------------------

def test_partial_failure_exit_code_2_clean_index(fleet_repo):
    """When archive_and_commit reports a per-item failure, exit_code:2 and index stays clean.

    archive_and_commit is mocked to inject a single failed item, simulating a git-mv
    failure mid-batch.  The op must propagate the failure into failed[] and return
    exit_code:2 (DETERMINATE-PARTIAL, contract §3.2).  Because the real git operations
    are mocked out, the working tree / index remains untouched — verifying that the
    handler does not leave a dirty index on partial failure.
    """
    sha = _head_sha(fleet_repo)
    cid = _seed_shipped_handoff(fleet_repo, "2026-07-05-sh-partial-fail.md", sha)

    # Mock archive_and_commit to return (acted=[], failed=[...]) — simulates one
    # git-mv failure for the single candidate.
    mock_failure = [{"id": cid, "reason": "mock-git-mv-failure"}]

    async def _mock_archive_and_commit(**kwargs):
        return [], mock_failure  # (acted, failed)

    with patch(_ARCHIVE_AND_COMMIT_PATCH, new=_mock_archive_and_commit):
        result = _run(_handler(
            _make_params(dry_run=False, candidate_ids=[cid]),
            repo_root=fleet_repo.common_dir,
        ))

    # exit_code:2 = DETERMINATE-PARTIAL (at least one item in failed[]).
    assert result["exit_code"] == 2, (
        f"partial failure must produce exit_code:2; got {result!r}"
    )
    assert result["acted"] == []
    assert result["failed"] == mock_failure, (
        f"failed[] must contain the injected failure; got {result['failed']!r}"
    )

    # Index must be clean — the mock didn't run any real git operations.
    assert fleet_repo.git_status_clean(), "index must be clean after partial-failure mock"


# ---------------------------------------------------------------------------
# Handoff-scan-failure (2026-07-22 follow-up): _scan_shipped's own
# collect_live_handoff_paths call degrades safe on an unreadable state/handoffs/
# rather than crashing — same posture as archive_handoffs._handle_preview_handoffs.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_state_handoffs_dry_run_returns_zero_candidates(fleet_repo, caplog):
    """Unreadable state/handoffs/ → dry_run:true preview returns zero candidates
    and logs a WARNING, instead of crashing.

    Seeds a genuinely-shipped, SHA-verified handoff (would otherwise surface as
    a candidate — proven archivable shape, not a vacuously-empty repo) then
    chmods state/handoffs/ unreadable before invoking the handler.
    """
    sha = _head_sha(fleet_repo)
    cid = _seed_shipped_handoff(fleet_repo, "2026-07-22-sh-scan-gap.md", sha)

    state_handoffs_dir = fleet_repo.root / "state" / "handoffs"
    original_mode = state_handoffs_dir.stat().st_mode
    os.chmod(state_handoffs_dir, 0o000)
    try:
        with caplog.at_level(logging.WARNING):
            result = _run(_handler(
                _make_params(dry_run=True), repo_root=fleet_repo.common_dir,
            ))
    finally:
        os.chmod(state_handoffs_dir, original_mode)

    assert result["candidates"] == [], (
        f"expected zero candidates on an unreadable state/handoffs/; "
        f"got {result['candidates']!r}"
    )
    assert any(
        "_scan_shipped" in r.message and "cannot scan" in r.message
        for r in caplog.records
    ), (
        "expected a logged WARNING naming the scan failure; "
        f"none found in: {[r.message for r in caplog.records]}"
    )

    # Signal must fire, not vacuously pass — the seeded handoff genuinely exists
    # and would otherwise be a candidate (cid computed above is unused in the
    # assertion by design: it never gets the chance to become one).
    assert cid


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_state_handoffs_act_skips_all_candidates(fleet_repo):
    """Race window: a candidate_id known from an earlier (clean-scan) preview,
    but state/handoffs/ becomes unreadable before the act call — _handle_act's
    own live_handoffs map build must degrade safe, not archive on partial
    knowledge.

    live_handoffs feeds the already-archived/still-live safety check every
    candidate_id is tested against; if the scan silently returned {} instead
    of raising, every candidate_id would look "already-archived" and be
    skipped for the WRONG reason — or worse, under a different implementation,
    treated as safe-to-move. Assert the SPECIFIC reason token so a regression
    that silently swallows the OSError (e.g. reverting to a bare pass) can't
    hide behind a coincidentally-empty skipped[] or a misleading reason.
    """
    sha = _head_sha(fleet_repo)
    cid = _seed_shipped_handoff(fleet_repo, "2026-07-22-sh-act-scan-gap.md", sha)

    # Preview succeeds while the dir is still readable — proves this candidate
    # is genuinely archivable and not skipped for some unrelated reason.
    preview = _run(_handler(_make_params(dry_run=True), repo_root=fleet_repo.common_dir))
    assert cid in [c["id"] for c in preview["candidates"]], (
        f"control check failed — {cid!r} must be a genuine candidate before "
        f"the scan gap is introduced; got candidates={preview['candidates']!r}"
    )

    state_handoffs_dir = fleet_repo.root / "state" / "handoffs"
    original_mode = state_handoffs_dir.stat().st_mode
    os.chmod(state_handoffs_dir, 0o000)
    try:
        result = _run(_handler(
            _make_params(dry_run=False, candidate_ids=[cid]),
            repo_root=fleet_repo.common_dir,
        ))
    finally:
        os.chmod(state_handoffs_dir, original_mode)

    assert result["acted"] == [], (
        f"nothing may be archived while the live-handoffs scan is failing; "
        f"got acted={result['acted']!r}"
    )
    skip_map = {s["id"]: s["reason"] for s in result["skipped"]}
    assert skip_map.get(cid) == "handoff-scan-failed: cannot verify liveness", (
        f"expected the specific scan-failure reason token; got skipped={result['skipped']!r}"
    )

    # Source file must remain in place — never moved on an incomplete scan.
    assert (fleet_repo.root / cid).exists(), (
        "handoff must remain in state/handoffs/ — never moved while the scan is failing"
    )
