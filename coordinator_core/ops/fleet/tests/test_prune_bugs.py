"""
coordinator_core.ops.fleet.tests.test_prune_bugs

C2 of docs/plans/2026-08-13-archive-family-coverage-restoration.md (the
easy calibration leg — 19 tests, 1 git call site at cull, RECOVERABLE per
state/audits/2026-09-10-archive-family-blob-recoverability.md). Ported
from the pre-cull blob at commit 6f0e89044efdca31026393e231eab8a2528fb4d7
(coordinator_core/ops/fleet/tests/test_prune_bugs.py) onto C1's git-free
seam (archive_git_free_seam.py) rather than the deleted `fleet_repo`
conftest fixture that made a real `git init -b main` per test ambiently
(the 2026-08-07 incident this whole plan exists to fix).

Coverage (unchanged from the pre-cull module):
  (a) dry_run:true → candidates[] with family:"bug"; no mutation.
  (b) dry_run:false → source gone, dest present, acted[] correct.
  (c) closed-only: open bugs untouched (D1 drift skip on act).
  (d) YYYY-MM derivation: filename prefix primary, created: fallback.
  (e) Idempotent replay (AC12): re-dispatch same candidate_ids → skipped
      "already-archived", exit_code:0, mutates nothing.

Git-free rewrite, per archive_git_free_seam.py's written discriminator:
every assertion above depends only on the filesystem (bytes at a path,
existence) and `_handler`'s return envelope — none of it depends on git's
own index/commit-history/blob state — so all but one test are reachable
through `patched_disposition_seam`, which patches `check_repo_root`,
`main_worktree_root`, and `archive_and_commit` on `prune_bugs` exactly as
test_archive_dest_conflict_wedge_detector.py and
test_archive_git_free_seam_smoke.py already do for this same module.
`fleet_repo.seed_bug(...)` is replaced by the local `_seed_bug` helper,
which writes a plain-YAML bug file directly under a `tmp_path`-derived
worktree — no repo needed to seed a candidate.

ONE test — test_act_git_log_contains_both_src_and_dst — is real-git by
necessity: its assertion (both src AND dst paths appear in the commit,
`--no-renames`) is about `archive_and_commit`'s OWN git mechanics, not
`prune_bugs`'s disposition decision (see archive_git_free_seam.py's
docstring: a test that patches `archive_and_commit` can never observe
what `archive_and_commit` does internally). It is kept in its own scoped
function with a `popup-intentional-last-resort` marker, mirroring
test_archive_and_commit_envelope_contract.py's governed real-git pattern,
and also carries the pre-cull module's `git status --porcelain` clean
check (the pre-cull module read that off the now-deleted `fleet_repo`
fixture) since both are facts about the SAME real commit this one test
already stands up — folding them avoids a second real-git call site.

`test_prune_bugs_registered` is REWRITTEN, not ported unmodified: `fleet.
prune_closed_bugs` was deleted as a dispatchable op 2026-08-27 (kill
ledger K-105, see prune_bugs.py's own module-level comment above
`_handler`) — the `@register_op` decorator is gone and `_handler` now
survives undecorated as shared disposition code two other suites drive
directly. The pre-cull assertion ("op IS registered") is stale against
current production; this rewrite pins the opposite fact so a future
re-decoration is caught either way.

Import guard (lesson: state/lessons/2026-07-04-universal-registry-completeness-tests-ov.yaml):
  Importing coordinator_core.ops.fleet.prune_bugs used to fire
  @register_op("fleet.prune_closed_bugs") at import time; it no longer
  does (see above). The import is still required to reach `_handler`.

Harness: asyncio.run() in sync test fns — no pytest-asyncio dependency
(archive_git_free_seam.run() wraps this).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# ---- Import guard: was the @register_op fire point; now just reaches _handler. ----
import coordinator_core.ops.fleet.prune_bugs  # noqa: F401

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.fleet import prune_bugs
from coordinator_core.ops.fleet.prune_bugs import _handler
from coordinator_core.ops.fleet.tests.archive_git_free_seam import (
    patched_disposition_seam,
    run,
)
from coordinator_core.win_portability import no_console_creationflags


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------

def _seed_bug(
    worktree: Path,
    name: str,
    status: str = "closed",
    *,
    title: str | None = None,
    created: str | None = None,
) -> Path:
    """Write a plain-YAML bug-backlog file directly under `worktree` —
    replaces the deleted `fleet_repo.seed_bug` fixture method. No repo
    needed: `patched_disposition_seam` never reaches real git for these
    tests."""
    bug_dir = worktree / "state" / "bug-backlog"
    bug_dir.mkdir(parents=True, exist_ok=True)
    path = bug_dir / name
    lines = [f"status: {status}"]
    if title is not None:
        lines.append(f"title: {title}")
    if created is not None:
        lines.append(f"created: {created}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _repo_rel(path: Path, worktree: Path) -> str:
    return str(path.relative_to(worktree)).replace("\\", "/")


def _act(worktree: Path, candidate_ids: list[str]) -> dict:
    with patched_disposition_seam(prune_bugs, worktree=worktree):
        return run(_handler(
            {"mode": "already-terminal", "dry_run": False, "candidate_ids": candidate_ids},
            repo_root=str(worktree),
        ))


def _dry_run(worktree: Path) -> dict:
    with patched_disposition_seam(prune_bugs, worktree=worktree):
        return run(_handler(
            {"mode": "already-terminal", "dry_run": True},
            repo_root=str(worktree),
        ))


# ---------------------------------------------------------------------------
# Import-guard floor assertion (rewritten — see module docstring)
# ---------------------------------------------------------------------------


def test_prune_bugs_registered():
    """fleet.prune_closed_bugs was DELETED as a dispatchable op 2026-08-27
    (K-105) — `_handler` survives undecorated. Pins the current fact
    (NOT registered) rather than the pre-cull one, so re-decoration is
    caught either way."""
    assert "fleet.prune_closed_bugs" not in _REGISTRY, (
        "fleet.prune_closed_bugs was deliberately deleted as an op "
        "(K-105, 2026-08-27) — _handler is shared disposition code now, "
        "not a registered op; re-decorating it is a change this test "
        "should surface, not silently absorb"
    )


# ---------------------------------------------------------------------------
# (a) dry_run:true — candidates[] with family:"bug"; no mutation
# ---------------------------------------------------------------------------


def test_dry_run_returns_only_closed_candidates(tmp_path: Path):
    """dry_run:true with mixed statuses → candidates[] contains only closed bugs."""
    worktree = tmp_path / "repo"
    _seed_bug(worktree, "2026-03-10-closed.yaml", "closed", title="Closed Bug")
    _seed_bug(worktree, "2026-03-11-open.yaml", "open", title="Open Bug")
    _seed_bug(worktree, "2026-03-12-wip.yaml", "in-progress", title="WIP Bug")

    result = _dry_run(worktree)

    assert result["exit_code"] == 0
    assert result["dry_run"] is True
    assert result["mode"] == "already-terminal"
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["id"] == "state/bug-backlog/2026-03-10-closed.yaml"


def test_dry_run_family_is_bug(tmp_path: Path):
    """Every candidate returned by dry_run:true must have family='bug'."""
    worktree = tmp_path / "repo"
    _seed_bug(worktree, "2026-04-01-bug-a.yaml", "closed")
    _seed_bug(worktree, "2026-04-02-bug-b.yaml", "closed")

    result = _dry_run(worktree)

    assert len(result["candidates"]) == 2
    for candidate in result["candidates"]:
        assert candidate["family"] == "bug", (
            f"Expected family='bug', got {candidate['family']!r} for {candidate['id']}"
        )


def test_dry_run_no_mutation(tmp_path: Path):
    """dry_run:true must not move any files."""
    worktree = tmp_path / "repo"
    bug_path = _seed_bug(worktree, "2026-03-10-closed.yaml", "closed")
    candidate_id = _repo_rel(bug_path, worktree)

    _dry_run(worktree)

    assert bug_path.exists(), "Source must still exist after dry_run"


def test_dry_run_empty_when_no_closed_bugs(tmp_path: Path):
    """dry_run:true with no closed bugs → candidates[] is empty, exit_code:0."""
    worktree = tmp_path / "repo"
    _seed_bug(worktree, "2026-03-10-open.yaml", "open")

    result = _dry_run(worktree)

    assert result["exit_code"] == 0
    assert result["candidates"] == []


def test_dry_run_candidate_fields(tmp_path: Path):
    """Candidate dict must contain id, title, status, family, terminal_since, note."""
    worktree = tmp_path / "repo"
    _seed_bug(worktree, "2026-04-01-my-bug.yaml", "closed", title="My Bug")

    result = _dry_run(worktree)

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
# (b) dry_run:false — source gone, dest present, acted[] correct
# ---------------------------------------------------------------------------


def test_act_archives_closed_bug(tmp_path: Path):
    """dry_run:false archives a closed bug: acted[] correct, Move built with the right dest."""
    worktree = tmp_path / "repo"
    bug_path = _seed_bug(worktree, "2026-03-15-closed-bug.yaml", "closed")
    candidate_id = _repo_rel(bug_path, worktree)

    with patched_disposition_seam(prune_bugs, worktree=worktree) as mover:
        result = run(_handler(
            {"mode": "already-terminal", "dry_run": False, "candidate_ids": [candidate_id]},
            repo_root=str(worktree),
        ))

    assert result["exit_code"] == 0
    assert result["acted"] == [{"id": candidate_id, "archived": True}]
    assert result["skipped"] == []
    assert result["failed"] == []

    # Disposition built exactly one Move to the derived archive destination —
    # the mover stub never mutates disk, so this is the disposition-decision
    # assertion (see module docstring's discriminator), not a mover-mechanics one.
    assert mover.captured is not None and len(mover.captured) == 1
    dest_rel = f"archive/bug-backlog/2026-03/{bug_path.name}"
    assert str(mover.captured[0].dst) == str(worktree / dest_rel), (
        f"Dest must be {dest_rel}; got {mover.captured[0].dst}"
    )


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_act_git_log_contains_both_src_and_dst(tmp_path: Path):
    """Real-git parity check: `archive_and_commit`'s own commit lists BOTH
    src AND dst (`--no-renames`), and `git status --porcelain` is clean
    afterward (AC4/AC10). This is the ONE git call site in this module —
    see module docstring for why it cannot move onto the git-free seam:
    the assertion is about `archive_and_commit`'s own git mechanics, not
    `prune_bugs`'s disposition decision, so patching `archive_and_commit`
    away (as every other test in this module does) would make the
    assertion vacuous. All git spawns are inlined here (not routed through
    a shared non-test helper) so the spawn ratchet can tier this one
    function rather than the whole module.

    popup-intentional-last-resort — test-only real-git spawn, mirrors
    test_archive_and_commit_envelope_contract.py's governed pattern; no
    console-window risk on the CI/dev platforms this suite runs on.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _kw = no_console_creationflags()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(root), capture_output=True, text=True, check=True, **_kw)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=str(root), capture_output=True, text=True, check=True, **_kw)
    subprocess.run(["git", "config", "user.name", "test"], cwd=str(root), capture_output=True, text=True, check=True, **_kw)

    bug_path = _seed_bug(root, "2026-05-20-my-bug.yaml", "closed")
    candidate_id = _repo_rel(bug_path, root)
    subprocess.run(["git", "add", "-A"], cwd=str(root), capture_output=True, text=True, check=True, **_kw)
    subprocess.run(["git", "commit", "-q", "-m", "seed: closed bug"], cwd=str(root), capture_output=True, text=True, check=True, **_kw)

    result = run(_handler(
        {"mode": "already-terminal", "dry_run": False, "candidate_ids": [candidate_id]},
        repo_root=str(root),
    ))

    assert result["exit_code"] == 0
    assert result["acted"] == [{"id": candidate_id, "archived": True}]

    dest_rel = f"archive/bug-backlog/2026-05/{bug_path.name}"
    log_result = subprocess.run(
        ["git", "log", "-1", "--no-renames", "--name-only", "--format="],
        cwd=str(root), capture_output=True, text=True, check=True, **_kw,
    )
    log_names = [line for line in log_result.stdout.strip().splitlines() if line.strip()]

    assert candidate_id in log_names, (
        f"src path must appear in git log (--no-renames); got {log_names}"
    )
    assert dest_rel in log_names, (
        f"dst path must appear in git log (--no-renames); got {log_names}"
    )

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(root), capture_output=True, text=True, check=True, **_kw,
    )
    assert status.stdout == "", (
        f"git status --porcelain must be empty after archival (AC4/AC10); got {status.stdout!r}"
    )


# ---------------------------------------------------------------------------
# (c) closed-only: open bugs untouched (D1 drift skip on act)
# ---------------------------------------------------------------------------


def test_act_skips_open_bugs(tmp_path: Path):
    """dry_run:false with an open bug in candidate_ids → it is skipped, not archived."""
    worktree = tmp_path / "repo"
    closed_path = _seed_bug(worktree, "2026-04-01-closed.yaml", "closed")
    open_path = _seed_bug(worktree, "2026-04-02-open.yaml", "open")

    closed_id = _repo_rel(closed_path, worktree)
    open_id = _repo_rel(open_path, worktree)

    result = _act(worktree, [closed_id, open_id])

    # exit_code:0 — failed[] is empty; open bug is skipped (D1 drift), not failed
    assert result["exit_code"] == 0
    assert len(result["acted"]) == 1
    assert result["acted"][0]["id"] == closed_id
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["id"] == open_id
    assert result["failed"] == []
    # Open bug still on disk
    assert open_path.exists(), "Open bug must not be moved"


def test_d1_drift_skip(tmp_path: Path):
    """D1 re-verify: a bug drifted open between T1 and T3 → skipped on act, not archived."""
    worktree = tmp_path / "repo"
    bug_path = _seed_bug(worktree, "2026-04-05-drifting.yaml", "closed")
    candidate_id = _repo_rel(bug_path, worktree)

    # Drift: mutate the bug back to open before the act dispatch
    bug_path.write_text("status: open\n", encoding="utf-8")

    result = _act(worktree, [candidate_id])

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
    assert bug_path.exists(), "Drifted bug must remain on disk"


# ---------------------------------------------------------------------------
# (d) YYYY-MM derivation from filename prefix (primary) / created: (fallback)
# ---------------------------------------------------------------------------


def test_yyyy_mm_from_filename_prefix(tmp_path: Path):
    """YYYY-MM derived from filename prefix YYYY-MM-DD-slug.yaml → archive/bug-backlog/YYYY-MM/."""
    worktree = tmp_path / "repo"
    bug_path = _seed_bug(worktree, "2026-05-20-mybug.yaml", "closed")
    candidate_id = _repo_rel(bug_path, worktree)

    with patched_disposition_seam(prune_bugs, worktree=worktree) as mover:
        result = run(_handler(
            {"mode": "already-terminal", "dry_run": False, "candidate_ids": [candidate_id]},
            repo_root=str(worktree),
        ))

    assert result["exit_code"] == 0
    assert mover.captured is not None and len(mover.captured) == 1
    move = mover.captured[0]
    assert str(move.dst) == str(worktree / "archive" / "bug-backlog" / "2026-05" / bug_path.name), (
        "Bug must be archived under archive/bug-backlog/2026-05/ per filename prefix"
    )


def test_yyyy_mm_fallback_from_created_frontmatter(tmp_path: Path):
    """When filename has no date prefix, YYYY-MM falls back to created: frontmatter."""
    worktree = tmp_path / "repo"
    bug_path = _seed_bug(
        worktree, "no-date-prefix-bug.yaml", "closed", created="2026-08-01"
    )
    candidate_id = _repo_rel(bug_path, worktree)

    with patched_disposition_seam(prune_bugs, worktree=worktree) as mover:
        result = run(_handler(
            {"mode": "already-terminal", "dry_run": False, "candidate_ids": [candidate_id]},
            repo_root=str(worktree),
        ))

    assert result["exit_code"] == 0
    assert mover.captured is not None and len(mover.captured) == 1
    move = mover.captured[0]
    assert str(move.dst) == str(worktree / "archive" / "bug-backlog" / "2026-08" / bug_path.name), (
        "Bug must be archived under archive/bug-backlog/2026-08/ per created: frontmatter"
    )


def test_yyyy_mm_correct_month_only(tmp_path: Path):
    """YYYY-MM extraction must take only the YYYY-MM portion (not YYYY-MM-DD)."""
    worktree = tmp_path / "repo"
    bug_path = _seed_bug(worktree, "2026-12-31-year-end.yaml", "closed")
    candidate_id = _repo_rel(bug_path, worktree)

    with patched_disposition_seam(prune_bugs, worktree=worktree) as mover:
        result = run(_handler(
            {"mode": "already-terminal", "dry_run": False, "candidate_ids": [candidate_id]},
            repo_root=str(worktree),
        ))

    assert result["exit_code"] == 0
    assert mover.captured is not None and len(mover.captured) == 1
    move = mover.captured[0]
    assert str(move.dst) == str(worktree / "archive" / "bug-backlog" / "2026-12" / bug_path.name), (
        "Bug dated 2026-12-31 must archive under 2026-12 (month only, not full date)"
    )


# ---------------------------------------------------------------------------
# SECURITY — path traversal containment (CRITICAL 2)
# ---------------------------------------------------------------------------


def test_path_traversal_absolute_rejected(tmp_path: Path):
    """Absolute-path candidate_id is rejected into failed[] before any file read or git op.

    Path('/x')/'/etc/passwd' resolves to /etc/passwd (absolute override); the
    containment guard must catch this before _read_plain_yaml reads the file
    and before the mover is ever reached.
    """
    worktree = tmp_path / "repo"
    # Seed a valid closed bug so the rejection is not due to an empty batch.
    _seed_bug(worktree, "2026-03-10-legit.yaml", "closed", title="Legit Bug")

    malicious_id = "/etc/passwd"
    with patched_disposition_seam(prune_bugs, worktree=worktree) as mover:
        result = run(_handler(
            {"mode": "already-terminal", "dry_run": False, "candidate_ids": [malicious_id]},
            repo_root=str(worktree),
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
    # Never even reaches the mover.
    assert mover.captured is None, "path-traversal must be rejected before the mover is reached"


def test_path_traversal_dotdot_rejected(tmp_path: Path):
    """../ traversal candidate_id is rejected into failed[] before any file read or git op.

    A candidate_id like 'state/bug-backlog/../../docs/plans/secret.md' resolves outside
    state/bug-backlog/ and must be caught by the containment guard.
    """
    worktree = tmp_path / "repo"
    _seed_bug(worktree, "2026-03-10-legit2.yaml", "closed", title="Legit Bug 2")

    traversal_id = "state/bug-backlog/../../docs/plans/escape.md"
    with patched_disposition_seam(prune_bugs, worktree=worktree) as mover:
        result = run(_handler(
            {"mode": "already-terminal", "dry_run": False, "candidate_ids": [traversal_id]},
            repo_root=str(worktree),
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
    assert mover.captured is None, "path-traversal must be rejected before the mover is reached"


def test_path_traversal_does_not_read_file(tmp_path: Path):
    """Traversal candidate_id must NOT cause _read_plain_yaml to read the target file.

    Seeds a valid closed bug alongside the malicious ID and verifies the closed bug
    is still acted on correctly — confirms the guard fires early (continue) without
    swallowing the valid batch.
    """
    worktree = tmp_path / "repo"
    closed_path = _seed_bug(worktree, "2026-05-01-safe.yaml", "closed")
    safe_id = _repo_rel(closed_path, worktree)
    malicious_id = "/etc/hostname"

    result = _act(worktree, [safe_id, malicious_id])

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


def test_idempotent_replay(tmp_path: Path):
    """Re-dispatch same candidate_ids after act → skipped 'already-archived', exit_code:0.

    AC12: already-archived items (source gone) classify as skipped reason:"already-archived",
    never failed[].  exit_code:0.  Mutates nothing on the second dispatch.

    The mover stub never deletes `src` (it only records the Move), so the
    second dispatch is driven by manually removing `src` between calls —
    what production's real mover does via `os.replace`, and what a real
    `_handler` sees on a genuine re-dispatch.
    """
    worktree = tmp_path / "repo"
    bug_path = _seed_bug(worktree, "2026-03-15-closed-bug.yaml", "closed")
    candidate_id = _repo_rel(bug_path, worktree)

    result1 = _act(worktree, [candidate_id])
    assert result1["exit_code"] == 0
    assert len(result1["acted"]) == 1, "First dispatch must archive the bug"

    # Simulate the real mover's effect (src removed) before the replay dispatch.
    bug_path.unlink()

    result2 = _act(worktree, [candidate_id])
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


def test_idempotent_replay_multiple(tmp_path: Path):
    """All already-archived candidates → exit_code:0, acted empty, all skipped."""
    worktree = tmp_path / "repo"
    bug_a = _seed_bug(worktree, "2026-04-01-bug-a.yaml", "closed")
    bug_b = _seed_bug(worktree, "2026-04-02-bug-b.yaml", "closed")
    id_a = _repo_rel(bug_a, worktree)
    id_b = _repo_rel(bug_b, worktree)

    r1 = _act(worktree, [id_a, id_b])
    assert r1["exit_code"] == 0
    assert len(r1["acted"]) == 2

    # Simulate the real mover's effect (both srcs removed) before the replay.
    bug_a.unlink()
    bug_b.unlink()

    r2 = _act(worktree, [id_a, id_b])
    assert r2["exit_code"] == 0
    assert r2["acted"] == []
    assert len(r2["skipped"]) == 2
    skipped_reasons = {s["id"]: s["reason"] for s in r2["skipped"]}
    assert skipped_reasons[id_a] == "already-archived"
    assert skipped_reasons[id_b] == "already-archived"
    assert r2["failed"] == []
