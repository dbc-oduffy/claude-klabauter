"""
coordinator_core.roadmap.pm_gate_signal — detect-pm-gate-signal: consumes
DoE's ``pm-gate-signal-fragment.json`` rule and emits ADVISORY recommendation
candidates for ``state/roadmap/<run-id>/pm-gates.md``, never a decided row.

Purpose: ``roadmap-planning`` Step 2.5 (MIXED) needs a mechanical first pass
over each stub's gate-adjacent prose before the EM authors a ``pm-gates.md``
row by hand. This module is the mechanical half only -- it recommends
stub_ids into an advisory judgment point, and it never emits, decides, or
authors a gate question (fragment ``row_template.authored_by``: "the EM, not
the detector").

Fragment resolution: the rule text lives in DoE-claude
(``coordinator/contract/pm-gate-signal-fragment.json``), owned by DoE and
pinned here as a byte-identical vendored copy under
``coordinator_core/roadmap/fragments/pm-gate-signal-fragment.json`` -- the
same "read a JSON fragment off disk, parse it verbatim" shape
``coordinator_core.ops.review_mint.op.load_fragment`` uses for its supplied
roster fragment, but WITHOUT that function's live sibling-clone resolution:
``review_mint/op.py``'s module docstring names its own
``read_doe_root_pointer()`` call as the ONE sanctioned live cross-repo read
site in that plan's surface, and ``dispatch_emit/emit.py``'s docstring names
any OTHER live sibling-clone resolution as the defect class it exists to
avoid. This module neither resolves the sibling root at runtime nor accepts
an injected-caller-data dict (``dispatch_emit/emit.py``'s shape) -- it reads
its own vendored copy, verbatim, off disk. ``test_pm_gate_signal.py``'s
byte-identity test pins that copy against the DoE source, the same guarantee
``test_supplied_fragments.py`` (DoE-claude) gives the source side.

THREE DETECTION LEGS, ONE JUDGMENT LEG -- not four legs of the same kind
(fragment ``legs``; see also the fragment's companion
``pm-gate-signal-fragment.md``):
  - ``pm-prefix``: case-sensitive ``"PM "`` prefix match, high precision.
  - ``decision-approval-policy-scope``: case-insensitive token match, LOW
    precision by design (``policy``/``scope`` fire on ordinary prose) --
    never tuned out; a missed PM gate blocks a roadmap close at Audit 3, an
    over-fired candidate costs the author one dismissal.
  - ``user-facing``: case-insensitive token match, high precision.
  - ``named-stakeholder``: a JUDGMENT leg (``match_kind: "judgment"``,
    ``judgment_required: true``, ``surfaces_as: "judgment_point"``). This
    module implements ONLY its ``mechanical_floor`` (fleet-stable role-word
    term list) -- a stakeholder is frequently a peer TEAM ("coordinate with
    claude-klabauter-em") rather than a role word, and resolving who is
    actually on the hook takes fleet knowledge no term list holds. A floor
    match surfaces as a ``judgment_point`` candidate, never a decided
    yes/no; reporting this leg "done" from the floor alone closes a
    judgment call by omission (fragment ``never``: no peer-team/person
    roster, hardcoded or inferred). The floor matches on a WORD BOUNDARY
    (``_match_tokens_word_boundary``), not bare substring -- the fragment is
    silent on that choice for this leg (unlike
    ``decision-approval-policy-scope``, whose doc explicitly specifies
    substring prose matching), and a 2-char term like ``"PM"``/``"VP"``
    firing inside "3pm"/"campmate" would undercut the fragment's own
    "high-precision term lookup" claim for this leg.

Field handling: scans ``blocking_notes`` (current) and the deprecated
``gate_dependency`` (nothing authors new values into it, but the existing
corpus still carries it and skipping it misses live stubs) -- both per
fragment ``scanned_fields``. EXCLUDES ``blocked_by`` by name (fragment
``excluded_fields``): it holds resolvable slugs, not prose, and the
``decision-approval-policy-scope`` leg's "scope" token would fire on any
stub_id containing that substring.

Output shape: ``emits: "recommendation"``, ``exhaustive: false`` (fragment
top-level fields, echoed verbatim on every call's return, never hardcoded
here). Every candidate carries the stub_id, the legs that matched, and any
judgment_point raised by the floor -- an ADVISORY list for the EM to fold
into (or supplement past, per fragment/doc "a manual row is a normal
outcome") a hand-authored ``pm-gates.md``, never a written row or a closed
set.

Negative-spec:
  - Does NOT write ``state/roadmap/<run-id>/pm-gates.md`` or any other file
    -- detection only, per ``emits: "recommendation"``.
  - Does NOT decide the ``named-stakeholder`` judgment leg past its
    mechanical floor -- a floor match is offered as a ``judgment_point``,
    never resolved to a specific peer-team or person.
  - Does NOT scan ``blocked_by`` -- excluded by name, see
    ``excluded_fields``.
  - Does NOT resolve a live sibling-clone path at runtime -- reads its own
    vendored fragment copy only (contrast
    ``coordinator_core.ops.review_mint.op.load_fragment``).

Spec backlink: state/dispatch-briefs/2026-08-21-engine-half-of-the-roadmap-
sprint-spine-split/C8.md
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

_FRAGMENT_RELPATH = "fragments/pm-gate-signal-fragment.json"

# Per fragment `scanned_fields` -- `blocked_by` is deliberately absent (see
# module docstring / fragment `excluded_fields`).
_SCANNED_FIELDS = ("blocking_notes", "gate_dependency")


def load_fragment() -> dict:
    """Read and parse this module's vendored ``pm-gate-signal-fragment.json``,
    verbatim.

    Local, disk-only read of the copy vendored alongside this module -- never
    a live sibling-clone resolution (see module docstring). Raises
    ``FileNotFoundError`` if the vendored copy is missing.
    """
    fragment_path = Path(__file__).parent / _FRAGMENT_RELPATH
    if not fragment_path.is_file():
        raise FileNotFoundError(
            f"pm-gate-signal fragment not found at {fragment_path}"
        )
    return json.loads(fragment_path.read_text(encoding="utf-8"))


def _leg_by_id(fragment: dict, leg_id: str) -> Optional[dict]:
    for leg in fragment.get("legs", []):
        if leg.get("id") == leg_id:
            return leg
    return None


def _match_prefix(text: str, terms: List[str], case_sensitive: bool) -> bool:
    haystack = text if case_sensitive else text.lower()
    for term in terms:
        needle = term if case_sensitive else term.lower()
        if haystack.startswith(needle):
            return True
    return False


def _match_tokens(text: str, terms: List[str], case_sensitive: bool) -> List[str]:
    haystack = text if case_sensitive else text.lower()
    matched = []
    for term in terms:
        needle = term if case_sensitive else term.lower()
        if needle in haystack:
            matched.append(term)
    return matched


def _match_tokens_word_boundary(
    text: str, terms: List[str], case_sensitive: bool
) -> List[str]:
    """Same shape as :func:`_match_tokens`, but each term matches only on a
    word boundary rather than as a bare substring -- ``"PM"`` fires on "PM
    decision" but not inside "3pm" or "campmate". Reserved for the
    ``named-stakeholder`` mechanical floor (fragment ``mechanical_floor``):
    that leg's terms are short, fleet-stable role words for which the
    fragment's own doc claims "a high-precision term lookup", and the fragment
    is silent on substring-vs-boundary semantics -- unlike
    ``decision-approval-policy-scope``, which the fragment doc explicitly
    specifies as deliberately substring-based low-precision prose matching
    ("scope" firing inside "out of scope") and must keep using
    :func:`_match_tokens` unchanged.
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    matched = []
    for term in terms:
        pattern = r"\b" + re.escape(term) + r"\b"
        if re.search(pattern, text, flags=flags):
            matched.append(term)
    return matched


def detect_stub(fragment: dict, stub: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Apply every leg in ``fragment`` to one stub's frontmatter-shaped dict.

    ``stub`` carries ``stub_id`` plus the scanned fields (``blocking_notes``,
    ``gate_dependency``); ``blocked_by``, even if present on ``stub``, is
    never read (excluded by name, see module docstring). Returns a candidate
    dict when any detection leg fires OR the ``named-stakeholder`` floor
    fires, else ``None`` -- a batch caller filters the ``None``s out.
    """
    stub_id = stub.get("stub_id")
    scanned_text = " ".join(
        str(stub.get(field) or "") for field in _SCANNED_FIELDS
    )

    matched_legs: List[Dict[str, Any]] = []

    pm_prefix_leg = _leg_by_id(fragment, "pm-prefix")
    if pm_prefix_leg is not None:
        for field in _SCANNED_FIELDS:
            field_text = str(stub.get(field) or "")
            if _match_prefix(
                field_text,
                pm_prefix_leg.get("terms", []),
                pm_prefix_leg.get("case_sensitive", True),
            ):
                matched_legs.append({"leg": "pm-prefix", "field": field})
                break

    scope_leg = _leg_by_id(fragment, "decision-approval-policy-scope")
    if scope_leg is not None:
        matched_terms = _match_tokens(
            scanned_text,
            scope_leg.get("terms", []),
            scope_leg.get("case_sensitive", False),
        )
        if matched_terms:
            matched_legs.append(
                {
                    "leg": "decision-approval-policy-scope",
                    "matched_terms": matched_terms,
                }
            )

    user_facing_leg = _leg_by_id(fragment, "user-facing")
    if user_facing_leg is not None:
        matched_terms = _match_tokens(
            scanned_text,
            user_facing_leg.get("terms", []),
            user_facing_leg.get("case_sensitive", False),
        )
        if matched_terms:
            matched_legs.append(
                {"leg": "user-facing", "matched_terms": matched_terms}
            )

    judgment_point = None
    stakeholder_leg = _leg_by_id(fragment, "named-stakeholder")
    if stakeholder_leg is not None:
        floor = stakeholder_leg.get("mechanical_floor", {})
        matched_terms = _match_tokens_word_boundary(
            scanned_text, floor.get("terms", []), floor.get("case_sensitive", False)
        )
        if matched_terms:
            # Only the floor is decided mechanically -- who the stakeholder
            # actually is stays unresolved here (module docstring).
            judgment_point = {
                "leg": "named-stakeholder",
                "match_kind": stakeholder_leg.get("match_kind", "judgment"),
                "judgment_required": stakeholder_leg.get("judgment_required", True),
                "surfaces_as": stakeholder_leg.get("surfaces_as", "judgment_point"),
                "matched_floor_terms": matched_terms,
            }

    if not matched_legs and judgment_point is None:
        return None

    return {
        "stub_id": stub_id,
        "matched_legs": matched_legs,
        "judgment_point": judgment_point,
    }


def detect(fragment: dict, stubs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run ``detect_stub`` over every stub in ``stubs`` and wrap the result as
    a fragment-level recommendation.

    Returns ``{"emits": ..., "exhaustive": ..., "candidates": [...]}`` --
    ``emits``/``exhaustive`` are echoed from ``fragment`` verbatim (never
    hardcoded here), so a fragment update to either field propagates without
    a code change. ``candidates`` is an ADVISORY, stub_id-keyed list; nothing
    here decides, writes, or authors a ``pm-gates.md`` row (module
    docstring / negative-spec). A stub that trips no leg is simply absent
    from ``candidates`` -- ``exhaustive: false`` means the author may still
    add rows this scan never trips (fragment doc, "a manual row is a normal
    outcome").
    """
    candidates = []
    for stub in stubs:
        result = detect_stub(fragment, stub)
        if result is not None:
            candidates.append(result)

    return {
        "emits": fragment.get("emits", "recommendation"),
        "exhaustive": fragment.get("exhaustive", False),
        "candidates": candidates,
    }
