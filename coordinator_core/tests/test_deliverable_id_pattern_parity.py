"""
coordinator_core.tests.test_deliverable_id_pattern_parity — pins
handoff.schema.json's `deliverable_id` pattern and
coverage.py::_DELIVERABLE_ID_RE against a shared case table so the two
cannot silently drift apart.

Spec backlink: pln-author-the-dlv-pattern-for-del-704e32
(C4). Companion to C3 (coverage.py widening).

BOUNDARY CORRECTION (2026-08-05, after C2 was executed and reverted): that
plan's C2 directed the schema half to be authored HERE. That was wrong.
`handoff.schema.json` under `coordinator_core/frontmatter/schemas/` is a
VENDORED copy of DoE-claude's `coordinator/schemas/handoff.schema.json`;
`schema_validate.check_schema_drift` is a byte-for-byte tamper-check against
DoE HEAD, so a local edit here reads as corruption, not as authorship. The
`dlv-` pattern is DoE's to land and claude-klabauter's to re-vendor. Only the
coverage.py half (C3) was ever claude-klabauter-owned, and it stands on its own merits:
`_DELIVERABLE_ID_RE` did not admit `.`, which misclassified a live
Example-retrieval-repo-ue-addon id in a guard whose whole job is avoiding a false COVERED.

NEGATIVE SPEC: this file does NOT exercise end-to-end frontmatter
validation (schema_validate.py's public entry points) for the
placeholder-rejection case. Claude-klabauter's own validator does not yet ENFORCE
`pattern` at all as of this plan (see the plan's § Sequencing dependency —
a peer session is landing `pattern` support in
coordinator_core/frontmatter/schema_validate.py concurrently, uncommitted
at authoring time). A test that ran a record through the full validator
and expected rejection would pass VACUOUSLY today (pattern ignored) and
only start passing for the right reason once that peer work lands —
indistinguishable from a test that never ran. Every assertion here
compiles and matches the pattern STRING directly, which is real now
regardless of validator wiring.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from coordinator_core.coverage import _DELIVERABLE_ID_RE

_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "frontmatter"
    / "schemas"
    / "handoff.schema.json"
)


def _load_schema_pattern() -> str | None:
    """The vendored schema's `deliverable_id` pattern, or None if absent.

    `handoff.schema.json` is a VENDORED copy of DoE-claude's
    `coordinator/schemas/handoff.schema.json` — claude-klabauter does not author it
    (`schema_validate.check_schema_drift` is a byte-for-byte tamper-check
    against DoE HEAD, and a local edit here fails it as corruption). The
    `dlv-` pattern is therefore DoE's to land; this repo picks it up on the
    next re-vendor.

    Returning None rather than raising is what lets the parity pins below
    SKIP until that re-vendor and then activate on their own — the pin
    exists precisely to catch the two patterns diverging at the moment the
    schema half arrives, which is exactly when nobody is thinking about it.
    """
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        schema = json.load(fh)
    any_of = schema["properties"]["deliverable_id"]["anyOf"]
    string_arm = next(arm for arm in any_of if arm.get("type") == "string")
    return string_arm.get("pattern")


_PENDING_REVENDOR = pytest.mark.skipif(
    _load_schema_pattern() is None,
    reason=(
        "deliverable_id carries no pattern in the vendored handoff.schema.json yet — "
        "DoE-claude authors it (memo 2026-08-05-claude-klabauter-em-dlv-pattern-taking-it-"
        "but-23-of-your-ids-would-strand.md); this pin activates on re-vendor."
    ),
)


# Shared case table: (value, expected accept/reject). Every non-null
# `deliverable_id` value in the fleet corpus (2200 values swept across 7
# repos in C1, zero rejections) must accept here; the placeholder shape
# must reject.
CASE_TABLE = [
    # Placeholder — the scaffolder's unfixed-title slug. Must be rejected
    # by BOTH patterns; this is the false-clear class the whole plan closes.
    ("dlv-placeholder-replace-with-one-line-spinof-4de80c", False),
    # dlv-<stub_id> mint shape — no hex suffix at all.
    ("dlv-sat-02", True),
    ("dlv-computed-skills-B8-review-ci", True),
    # dlv-<slug>-<6hex> mint shape.
    ("dlv-handoff-spinoff-hardening-claude-klabauter-accommo-11603c", True),
    # Uppercase body — case-permissive per Constraint 1 (mint-from-stub
    # passes stub_id through verbatim, no case-folding).
    ("dlv-agent-fleet-G6-example-game-repo-pattern-carry", True),
    # Dot-bearing body — Constraint 2 (example-retrieval-repo-ue-addon live carrier).
    ("dlv-first-class-consumer-install-5.8-dogfood-2d336d", True),
    # Trailing-dash-before-hex slug-truncation artifact — Constraint 3
    # (must NOT anchor on -[0-9a-f]{6}).
    ("dlv-ac-6-pickup-brief-remaining-230ms-is-an--978c46", True),
    # Malformed shapes that must stay rejected (injection-hazard guard —
    # coverage.py interpolates this value raw into `git log --grep`).
    ("hnd-not-a-deliverable-id-123456", False),
    ("dlv-", False),
    ("dlv-has a space-4de80c", False),
    ("dlv-has;a;semicolon-4de80c", False),
]


@pytest.mark.parametrize("value,expected", CASE_TABLE)
@_PENDING_REVENDOR
def test_schema_pattern_matches_case_table(value: str, expected: bool) -> None:
    pattern = _load_schema_pattern()
    assert bool(re.match(pattern, value)) is expected


@pytest.mark.parametrize("value,expected", CASE_TABLE)
def test_coverage_regex_matches_case_table(value: str, expected: bool) -> None:
    assert bool(_DELIVERABLE_ID_RE.match(value)) is expected


@pytest.mark.parametrize("value,expected", CASE_TABLE)
@_PENDING_REVENDOR
def test_schema_pattern_and_coverage_regex_agree(value: str, expected: bool) -> None:
    """The drift pin: both patterns must reach the SAME verdict on every
    case, not merely the expected verdict independently. A future edit to
    either pattern that changes its accepted language without a matching
    edit to the other trips this test even if the individual per-pattern
    tests above still happen to pass on unrelated cases."""
    pattern = _load_schema_pattern()
    schema_verdict = bool(re.match(pattern, value))
    coverage_verdict = bool(_DELIVERABLE_ID_RE.match(value))
    assert schema_verdict == coverage_verdict == expected


@_PENDING_REVENDOR
def test_placeholder_rejected_by_schema_pattern_directly() -> None:
    """AC1 oracle, made explicit and independent of the shared case table
    above (which already covers it) so a reader can find the single
    assertion the acceptance criterion names without cross-referencing
    the parametrized table."""
    pattern = _load_schema_pattern()
    placeholder = "dlv-placeholder-replace-with-one-line-spinof-4de80c"
    assert re.match(pattern, placeholder) is None
