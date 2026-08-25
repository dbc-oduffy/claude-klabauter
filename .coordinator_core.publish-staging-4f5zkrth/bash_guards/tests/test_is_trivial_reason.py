"""Pass/fail boundary table for
``coordinator_core.bash_guards._helpers.is_trivial_reason`` — the shared
non-triviality bar consolidated (and tightened, PM-authorised) from the two
duplicate copies previously carried by ``write_guards/nudge_improvement_
queue_write.py`` and ``write_guards/nudge_baton_body_bar.py`` (2026-07-30).

WHY A TABLE, NOT SEPARATE TESTS: the acceptance bar for this predicate is a
single boundary — realistic terse-but-genuine reasons on one side, degenerate
filler on the other — and the risk being guarded against is exactly a rule
that looks reasonable in isolation but silently rejects one of the terse
cases. Keeping both directions in one table makes the boundary visible in one
place, per the dispatch brief that added this file: "If a rule you are
considering rejects any of these, that rule is wrong; drop it."

Spec backlink: 2026-07-30 triviality-bar tightening dispatch (PM-authorised),
following the pre-existing pure-length-test gap in the pre-tightening
``_is_trivial_reason`` (any 12-char string, including "aaaaaaaaaaaa", used to
pass) that mattered more once the predicate started gating a durable content
field (the improvement-queue ``justification:`` line) rather than only an
operator-set env var.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards._helpers import is_trivial_reason

#: TERSE-but-genuine reasons that MUST still pass (i.e. ``is_trivial_reason``
#: returns ``False``) after the tightening. Includes hyphenated single
#: tokens and short clipped phrases, per the dispatch brief's explicit list.
GENUINE_REASONS = [
    "genuinely cross-cutting",
    "needs its own plan",
    "blocked on rag schema",
    "upstream fix pending",
    "architecturally separate",
    "cross-cutting concern",
    "out of scope for this chunk",
    "duplicate of pcore-04",
    "waiting on schema bump",
    "needs PM ruling first",
]

#: Degenerate values that MUST still fail (i.e. ``is_trivial_reason`` returns
#: ``True``) — the exact-match denylist, the length floor, and the new
#: character-variety floor, each represented at least once.
DEGENERATE_REASONS = [
    "",
    "1",
    "ok",
    "yes",
    "true",
    "fine",
    "-",
    "x",
    "short",  # under the length floor, has real letters
    "aaaaaaaaaaaa",  # 12 chars, 1 distinct letter
    "abababababab",  # 12 chars, 2 distinct letters
    "123456789012",  # 12 digits, 0 letters
    "            ",  # 12 spaces, collapses to the empty-string denylist hit
    "aaaaaaaaaaaaaaaaaaaa",  # long but still 1 distinct letter
]


@pytest.mark.parametrize("reason", GENUINE_REASONS)
def test_genuine_terse_reasons_pass(reason: str) -> None:
    assert is_trivial_reason(reason) is False, (
        "a realistic terse-but-genuine reason must not be rejected: %r" % reason
    )


@pytest.mark.parametrize("reason", DEGENERATE_REASONS)
def test_degenerate_reasons_still_fail(reason: str) -> None:
    assert is_trivial_reason(reason) is True, (
        "a degenerate/placeholder reason must still be rejected: %r" % reason
    )
