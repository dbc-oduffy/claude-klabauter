"""
Tests for coordinator_core.housekeeping.cycle — Step E (move + ONE commit
through the existing `archive_and_commit` seam) and the assembled `run(...)`
entry point (plan chunk C6c).

Covers: `run(...)` never calls into the `archive_and_commit` seam for an
empty combined batch; handoff and memo `Move` lists are both built inline
in `run(...)` (C2, the actioned-memo class gets an occasion) and land in
ONE commit; the seam's (acted, failed) split is passed through unmodified
(no second-guessing, no leave-and-log); and an end-to-end `run(...)` cycle
on a small real git repo — a gate genuinely clears, a terminal record is
archived and landed in ONE commit, a claim-held terminal record is
retained, and a gate-clear CONFLICT is reported rather than silently
dropped.

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

from coordinator_core.housekeeping import cycle
from coordinator_core.housekeeping.gate_clear import CONFLICT
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
# Step E's empty-batch short-circuit — no longer a dedicated function after
# F4 (overengineering-reviewer, 2026-08-30): `archive_terminal_batch` was a
# single-caller passthrough (empty-check + one asyncio.run) once the Move
# construction moved into run(); inlined there, so the seam-never-called-
# for-an-empty-batch guarantee is now asserted at `run(...)` itself.
# ---------------------------------------------------------------------------


def _write_memo(path: Path, status: str, body: str = "Fixture memo body.\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nstatus: {status}\n---\n\n{body}", encoding="utf-8")


def test_the_seam_fires_for_memos_alone_with_zero_terminal_handoffs(tmp_path, monkeypatch):
    """coordinator:code-reviewer F3 -- the F4 inline's guarantee is `if
    handoff_moves or memo_moves:` (OR, not AND). Nothing before this test
    isolated "handoffs empty, memos non-empty" from "both empty" or "both
    non-empty", so a latent `if handoff_moves and memo_moves:` typo would
    have gone undetected -- every existing fixture either has both families
    empty or both populated."""
    root = _init_repo(tmp_path / "repo")
    _write_memo(root / "cross-repo" / "inbox" / "memo-only.md", "actioned")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture: one actioned memo, no handoffs")

    calls = {"n": 0}
    real_archive_and_commit = cycle.archive_and_commit

    async def _counting(*args, **kwargs):
        calls["n"] += 1
        return await real_archive_and_commit(*args, **kwargs)

    monkeypatch.setattr(cycle, "archive_and_commit", _counting)

    result = cycle.run(root, cap=10)

    assert result["archived"] == []
    assert result["failed"] == []
    assert result["memos_archived"] == ["cross-repo/inbox/memo-only.md"]
    assert result["memos_failed"] == []
    assert calls["n"] == 1, "the seam must fire exactly once for a memo-only batch"


def test_the_seam_fires_for_handoffs_alone_with_zero_actioned_memos(tmp_path, monkeypatch):
    """The reverse of the case above -- handoffs non-empty, memos empty
    (no `cross-repo/inbox/` at all)."""
    root = _init_repo(tmp_path / "repo")
    _write_frontmatter(
        root / "state" / "handoffs" / "2026-06-01_00003_terminal.md",
        {"handoff_id": "hnd-t1", "deployment_state": "closed"},
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture: one terminal handoff, no memos")

    calls = {"n": 0}
    real_archive_and_commit = cycle.archive_and_commit

    async def _counting(*args, **kwargs):
        calls["n"] += 1
        return await real_archive_and_commit(*args, **kwargs)

    monkeypatch.setattr(cycle, "archive_and_commit", _counting)

    result = cycle.run(root, cap=10)

    assert result["archived"] == ["state/handoffs/2026-06-01_00003_terminal.md"]
    assert result["failed"] == []
    assert result["memos_archived"] == []
    assert result["memos_failed"] == []
    assert calls["n"] == 1, "the seam must fire exactly once for a handoff-only batch"


def test_run_never_calls_the_seam_when_the_batch_is_empty(tmp_path, monkeypatch):
    root = _init_repo(tmp_path / "repo")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "empty fixture", "--allow-empty")

    def _boom(*args, **kwargs):
        raise AssertionError("archive_and_commit must not be called for an empty batch")

    monkeypatch.setattr(cycle, "archive_and_commit", _boom)

    result = cycle.run(root, cap=150)

    assert result["archived"] == []
    assert result["failed"] == []
    assert result["memos_archived"] == []
    assert result["memos_failed"] == []


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
    # T2: terminal, and its claim is held by a live session -- since the
    # 2026-09-04 ruling that is not a retention ground, so it archives too.
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


def test_run_clears_a_gate_and_archives_every_terminal_record(repo):
    """Was `..._and_retains_held_claims`, renamed 2026-09-04 when the claim
    it retained on stopped being a retention ground. T2's claim is held; T2
    archives anyway, alongside T1."""
    result = cycle.run(str(repo), cap=10)

    # Hardcoded forward-slash literal, not a `relative_to()` round-trip of
    # the production id-format bug (coordinator:code-reviewer F2) -- the
    # round-trip form passes on any platform regardless of what `archived`
    # actually carries, so it can never catch a regression to the native-
    # separator `str(relative_to())` form on Windows.
    t1_id = "state/handoffs/2026-06-01_00003_terminal.md"
    t2_id = "state/handoffs/2026-06-02_00004_held.md"

    assert result["closed"] == 1
    assert result["conflicts"] == []
    assert result["failed"] == []
    assert set(result["archived"]) == {t1_id, t2_id}
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

    # T2 archived too: a held claim no longer retains a complete baton.
    assert not (repo / "state" / "handoffs" / "2026-06-02_00004_held.md").exists()
    assert (repo / "archive" / "handoffs" / "2026-06" / "2026-06-02_00004_held.md").exists()

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

    # Hardcoded forward-slash literals (coordinator:code-reviewer F2) -- see
    # the sibling fix above; a `relative_to()` round-trip here would mirror
    # rather than catch a native-separator regression.
    t1_id = "state/handoffs/2026-06-01_00003_terminal.md"
    t2_id = "state/handoffs/2026-06-02_00004_held.md"

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
    terminal = repo / "state" / "handoffs" / "2026-06-01_00003_terminal.md"
    terminal.write_text(
        terminal.read_text(encoding="utf-8") + "\nAn uncommitted edit.\n", encoding="utf-8"
    )

    result = cycle.run(repo, cap=10)

    assert terminal.exists(), "a dirty terminal record must stay in state/handoffs/"
    assert "state/handoffs/2026-06-01_00003_terminal.md" not in [
        m.replace("\\", "/") for m in result["archived"]
    ]


def test_a_live_claimant_no_longer_retains_a_terminal_record(repo, monkeypatch):
    """Replaces four tests that pinned the claimant rail: the `claimed_by`
    live case, the dead-claimant case, the retired `consumed_by` spelling, and
    the precedence between the two spellings. All four measured a rail deleted
    on the 2026-09-04 PM ruling -- "a claim on a baton shouldn't prevent it
    from getting archived. What matters is that the baton is complete, not the
    liveness of the holder."

    Both spellings are seeded, both naming a session that IS live, which is
    the strongest form of the old retain. It must archive.
    """
    claimed = repo / "state" / "handoffs" / "2026-06-01_00003_terminal.md"
    _write_frontmatter(
        claimed,
        {
            "handoff_id": "hnd-t1",
            "deployment_state": "closed",
            "claimed_by": "sess-alive-0001",
            "consumed_by": "sess-alive-0001",
        },
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "record names a live claiming session")

    result = cycle.run(repo, cap=10)

    assert not claimed.exists(), (
        "a complete baton must archive whether or not its claimant is alive"
    )
    assert "state/handoffs/2026-06-01_00003_terminal.md" in [
        m.replace("\\", "/") for m in result["archived"]
    ]


def test_an_excluded_path_is_left_for_its_own_caller(repo, monkeypatch):
    """`exclude` is what stops the population sweep completing a move the
    targeted transition failed to make. Without it the sweep archives the
    transition's own handoff through its own seam, and
    `cs_chain_archive_handoff`'s source-gone-AND-destination-present check —
    which cannot tell whose move it was — reads the failure as a success."""
    owned = repo / "state" / "handoffs" / "2026-06-01_00003_terminal.md"

    result = cycle.run(
        repo, cap=10, exclude={"state/handoffs/2026-06-01_00003_terminal.md"}
    )

    assert owned.exists(), "an excluded path belongs to its caller, not to the sweep"
    assert "state/handoffs/2026-06-01_00003_terminal.md" not in [
        m.replace("\\", "/") for m in result["archived"]
    ]


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
    result = cycle.run(repo, cap=10)

    assert result["close_error"] is None
