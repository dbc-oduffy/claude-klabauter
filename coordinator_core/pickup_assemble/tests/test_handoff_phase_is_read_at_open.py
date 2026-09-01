"""`/pickup` reads the declared plan->execute seam it was ignoring.

`handoff_phase` is schema-declared, written by `baton_assemble`, and carried
by 4 of 296 live handoffs (140 more carry `continuation`) at 2026-09-01 --
and `pickup_assemble` read it nowhere. A baton whose declared next move is
`/execute-plan` opened identically to one that still needs shaping, so the EM
had to reconstruct from the body what the frontmatter already stated.

Negative-spec:
  - Absence is CONTINUATION, per the field's own schema description -- an
    unstamped baton must gain no prefix at all, or the signal becomes noise
    on the 152 handoffs that carry nothing.
  - `continuation` is likewise silent. Saying "this is not an execution
    baton" on 140 records is not information.
  - This must stay a FACT, never a gate: no judgment point, no directive,
    nothing blocked. Same posture as `unsized_next_move_prefix`.
  - It must NOT be folded into `sizing_disposition`'s own `execution` value.
    That one means "a `governing_plan` resolves on disk"; this one means "a
    PM authorized executing it". A baton can cite a resolvable plan with no
    authorization stamp -- which is why the schema carries a separate
    `_cf_execution_stamp_required` cross-field rule -- so collapsing them
    would let a citation read as an authorization.

Run: python -m pytest coordinator_core/pickup_assemble/tests/test_handoff_phase_is_read_at_open.py -q
"""
from __future__ import annotations

import coordinator_core.pickup_assemble as pa


def test_execution_phase_earns_a_prefix_naming_execute_plan():
    prefix = pa.execution_phase_prefix({"handoff_phase": "execution"})
    assert "/execute-plan" in prefix
    # the obligation the phase carries, not merely the routing
    assert "execution-authorization stamp" in prefix


def test_absence_is_continuation_and_says_nothing():
    assert pa.execution_phase_prefix({}) == ""


def test_continuation_says_nothing():
    assert pa.execution_phase_prefix({"handoff_phase": "continuation"}) == ""


def test_an_unrecognized_value_says_nothing():
    # Positive match only. A future third phase value must not be narrated as
    # execution just because it is not "continuation".
    assert pa.execution_phase_prefix({"handoff_phase": "something-else"}) == ""
    assert pa.execution_phase_prefix({"handoff_phase": None}) == ""


def test_the_two_execution_axes_are_not_the_same_field():
    # `sizing_disposition` resolves its own "execution" from `governing_plan`.
    # This function must not consult that field at all: a resolvable citation
    # is not an authorization.
    assert pa.execution_phase_prefix({"governing_plan": "docs/plans/x.md"}) == ""
