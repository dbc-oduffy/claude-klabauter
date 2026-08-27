"""
coordinator_core.ops.ceremony.tests.test_dirty_tree_gate_scope

C3 (docs/plans/2026-08-27-the-commit-op-resolves-one-pass-context.md):
`dirty_tree_gate`'s corpus walk (`_build_known_scope`) leaves the scoped
(hot) branch entirely -- it moves INTO the unscoped (`gate_paths is None`)
branch, computed lazily there for that branch's own callers. The scoped
branch now asks only "is any of MY k paths unattributable", using the
caller's own `gate_paths` -- a known-concurrent-owner (peer claim) excuse no
longer applies there.

This is a STRICTNESS change on the scoped branch: a path claimed by a peer
(present in `known_scope`) AND dirty within the caller's own `gate_paths`
was previously silently excused via the corpus walk; it is now reported
unattributable. The unscoped branch is untouched -- a peer-claimed path in
the whole-tree walk is still excused via `_build_known_scope`, exactly as
before this chunk.

Negative-spec: a test asserting the OLD excusing behaviour on the scoped
branch (peer claim wins over the caller's own pathspec) would pass today
and pin the very defect this chunk removes -- do not add one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core import claim_state
from coordinator_core.ops.ceremony import commit_gates as _cg
from coordinator_core.ops.ceremony.commit_gates import dirty_tree_gate

# Real-git spawn is load-bearing (index/worktree state); cadence-scoped like
# the sibling dirty_tree_gate suites in this module.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _seed_file(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _write_peer_handoff_claiming(repo: Path, name: str, scoped_path: str) -> None:
    """A handoff whose `scope:` claims `scoped_path`, committed so it is
    visible to `_build_known_scope`'s corpus walk."""
    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    handoff = repo / "state" / "handoffs" / name
    handoff.write_text(
        "---\n"
        "status: open\n"
        f"scope:\n  - {scoped_path}\n"
        "category: workstream\n"
        "---\n"
        "\n# peer handoff claiming the path\n",
        encoding="utf-8",
    )
    _git(["add", "--", f"state/handoffs/{name}"], repo)
    _git(["commit", "-q", "-m", "seed: peer handoff"], repo)


def _write_ledger_claim(repo: Path, handoff_name: str, session_id: str = "peer-session-id") -> None:
    common_dir = repo / ".git"
    claim_dir = common_dir / "coordinator-sessions" / "handoff-claims" / handoff_name
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-08-27T10:00:00Z", encoding="utf-8")


def _seed_peer_claimed_unstaged_deletion(repo: Path, handoff_name: str, path: str) -> None:
    """The one member DR-227 proves reachable through the real caller on the
    scoped branch: an index entry whose worktree path vacated without ever
    being staged for deletion -- here synthesised as a peer-claimed path."""
    _seed_file(repo, path, "content")
    _git(["add", "--", path], repo)
    # `_write_peer_handoff_claiming` commits everything currently staged,
    # which lands `path` alongside the handoff in the same commit.
    _write_peer_handoff_claiming(repo, handoff_name, path)
    _write_ledger_claim(repo, handoff_name)
    (repo / path).unlink()


def test_dirty_tree_gate_scoped_peer_claim_no_longer_excuses_own_pathspec_hit(tmp_path):
    """AC7: the scoped branch gets STRICTER. Before this chunk, a peer claim
    (via the corpus walk) silently excused a path that was ALSO dirty inside
    the caller's own `gate_paths` -- this test is red on that prior shape and
    green after: the caller's own pathspec now wins, unattributable."""
    repo = _init_repo(tmp_path)
    handoff_name = "2026-08-27_120000_peer.md"
    _seed_peer_claimed_unstaged_deletion(repo, handoff_name, "doomed.txt")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        outcome = dirty_tree_gate(repo, gate_paths=["doomed.txt"])

    assert outcome.passed is False
    assert outcome.unattributable == ["doomed.txt"]


def test_dirty_tree_gate_unscoped_peer_claim_still_excused(tmp_path):
    """The unscoped (`gate_paths=None`) branch is untouched by this chunk: a
    peer-claimed path in the whole-tree walk is still excused via
    `_build_known_scope`, exactly as before."""
    repo = _init_repo(tmp_path)
    handoff_name = "2026-08-27_120001_peer.md"
    _seed_peer_claimed_unstaged_deletion(repo, handoff_name, "doomed.txt")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        outcome = dirty_tree_gate(repo, gate_paths=None)

    assert outcome.passed is True
    assert outcome.unattributable == []


def test_dirty_tree_gate_scoped_branch_no_longer_calls_build_known_scope(tmp_path):
    """Structural pin: the scoped branch must not even consult
    `_build_known_scope` -- not merely happen to disagree with it on this
    fixture. Patched to raise; a call from the scoped branch would surface
    as a test failure rather than a silent behavioural difference."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "doomed.txt", "content")
    _git(["add", "--", "doomed.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    (repo / "doomed.txt").unlink()

    with mock.patch.object(
        _cg, "_build_known_scope", side_effect=AssertionError("scoped branch must not call _build_known_scope")
    ):
        outcome = dirty_tree_gate(repo, gate_paths=["doomed.txt"])

    assert outcome.passed is False
    assert outcome.unattributable == ["doomed.txt"]
