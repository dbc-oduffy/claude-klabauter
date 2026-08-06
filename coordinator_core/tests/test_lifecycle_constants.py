"""Regression tests — DR-084 C3 terminal-predicate SSOT axis membership.

Pins two invariants for ``coordinator_core/lifecycle_constants.py``:

  1. Dual-vocabulary membership on the HANDOFF_* axes during the P1 migration window —
     new terms (``claimed``, ``continued``, ``closed``) are members alongside their retiring
     old counterparts.
  2. Axis separation — the whole point of C3 consolidating per-entity-axis exports is that
     HANDOFF_* tokens never leak onto PLAN_* / SPEC_* axes (independent entities, independent
     status enums). A new handoff token silently appearing on a plan/spec axis would be a
     regression this test exists to catch.

Spec backlink: docs/plans/2026-07-22-handoff-lifecycle-vocabulary-overhaul-scope.md § C3/C4
"""

from __future__ import annotations

from coordinator_core.lifecycle_constants import (
    HANDOFF_ANCHOR_EXCLUDED_STATUSES,
    HANDOFF_ARCHIVAL_TERMINAL_STATUSES,
    HANDOFF_TERMINAL_DEPLOYMENT,
    HANDOFF_TERMINAL_STATUS,
    PLAN_ARCHIVABLE_STATUS,
    PLAN_TERMINAL_STATUS,
    SPEC_RIPE_STATUSES,
    SPEC_SKIP_STATUSES,
)

_HANDOFF_NEW_VOCAB_TOKENS = frozenset({"open", "claimed", "continued", "closed"})


# ---------------------------------------------------------------------------
# dual-vocabulary membership
# ---------------------------------------------------------------------------

def test_claimed_is_a_handoff_terminal_status() -> None:
    assert "claimed" in HANDOFF_TERMINAL_STATUS


def test_consumed_and_superseded_remain_in_handoff_terminal_status() -> None:
    assert "consumed" in HANDOFF_TERMINAL_STATUS
    assert "superseded" in HANDOFF_TERMINAL_STATUS


def test_continued_and_closed_are_handoff_terminal_deployment() -> None:
    assert "continued" in HANDOFF_TERMINAL_DEPLOYMENT
    assert "closed" in HANDOFF_TERMINAL_DEPLOYMENT


def test_abandoned_and_shipped_remain_handoff_terminal_deployment() -> None:
    assert "abandoned" in HANDOFF_TERMINAL_DEPLOYMENT
    assert "shipped" in HANDOFF_TERMINAL_DEPLOYMENT


def test_claimed_is_a_handoff_anchor_excluded_status() -> None:
    assert "claimed" in HANDOFF_ANCHOR_EXCLUDED_STATUSES


def test_consumed_remains_a_handoff_anchor_excluded_status() -> None:
    assert "consumed" in HANDOFF_ANCHOR_EXCLUDED_STATUSES


def test_handoff_archival_terminal_statuses_widens_with_terminal_status() -> None:
    assert HANDOFF_TERMINAL_STATUS <= HANDOFF_ARCHIVAL_TERMINAL_STATUSES
    assert "claimed" in HANDOFF_ARCHIVAL_TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# axis separation — HANDOFF_* new-vocabulary tokens must not leak onto
# PLAN_*/SPEC_* axes
# ---------------------------------------------------------------------------

def test_plan_terminal_status_free_of_handoff_vocabulary_tokens() -> None:
    assert PLAN_TERMINAL_STATUS.isdisjoint(_HANDOFF_NEW_VOCAB_TOKENS)


# ---------------------------------------------------------------------------
# C8b rename — PLAN_TERMINAL_STATUS is now a backward-compatible alias of the
# question-named PLAN_ARCHIVABLE_STATUS (archivability, not liveness/
# flippability — see lifecycle_constants.py's own comment at the definition).
# ---------------------------------------------------------------------------

def test_plan_archivable_status_is_the_canonical_name() -> None:
    assert PLAN_ARCHIVABLE_STATUS is PLAN_TERMINAL_STATUS


def test_spec_ripe_statuses_free_of_handoff_vocabulary_tokens() -> None:
    assert SPEC_RIPE_STATUSES.isdisjoint(_HANDOFF_NEW_VOCAB_TOKENS)


def test_spec_skip_statuses_free_of_handoff_vocabulary_tokens() -> None:
    assert SPEC_SKIP_STATUSES.isdisjoint(_HANDOFF_NEW_VOCAB_TOKENS)
