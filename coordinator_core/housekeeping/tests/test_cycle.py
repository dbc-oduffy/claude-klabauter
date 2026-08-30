"""
Tests for coordinator_core.housekeeping.cycle — Step E (move + ONE commit
through the existing `archive_and_commit` seam) and the assembled `run(...)`
entry point (plan chunk C6c).

Covers: `archive_terminal_batch` passes a prebuilt `Move` list through to
the seam unchanged and never calls into the seam for an empty batch; the
handoff `Move` list itself is built by `run(...)` since C2 widened the batch
function (the actioned-memo class gets an occasion); the seam's (acted,
failed) split is passed through unmodified (no second-guessing, no leave-and-log); and an
end-to-end `run(...)` cycle on a small real git repo — a gate genuinely
clears, a terminal record is archived and landed in ONE commit, a
claim-held terminal record is retained, and a gate-clear CONFLICT is
reported rather than silently dropped.

Spec backlink: docs/plans/2026-08-29-the-housekeeping-cycle-stops-committing.md
  § C6c; docs/research/2026-08-29-housekeeping-v2-target-shape.md § 2 step E.

Negative-spec: this file does not re-test `archive_and_commit`'s own
mechanics (rollback-on-failure, CAS, mode preservation — `ops/fleet/
tests/`'s job), C3/C4's scan/index mechanics, or C5/C6/C6b's own resolver/
gate/terminal-set logic — only what THIS module does with them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from coordinator_core.claim_state import handoff_claim_dir
from coordinator_core.housekeeping import cycle
from coordinator_core.housekeeping.gate_clear import CONFLICT
from coordinator_core.housekeeping.terminal import TerminalEntry
from coordinator_core.ops.fleet._common import Move
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "cycle-test@example.invalid")
    _git(root, "config", "user.name", "cycle-test")
    return root


def _write_frontmatter(path: Path, fields: Dict[str, Any], body: str = "Fixture body.\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            continue
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# archive_terminal_batch — Step E in isolation
# ---------------------------------------------------------------------------


def test_archive_terminal_batch_empty_never_calls_the_seam(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("archive_and_commit must not be called for an empty batch")

    monkeypatch.setattr(cycle, "archive_and_commit", _boom)
    acted, failed = cycle.archive_terminal_batch(tmp_path, [], "subject")
    assert acted == []
    assert failed == []


def _handoff_move(worktree_root, path):
    """Build a handoff Move the way `run(...)` does — the construction
    `archive_terminal_batch` owned before C2 widened it to take a prebuilt
    list."""
    return Move(
        src=path,
        dst=cycle.handoff_archive_dest(worktree_root, path),
        candidate_id=str(path.relative_to(worktree_root)),
        force=False,
        restage_src=False,
    )


def test_archive_terminal_batch_passes_prebuilt_moves_through_to_the_seam(tmp_path, monkeypatch):
    """C2 widened this function from `List[TerminalEntry]` to a prebuilt
    `List[Move]` so both families land in one commit; it now hands the list
    to the seam unchanged rather than building it."""
    worktree_root = tmp_path
    p1 = worktree_root / "state" / "handoffs" / "2026-01-01_00001_a.md"
    p2 = worktree_root / "state" / "handoffs" / "2026-01-02_00002_b.md"
    moves = [_handoff_move(worktree_root, p1), _handoff_move(worktree_root, p2)]

    captured = {}

    async def _fake_archive_and_commit(root, seam_moves, subject):
        captured["root"] = root
        captured["moves"] = seam_moves
        captured["subject"] = subject
        return (
            [{"id": m.candidate_id, "archived": True} for m in seam_moves],
            [],
        )

    monkeypatch.setattr(cycle, "archive_and_commit", _fake_archive_and_commit)

    acted, failed = cycle.archive_terminal_batch(worktree_root, moves, "the subject")

    assert captured["root"] == worktree_root
    assert captured["subject"] == "the subject"
    assert captured["moves"] == moves

    assert failed == []
    assert {item["id"] for item in acted} == {
        str(p1.relative_to(worktree_root)),
        str(p2.relative_to(worktree_root)),
    }


def test_archive_terminal_batch_passes_through_failed_without_retry(tmp_path, monkeypatch):
    """The seam's own (acted, failed) split is reported as-is — this module
    never retries or patches a failed item (D5: complete-or-restore lives
    INSIDE archive_and_commit, never re-attempted here)."""
    p1 = tmp_path / "state" / "handoffs" / "2026-01-01_00001_a.md"
    moves = [_handoff_move(tmp_path, p1)]

    async def _fake_archive_and_commit(root, seam_moves, subject):
        return ([], [{"id": seam_moves[0].candidate_id, "reason": "dst-exists"}])

    monkeypatch.setattr(cycle, "archive_and_commit", _fake_archive_and_commit)

    acted, failed = cycle.archive_terminal_batch(tmp_path, moves, "subject")
    assert acted == []
    assert failed == [{"id": str(p1.relative_to(tmp_path)), "reason": "dst-exists"}]


# ---------------------------------------------------------------------------
# run(...) — end-to-end on a small real git repo
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo(tmp_path) -> Path:
    root = _init_repo(tmp_path / "repo")

    live_dir = root / "state" / "handoffs"
    archive_dir = root / "archive" / "handoffs"

    # R1: awaiting_gate, blocked by B1 (an already-archived, terminal record)
    # -- genuinely clears this cycle.
    _write_frontmatter(
        live_dir / "2026-08-01_00001_clearing.md",
        {
            "handoff_id": "hnd-r1",
            "stub_id": "sat-r1",
            "deployment_state": "awaiting_gate",
            "blocked_by": ["sat-b1"],
        },
    )
    # R2: awaiting_gate, blocked by an id that resolves to nothing -- never clears.
    _write_frontmatter(
        live_dir / "2026-08-02_00002_stuck.md",
        {
            "handoff_id": "hnd-r2",
            "deployment_state": "awaiting_gate",
            "blocked_by": ["hnd-does-not-exist"],
        },
    )
    # T1: already terminal before the cycle runs -- must be archived this cycle.
    _write_frontmatter(
        live_dir / "2026-06-01_00003_terminal.md",
        {"handoff_id": "hnd-t1", "deployment_state": "closed"},
    )
    # T2: terminal but held by a live claim -- must be retained.
    _write_frontmatter(
        live_dir / "2026-06-02_00004_held.md",
        {"handoff_id": "hnd-t2", "deployment_state": "shipped"},
    )
    # A plain non-terminal record -- must be left alone entirely.
    _write_frontmatter(
        live_dir / "2026-08-03_00005_plain.md",
        {"handoff_id": "hnd-r3", "deployment_state": "ready_to_fire"},
    )
    # B1: the archived record R1's gate blocker resolves to.
    _write_frontmatter(
        archive_dir / "2026-07" / "2026-07-01_00000_blocker.md",
        {"handoff_id": "hnd-b1", "stub_id": "sat-b1", "deployment_state": "shipped"},
    )

    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture baseline")
    return root


def _held_claim_dir(repo: Path) -> Path:
    from coordinator_core.lifecycle import git_common_dir

    common_dir = git_common_dir(repo)
    held_path = repo / "state" / "handoffs" / "2026-06-02_00004_held.md"
    return handoff_claim_dir(common_dir, held_path)


def test_run_clears_a_gate_archives_terminal_records_and_retains_held_claims(
    repo, monkeypatch
):
    held_claim_dir = _held_claim_dir(repo)

    def _fake_claim_holder_live(claim_path: str) -> bool:
        return Path(claim_path) == held_claim_dir

    monkeypatch.setattr(cycle, "cs_claim_holder_live", _fake_claim_holder_live)

    result = cycle.run(str(repo), cap=10)

    t1_id = str(
        (repo / "state" / "handoffs" / "2026-06-01_00003_terminal.md").relative_to(repo)
    )

    assert result["closed"] == 1
    assert result["conflicts"] == []
    assert result["failed"] == []
    assert set(result["archived"]) == {t1_id}
    assert result["live_read_count"] == 5

    # R1 cleared in place: ready_to_fire, gate_blocker_id gone.
    r1_text = (repo / "state" / "handoffs" / "2026-08-01_00001_clearing.md").read_text(
        encoding="utf-8"
    )
    assert "deployment_state: ready_to_fire" in r1_text
    assert "blocked_by" not in r1_text

    # R2 never cleared -- still awaiting_gate.
    r2_text = (repo / "state" / "handoffs" / "2026-08-02_00002_stuck.md").read_text(
        encoding="utf-8"
    )
    assert "deployment_state: awaiting_gate" in r2_text

    # T1 archived: gone from live, present at its archive destination.
    assert not (repo / "state" / "handoffs" / "2026-06-01_00003_terminal.md").exists()
    assert (repo / "archive" / "handoffs" / "2026-06" / "2026-06-01_00003_terminal.md").exists()

    # T2 retained (live claim holder) -- still live, never moved.
    assert (repo / "state" / "handoffs" / "2026-06-02_00004_held.md").exists()

    # Landed as exactly ONE new commit on top of the fixture baseline.
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    ).stdout.strip().splitlines()
    assert len(log) == 2, f"expected fixture baseline + one archival commit, got: {log!r}"
    assert "housekeeping" in log[0]


def test_run_reports_gate_clear_conflict_without_losing_the_cycle(repo, monkeypatch):
    """A CONFLICT from apply_gate_clear is surfaced in `conflicts`, never
    silently dropped or allowed to abort the rest of the cycle."""

    def _fake_apply_gate_clear(path, repo_root, **kwargs):
        from coordinator_core.housekeeping.gate_clear import ApplyResult

        return ApplyResult(status=CONFLICT)

    monkeypatch.setattr(cycle, "apply_gate_clear", _fake_apply_gate_clear)
    monkeypatch.setattr(cycle, "cs_claim_holder_live", lambda claim_path: False)

    t1_id = str(
        (repo / "state" / "handoffs" / "2026-06-01_00003_terminal.md").relative_to(repo)
    )
    t2_id = str(
        (repo / "state" / "handoffs" / "2026-06-02_00004_held.md").relative_to(repo)
    )

    result = cycle.run(str(repo), cap=10)

    assert result["closed"] == 0
    assert len(result["conflicts"]) == 1
    assert Path(result["conflicts"][0]).name == "2026-08-01_00001_clearing.md"
    # The rest of the cycle still ran: both pre-terminal records archived.
    assert set(result["archived"]) == {t1_id, t2_id}
    assert result["failed"] == []


def test_run_on_a_repo_that_has_never_archived_is_not_a_traceback(tmp_path):
    """A repo with no `archive/handoffs/` yet has an EMPTY archive, not a
    broken one.

    `build_index`'s default `onerror` re-raises, so routing an absent archive
    root through `open_index` made the FIRST `archive-stamp-cli ship-handoff`
    in a freshly onboarded repo raise `FileNotFoundError` out of
    `cs_ship_handoff` -- a traceback where the contract is an error dict, and
    a handoff left unstamped. Found while measuring this call path
    (state/kill-ledger.md § K-022, 2026-08-30), not by a test."""
    root = _init_repo(tmp_path / "repo")
    _write_frontmatter(
        root / "state" / "handoffs" / "2026-08-01_00001_plain.md",
        {"handoff_id": "hnd-only", "deployment_state": "ready_to_fire"},
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture baseline")
    assert not (root / "archive" / "handoffs").exists()

    result = cycle.run(root, cap=150)

    assert result["archived"] == []
    assert result["failed"] == []
    assert result["live_read_count"] == 1
    assert result["index_rebuilt"] is False
    assert result["index_cache_written"] is False


# ---------------------------------------------------------------------------
# The rails the sweep carried and the cycle did not
# ---------------------------------------------------------------------------
#
# Found by C8's repoint (see state/audits/2026-08-30-the-cycle-lost-the-
# sweeps-exclusion-rails.md), not by the plan's own exit criterion — whose
# fixture carries no dirty, consumed, or transition-owned records and so
# returned CRITERION_PASSES = True on both sides of the defect.


def test_a_worktree_dirty_terminal_record_is_retained_not_archived(repo, monkeypatch):
    """`archive_terminal_handoffs` states the ground itself: moving and
    committing a handoff whose bytes have diverged from HEAD either commits
    content nobody staged, or dies on `archive_and_commit`'s own drift refusal
    at act time. On a tree ~50 sessions write to, that is a peer-safety
    property, not a tidiness one."""
    monkeypatch.setattr(cycle, "_claim_holder_live_predicate", lambda common_dir: (lambda p, r: False))

    terminal = repo / "state" / "handoffs" / "2026-06-01_00003_terminal.md"
    terminal.write_text(
        terminal.read_text(encoding="utf-8") + "\nAn uncommitted edit.\n", encoding="utf-8"
    )

    result = cycle.run(repo, cap=10)

    assert terminal.exists(), "a dirty terminal record must stay in state/handoffs/"
    assert "state/handoffs/2026-06-01_00003_terminal.md" not in [
        m.replace("\\", "/") for m in result["archived"]
    ]


def test_a_record_claimed_by_a_live_session_is_retained(repo, monkeypatch):
    """The claim dir is the primary key and the record's own `claimed_by` is
    the fallback — mirroring `_scan_terminal`'s own Check 4. A record another
    live session is working through is not this sweep's to file.

    `claimed_by`, not `consumed_by`: the first cut of this rail read the
    retired name, which NO live record carries, and this test passed anyway
    because its fixture was authored from the same wrong field. The rail
    could not have fired in production."""
    monkeypatch.setattr(cycle, "_claim_holder_live_predicate", lambda common_dir: (lambda p, r: False))

    consumed = repo / "state" / "handoffs" / "2026-06-01_00003_terminal.md"
    _write_frontmatter(
        consumed,
        {
            "handoff_id": "hnd-t1",
            "deployment_state": "closed",
            "claimed_by": "sess-alive-0001",
        },
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "record names a claiming session")
    monkeypatch.setattr(cycle, "resolve_live_session_ids", lambda: frozenset({"sess-alive-0001"}))

    result = cycle.run(repo, cap=10)

    assert consumed.exists(), "a record consumed by a live session must be retained"
    assert "state/handoffs/2026-06-01_00003_terminal.md" not in [
        m.replace("\\", "/") for m in result["archived"]
    ]


def test_a_dead_claiming_session_does_not_retain(repo, monkeypatch):
    """The rail keys on LIVENESS, not on the field being present. A stale
    `consumed_by` naming a session that exited must not wedge the record in
    state/handoffs/ forever."""
    monkeypatch.setattr(cycle, "_claim_holder_live_predicate", lambda common_dir: (lambda p, r: False))

    consumed = repo / "state" / "handoffs" / "2026-06-01_00003_terminal.md"
    _write_frontmatter(
        consumed,
        {
            "handoff_id": "hnd-t1",
            "deployment_state": "closed",
            "claimed_by": "sess-long-gone",
        },
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "record names a dead session")
    monkeypatch.setattr(cycle, "resolve_live_session_ids", lambda: frozenset({"sess-someone-else"}))

    result = cycle.run(repo, cap=10)

    assert not consumed.exists(), "a dead consumer must not retain the record"
    assert result["archived"]


def test_an_excluded_path_is_left_for_its_own_caller(repo, monkeypatch):
    """`exclude` is what stops the population sweep completing a move the
    targeted transition failed to make. Without it the sweep archives the
    transition's own handoff through its own seam, and
    `cs_chain_archive_handoff`'s source-gone-AND-destination-present check —
    which cannot tell whose move it was — reads the failure as a success."""
    monkeypatch.setattr(cycle, "_claim_holder_live_predicate", lambda common_dir: (lambda p, r: False))

    owned = repo / "state" / "handoffs" / "2026-06-01_00003_terminal.md"

    result = cycle.run(
        repo, cap=10, exclude={"state/handoffs/2026-06-01_00003_terminal.md"}
    )

    assert owned.exists(), "an excluded path belongs to its caller, not to the sweep"
    assert "state/handoffs/2026-06-01_00003_terminal.md" not in [
        m.replace("\\", "/") for m in result["archived"]
    ]


def test_the_retired_consumed_by_spelling_is_still_tolerated(repo, monkeypatch):
    """`coverage.py :: _parse_handoff_consumed_by` is dual-tolerant with
    `claimed_by` winning, and this rail mirrors that rather than inventing a
    second rule. Asserted separately from the `claimed_by` case so a refactor
    that drops the retired name fails here rather than silently narrowing
    which records the rail can see."""
    monkeypatch.setattr(cycle, "_claim_holder_live_predicate", lambda common_dir: (lambda p, r: False))

    legacy = repo / "state" / "handoffs" / "2026-06-01_00003_terminal.md"
    _write_frontmatter(
        legacy,
        {
            "handoff_id": "hnd-t1",
            "deployment_state": "closed",
            "consumed_by": "sess-alive-0001",
        },
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "record uses the retired spelling")
    monkeypatch.setattr(cycle, "resolve_live_session_ids", lambda: frozenset({"sess-alive-0001"}))

    result = cycle.run(repo, cap=10)

    assert legacy.exists(), "the retired spelling must still retain"
    assert "state/handoffs/2026-06-01_00003_terminal.md" not in [
        m.replace("\\", "/") for m in result["archived"]
    ]


def test_claimed_by_wins_over_the_retired_spelling(repo, monkeypatch):
    """Precedence, not merely tolerance. A record carrying BOTH must be judged
    on `claimed_by` — reading `consumed_by` first would retain a record whose
    live claimant has gone."""
    monkeypatch.setattr(cycle, "_claim_holder_live_predicate", lambda common_dir: (lambda p, r: False))

    both = repo / "state" / "handoffs" / "2026-06-01_00003_terminal.md"
    _write_frontmatter(
        both,
        {
            "handoff_id": "hnd-t1",
            "deployment_state": "closed",
            "claimed_by": "sess-long-gone",
            "consumed_by": "sess-alive-0001",
        },
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "record carries both spellings")
    monkeypatch.setattr(cycle, "resolve_live_session_ids", lambda: frozenset({"sess-alive-0001"}))

    result = cycle.run(repo, cap=10)

    assert not both.exists(), "claimed_by names a dead session -- consumed_by must not rescue it"


def test_an_excluded_repo_relative_path_is_resolved_against_the_worktree(repo, monkeypatch, tmp_path):
    """`_transition_target_rel` gets a REPO-RELATIVE `handoff_path` from its
    real callers. Resolving it bare resolves against the process CWD, so a
    caller whose CWD is not the worktree root would silently exclude nothing
    -- fail-OPEN on the rail whose whole job is to stop the sweep touching
    another leg's handoff."""
    import os

    monkeypatch.chdir(tmp_path)
    rel = "state/handoffs/2026-06-01_00003_terminal.md"

    assert cycle._transition_target_rel(repo, {"handoff_path": rel}) == {rel}, (
        f"resolved against CWD ({os.getcwd()}) instead of the worktree root"
    )


def test_a_raising_close_pass_still_lets_the_sweep_run_and_says_so(repo, monkeypatch):
    """Two properties in one, both asserted by the fusion-contract tests that
    were deleted with `ops/handoff_housekeeping.py` before this module carried
    them: a close pass that raises must not eat the sweep, and it must not
    vanish either. Without `close_error` a caller cannot tell "nothing needed
    closing" from "the close pass died" — both render as closed=0."""
    monkeypatch.setattr(cycle, "_claim_holder_live_predicate", lambda common_dir: (lambda p, r: False))

    def _boom(record, resolver):
        raise RuntimeError("gate evaluation exploded")

    monkeypatch.setattr(cycle, "evaluate_gate_clear", _boom)

    result = cycle.run(repo, cap=10)

    assert result["closed"] == 0
    assert result["close_error"] and "gate evaluation exploded" in result["close_error"]
    assert result["archived"], "the sweep must still have run"


def test_a_clean_close_pass_reports_no_close_error(repo, monkeypatch):
    """`close_error` is None on a clean pass, never the empty-string/absent
    shape that would read as falsy-but-present to a caller branching on it."""
    monkeypatch.setattr(cycle, "_claim_holder_live_predicate", lambda common_dir: (lambda p, r: False))

    result = cycle.run(repo, cap=10)

    assert result["close_error"] is None
