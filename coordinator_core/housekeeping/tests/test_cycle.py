"""
Tests for coordinator_core.housekeeping.cycle — Step E (move + ONE commit
through the existing `archive_and_commit` seam) and the assembled `run(...)`
entry point (plan chunk C6c).

Covers: `archive_terminal_batch` builds the correct `Move` list and never
calls into the seam for an empty batch; the seam's `(acted, failed)` split
is passed through unmodified (no second-guessing, no leave-and-log); and an
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


def test_archive_terminal_batch_builds_moves_and_passes_through_the_seam_result(tmp_path, monkeypatch):
    worktree_root = tmp_path
    p1 = worktree_root / "state" / "handoffs" / "2026-01-01_00001_a.md"
    p2 = worktree_root / "state" / "handoffs" / "2026-01-02_00002_b.md"
    entries = [
        TerminalEntry(path=p1, record={"handoff_id": "hnd-a", "deployment_state": "closed"}),
        TerminalEntry(path=p2, record={"handoff_id": "hnd-b", "deployment_state": "shipped"}),
    ]

    captured = {}

    async def _fake_archive_and_commit(root, moves, subject):
        captured["root"] = root
        captured["moves"] = moves
        captured["subject"] = subject
        return (
            [{"id": m.candidate_id, "archived": True} for m in moves],
            [],
        )

    monkeypatch.setattr(cycle, "archive_and_commit", _fake_archive_and_commit)

    acted, failed = cycle.archive_terminal_batch(worktree_root, entries, "the subject")

    assert captured["root"] == worktree_root
    assert captured["subject"] == "the subject"
    moves = captured["moves"]
    assert [m.src for m in moves] == [p1, p2]
    assert [m.dst for m in moves] == [
        cycle.handoff_archive_dest(worktree_root, p1),
        cycle.handoff_archive_dest(worktree_root, p2),
    ]
    assert [m.candidate_id for m in moves] == [
        str(p1.relative_to(worktree_root)),
        str(p2.relative_to(worktree_root)),
    ]
    assert all(m.force is False for m in moves)
    assert all(m.restage_src is False for m in moves)

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
    entries = [TerminalEntry(path=p1, record={"handoff_id": "hnd-a", "deployment_state": "closed"})]

    async def _fake_archive_and_commit(root, moves, subject):
        return ([], [{"id": moves[0].candidate_id, "reason": "dst-exists"}])

    monkeypatch.setattr(cycle, "archive_and_commit", _fake_archive_and_commit)

    acted, failed = cycle.archive_terminal_batch(tmp_path, entries, "subject")
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
