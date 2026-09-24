"""
coordinator_core.ops.session.tests.test_phantom_cross_session_handoff

Plan: docs/plans/2026-09-22-wsc-and-wave-commit-residual-defects.md (P161-C2)

Rebuilds baton record 2's L46 phantom cross-session handoff scenario as a
`tmp_path` git fixture against the LIVE resolver
(`session.resolve_chain_terminal_disposition::_classify_sync`), and asserts
the two shapes the record described never attribute a peer session's commit
or a foreign-consumed handoff to this session.

Coverage:
  (a) test_deleted_peer_handoff_never_attributed_to_a_different_sid --
      session B's commit (`Session-Id: B`) ships/consumes a handoff, the file
      is then deleted from the tree, and session A runs the resolution. A
      must resolve `chain_terminal: False` and never name B's handoff as its
      own `evidence.consumed_handoff`.
  (b) test_restoration_commit_spoof_is_rejected_with_a_diagnostic --
      a commit carrying `Session-Id: A` restores an archived handoff whose
      OWN frontmatter is stamped `claimed_by: B`. Session A's resolution must
      reject the restoration (the foreign-consumer spoof guard already in
      `_classify_sync`), naming the mismatch in a diagnostic note, rather
      than treating the handoff as A's own.

Both cases exercise `resolve_chain_terminal_disposition.py`'s EXISTING
detector-B ownership guard (module docstring's 2026-08-05/2026-08-10
incidents) -- this is a trace-and-pin row, not a fix. See the paired audit,
`state/audits/2026-09-22-phantom-cross-session-handoff-trace.md`, for the
verdict on whether any production caller can still reach this shape with
`chain_terminal=True`.

Negative-spec:
  - Does NOT force a `consumed_by` stamp or loop-reinvoke a ceremony op
    (L46's own named anti-workarounds) -- fixtures write handoff frontmatter
    directly and commit once per step.
  - Does NOT patch or re-fix the resolver -- a red assertion here would be a
    Branch D finding for the EM, never an in-row edit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.session.resolve_chain_terminal_disposition as rctd
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


def _init_repo_with_origin(tmp_path: Path) -> Path:
    """A git repo with an `origin` remote pointing at a local bare repo, so
    `git merge-base origin/main HEAD` resolves for Detector B (mirrors
    `test_resolver_git_provenance.py`'s fixture shape)."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "phantom-handoff@claude-klabauter.test")
    _git(root, "config", "user.name", "Phantom Handoff Test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "chore: initial skeleton")

    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )
    _git(root, "remote", "add", "origin", str(bare))
    push = _git(root, "push", "-u", "origin", "main")
    assert push.returncode == 0, push.stderr
    return root


def _commit_unpushed(root: Path, message: str) -> None:
    """Commit locally WITHOUT pushing -- `origin/main` stays at the fixture's
    initial skeleton commit, so the new commit is genuinely ahead of it for
    Detector B's merge-base..HEAD scan."""
    _git(root, "add", "-A")
    result = _git(root, "commit", "-m", message)
    assert result.returncode == 0, result.stderr


def _write_archived_handoff(root: Path, name: str, *, claimed_by: str = "") -> Path:
    path = root / "archive" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['title: "Test Archived Handoff"', "created: 2026-01-01", "status: archived", "predecessor: null"]
    if claimed_by:
        lines.append(f"claimed_by: {claimed_by}")
    fm = "\n".join(lines)
    path.write_text(f"---\n{fm}\n---\n\n# Handoff Body\n", encoding="utf-8")
    return path


class TestDeletedPeerHandoffNeverAttributedToADifferentSid:
    def test_deleted_peer_handoff_never_attributed_to_a_different_sid(self, tmp_path):
        repo = _init_repo_with_origin(tmp_path)
        sid_a = "sess-phantom-a"
        sid_b = "sess-phantom-b"

        # B ships/consumes a handoff under B's own Session-Id trailer.
        shipped = _write_archived_handoff(repo, "shipped-by-b.md", claimed_by=sid_b)
        _commit_unpushed(repo, f"archive: ship handoff\n\nSession-Id: {sid_b}")

        # That handoff file is then deleted from the tree entirely.
        shipped.unlink()
        _commit_unpushed(repo, "chore: drop shipped handoff")

        result = rctd._classify_sync(repo, sid_a, {})

        assert result["chain_terminal"] is False
        assert result["disposition"] == "open"
        assert result["evidence"]["consumed_handoff"] is None

    def test_deleted_peer_handoff_leaves_no_trace_in_notes_or_warnings(self, tmp_path):
        """Regression guard: B's trailer must never even be considered a
        candidate for A -- Detector B only matches commits whose trailer
        EQUALS the sid being classified, so a deleted-file phantom read must
        not silently sneak a mismatched note in either."""
        repo = _init_repo_with_origin(tmp_path)
        sid_a = "sess-phantom-a2"
        sid_b = "sess-phantom-b2"

        shipped = _write_archived_handoff(repo, "shipped-by-b2.md", claimed_by=sid_b)
        _commit_unpushed(repo, f"archive: ship handoff\n\nSession-Id: {sid_b}")
        shipped.unlink()
        _commit_unpushed(repo, "chore: drop shipped handoff")

        result = rctd._classify_sync(repo, sid_a, {})

        assert sid_b not in "".join(result["evidence"]["notes"])
        assert sid_b not in "".join(result["evidence"]["warnings"])


class TestRestorationCommitSpoofIsRejected:
    def test_restoration_commit_spoof_is_rejected_with_a_diagnostic(self, tmp_path):
        repo = _init_repo_with_origin(tmp_path)
        sid_a = "sess-phantom-restore-a"
        sid_b = "sess-phantom-restore-b"

        # The handoff's OWN frontmatter names B as claim holder.
        _write_archived_handoff(repo, "restored-by-a.md", claimed_by=sid_b)
        # A commit carrying A's trailer restores/re-adds it.
        _commit_unpushed(repo, f"restore: recover archived handoff\n\nSession-Id: {sid_a}")

        result = rctd._classify_sync(repo, sid_a, {})

        assert result["chain_terminal"] is False
        assert result["disposition"] == "open"
        assert result["evidence"]["consumed_handoff"] is None
        notes = result["evidence"]["notes"]
        assert any(
            sid_b in note and "restored-by-a.md" in note for note in notes
        ), notes
