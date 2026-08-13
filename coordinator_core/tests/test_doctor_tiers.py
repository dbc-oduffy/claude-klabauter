"""
coordinator_core.tests.test_doctor_tiers — Tests for the claude-klabauter doctor envelope reducer.

Covers:
  C0 — verdict-envelope reducer (worst-of; required-SKIP→DEGRADED;
       optional-SKIP→advisory-not-DEGRADED; all-INFO→INFO; empty→INFO).
  (C3 block deleted — health-op tier removed under DR-215/C2; this file
   now covers only the tier-independent envelope reducer.)

DR-215 note: Tier-1 UDS cold-spawn+ping liveness tests (TestTier1Liveness) were
deleted.  coordinator_core is a command-type engine — no resident UDS server exists.
The prose-based doctor rebuild is tracked in the spinoff
state/handoffs/2026-07-06_135806_claude_klabauter-doctor-prose-based-command-type.md.

Test-name discipline: names in this file do NOT overlap with the six stranded
engine tests awaiting relocation (test_ping, test_wire_protocol, test_lifecycle,
test_coverage_gate, test_handoff_children, test_mcp_shim).

All async op handlers are exercised via asyncio.run() in plain sync test functions
to avoid the pytest-asyncio dependency (engine is stdlib-only; no test-infra additions).

Spec backlink: pln-claude-klabauter-install-doctor-system-f-537d61 § C8
"""

from __future__ import annotations

import asyncio
import sys
import unittest.mock as mock
from pathlib import Path
from typing import List

import pytest

from coordinator_core.doctor_envelope import (
    BROKEN,
    DEGRADED,
    INFO,
    PASS,
    ProbeResult,
    build_envelope,
    reduce_overall,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _pr(probe: str, status: str, required: bool = True, skipped: bool = False) -> ProbeResult:
    """Build a minimal ProbeResult for reduce_overall / build_envelope tests."""
    return ProbeResult(
        probe=probe,
        status=status,
        detail="test detail",
        remediation="—",
        required=required,
        skipped=skipped,
    )


# ---------------------------------------------------------------------------
# C0 — Verdict-envelope reducer
# ---------------------------------------------------------------------------


class TestDoctorEnvelopeReduction:
    """reduce_overall() and build_envelope() — the shared worst-of reducer (C0).

    Each test name uses the pattern test_doctor_envelope_reduction_* to avoid
    any collision with the six stranded engine tests.
    """

    def test_doctor_envelope_reduction_worst_of(self) -> None:
        """BROKEN > DEGRADED > PASS — BROKEN wins when mixed with lesser verdicts."""
        results: List[ProbeResult] = [
            _pr("p.pass", PASS),
            _pr("p.degraded", DEGRADED),
            _pr("p.broken", BROKEN),
        ]
        assert reduce_overall(results) == BROKEN

    def test_doctor_envelope_reduction_degraded_beats_pass(self) -> None:
        """DEGRADED beats PASS in the worst-of ordering."""
        results: List[ProbeResult] = [
            _pr("p.pass1", PASS),
            _pr("p.pass2", PASS),
            _pr("p.degraded", DEGRADED),
        ]
        assert reduce_overall(results) == DEGRADED

    def test_doctor_envelope_reduction_all_pass(self) -> None:
        """All-PASS list → overall PASS."""
        results: List[ProbeResult] = [
            _pr("p.pass1", PASS),
            _pr("p.pass2", PASS),
        ]
        assert reduce_overall(results) == PASS

    def test_doctor_envelope_reduction_required_skip_degraded(self) -> None:
        """required-probe with skipped=True contributes DEGRADED to worst-of.

        A required check that could not run is NOT a confirmation of health;
        it must not return PASS (no false green).  The overall must be at least
        DEGRADED when a required probe is skipped.
        """
        results: List[ProbeResult] = [
            _pr("p.pass", PASS),
            _pr("p.required_skip", PASS, required=True, skipped=True),
        ]
        overall = reduce_overall(results)
        assert overall == DEGRADED, (
            f"required-SKIP must reduce as DEGRADED, not {overall!r}; "
            "all-SKIP-required must not return PASS or INFO"
        )

    def test_doctor_envelope_reduction_all_required_skip_is_degraded(self) -> None:
        """All probes required+skipped → DEGRADED, never PASS.

        Regression guard for the false-green footgun: a run where every probe
        is skipped must not return PASS (that would declare health without evidence).
        """
        results: List[ProbeResult] = [
            _pr("p.skip1", PASS, required=True, skipped=True),
            _pr("p.skip2", PASS, required=True, skipped=True),
        ]
        overall = reduce_overall(results)
        assert overall == DEGRADED, (
            f"all-required-SKIP must be DEGRADED, not {overall!r}"
        )

    def test_doctor_envelope_reduction_optional_skip_advisory_not_degraded(self) -> None:
        """optional-probe with skipped=True is EXCLUDED from worst-of (advisory only).

        An expected absence (coordinator-claude absent → shim absent → key not needed)
        must NOT count as DEGRADED.  It is surfaced in warnings/missing_optional only.
        """
        results: List[ProbeResult] = [
            _pr("p.pass", PASS),
            _pr("p.opt_skip", PASS, required=False, skipped=True),
        ]
        overall = reduce_overall(results)
        assert overall == PASS, (
            f"optional-SKIP must not contribute to worst-of (got {overall!r}); "
            "it is advisory — coordinator-absent installs must not degrade to DEGRADED"
        )

    def test_doctor_envelope_reduction_only_optional_skip_is_info(self) -> None:
        """All probes are optional+skipped → overall INFO (no health confirmed, not PASS)."""
        results: List[ProbeResult] = [
            _pr("p.opt1", PASS, required=False, skipped=True),
            _pr("p.opt2", PASS, required=False, skipped=True),
        ]
        overall = reduce_overall(results)
        assert overall == INFO, (
            f"all-optional-SKIP → no health confirmed → INFO, not {overall!r}"
        )

    def test_doctor_envelope_reduction_all_info(self) -> None:
        """All-INFO results → overall INFO, never PASS.

        INFO is a neutral observation, not a health verdict.  An all-INFO run has
        not confirmed health, so returning PASS would be a semantic lie.
        """
        results: List[ProbeResult] = [
            _pr("p.info1", INFO),
            _pr("p.info2", INFO),
        ]
        overall = reduce_overall(results)
        assert overall == INFO, (
            f"all-INFO must not reduce to PASS (got {overall!r}); "
            "INFO is advisory, not a health confirmation"
        )

    def test_doctor_envelope_reduction_empty_list(self) -> None:
        """Empty results list → overall INFO (edge case: no evidence = no health claim)."""
        assert reduce_overall([]) == INFO

    def test_doctor_envelope_reduction_info_excluded_from_worst_of(self) -> None:
        """INFO probes do NOT participate in worst-of — PASS wins when only INFO and PASS."""
        results: List[ProbeResult] = [
            _pr("p.pass", PASS),
            _pr("p.info", INFO),
        ]
        assert reduce_overall(results) == PASS

    # build_envelope shape tests

    def test_doctor_envelope_reduction_envelope_schema_version(self) -> None:
        """build_envelope() emits schema_version=1 and status_vocab with all four values."""
        env = build_envelope([_pr("p.pass", PASS)])
        assert env["schema_version"] == 1
        assert set(env["status_vocab"]) == {BROKEN, DEGRADED, INFO, PASS}

    def test_doctor_envelope_reduction_required_skip_shown_as_degraded_in_probes(self) -> None:
        """Required+skipped probe appears as DEGRADED in probes[] (matches its reduction)."""
        results = [_pr("p.req_skip", PASS, required=True, skipped=True)]
        env = build_envelope(results)
        probe_row = env["probes"][0]
        assert probe_row["probe"] == "p.req_skip"
        assert probe_row["status"] == DEGRADED, (
            f"Required+skipped probe must appear as DEGRADED in probes[], got {probe_row['status']!r}"
        )

    def test_doctor_envelope_reduction_optional_skip_shown_as_skip_in_probes(self) -> None:
        """Optional+skipped probe appears as 'SKIP' in probes[] and in missing_optional."""
        results = [_pr("p.opt_skip", PASS, required=False, skipped=True)]
        env = build_envelope(results)
        probe_row = env["probes"][0]
        assert probe_row["status"] == "SKIP", (
            f"Optional+skipped probe must appear as 'SKIP' in probes[], got {probe_row['status']!r}"
        )
        assert "p.opt_skip" in env["missing_optional"]
        assert any("p.opt_skip" in w for w in env["warnings"]), (
            "Optional+skipped probe must appear in warnings list"
        )



