"""
coordinator_core.ops.tests.test_completion_ops_claim_state

Coverage for the C6a7 widening of `completion_ops.day_coverage_sweep`'s
``open_claimed_by`` (in-flight) read: it now also resolves via
`coordinator_core.claim_state.resolve_claim_state` (ledger-first, mirror
fallback) in addition to the raw `claimed_by`/`consumed_by` frontmatter
mirror field, so a branch-switch-reverted mirror does not make an
actively-claimed baton's commit look GENUINELY ORPHANED.

Coverage:
  (a) a desynced baton — the claim ledger holds a live claim, the tracked
      `claimed_by` mirror on the open handoff is empty — a same-day commit
      carrying that holder's Session-Id trailer is classified `in_flight`,
      NOT `orphaned` (the false alarm this chunk closes).
  (b) a genuine orphan — no live ledger claim, no mirror claim, no known
      entry, no sibling-tree evidence — a same-day commit with an unmatched
      Session-Id trailer is still classified `orphaned` (the polarity this
      chunk must not disturb: widening never hides a real orphan).

Spec backlink: coordinator_core/ops/completion_ops.py::day_coverage_sweep
               (module docstring GENUINELY ORPHANED / IN-FLIGHT partitions).
"""

from __future__ import annotations

import datetime
import subprocess
from pathlib import Path

import pytest

# Import guard — MUST precede any test so @register_op fires first.
import coordinator_core.ops.completion_ops  # noqa: F401 — fires @register_op

from coordinator_core import claim_state as _claim_state_module
from coordinator_core.ops.completion_ops import day_coverage_sweep

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_HOLDER_SESSION = "b1111111-2222-3333-4444-555555555555"
_ORPHAN_SESSION = "e1111111-2222-3333-4444-555555555555"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        capture_output=True,
        check=True,
    )


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "completion-ops-claim-state-test@claude-klabauter.test")
    _git(repo, "config", "user.name", "Completion Ops Claim State Test")
    _git(repo, "config", "commit.gpgsign", "false")

    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    (repo / "archive" / "completed").mkdir(parents=True, exist_ok=True)
    (repo / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    (repo / "archive" / "completed" / ".gitkeep").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    # Pinned to a date well outside any test's swept `day` — the sweep is
    # UTC-day-scoped (`_day_commit_log`), so an undated init commit landing
    # on "today" would otherwise pollute both partitions' commit counts.
    subprocess.run(
        ["git", "commit", "-m", "chore: initial skeleton"],
        cwd=str(repo),
        capture_output=True,
        check=True,
        env=_committer_env("2020-01-01T00:00:00+00:00"),
    )

    return repo


def _seed_desynced_open_handoff(repo: Path, name: str) -> Path:
    """A `status: claimed` handoff with NO `claimed_by` on the mirror — only
    a separately-written claim ledger entry carries the true holder."""
    path = repo / "state" / "handoffs" / name
    content = '---\ntitle: "Desynced Handoff"\nstatus: claimed\n---\n\n# Body.\n'
    path.write_text(content, encoding="utf-8")
    return path


def _write_ledger_claim(repo: Path, handoff_name: str, holder_session_id: str) -> Path:
    """<common_dir>/coordinator-sessions/handoff-claims/<handoff_name>/session_id
    — the same dir shape `coordinator_core.claim_state.handoff_claim_dir`
    derives."""
    claim_dir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / handoff_name
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(holder_session_id, encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-08-07T10:00:00Z", encoding="utf-8")
    return claim_dir


def _commit_with_session_trailer(repo: Path, fname: str, session_id: str, day: str) -> None:
    (repo / fname).write_text(f"content for {fname}\n", encoding="utf-8")
    _git(repo, "add", fname)
    ts = f"{day}T12:00:00+00:00"
    subprocess.run(
        [
            "git",
            "commit",
            "-m",
            f"work: {fname}\n\nSession-Id: {session_id}\n",
        ],
        cwd=str(repo),
        capture_output=True,
        check=True,
        env=_committer_env(ts),
    )


def _committer_env(ts: str) -> dict:
    import os

    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = ts
    env["GIT_COMMITTER_DATE"] = ts
    return env


def _today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# (a) Desynced ledger-only claim — commit reclassified in_flight, not orphaned
# ---------------------------------------------------------------------------


def test_desynced_ledger_claim_reclassifies_in_flight_not_orphaned(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    day = _today()

    hpath = _seed_desynced_open_handoff(repo, "2026-08-07-desynced.md")
    _write_ledger_claim(repo, hpath.name, _HOLDER_SESSION)
    _commit_with_session_trailer(repo, "work.txt", _HOLDER_SESSION, day)

    monkeypatch.setattr(_claim_state_module, "cs_claim_holder_live", lambda *_a, **_k: True)

    result = day_coverage_sweep(repo, day)

    assert result["orphaned_count"] == 0, result
    assert result["in_flight_count"] == 1, result
    assert result["total_commits"] == 1, result


# ---------------------------------------------------------------------------
# (b) Genuine orphan — no ledger claim, no mirror claim — stays orphaned
# ---------------------------------------------------------------------------


def test_genuine_orphan_still_reported_orphaned(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    day = _today()

    # No handoff, no ledger claim, no completion entry for _ORPHAN_SESSION.
    _commit_with_session_trailer(repo, "other.txt", _ORPHAN_SESSION, day)

    monkeypatch.setattr(_claim_state_module, "cs_claim_holder_live", lambda *_a, **_k: True)

    result = day_coverage_sweep(repo, day)

    assert result["orphaned_count"] == 1, result
    assert result["in_flight_count"] == 0, result
    assert result["total_commits"] == 1, result
