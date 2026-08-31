"""
coordinator_core.authz.tests.test_classification — per-chunk classification pins
for ops landed against docs/plans/2026-08-31-the-hook-category-stops-paying-an-
interpreter-start.md.

Purpose: `test_authz_contract.py` already covers the classification framework
(fail-closed KeyError, drift-guard, registry-size). This module pins the
SPECIFIC entries this plan's chunks add, one test per op, so a later chunk's
edit to `OP_CLASSIFICATION` cannot silently regress an earlier chunk's op
without a named failure pointing at it.

Test convention: pytest. Invoke via
``pytest coordinator_core/authz/tests/test_classification.py -v``
"""

from __future__ import annotations

from coordinator_core.authz.classification import OpClass, classify


class TestNudgeAutonomousAskuserquestion:
    """C2 — the first hot-path reconstructable unit built against
    docs/reference/warm-hook-migration.md. Routability (the `hooks.` prefix)
    and authz classification are independent obligations (staff-eng finding
    6) — this test asserts the classification half explicitly, since no
    routing test ever calls `_is_compute_only` for a prefixed op."""

    def test_classifies_compute_only(self) -> None:
        assert classify("hooks.nudge_autonomous_askuserquestion") is OpClass.COMPUTE_ONLY
