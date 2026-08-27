"""
coordinator_core.doctor_envelope — shared probe-verdict envelope schema and reducer.

Purpose: Defines the closed verdict enum (BROKEN | DEGRADED | INFO | PASS), the
ProbeResult dataclass, the worst-of reduce_overall() reducer, and build_envelope()
which emits the JSON-serialisable envelope consumed by both the Tier-1 out-of-band
liveness probe (bin/) and the Tier-2 in-engine health op (coordinator_core.ops.health).

The two doctor tiers share this single source so the envelope shape is authoritative
and consistent regardless of which transport delivers the result.

Negative-spec:
    This module is the probe-VERDICT envelope for the claude-klabauter two-tier doctor.  It is
    entirely unrelated to coordinator_core.hooks._envelope, which is the hook-PAYLOAD
    domain (PreToolUse/PostToolUse return shapes).  Do NOT import or extend that module
    from here, and do NOT import this module from there.

    Also unrelated to coordinator_core.plugin_health — that package is a DIFFERENT
    "doctor"/"health" domain: read-only drift/currency probes for DoE-claude's OWN
    plugin ecosystem (live-install git-state, venv-state, sentinel/scan health).
    It does NOT use this envelope schema (ProbeResult/BROKEN|DEGRADED|INFO|PASS)
    and is not a third tier
    of claude-klabauter's own two-tier doctor. Do NOT import either module from the other.

    stdlib-only: no third-party imports anywhere in this module.

Spec backlink: pln-claude-klabauter-install-doctor-system-f-537d61 § C0
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Closed verdict enum — string constants
# ---------------------------------------------------------------------------

BROKEN: str = "BROKEN"
"""Hard failure — the probe found a condition that prevents correct operation."""

DEGRADED: str = "DEGRADED"
"""Soft failure — the probe found a condition that impairs but does not halt operation."""

INFO: str = "INFO"
"""Neutral observation — no health claim, excluded from worst-of reduction.

An all-INFO result is not a health confirmation; the overall verdict is INFO, not PASS.
"""

PASS: str = "PASS"
"""Healthy — the probe confirmed the condition is correct."""

#: Tuple of the four closed-enum verdict values, exported in the envelope ``status_vocab``.
#: This is the authoritative set; do not extend it without bumping ``ENVELOPE_SCHEMA_VERSION``.
STATUS_VOCAB: tuple[str, ...] = (BROKEN, DEGRADED, INFO, PASS)

#: Schema version for the envelope dict emitted by build_envelope().
#: Bump when the probe-row or top-level envelope shape changes in a breaking way.
ENVELOPE_SCHEMA_VERSION: int = 1

# Synthetic sentinel written into probes[] for a skipped probe in the output.
# NOT in STATUS_VOCAB — it carries no health verdict and is never a worst-of contributor.
_SKIP_SENTINEL: str = "SKIP"

# Worst-of rank table (higher = worse).  INFO and _SKIP_SENTINEL are intentionally absent.
_RANK: Dict[str, int] = {BROKEN: 3, DEGRADED: 2, PASS: 1}


# ---------------------------------------------------------------------------
# ProbeResult dataclass
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """Result emitted by a single doctor probe.

    Purpose: Typed carrier for a probe verdict.  Both Tier-1 (bin/ liveness probe)
    and Tier-2 (coordinator_core.ops.health) produce lists of ProbeResult; the shared
    reduce_overall() and build_envelope() consume them.

    Fields:
        probe:       Unique probe identifier (matches the ``id`` field in
                     skills/doctor/doctor-probes.toml).
        status:      Verdict from STATUS_VOCAB (BROKEN | DEGRADED | INFO | PASS).
                     When skipped=True, the status field is semantically undefined;
                     callers should set it to the worst-case they would expect if the
                     probe had run, or leave it as PASS (harmless — the skipped flag
                     overrides the worst-of path regardless of the stored status value).
        detail:      Human-readable finding or skip rationale (surfaced verbatim to the
                     operator in the envelope and in --step-zero NDJSON output).
        remediation: Corrective action string, or "—" if none is required.
        data:        Optional structured payload.  Downstream callers MUST treat this
                     field as optional — it is absent from many probes and must never be
                     assumed present.
        required:    True when the probe is considered required (default).  A required
                     probe that is skipped reduces as DEGRADED (never PASS — no false
                     green).  False for coordinator-dependent or optional probes (e.g.
                     shim liveness, tools/list) whose absence when coordinator-claude is
                     not installed is expected and advisory, not a failure.
        skipped:     True when the probe did not run (e.g. coordinator absent for an
                     optional probe, or a hard prerequisite failed for a required one).
                     Overrides the worst-of path per the required/optional split described
                     in reduce_overall().

    Spec backlink: pln-claude-klabauter-install-doctor-system-f-537d61 § C0
    """

    probe: str
    status: str
    detail: str
    remediation: str
    data: Optional[Dict[str, Any]] = None
    required: bool = True
    skipped: bool = False


# ---------------------------------------------------------------------------
# Verdict reducer
# ---------------------------------------------------------------------------


def reduce_overall(results: List[ProbeResult]) -> str:
    """Reduce a list of ProbeResult instances to a single overall verdict.

    Purpose: Implements the fleet's canonical worst-of reduction with probe-class
    awareness for skipped probes.  Both doctor tiers call this before emitting their
    envelope; build_envelope() calls it internally as well.

    Reduction rules (applied in this order):
        1. REQUIRED probe with skipped=True  → treated as DEGRADED in worst-of.
           Rationale: a required check that could not run is not a confirmation of
           health — no false green.
        2. OPTIONAL probe with skipped=True  → excluded from worst-of entirely.
           Surfaced by build_envelope() in warnings / missing_optional.  DEGRADED is
           reserved for genuine failures with coordinator present; an expected absence
           must not alarm.
        3. INFO status (non-skipped probe)   → excluded from worst-of.
           INFO is a neutral observation, not a health verdict.
        4. All remaining probes              → worst-of: BROKEN > DEGRADED > PASS.
        5. Edge cases:
           - All contributors are INFO       → overall is INFO (not PASS — an all-INFO
             run has not confirmed health; PASS would be a semantic lie).
           - Empty results list              → overall is INFO.

    Args:
        results: List of ProbeResult instances from one or both doctor tiers.

    Returns:
        One of the STATUS_VOCAB strings: BROKEN, DEGRADED, INFO, or PASS.

    Spec backlink: pln-claude-klabauter-install-doctor-system-f-537d61 § C0
    """
    best: Optional[str] = None

    for r in results:
        if r.skipped:
            if r.required:
                # Required probe that was skipped → counts as DEGRADED (no false green).
                s = DEGRADED
            else:
                # Optional probe that was skipped → advisory only; excluded from worst-of.
                continue
        else:
            s = r.status
            if s == INFO:
                # INFO is excluded from worst-of — neutral observation, not a health claim.
                continue

        rank = _RANK.get(s, 0)
        if best is None or rank > _RANK.get(best, 0):
            best = s

    if best is None:
        # All contributors were INFO, all probes were optional-skipped, or the list was
        # empty — no health confirmation was made; return INFO (not PASS).
        return INFO

    return best


# ---------------------------------------------------------------------------
# Envelope builder
# ---------------------------------------------------------------------------


def build_envelope(results: List[ProbeResult]) -> Dict[str, Any]:
    """Build the JSON-serialisable doctor verdict envelope from a list of probe results.

    Purpose: Single source for the shared envelope shape consumed by both doctor tiers
    (bin/ Tier-1 liveness probe and coordinator_core.ops.health Tier-2) and by the
    skills/doctor/ agentic layer that composes them.

    Envelope shape:
        schema_version:   int        — ENVELOPE_SCHEMA_VERSION; bump on breaking changes.
        status_vocab:     list[str]  — the four closed-enum verdict strings (STATUS_VOCAB).
        overall:          str        — worst-of result from reduce_overall(results).
        probes:           list[dict] — one entry per ProbeResult:
            probe:        str        — probe id.
            status:       str        — verdict (STATUS_VOCAB member) or "SKIP" for
                                       skipped probes (REQUIRED skipped probes are shown
                                       as DEGRADED because that is how they reduce).
            detail:       str        — human-readable finding or skip rationale.
            remediation:  str        — corrective action or "—".
            data:         dict|None  — optional structured payload; absent from many probes.
        warnings:         list[str]  — advisory strings for skipped optional probes.
        missing_optional: list[str]  — probe IDs of optional probes that were skipped.

    Skipped-probe representation in probes[]:
        - REQUIRED + skipped  → status "DEGRADED" (matches its worst-of contribution).
        - OPTIONAL + skipped  → status "SKIP" (advisory sentinel; probe ID also appears
                                 in missing_optional and a notice appears in warnings).

    Args:
        results: List of ProbeResult instances, typically the union of Tier-1 and Tier-2
                 results (or either tier's results individually when composing is not yet
                 possible, e.g. when the channel is dead and only Tier-1 ran).

    Returns:
        A dict that is JSON-serialisable with the standard library json module.

    Spec backlink: pln-claude-klabauter-install-doctor-system-f-537d61 § C0
    """
    overall = reduce_overall(results)

    probe_rows: List[Dict[str, Any]] = []
    warnings: List[str] = []
    missing_optional: List[str] = []

    for r in results:
        if r.skipped:
            if r.required:
                # Required probe skipped → elevate to DEGRADED in the output row so the
                # displayed status matches what reduce_overall counted it as.
                probe_rows.append(
                    {
                        "probe": r.probe,
                        "status": DEGRADED,
                        "detail": r.detail,
                        "remediation": r.remediation,
                        "data": r.data,
                    }
                )
            else:
                # Optional probe skipped → advisory surface; use _SKIP_SENTINEL in
                # the row so readers can distinguish an advisory skip from a real PASS.
                missing_optional.append(r.probe)
                warnings.append(
                    f"optional probe skipped (coordinator-dependent or prerequisite absent):"
                    f" {r.probe} — {r.detail}"
                )
                probe_rows.append(
                    {
                        "probe": r.probe,
                        "status": _SKIP_SENTINEL,
                        "detail": r.detail,
                        "remediation": r.remediation,
                        "data": r.data,
                    }
                )
        else:
            probe_rows.append(
                {
                    "probe": r.probe,
                    "status": r.status,
                    "detail": r.detail,
                    "remediation": r.remediation,
                    "data": r.data,
                }
            )

    return {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "status_vocab": list(STATUS_VOCAB),
        "overall": overall,
        "probes": probe_rows,
        "warnings": warnings,
        "missing_optional": missing_optional,
    }
