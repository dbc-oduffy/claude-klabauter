"""coordinator_core.bash_guards._firing_shape -- the mechanism half of the
firing-shape gate: "should this emitter have fired, and does it carry what
Axis A requires."

``_alternative_liveness.py`` answers "is the named alternative executable".
Nothing in this package answered the prior question -- three shipped
emitters (a prose-only advisory naming no applicable alternative, a
byte-identical repeated report, a dispatch brief handing the agent a literal
``[TODO: ...]`` placeholder) were invisible to every existing gate, at any
severity, because none of them measured firing SHAPE at all.

This module is the mechanism (a declared-class registry plus the
mechanically-decidable checks that follow from each class);
``tests/test_firing_shape_gate.py`` is the gate that drives it. Mirrors the
mechanism-module/test-module split ``_alternative_liveness.py`` +
``tests/test_alternative_liveness_gate.py`` already establishes as
precedent in this package.

===========================================================================
Declare-then-check, not infer-then-classify
===========================================================================
An earlier design classified each emission as ASK or REPORT by pattern-
matching its rendered prose. That is judgment dressed as an oracle: a regex
over emitted text both false-positives and is defeated by rewording, and
"names a concrete alternative that APPLIES to the triggering input" is
semantic entailment -- not a decidable predicate. This module inverts the
direction: each emitter's firing class is DECLARED, at construction time, in
the ``REGISTRY`` table below -- a new emitter picking a class when it is
added is the drift-visibility this gate exists to create, per the EM
decision realizing this as "a registry table keyed by emitter" (the plan's
own offered alternative shape) rather than editing every ``_advisory()``/
``deny()`` call site across the emitter modules, which stay untouched by
this chunk.

The two-axis firing predicate this module operationalizes (coordinator-claude's contract,
adopted verbatim, docs/plans/2026-08-01-advisory-firing-shape-predicate.md):

- **Axis A (obligation).** Any emission that asks the agent to change or
  reconsider the triggering action must name a concrete alternative -- deny
  and advisory alike, no exemption. An emission that asks nothing is a
  REPORT, legal without an alternative only if it carries state the agent
  could not already know AND does not repeat with substantively identical
  text.
- **Axis B (bypass).** Withholding the override incantation is orthogonal to
  Axis A and stays legal -- sometimes required (the sentinel guards'
  misdirection carve-out). This module never grades Axis B; a withheld
  override is never, by itself, a firing-shape violation.

Enforcing Axis A means distinguishing *"no alternative because none
applies"* (the sentinel guards, a deliberate, declared policy exemption)
from *"no alternative because none was written"* (the three defects this
gate closes). ``EmitterSpec.alternative_required=False`` is that
declaration -- the registry's realization of the plan's own carve-out
language, not a third firing class: every registry row is still exactly
ASK or REPORT.

===========================================================================
NEGATIVE-SPEC -- what this gate deliberately does NOT do
===========================================================================
- Does NOT check that a named alternative APPLIES to the triggering input.
  That claim is semantic entailment and was dropped on purpose -- an
  overclaiming gate gets trusted for a property it cannot actually verify.
  This module checks only the MECHANICALLY DECIDABLE consequence of a
  declared class: presence of an alternative-shaped signal (reusing
  ``_alternative_liveness.extract_alternatives`` rather than
  re-implementing alternative detection), that named alternative's liveness
  (reusing ``_alternative_liveness.probe_alternative``/``probe_command``/
  ``probe_flag``/etc.), latching/repetition state for REPORT-class
  emitters, and literal ``[TODO:``/placeholder text.
- Does NOT grade Axis B. A withheld ``COORDINATOR_ALLOW_*``/``OVERRIDE_*``
  incantation is never itself a violation -- see Axis B above.
- Does NOT infer an emitter's class from its rendered prose. The class is
  read from ``REGISTRY``, never guessed from message shape -- this is the
  declare-then-check inversion the module docstring's first section names.
- Does NOT build on a single-run assumption. ``_alternative_liveness.py``'s
  own module docstring records a documented determinism hazard (concurrent-
  edit sibling imports can make one run observe a guard mid-edit); this
  module's own live-corpus ratchet (``LIVE_VIOLATION_CHECKS``) is asserted
  by its test suite across N consecutive runs for an identical verdict set,
  never trusted off one run (AC11).
- Does NOT amnesty a fixed emitter forever. ``KNOWN_VIOLATIONS`` is a frozen
  set whose members must each still evaluate as violating (a fixed emitter
  fails the gate until delisted, never stays silently green), and whose
  membership COUNT is asserted so growth is a visible diff, never a silent
  append.

===========================================================================
Design-as-offers
===========================================================================
This gate operationalizes the design-as-offers doctrine (coordinator-claude
``coordinator/docs/wiki/eager-agent-calibration.md``: "Design agent-facing
tooling as offers, not nags... lead with the better alternative, not the
violation") as an ENFORCEMENT surface: Axis A is exactly "does this emission
lead with an offer" made mechanically checkable, and the report clause is
exactly "or does it at least avoid nagging with the same unchanged text
forever" made mechanically checkable.

Spec backlink: pln-advisory-firing-shape-predicat-802b35 (C6)
Sibling: ``_alternative_liveness.py`` (alternative liveness, reused here);
``tests/test_firing_shape_gate.py`` (the gate).
"""

from __future__ import annotations

import enum
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from coordinator_core.bash_guards import _alternative_liveness as altlive
from coordinator_core.bash_guards import dispatch_checks as _dc
from coordinator_core.bash_guards import guard_inprocess_search as _gis


# ---------------------------------------------------------------------------
# (a) The declaration surface -- a registry table keyed by emitter, per the
# EM decision (2026-08-01, execute-plan) realizing "each emitter DECLARES
# its class" this way rather than by editing call sites across the emitter
# modules. A NEW emitter is expected to add its own row here.
# ---------------------------------------------------------------------------


class FiringClass(enum.Enum):
    """Exactly the two classes Axis A's contract distinguishes. Never a
    third value -- the sentinel guards' zero-alternative legality is
    realized as ``EmitterSpec.alternative_required=False`` on an ASK row,
    not as a separate class (see module docstring)."""

    ASK = "ask"
    REPORT = "report"


@dataclass(frozen=True)
class EmitterSpec:
    name: str
    cls: FiringClass
    #: ASK-class only. False marks a DECLARED, reviewed policy exemption
    #: ("no alternative because none applies") -- never set to silence a
    #: real prose-only-ask defect ("no alternative because none was
    #: written"). Meaningless for REPORT rows (repetition is the axis that
    #: matters there, not alternative presence).
    alternative_required: bool = True
    notes: str = ""


#: Every emitter this chunk's ratchet covers. Deliberately the items this
#: plan's C1a/C3/C4/C5/C7 fix chunks touched plus the two canonical
#: legal-zero-alternative sentinel guards named explicitly in this chunk's
#: own brief -- not a corpus-wide sweep (that is D1, deferred and
#: PM-ungated).
REGISTRY: Dict[str, EmitterSpec] = {
    "check_multiprobe_banner_rewrite": EmitterSpec(
        name="check_multiprobe_banner_rewrite",
        cls=FiringClass.ASK,
        alternative_required=True,
        notes=(
            "C4 (item 1): offers a concrete python3 -c rewrite when every "
            "probe segment is recognized, or emits NOTHING (None) when one "
            "is not -- never a prose-only advisory naming no applicable "
            "alternative. A fired envelope with no updatedInput/backtick "
            "alternative is the pre-fix defect shape."
        ),
    ),
    "guard_inprocess_search": EmitterSpec(
        name="guard_inprocess_search",
        cls=FiringClass.REPORT,
        notes=(
            "C3 (item 2): a report of already-answered state, legal under "
            "Axis A with no alternative -- but its explanatory paragraph "
            "must not repeat byte-identical across consecutive answered "
            "calls in one session (the report clause). Latched to a "
            "one-line invariant marker on repeat."
        ),
    ),
    "nudge_em_code_dispatch_brief": EmitterSpec(
        name="nudge_em_code_dispatch_brief",
        cls=FiringClass.ASK,
        alternative_required=True,
        notes=(
            "C5 (item 3): the pre-assembled executor dispatch brief asks "
            "the agent to dispatch per its own content -- a literal "
            "'[TODO: ...]' placeholder in that content names no concrete "
            "alternative, which is the same Axis-A defect as item 1's "
            "prose-only ask, checked here via TODO_PLACEHOLDER_RE rather "
            "than alternative-extraction (the brief is plain text, not a "
            "hookSpecificOutput envelope)."
        ),
    ),
    "block_worktree_sentinel_creation": EmitterSpec(
        name="block_worktree_sentinel_creation",
        cls=FiringClass.ASK,
        alternative_required=False,
        notes=(
            "Canonical legal-zero-alternative case (this chunk's own "
            "brief, and _alternative_liveness.py's negative-spec): a pure "
            "policy deny with a deliberate misdirection carve-out -- "
            "naming no alternative is the considered behavior, not an "
            "omission."
        ),
    ),
    "block_approval_sentinel_creation": EmitterSpec(
        name="block_approval_sentinel_creation",
        cls=FiringClass.ASK,
        alternative_required=False,
        notes="Same shape as block_worktree_sentinel_creation above.",
    ),
}


# ---------------------------------------------------------------------------
# (b) ASK-class evaluation -- the mechanically decidable consequence of
# Axis A's obligation clause: an alternative-shaped signal must be present
# (unless declared exempt), it must classify (reusing extract_alternatives
# rather than re-implementing alternative detection), and no literal
# "[TODO:" placeholder may stand in for one.
# ---------------------------------------------------------------------------

TODO_PLACEHOLDER_RE = re.compile(r"\[TODO:")


@dataclass
class AskEvaluation:
    fired: bool
    violated: bool
    reasons: List[str] = field(default_factory=list)


def evaluate_ask_hso(hso: Optional[Dict[str, Any]], *, alternative_required: bool = True) -> AskEvaluation:
    """Evaluate one fired ASK-class ``hookSpecificOutput`` envelope.

    ``hso is None`` (the emitter chose not to fire at all -- e.g. C4's exit
    (b) on an unrecognized probe segment) is NOT a violation: asking
    nothing is never a prose-only-ask defect, it is simply not asking.
    """
    if hso is None:
        return AskEvaluation(fired=False, violated=False)

    text = hso.get("permissionDecisionReason") or hso.get("additionalContext") or ""
    reasons: List[str] = []

    if TODO_PLACEHOLDER_RE.search(text):
        reasons.append("emitted text carries a literal '[TODO:' placeholder in place of a concrete alternative")

    updated = hso.get("updatedInput")
    has_updated_input = isinstance(updated, dict) and bool(updated.get("command"))

    try:
        alts = altlive.extract_alternatives(hso)
    except altlive.UnclassifiableAlternative as exc:
        reasons.append("alternative-shaped cue phrase present but unclassifiable: %s" % exc)
        alts = []

    # Axis B (a withheld/offered override incantation) is orthogonal to
    # Axis A and never satisfies it on its own -- an OVERRIDE-kind
    # extraction is a bypass, not an offered course of action, so it is
    # excluded here. Without this exclusion, item 1's pre-fix message
    # (which names no genuine alternative but DOES embed its
    # `COORDINATOR_ALLOW_MULTIPROBE_BANNER` override note) would have
    # wrongly graded as satisfying Axis A -- exactly the "syntactically
    # compliant, substantively nonsense" shape the plan's own Problem
    # section names.
    non_override_alts = [a for a in alts if a.kind is not altlive.AlternativeKind.OVERRIDE]

    if alternative_required and not non_override_alts and not has_updated_input:
        reasons.append("ASK-class emission names no alternative-shaped signal (prose-only ask)")

    return AskEvaluation(fired=True, violated=bool(reasons), reasons=reasons)


def evaluate_ask_text(text: str, *, alternative_required: bool = True) -> AskEvaluation:
    """Evaluate a plain-text ASK-class emission that is not itself a
    ``hookSpecificOutput`` envelope (e.g. ``nudge_em_code_dispatch``'s
    dispatch-brief string). Only the TODO-placeholder check applies --
    alternative-shaped-signal extraction is envelope-specific
    (``permissionDecisionReason``/``additionalContext``/``updatedInput``)
    and does not generalize to an arbitrary brief string, so this path
    checks the one property that DOES generalize: no placeholder standing
    in for a concrete instruction."""
    reasons: List[str] = []
    if TODO_PLACEHOLDER_RE.search(text):
        reasons.append("emitted text carries a literal '[TODO:' placeholder in place of a concrete alternative")
    return AskEvaluation(fired=True, violated=bool(reasons), reasons=reasons)


# ---------------------------------------------------------------------------
# (c) REPORT-class evaluation -- the mechanically decidable consequence of
# the report clause: two consecutive firings within one session must not be
# byte-identical.
# ---------------------------------------------------------------------------


def evaluate_report_repetition(first_text: str, second_text: str) -> Tuple[bool, str]:
    """True (violated) iff two consecutive REPORT-class firings in the same
    session render byte-identical text -- no already-handled/new-state
    signal to distinguish call #2 from call #1."""
    if first_text == second_text:
        return True, "REPORT-class emission repeats byte-identical text on a subsequent firing in the same session"
    return False, ""


# ---------------------------------------------------------------------------
# (d) Live-corpus triggers -- one hermetic, no-checkout-required callable
# per registered emitter this ratchet actually re-fires against the
# SHIPPED, current code (never a frozen fixture -- fixtures are the pre-fix
# demonstration's job in the test module, not this ratchet's).
# ---------------------------------------------------------------------------


def _trigger_multiprobe_banner_still_violates() -> bool:
    """Re-fire ``check_multiprobe_banner_rewrite`` on the live-confirmed
    unrecognized-segment shape (two ``sed`` segments and a bare ``echo``,
    no probe shape ``_bt_probe_segment_kind`` recognizes) and evaluate the
    result as ASK-class. Post-C4 this returns ``None`` (silence, not a
    violation) -- the row exists so a REGRESSION back to the pre-fix
    prose-only advisory fails loud."""
    envelope = _dc.check_multiprobe_banner_rewrite(
        "sed -n 1,5p a.py; echo ===; sed -n 6,9p b.py", "firing-shape-ratchet-probe"
    )
    hso = envelope.get("hookSpecificOutput") if envelope else None
    spec = REGISTRY["check_multiprobe_banner_rewrite"]
    return evaluate_ask_hso(hso, alternative_required=spec.alternative_required).violated


def _trigger_inprocess_search_still_violates() -> bool:
    """Fire ``guard_inprocess_search._footer()`` twice in one synthetic
    session and evaluate REPORT-class repetition. Hermetic: a bare ``.git``
    directory (no real git commands -- ``_repo_root_from_cwd`` only checks
    for its existence) is all the latch path resolution needs."""
    tmp = tempfile.mkdtemp(prefix="firing-shape-inprocess-search-")
    try:
        os.makedirs(os.path.join(tmp, ".git"), exist_ok=True)
        sid_var = _gis._SESSION_ID_ENV_VAR
        old_sid = os.environ.get(sid_var)
        os.environ[sid_var] = "firing-shape-ratchet-session"
        try:
            first = _gis._footer(tmp)
            second = _gis._footer(tmp)
        finally:
            if old_sid is None:
                os.environ.pop(sid_var, None)
            else:
                os.environ[sid_var] = old_sid
        violated, _reason = evaluate_report_repetition(first, second)
        return violated
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _trigger_nudge_dispatch_brief_still_violates() -> bool:
    """Build a real dispatch brief via the shipped ``_build_dispatch_brief``
    + ``_describe_edit`` and evaluate it for a literal TODO placeholder."""
    from coordinator_core.hooks import nudge_em_code_dispatch as _nudge

    edit_description = _nudge._describe_edit(
        {"tool_name": "Edit", "tool_input": {"old_string": "a", "new_string": "b"}}
    )
    brief_text = _nudge._build_dispatch_brief("some/file.py", "generic-executor", edit_description)
    spec = REGISTRY["nudge_em_code_dispatch_brief"]
    return evaluate_ask_text(brief_text, alternative_required=spec.alternative_required).violated


def _trigger_worktree_sentinel_still_violates() -> bool:
    """Re-fire the real, registered ``_alternative_liveness`` trigger for
    this guard (reused rather than re-derived) and evaluate as ASK-class
    with the declared zero-alternative exemption -- this row exists to
    prove the legal-zero-alternative carve-out never fails the gate, so it
    is expected to always evaluate ``False`` (not violated)."""
    envelope = altlive.LIVE_TRIGGERS["block_worktree_sentinel_creation"]()
    hso = envelope.get("hookSpecificOutput") if envelope else None
    spec = REGISTRY["block_worktree_sentinel_creation"]
    return evaluate_ask_hso(hso, alternative_required=spec.alternative_required).violated


def _trigger_approval_sentinel_still_violates() -> bool:
    """Same shape as ``_trigger_worktree_sentinel_still_violates`` above,
    for ``block_approval_sentinel_creation``."""
    envelope = altlive.LIVE_TRIGGERS["block_approval_sentinel_creation"]()
    hso = envelope.get("hookSpecificOutput") if envelope else None
    spec = REGISTRY["block_approval_sentinel_creation"]
    return evaluate_ask_hso(hso, alternative_required=spec.alternative_required).violated


#: One hermetic, re-runnable "does this emitter still violate its declared
#: class" callable per ``REGISTRY`` row -- the ratchet's live-fire half.
#: Every ``REGISTRY`` key MUST appear here (checked by the gate) so a new
#: registry row can never silently escape ratchet coverage.
LIVE_VIOLATION_CHECKS: Dict[str, Callable[[], bool]] = {
    "check_multiprobe_banner_rewrite": _trigger_multiprobe_banner_still_violates,
    "guard_inprocess_search": _trigger_inprocess_search_still_violates,
    "nudge_em_code_dispatch_brief": _trigger_nudge_dispatch_brief_still_violates,
    "block_worktree_sentinel_creation": _trigger_worktree_sentinel_still_violates,
    "block_approval_sentinel_creation": _trigger_approval_sentinel_still_violates,
}


# ---------------------------------------------------------------------------
# (e) The ratchet -- monotone and self-liquidating. Every fix chunk in this
# plan (C1a, C2, C3, C4, C5, C7) landed BEFORE this chunk was authored (the
# EM's deliberate sequencing decision, execute-plan Execution Notes): the
# known-violations list below is authored against the CURRENT, post-fix
# corpus, so it is empty by construction -- not amnesty, correctness. Both
# ratchet gaps the plan names are still closed structurally: (a) a fixed
# emitter re-listed here would fail the gate until delisted (see
# ``test_known_violations_still_violate`` in the test module, which
# iterates this exact set and re-fires each via ``LIVE_VIOLATION_CHECKS``);
# (b) the set's membership COUNT is asserted so growth is a visible diff,
# never a silent append.
# ---------------------------------------------------------------------------

KNOWN_VIOLATIONS: frozenset = frozenset()
