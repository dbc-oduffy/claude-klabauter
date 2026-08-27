"""
coordinator_core.reconcile.tests.test_commit_reality — surviving-helper fixtures.

**SHRUNK 2026-08-26 (C10, `state/kill-ledger.md`).** This file formerly pinned the DEC-1
three-signal commit-reality shipped-ness matcher (`evaluate_commit_reality`) across a full
scenario matrix. That matcher is deleted (see `commit_reality.py`'s own module docstring); the
verdict fixtures and their real-git-repo scaffolding are deleted with it. What remains pins the
two helpers `archive_stamp.py` and `ops/completion_ops.py` still import directly:
`_is_mechanical_subject` (plus its `_DEFAULT_MECHANICAL_DENYLIST` default) and `_git`.

Spec backlink: pln-claude-klabauter-auto-reconcile-pass-off-425848 § C2 (DEC-1) — historical.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.reconcile.commit_reality import (
    _DEFAULT_MECHANICAL_DENYLIST,
    _git,
    _is_mechanical_subject,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_MECHANICAL_DENYLIST = [
    "pickup:",
    "reclaim(docs)",
    "session-init",
    "memo:",
    "handoff.transition",
]


class TestMechanicalDenylistPrefixNotSubstring:
    """Slice-A review Finding 1: every denylist entry except the
    `handoff.transition`-family marker matches as a PREFIX, not a substring.
    A real subject that merely CONTAINS a denylist token (not as a prefix)
    must NOT be denylisted."""

    def test_subject_containing_but_not_prefixed_by_memo_token_is_not_denylisted(
        self,
    ) -> None:
        # "memo:" appears mid-subject, not as a prefix — must not match.
        assert not _is_mechanical_subject(
            "feat: land handoff-memo: rendering fix",
            _MECHANICAL_DENYLIST,
        )

    def test_subject_containing_but_not_prefixed_by_pickup_token_is_not_denylisted(
        self,
    ) -> None:
        assert not _is_mechanical_subject(
            "fix: repair pickup: field validation on ingest",
            _MECHANICAL_DENYLIST,
        )

    def test_subject_prefixed_by_denylist_token_is_still_denylisted(self) -> None:
        assert _is_mechanical_subject(
            "memo: send cross-repo brief", _MECHANICAL_DENYLIST
        )

    def test_handoff_transition_family_still_matches_as_substring(self) -> None:
        # The one documented substring exception — must still match mid-subject.
        assert _is_mechanical_subject(
            "chore: handoff.transition: ship h-123",
            _MECHANICAL_DENYLIST,
        )


class TestEmptyDenylistDisablesFiltering:
    """An empty denylist list (as opposed to an absent key) is treated as a real,
    deliberately-empty value by `_is_mechanical_subject` — the caller
    (`archive_stamp.py`) is responsible for substituting `_DEFAULT_MECHANICAL_DENYLIST`
    when its own policy-sourced list is empty; this helper does not do that
    substitution itself."""

    def test_empty_denylist_still_rejects_a_mechanical_subject_with_the_default(self) -> None:
        subject = "pickup: claim handoff 2026-01-01"
        assert _is_mechanical_subject(subject, list(_DEFAULT_MECHANICAL_DENYLIST))
        assert not _is_mechanical_subject(subject, [])


class TestGitHelper:
    """`_git` is the read-only git subprocess choke point `ops/completion_ops.py`
    imports as `_reality_git` — pinned directly rather than only via a caller."""

    def test_git_runs_read_only_subcommand_in_worktree(self, tmp_path) -> None:
        subprocess.run(
            ["git", "init", "-q"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        result = _git(tmp_path, ["status", "--short"])
        assert result.returncode == 0

    def test_git_reports_nonzero_on_invalid_subcommand(self, tmp_path) -> None:
        result = _git(tmp_path, ["not-a-real-git-subcommand"])
        assert result.returncode != 0
