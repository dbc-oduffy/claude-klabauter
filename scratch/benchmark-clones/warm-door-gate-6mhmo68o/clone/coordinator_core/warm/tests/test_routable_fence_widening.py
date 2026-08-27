"""The op-prefix fence widened to COMPUTE_ONLY reads, and nothing mutating.

Spec backlink: docs/plans/2026-08-26-the-loopback-listener-gets-a-credential.md § C4/AC8.

WHY THE FENCE COULD NOT MOVE FIRST, recorded because the ordering is the whole control
and a later reader will see only the widened fence. `ROUTABLE_OP_PREFIXES` was the ONLY
thing bounding blast radius while the listener accepted unauthenticated requests: its
own comment said the endpoint was "reachable by anything that can open a loopback
socket". That stopped being true at `34a0a556e`, when `supervisor.parse_request` began
requiring the boot cookie on every non-health request. A measurement someone wanted to
take was never a reason to move it; a landed credential is.

WHY BY CLASS AND NOT BY MORE PREFIXES. An op CLI wants its READ ops served warm, and
read ops do not share a namespace -- they are spread across every prefix. Adding
strings would have meant either a long list that drifts, or a prefix broad enough to
admit mutating siblings. `authz.classification` already answers the actual question,
with a MUTATING default and a KeyError on unclassified ops that its own docstring
requires HTTP dispatch to read as DENY.

AC8 ASKED FOR A STRUCTURAL CRITERION, NOT "THE DIFF SHOWS IT". The original AC said the
widening and the credential appear in one commit -- unfalsifiable once the commits are
behind you. What is checkable, and what this file asserts, is that no MUTATING op is
routable through the fence at all.
"""

import pytest

from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.warm import hook_http


def _path(op: str) -> str:
    return f"{hook_http.HOOK_PATH}/{op}"


def test_a_compute_only_op_outside_the_prefixes_is_routable():
    """The widening itself. `cutover.gate` matches no entry in
    ROUTABLE_OP_PREFIXES and is classified COMPUTE_ONLY."""
    assert not any(
        "cutover.gate".startswith(p) for p in hook_http.ROUTABLE_OP_PREFIXES
    ), "fixture drift: cutover.gate must sit OUTSIDE the prefix set for this to prove anything"
    assert hook_http.op_for_path(_path("cutover.gate")) == "cutover.gate"


def test_the_hook_prefixes_still_route():
    """The widening is additive. A hook op is routable on its prefix alone,
    without consulting the classifier -- the hot path did not get slower."""
    assert hook_http.op_for_path(_path("hooks.session_heartbeat")) == (
        "hooks.session_heartbeat"
    )


@pytest.mark.parametrize("op", ["invoke.from_argv", "handoff.close_origin_stub"])
def test_a_mutating_op_is_not_routable(op):
    assert OP_CLASSIFICATION[op] is OpClass.MUTATING, "fixture drift"
    assert hook_http.op_for_path(_path(op)) is None


def test_the_ceremony_namespace_stays_unroutable():
    """The fence's original stated purpose, in its own words: a rewritten
    registration must never reach `ceremony.*`. Asserted by namespace rather
    than by a named op, because the op this was written against
    (`ceremony.scoped_git_commit`) has since been killed and a test keyed to
    a dead name proves nothing."""
    ceremony_ops = [op for op in OP_CLASSIFICATION if op.startswith("ceremony.")]
    assert ceremony_ops, "fixture drift: no ceremony.* ops in the map at all"
    assert [op for op in ceremony_ops if hook_http.op_for_path(_path(op))] == []


def test_an_unclassified_op_is_denied_not_admitted():
    """`classify` RAISES for an op absent from the map, and its docstring
    requires HTTP dispatch to treat that as DENY. Silently treating an
    unclassified op as a read is the privilege-escalation path it names."""
    assert hook_http.op_for_path(_path("totally.not.an.op")) is None


def test_the_widening_admits_reads_and_only_reads():
    """THE STRUCTURAL CRITERION AC8 ASKS FOR, over the whole classification
    map rather than a sampled few.

    SCOPED TO WHAT THE WIDENING ADDED, and the distinction is not a
    convenience. The pre-existing prefixes already admit 21 MUTATING ops --
    `hooks.*` and `session.*` handlers write state, and always did. That was
    never a defect: the fence's own comment scopes it to "a rewritten
    registration can at worst reach a different HOOK op", a NAMESPACE bound,
    not a read-only one. Asserting "no MUTATING op is routable" would
    therefore fail against behaviour that predates this change and has its
    own justification.

    What must hold is that the CLASS-based widening added reads only: every
    op newly reachable that no prefix would have admitted is COMPUTE_ONLY.
    """
    newly_admitted = [
        op
        for op in OP_CLASSIFICATION
        if hook_http.op_for_path(_path(op)) is not None
        and not any(op.startswith(p) for p in hook_http.ROUTABLE_OP_PREFIXES)
    ]
    assert newly_admitted, "the widening admits nothing -- it did not take effect"
    leaked = [
        op for op in newly_admitted if OP_CLASSIFICATION[op] is OpClass.MUTATING
    ]
    assert not leaked, (
        "the class-based widening admitted MUTATING ops: "
        f"{sorted(leaked)}. It must admit reads only."
    )


def test_the_widening_is_not_silently_enormous():
    """BLAST RADIUS, NAMED RATHER THAN ASSUMED. The widening roughly doubles
    what the transport will route, and a number nobody states is a number
    nobody notices growing. This is a tripwire on the ORDER of magnitude, not
    a golden count -- adjust it deliberately, with a reason, never to make a
    red test green."""
    newly_admitted = [
        op
        for op in OP_CLASSIFICATION
        if hook_http.op_for_path(_path(op)) is not None
        and not any(op.startswith(p) for p in hook_http.ROUTABLE_OP_PREFIXES)
    ]
    assert len(newly_admitted) < 200, (
        f"{len(newly_admitted)} ops newly routable -- if the classification map grew "
        "this much, re-read whether every one of them should be reachable over HTTP."
    )


def test_the_fence_still_refuses_a_nested_path():
    """Widening the CLASS must not widen the SHAPE: a path with a slash in
    the op segment is still refused, so the fence cannot be walked."""
    assert hook_http.op_for_path(_path("cutover.gate/../ceremony.scoped_git_commit")) is None
