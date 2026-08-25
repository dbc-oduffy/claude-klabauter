"""Anti-regression pin for `operator_override_note`'s 2026-08-11 reshapes
(see that function's own docstring, "RESHAPED 2026-08-11" and NEGATIVE
SPEC 4, THEN "RESHAPED AGAIN 2026-08-11" and NEGATIVE SPEC 5): the rendered
override pointer must never contain a pasteable ``KEY=`` assignment for any
``COORDINATOR_*`` key, in either the flag-shaped (default) or reason-shaped
(``reason_placeholder=``) render path.

WHY THIS EXISTS

A cross-repo memo (market-intelligence-em, plus two prior siblings) reported
two independently-dispatched agents (a code-reviewer, a review-integrator,
no shared context) classifying the OLD render --
``"Bypass options for a human operator, not this agent: COORDINATOR_ALLOW_X=1
(pre-launch only) -- full list: ..."`` -- as prompt injection, because a
pasteable ``KEY=1`` assignment sitting inside tool output reads as an
instruction, not a fact, to a model scanning for actionable text. This is
the invariant three sibling repos paid for; this file makes it enforced by
construction rather than by a prior claim in a docstring.

NEGATIVE SPEC -- this asserts the render never contains a ``KEY=`` substring
for ANY ``COORDINATOR_*``-shaped key, not merely that today's two known env
vars are absent one -- a future call site passing a new env var name must
not be able to reintroduce the assignment shape by construction.

SECOND RESHAPE, SAME DAY (docs/plans/2026-08-11-guard-messages-point-to-docs-
never-name.md) -- the reshape this file originally pinned still NAMED the
key, bare (no ``=``). The follow-up plan removed the key from the render
entirely: the builder now returns a doc-pointer-only string, identical for
every ``env_var``/``reason_placeholder``. The no-assignment-form assertions
below hold trivially now (there is no key substring left to accidentally
suffix with ``=``), but are KEPT as a still-meaningful regression pin: a
future edit that re-introduces the key into the render must not be able to
re-introduce the assignment shape either. ``test_key_is_named_bare_in_both_
render_paths`` -- which asserted the OPPOSITE of what this render must do
now (the key must NOT appear at all) -- is replaced below with ``test_key_
is_not_named_in_either_render_path``.
"""

from __future__ import annotations

import re

from coordinator_core.bash_guards._helpers import operator_override_note

#: Matches a pasteable assignment of any COORDINATOR_* key -- the exact
#: shape this reshape removes. Deliberately broader than `_helpers.
#: _VIOLATION_RE`-style `=1`-only matching in
#: `test_no_handwritten_override_clauses.py`: THAT gate polices hand-written
#: clauses elsewhere in the package; THIS test polices this one builder's
#: own output against ANY assignment form (`=1`, `="..."`, or any other
#: value), since the builder itself must never render one, regardless of
#: what a hypothetical future value looked like.
_ASSIGNMENT_RE = re.compile(r"\bCOORDINATOR_[A-Z0-9_]+=")


def test_flag_shaped_render_has_no_assignment_form():
    note = operator_override_note(
        "COORDINATOR_ALLOW_NO_ASSIGNMENT_CHECK", payload={"session_id": "sess-c1d-em"}
    )
    assert not _ASSIGNMENT_RE.search(note), (
        "operator_override_note() (flag-shaped, default) rendered a pasteable "
        "KEY= assignment -- got: %r" % note
    )
    assert "COORDINATOR_ALLOW_NO_ASSIGNMENT_CHECK=" not in note


def test_reason_placeholder_render_has_no_assignment_form():
    note = operator_override_note(
        "COORDINATOR_QUEUE_PUNT_NO_ASSIGNMENT_CHECK",
        payload={"session_id": "sess-c1d-em"},
        reason_placeholder="not now, doing X",
    )
    assert not _ASSIGNMENT_RE.search(note), (
        "operator_override_note() (reason-shaped) rendered a pasteable "
        "KEY= assignment -- got: %r" % note
    )
    assert "COORDINATOR_QUEUE_PUNT_NO_ASSIGNMENT_CHECK=" not in note


def test_key_is_not_named_in_either_render_path():
    """2026-08-11 second reshape (guard-messages-point-to-docs-never-name
    plan): the render is now a doc pointer ONLY -- the key is not named at
    all, in either branch. This replaces this file's original ``test_key_
    is_named_bare_in_both_render_paths``, which pinned the OPPOSITE
    invariant under the first reshape (the key had to still be named, just
    never assigned). That invariant is retired, not merely relaxed: a
    reader's route to the key is now the referenced doc
    (``OVERRIDE_KEYS_DOC``), not the per-firing message -- see
    ``test_operator_override_note_retains_affordances.py`` for the pointer
    coverage that replaces it."""
    flag_note = operator_override_note(
        "COORDINATOR_ALLOW_BARE_NAME_CHECK", payload={"session_id": "sess-c1d-em"}
    )
    assert "COORDINATOR_ALLOW_BARE_NAME_CHECK" not in flag_note

    reason_note = operator_override_note(
        "COORDINATOR_QUEUE_PUNT_BARE_NAME_CHECK",
        payload={"session_id": "sess-c1d-em"},
        reason_placeholder="why",
    )
    assert "COORDINATOR_QUEUE_PUNT_BARE_NAME_CHECK" not in reason_note
