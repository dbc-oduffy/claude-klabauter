"""
coordinator_core.ops.tests.test_handoff_author_fork_claim_state — proves
``handoff_author_fork._resolve_origin_handoff`` resolves ledger-first via
``coordinator_core.claim_state.resolve_claim_state``, rather than the old
mirror-only ``claimed_by``/``consumed_by`` frontmatter read.

Coverage:
  (a) ledger_only_desync_resolves_origin_handoff — the C6a incident shape:
      a handoff claimed on the branch-independent ledger, whose tracked
      frontmatter mirror carries NO claimed_by/consumed_by (the branch-switch
      revert). Before this fix, ``_resolve_origin_handoff`` read the mirror
      only and found nothing, so the fork was authored with a null
      ``origin_handoff`` — permanent, silent provenance loss. After the fix,
      the fork's ``origin_handoff`` must resolve to the desynced handoff.
  (b) dead_ledger_holder_degrades_to_mirror — a live claim ledger entry does
      not exist (holder dead / no liveness) but the mirror carries
      claimed_by matching the session — origin_handoff still resolves via
      the mirror fallback (source == "mirror" per resolve_claim_state's own
      contract; a dead ledger holder is never surfaced as "ledger").
  (c) no_claim_anywhere_stays_null — neither the ledger nor the mirror name
      the session — origin_handoff resolves to null (unchanged from the
      pre-existing mirror-only behaviour for the true no-match case).

Spec backlink: docs/plans/2026-08-07-claim-state-ledger-first-authoritative-read.md
§ Tasks, chunk C6a (AC5).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Optional
from unittest import mock

import pytest

# Import guard — fires ALL @register_op(...) side-effects (CBR #12), matching
# the sibling test_handoff_author_fork.py's own discipline.
import coordinator_core.ops  # noqa: F401 — populates _REGISTRY

from coordinator_core import claim_state as claim_state_module
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_author_fork import _handler, _resolve_origin_handoff

assert "handoff.author_fork" in _REGISTRY, (
    "import guard failed: 'handoff.author_fork' not in _REGISTRY — "
    "coordinator_core.ops.handoff_author_fork @register_op did not fire"
)


def _run(coro):
    return asyncio.run(coro)


_NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _make_git_repo(root: Path) -> Path:
    """Create a minimal git repo at ``root``; return its common_dir (.git path)."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(root), capture_output=True, check=True, creationflags=_NO_WIN,
    )
    subprocess.run(
        ["git", "config", "user.email", "claim-state-fork-test@claude-klabauter.test"],
        cwd=str(root), capture_output=True, check=True, creationflags=_NO_WIN,
    )
    subprocess.run(
        ["git", "config", "user.name", "Claim State Fork Test"],
        cwd=str(root), capture_output=True, check=True, creationflags=_NO_WIN,
    )
    return (root / ".git").resolve()


def _seed_plan(plans_dir: Path, filename: str, *, title: str, plan_id: str) -> None:
    plans_dir.mkdir(parents=True, exist_ok=True)
    content = f'---\ntitle: "{title}"\nplan_id: "{plan_id}"\nstatus: draft\n---\n\n# Body\n'
    (plans_dir / filename).write_text(content, encoding="utf-8")


def _seed_handoff(
    handoffs_dir: Path,
    filename: str,
    *,
    claimed_by: Optional[str] = None,
    status: str = "open",
) -> Path:
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"status: {status}"]
    if claimed_by:
        lines.append(f"claimed_by: {claimed_by}")
    lines.append("---")
    lines.append("")
    lines.append("# body")
    path = handoffs_dir / filename
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_ledger_claim(common_dir: Path, handoff_name: str, session_id: str) -> None:
    claim_dir = common_dir / "coordinator-sessions" / "handoff-claims" / handoff_name
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-08-07T10:00:00Z", encoding="utf-8")


class TestResolveOriginHandoffLedgerFirst:
    """_resolve_origin_handoff routes through claim_state.resolve_claim_state."""

    def test_ledger_only_desync_resolves_origin_handoff(self, tmp_path, monkeypatch):
        """C6a incident shape: ledger holds the claim, mirror was reverted to
        open by a branch switch. origin_handoff must still resolve — not null."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        session_id = "sess-ledger-only-desync"

        _seed_plan(
            repo_root / "docs" / "plans", "2026-08-07-plan.md",
            title="Ledger Desync Plan", plan_id="pln-ledger-desync",
        )

        handoffs_dir = repo_root / "state" / "handoffs"
        handoff_name = "2026-08-07_090000_deadbeef.md"
        _seed_handoff(handoffs_dir, handoff_name, claimed_by=None, status="open")
        _write_ledger_claim(common_dir, handoff_name, session_id)

        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)

        with mock.patch.object(claim_state_module, "cs_claim_holder_live", return_value=True):
            result = _run(
                _handler(
                    {
                        "title": "Fork Over Desynced Baton",
                        "origin_plan_id": "pln-ledger-desync",
                        "origin_goal_id": None,
                    },
                    repo_root=common_dir,
                )
            )

        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_handoff"] == f"state/handoffs/{handoff_name}", (
            "ledger-only claim must resolve origin_handoff — the C6a "
            "silent-provenance-loss regression this chunk fixes"
        )

    def test_dead_ledger_holder_degrades_to_mirror(self, tmp_path, monkeypatch):
        """A dead ledger holder must not shadow a live mirror match —
        resolve_claim_state degrades to source=='mirror'."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        session_id = "sess-dead-ledger-mirror-live"

        _seed_plan(
            repo_root / "docs" / "plans", "2026-08-07-plan2.md",
            title="Dead Ledger Plan", plan_id="pln-dead-ledger",
        )

        handoffs_dir = repo_root / "state" / "handoffs"
        handoff_name = "2026-08-07_091500_cafebabe.md"
        _seed_handoff(handoffs_dir, handoff_name, claimed_by=session_id, status="claimed")
        _write_ledger_claim(common_dir, handoff_name, "some-other-stale-session")

        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)

        with mock.patch.object(claim_state_module, "cs_claim_holder_live", return_value=False):
            result = _run(
                _handler(
                    {
                        "title": "Fork Over Dead-Ledger Baton",
                        "origin_plan_id": "pln-dead-ledger",
                        "origin_goal_id": None,
                    },
                    repo_root=common_dir,
                )
            )

        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_handoff"] == f"state/handoffs/{handoff_name}"

    def test_no_claim_anywhere_stays_null(self, tmp_path, monkeypatch):
        """Neither ledger nor mirror name the session — origin_handoff is null."""
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)

        _seed_plan(
            repo_root / "docs" / "plans", "2026-08-07-plan3.md",
            title="No Claim Plan", plan_id="pln-no-claim",
        )
        _seed_handoff(
            repo_root / "state" / "handoffs", "2026-08-07_093000_f00dcafe.md",
            claimed_by="unrelated-session", status="claimed",
        )

        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-truly-unclaimed")

        result = _run(
            _handler(
                {
                    "title": "Fork With No Origin",
                    "origin_plan_id": "pln-no-claim",
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )

        assert result.get("status") == "ok", f"unexpected: {result}"
        assert result["origin_handoff"] is None

    def test_unreadable_candidate_logs_a_diagnostic(self, tmp_path, monkeypatch, caplog):
        """Review: code-reviewer (Finding 1). Before routing the claim read
        through resolve_claim_state, EVERY unreadable candidate file logged
        a warning unconditionally. That diagnostic silently vanished for an
        unreadable, non-matching candidate once resolve_claim_state's own
        mirror read started swallowing OSError internally. This proves the
        restored os.access() probe logs a warning for such a file, and that
        resolution still proceeds (does not raise, does not resolve this
        file as the origin)."""
        import logging

        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)

        handoffs_dir = repo_root / "state" / "handoffs"
        handoff_name = "2026-08-07_094500_baadf00d.md"
        _seed_handoff(handoffs_dir, handoff_name, claimed_by=None, status="open")
        target = handoffs_dir / handoff_name

        real_access = __import__("os").access

        def _fake_access(path, mode):
            if Path(path) == target:
                return False
            return real_access(path, mode)

        with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.handoff_author_fork"):
            with mock.patch("coordinator_core.ops.handoff_author_fork.os.access", side_effect=_fake_access):
                origin_handoff, origin_handoff_id = _resolve_origin_handoff(
                    handoffs_dir, "sess-irrelevant", repo_root=common_dir
                )

        assert origin_handoff is None
        assert origin_handoff_id is None
        assert any(
            "unreadable" in rec.message and handoff_name in rec.message
            for rec in caplog.records
        ), f"expected an unreadable-candidate warning; got: {[r.message for r in caplog.records]}"
