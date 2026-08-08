"""
coordinator_core.ops.ceremony.tests.test_commit_gates_known_scope

AC4 (docs/plans/2026-08-07-claim-state-ledger-first-authoritative-read.md § C3):
`ceremony/commit_gates.py::_build_known_scope` and
`ops/dirty_tree_gate.py::_build_known_scope` are INDEPENDENT COPIES of the
same case-(b) known-concurrent-owner predicate. Before this fix, both skipped
a handoff with no mirror `claimed_by`/`consumed_by`, so a branch-switch-victim
handoff — live in the claim ledger, reverted in the tracked frontmatter mirror
— dropped its `scope:` paths from `known_scope` entirely. This proves, for
BOTH predicates independently, that a desynced handoff's `scope:` paths stay
in `known_scope`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core import claim_state
from coordinator_core.ops.ceremony import commit_gates
from coordinator_core.ops import dirty_tree_gate as dirty_tree_gate_mod


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _write_desynced_handoff(repo: Path, name: str) -> Path:
    """A handoff whose tracked frontmatter mirror is reverted to `open` (no
    claimed_by/consumed_by) but that a peer session still holds via the claim
    ledger -- the exact branch-switch-revert desync AC4 exists to fix."""
    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    handoff = repo / "state" / "handoffs" / name
    handoff.write_text(
        "---\n"
        "status: open\n"
        "scope:\n"
        "  - peers/owned-file.txt\n"
        "category: workstream\n"
        "---\n"
        "\n# desynced peer handoff\n",
        encoding="utf-8",
    )
    _git(["add", "--", f"state/handoffs/{name}"], repo)
    _git(["commit", "-q", "-m", "seed: desynced peer handoff"], repo)
    return handoff


def _write_ledger_claim(repo: Path, handoff_name: str, session_id: str = "peer-session-id") -> None:
    common_dir = repo / ".git"
    claim_dir = common_dir / "coordinator-sessions" / "handoff-claims" / handoff_name
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-08-07T10:00:00Z", encoding="utf-8")


# ---------------------------------------------------------------------------
# ceremony/commit_gates.py::_build_known_scope
# ---------------------------------------------------------------------------


def test_commit_gates_known_scope_desynced_handoff_scope_paths_survive(tmp_path):
    repo = _init_repo(tmp_path)
    handoff_name = "2026-08-07_120000_peer.md"
    _write_desynced_handoff(repo, handoff_name)
    _write_ledger_claim(repo, handoff_name)

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        known_scope = commit_gates._build_known_scope(repo)

    assert "peers/owned-file.txt" in known_scope


def test_commit_gates_known_scope_dead_ledger_holder_no_mirror_drops_scope(tmp_path):
    """Control: a dead ledger holder with no mirror claim is genuinely
    unclaimed -- its scope paths must NOT survive (guards against a
    trivially-permissive implementation)."""
    repo = _init_repo(tmp_path)
    handoff_name = "2026-08-07_120001_dead.md"
    _write_desynced_handoff(repo, handoff_name)
    _write_ledger_claim(repo, handoff_name, session_id="dead-session-id")

    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=False):
        known_scope = commit_gates._build_known_scope(repo)

    assert "peers/owned-file.txt" not in known_scope


# ---------------------------------------------------------------------------
# ops/dirty_tree_gate.py::_build_known_scope
# ---------------------------------------------------------------------------


def test_dirty_tree_gate_known_scope_desynced_handoff_scope_paths_survive(tmp_path):
    repo = _init_repo(tmp_path)
    handoff_name = "2026-08-07_120000_peer.md"
    _write_desynced_handoff(repo, handoff_name)
    _write_ledger_claim(repo, handoff_name)

    handoffs_dir = str(repo / "state" / "handoffs")
    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        known_scope = dirty_tree_gate_mod._build_known_scope(handoffs_dir, repo_root=str(repo))

    assert "peers/owned-file.txt" in known_scope


def test_dirty_tree_gate_known_scope_dead_ledger_holder_no_mirror_drops_scope(tmp_path):
    repo = _init_repo(tmp_path)
    handoff_name = "2026-08-07_120001_dead.md"
    _write_desynced_handoff(repo, handoff_name)
    _write_ledger_claim(repo, handoff_name, session_id="dead-session-id")

    handoffs_dir = str(repo / "state" / "handoffs")
    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=False):
        known_scope = dirty_tree_gate_mod._build_known_scope(handoffs_dir, repo_root=str(repo))

    assert "peers/owned-file.txt" not in known_scope
