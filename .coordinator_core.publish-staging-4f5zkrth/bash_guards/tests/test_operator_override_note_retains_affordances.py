"""Pins the operator-override note's load-bearing content, split across the
per-firing in-message note and the reference doc it now points to.

Why this file exists: the note was cut from 1151 to ~394 bytes without a
single test needing an update, because nothing pinned it. That is the
hazard, not the cut -- prose shipped into every advisory message, with no
gate under it, is one careless trim away from losing a correctness fact and
no suite noticing.

Second cut (2026-07-30, PM ruling, same session as H12): the note fired in
FULL on EVERY guard firing, so its ~50 words were paid by every advisory
message in the suite every time, not just once. The in-message note now
carries only the ONE fact that must survive at decision time -- the env var
this guard names is pre-launch-only, so an agent does not burn a turn trying
to set it from inside the session. The two relayable routes (the
`!`-prefixed prompt, the blanket-disarm marker) and the CONFINEMENT_DENY
caveat moved to `docs/reference/guard-override-keys.md` -- this file now
pins BOTH halves, so a careless trim of either fails the suite the same way
the pre-cut note's total absence of coverage would have.

The byte ceiling on the in-message note is deliberately tight now: it is a
one-line pointer, not a paragraph, and a regrowth back toward the old ~400
byte content is the exact regression this second cut exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.bash_guards._helpers import (
    OVERRIDE_KEYS_DOC,
    operator_override_note,
)

# The in-message note is now a single-line pointer (~150-170 bytes for a
# real env var name). Ceiling binds the actual shape, not the old essay.
_MAX_BYTES = 220

_SENTINEL_ENV_VAR = "COORDINATOR_ALLOW_EXAMPLE"


def _note() -> str:
    return operator_override_note(
        _SENTINEL_ENV_VAR, payload={"session_id": "sess-c1d-em"}
    )


def _doc_text() -> str:
    doc_path = Path(__file__).resolve().parents[3] / OVERRIDE_KEYS_DOC
    assert doc_path.is_file(), (
        "OVERRIDE_KEYS_DOC (%s) names a doc that does not exist at %s -- the "
        "in-message note points here for the full bypass-options content; "
        "if the path moved, update OVERRIDE_KEYS_DOC to match."
        % (OVERRIDE_KEYS_DOC, doc_path)
    )
    return doc_path.read_text(encoding="utf-8")


def test_note_names_no_env_var():
    """2026-08-11 second reshape (guard-messages-point-to-docs-never-name
    plan) -- the in-message note stopped naming the key entirely (see
    `operator_override_note`'s own docstring, "RESHAPED AGAIN 2026-08-11").
    This replaces this file's original ``test_note_states_the_env_var_is_
    not_reachable_in_session``, which asserted the OPPOSITE (the env var
    had to be named, paired with the unsettable-in-session constraint) --
    that invariant is retired, not merely relaxed: the note is a doc
    pointer only now, and the pre-launch-only fact it used to carry inline
    moved wholly into the reference doc (see
    ``test_reference_doc_states_the_env_var_is_not_reachable_in_session``
    below)."""
    note = _note()
    assert _SENTINEL_ENV_VAR not in note, "the note must not name the caller's env var any more"


def test_reference_doc_states_the_env_var_is_not_reachable_in_session():
    """The one fact the in-message note used to carry inline --
    unsettable-from-inside-a-session -- must still be reachable somewhere a
    reader following the note's pointer lands, or the constraint is lost
    outright rather than merely relocated. Mirrors this file's existing
    doc-content pins (``test_reference_doc_keeps_the_confinement_deny_
    caveat`` etc.) -- this repo's ``docs/reference/guard-override-keys.md``
    is a peer chunk's scope in the same dispatch, not this file's own edit
    surface, but the fact still needs a gate somewhere or its loss goes
    unnoticed."""
    doc = _doc_text()
    assert "unsettable" in doc.lower() and "session" in doc.lower(), (
        "the reference doc must state the env var is unsettable from inside a session -- "
        "this fact moved here from the in-message note's own 2026-08-11 second reshape "
        "and must not be lost in the move"
    )


def test_note_carries_no_instruction_or_disclaimer_register():
    """2026-08-11 reshape (cross-repo memo, market-intelligence-em plus two
    prior siblings): the old note read as an INSTRUCTION (a pasteable
    ``KEY=1`` assignment) framed by a disclaimer ("...not this agent:") that
    is itself an injection tell. Two independently-dispatched agents
    classified it as prompt injection and declined to act. The note now
    states the key's unusability as a plain fact instead -- no disclaimer
    register, and (per `test_operator_override_note_no_assignment_form.py`)
    no assignment form."""
    note = _note()
    assert "not this agent" not in note.lower(), (
        "the old disclaimer register ('...not this agent:') must not "
        "reappear -- it was itself the injection tell this reshape removed"
    )
    assert "%s=" % _SENTINEL_ENV_VAR not in note, (
        "the env var must never be rendered as a pasteable assignment"
    )


def test_note_points_at_the_reference_doc():
    """The note's whole job now is to be a pointer -- if it stops naming the
    doc, a reader has no path from the one-liner to the full content."""
    note = _note()
    assert OVERRIDE_KEYS_DOC in note, (
        "the in-message note must name the reference doc path -- otherwise "
        "the relayable routes and the CONFINEMENT_DENY caveat are unreachable "
        "from the message the agent actually sees"
    )


def test_note_stays_short():
    size = len(_note().encode("utf-8"))
    assert size <= _MAX_BYTES, (
        "operator-override note is %d bytes, over the %d ceiling. This note ships inside every "
        "advisory the suite emits, so growth here is multiplied across the whole message "
        "population. If the addition is genuinely load-bearing, raise the ceiling deliberately "
        "in the same commit and say why." % (size, _MAX_BYTES)
    )


def test_note_stays_under_twenty_words():
    words = len(_note().split())
    assert words <= 20, (
        "operator-override note is %d words, over the 20-word target for a "
        "per-firing pointer -- push detail into the reference doc instead of "
        "growing the inline note." % words
    )


def test_reference_doc_names_the_two_in_session_relayable_mechanisms():
    """Both surviving affordances are ones an agent can RELAY to a human. Cut
    either from the doc and the note's pointer leads nowhere useful, which is
    the mistrust shape this suite's design rules reject."""
    doc = _doc_text()
    assert "!" in doc, "the !-prefixed prompt is one of only two relayable routes -- do not drop it"
    assert "disarm" in doc.lower(), (
        "the blanket-disarm marker is the other relayable route -- do not drop it"
    )


def test_reference_doc_keeps_the_confinement_deny_caveat():
    """Correctness, not tone. Without this line a reader can believe a
    confinement guard is suppressible by the marker named one clause earlier."""
    doc = _doc_text()
    assert "CONFINEMENT_DENY" in doc, (
        "the CONFINEMENT_DENY carve-out is a correctness fact: the marker named in this same "
        "doc does NOT suppress that band, and a reader who misses it can act on a false belief "
        "about what is silenceable"
    )


def test_reference_doc_names_the_subprocess_pooling_regression_risk():
    """The unreachability guarantee is a side effect of a performance
    decision (fresh subprocess per event), not a designed security feature --
    the doc must say so, or a future pooling optimisation could silently
    delete the boundary with no one having been warned it was load-bearing."""
    doc = _doc_text()
    assert "pool" in doc.lower(), (
        "the doc must warn that a future hook-process-pooling change would "
        "silently delete the fresh-subprocess-per-event unreachability "
        "guarantee -- see test_override_unreachability_boundary.py's own "
        "warning, which this doc must not contradict"
    )
