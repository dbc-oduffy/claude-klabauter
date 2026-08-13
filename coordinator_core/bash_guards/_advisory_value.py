"""coordinator_core.bash_guards._advisory_value -- per-guard OS-aware
advisory classification and the emit-time suppression predicate, wired
into ``dispatch.evaluate_payload_json``'s loop by H4
(docs/plans/2026-07-30-os-aware-guard-advisory-defaults.md).

Own module, not appended to ``dispatch.py``, so ``_blanket_disarm.py`` can
import it without the circular-import problem it already has to dodge for
``GuardBand`` (``_blanket_disarm.py`` imports ``GuardBand`` FROM
``dispatch.py`` at its own module top level; ``dispatch.py`` in turn needs
``AdvisoryValue`` for ``GuardEntry``'s ``advisory_value`` field). Putting
``AdvisoryValue`` here, in a module that imports nothing from ``dispatch``,
means ``dispatch.py`` -> ``_advisory_value.py`` is one-directional -- no new
deferred-import workaround is needed the way ``_blanket_disarm.py`` needs one
for ``GuardBand``.

Spec backlink: docs/plans/2026-07-30-os-aware-guard-advisory-defaults.md
(coordinator-claude) row H1.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional


class AdvisoryValue(Enum):
    """Per-guard classification of whether an advisory's argument is
    Windows-spawn-cost-only, holds regardless of host, was never a
    spawn-cost argument at all, or is unclassified.

    ``UNCLASSIFIED`` is a REAL member, never ``None`` -- a validation test
    can name what it found on an unclassified registration rather than
    distinguishing "None" from "some sentinel" from "field absent". A
    ``dispatch.GuardEntry`` with no explicit ``advisory_value`` defaults to
    this member (AC-4: a newly-added, unclassified guard renders SHOWN);
    the registry-validation test (H5(a)) fails loud the moment any
    REGISTERED guard carries it (AC-1). Those two requirements are only
    jointly satisfiable with a defaulted dataclass field plus a separate
    test -- do not collapse them into a single check.
    """

    WINDOWS_COST_ONLY = "windows_cost_only"
    HOST_INDEPENDENT = "host_independent"
    NOT_COST_ARGUED = "not_cost_argued"
    UNCLASSIFIED = "unclassified"


def suppress_advisory(
    envelope: Optional[Dict[str, Any]],
    *,
    advisory_value: "AdvisoryValue",
    band: Any,
    host_is_windows: bool,
) -> bool:
    """True when ``envelope`` (a guard's already-computed, non-``None``
    verdict) is eligible for host-default suppression at emit time.

    Wired by H4 (docs/plans/2026-07-30-os-aware-guard-advisory-defaults.md)
    from inside ``dispatch.evaluate_payload_json``'s loop, AFTER the guard
    has been called (D-2: run-then-drop, never skip) and OUTSIDE that
    loop's per-guard ``try/except`` -- an exception raised here would crash
    ``evaluate_payload_json`` itself, denying every Bash call in every live
    session. This function is therefore TOTAL BY CONSTRUCTION: every branch
    below reads with ``.get()``/``getattr`` and an ``isinstance`` guard,
    never a bare subscript, so it cannot raise on ANY input shape,
    including the two reachable envelopes that carry no
    ``hookSpecificOutput`` key at all -- ``guard_offer_git_c.py``'s bare
    ``{}`` return, and a hand-written test fixture's arbitrary marker dict.

    Negative-spec, load-bearing (AC-5): returns ``False`` for
    ``GuardBand.CONFINEMENT_DENY`` UNCONDITIONALLY, before reading
    ``advisory_value`` at all. No member of ``AdvisoryValue`` may ever
    suppress a confinement deny -- this is the first thing checked and it
    short-circuits every other branch, so a future ``AdvisoryValue`` member
    cannot accidentally acquire the ability to silence a confinement guard
    merely by being matched further down.

    ``band`` is compared by its string ``.value`` rather than by importing
    ``dispatch.GuardBand`` and doing an ``isinstance`` check, so this
    module never imports ``dispatch`` at all -- ``dispatch.py`` imports
    THIS module for ``GuardEntry.advisory_value``, so a top-level import
    back into ``dispatch`` would be a circular import.

    ALLOW-WHITELIST, NOT DENY-BLACKLIST (finding 2): the ONLY envelope this
    returns ``True`` for is one this function positively recognises as a
    pure allow verdict (``hookSpecificOutput.get("permissionDecision") ==
    "allow"``) from a ``WINDOWS_COST_ONLY`` guard on a non-Windows host.
    Anything else -- a deny, an unrecognised shape, ``{}``, a dict with no
    ``hookSpecificOutput`` key -- returns ``False`` (not-suppressed) rather
    than requiring this function to enumerate every deny spelling in the
    package (there are at least two -- ``_hook_envelope.deny()`` and
    ``dispatch_checks._deny()`` -- plus nine per-module inline literals).
    This is also what keeps a deny-capable ``ADVISORY_REWRITE`` guard
    (``offer-git-c`` rung B, ``validate-commit`` under
    ``COORDINATOR_SCOPE_STRICT=1``, ``offer-invoke-params-stdin`` on a
    multi-line command) safe even if a future reclassification ever moved
    one of them onto ``WINDOWS_COST_ONLY``: a deny envelope never satisfies
    the ``permissionDecision == "allow"`` check, so it is never suppressed
    regardless of the guard's classification.

    Callers: on ``True``, pass ``envelope`` to ``resolve_suppressed_envelope``
    to compute what to actually emit (leg-scoped -- an auto-rewrite's
    ``updatedInput`` leg survives even when its advisory ``additionalContext``
    is dropped; see that function's own docstring).
    """
    band_value = getattr(band, "value", band)
    if band_value == "confinement-deny":
        return False
    if host_is_windows:
        return False
    if advisory_value is not AdvisoryValue.WINDOWS_COST_ONLY:
        return False
    if not isinstance(envelope, dict):
        return False
    hsp = envelope.get("hookSpecificOutput")
    if not isinstance(hsp, dict):
        return False
    return hsp.get("permissionDecision") == "allow"


def resolve_suppressed_envelope(envelope: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Given an ``envelope`` for which ``suppress_advisory(...)`` returned
    ``True``, return what the dispatch loop should actually emit:

      - a copy of ``envelope`` with ``additionalContext`` stripped, IF
        ``updatedInput`` is also present (LEG-SCOPED suppression, H4
        finding 4) -- ``find-exec-rewrite`` and ``head-tail-plumbing-
        rewrite`` are auto-rewrites that cost the agent zero context;
        suppressing their whole envelope would also drop the free rewrite,
        making macOS/Linux and Windows execute DIFFERENT commands for
        identical agent input. Only the advisory prose leg is dropped; the
        rewrite leg ships silently.
      - ``None`` if no leg remains after stripping ``additionalContext``
        (a pure advisory, ``permissionDecision: allow`` +
        ``additionalContext`` with no ``updatedInput``) -- the caller
        treats this exactly as if the guard had returned ``None`` and
        continues the chain.

    Callers only reach this function after ``suppress_advisory`` has
    already confirmed ``envelope`` carries a well-shaped
    ``hookSpecificOutput`` dict with ``permissionDecision == "allow"``, but
    this function re-checks defensively (``.get()``, ``isinstance``) rather
    than trusting that precondition, so it stays safe to call in isolation
    (H5/H6 unit-exercise it directly).
    """
    hsp = envelope.get("hookSpecificOutput") if isinstance(envelope, dict) else None
    if not isinstance(hsp, dict):
        return None
    if "updatedInput" in hsp:
        stripped_hsp = {k: v for k, v in hsp.items() if k != "additionalContext"}
        return {**envelope, "hookSpecificOutput": stripped_hsp}
    return None
