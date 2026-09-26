"""
coordinator_core.reconcile.tests.test_commit_reality — surviving-helper fixtures.

**SHRUNK 2026-08-26 (C10, `state/kill-ledger.md`).** This file formerly pinned the DEC-1
three-signal commit-reality shipped-ness matcher (`evaluate_commit_reality`) across a full
scenario matrix. That matcher is deleted (see `commit_reality.py`'s own module docstring); the
verdict fixtures and their real-git-repo scaffolding are deleted with it. What remains pins the
one helper `archive_stamp.py` still imports directly: `_is_mechanical_subject` (plus its
`_DEFAULT_MECHANICAL_DENYLIST` default).

**SHRUNK FURTHER 2026-09-23 (R3, `docs/plans/2026-09-22-spawn-budget-and-census.md`).** `_git` is
deleted from `commit_reality.py`; `ops/completion_ops.py` now spawns git through
`coordinator_core.git.run.run_git` directly, pinned by `coordinator_core/tests/
test_shared_git_runner.py`, not here. The `_git` fixtures below are removed with it.

Spec backlink: pln-claude-klabauter-auto-reconcile-pass-off-425848 § C2 (DEC-1) — historical.
"""

from __future__ import annotations

import pytest

from coordinator_core.reconcile.commit_reality import (
    _DEFAULT_MECHANICAL_DENYLIST,
    _is_mechanical_subject,
)

pytestmark = [pytest.mark.cadence]

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
        assert _is_mechanical_subject(
            "chore: handoff.transition: ship h-123",
            _MECHANICAL_DENYLIST,
        )


class TestChoreHandoffsPrefixEntry:
    """P029-T1 (docs/plans/2026-09-07-baton-lifecycle-refusal-drain-authz.md): `chore(handoffs):`
    is a PREFIX entry in `_DEFAULT_MECHANICAL_DENYLIST`, not a `_SUBSTRING_FAMILY_TOKENS`
    member — it must match the observed mechanical subject as a prefix, and must NOT match a
    subject that merely contains the token mid-subject."""

    def test_observed_mechanical_subject_is_denylisted(self) -> None:
        assert _is_mechanical_subject(
            "chore(handoffs): apply shipped/consumed",
            list(_DEFAULT_MECHANICAL_DENYLIST),
        )

    def test_mid_subject_chore_handoffs_token_is_not_denylisted(self) -> None:
        assert not _is_mechanical_subject(
            "feat(cascade): stop chore(handoffs): subjects resolving as evidence",
            list(_DEFAULT_MECHANICAL_DENYLIST),
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
