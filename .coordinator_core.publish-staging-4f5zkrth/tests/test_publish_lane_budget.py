"""The publish lane is a closed carve-out, not a widening knob.

`coordinator_core.publish_lane` is the only thing in this tree that can resolve an op
above `ipc.CEREMONY_BUDGET_SECS` or past `op_budget_suspension`'s roster. That makes it
the shape every prior budget bypass took, so it is guarded the way the two ratchets
either side of it are guarded — with independent second literals here, so widening it
in the source alone leaves the tree red.

What a reader should be able to conclude from this module passing:

  1. The lane is OFF by default. With no declaration, every assertion the ceremony and
     suspension ratchets make still holds, unchanged.
  2. The lane cannot be entered for an op its closed list does not name. No environment
     value, no envelope field, no combination of the two admits a second op.
  3. The lane cannot be widened from outside the engine. The budget is not read from
     the environment; the environment carries a boolean and nothing else.

Negative-spec:
    - Does NOT assert `ceremony.scoped_git_commit` completes in any amount of time.
      That is a latency property and DR-344 forbids resting a conclusion on a
      wall-clock number taken on a loaded box. This module pins reachability and
      bounds, never duration.
    - Does NOT assert the lane budget is large enough for a real publish. It is the
      PM's number; if a round outgrows it that is a new fact for the PM, not a test
      failure to be edited away here.
    - Does NOT re-assert the ceremony ratchet's own properties. Those live in
      `test_ceremony_budget_ratchet.py` and duplicating them here would let the two
      drift while both stayed green.

Spec backlink: docs/decisions/DR-350-the-publish-lane-is-not-the-close-ceremony.md
"""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core import ipc, op_budget_suspension, publish_lane


#: The independent second literal, deliberately NOT imported from the module under
#: test — importing it would make this file agree with any value whatsoever. Lowering
#: the source below this is always fine; raising it above needs this number lifted too,
#: in the same commit, with a PM ruling in DR-350 behind it.
_PINNED_LANE_BUDGET_SECS = 600.0

#: The ratified lane roster. One op. A second row here is a PM-visible fact about what
#: the publish path is allowed to reach, and it lands in the same commit as the source
#: edit or the suite is red.
_RATIFIED_LANE_OPS = frozenset({
    "ceremony.scoped_git_commit",
})


@pytest.fixture
def in_lane(monkeypatch):
    """Put this process inside a declared percolate/publish round."""
    monkeypatch.setenv(publish_lane.PUBLISH_LANE_ENV, "1")


@pytest.fixture(autouse=True)
def _lane_off_unless_asked(monkeypatch):
    """Every test here starts outside the lane, whatever the real environment holds.

    Without this the suite's verdict would depend on whether it happened to be run
    from inside a publish round — which is exactly the load-order dependence the
    ceremony budget's own membership test was rewritten to eliminate.
    """
    monkeypatch.delenv(publish_lane.PUBLISH_LANE_ENV, raising=False)


# ---------------------------------------------------------------------------
# The ratchets
# ---------------------------------------------------------------------------

def test_lane_budget_is_not_raised():
    assert publish_lane.PUBLISH_LANE_BUDGET_SECS <= _PINNED_LANE_BUDGET_SECS, (
        f"The publish-lane budget was raised to "
        f"{publish_lane.PUBLISH_LANE_BUDGET_SECS}s, above the pinned "
        f"{_PINNED_LANE_BUDGET_SECS}s. This bound already accommodates 53 git spawns "
        f"for one commit; the remedy for a round that outgrows it is fewer spawns, "
        f"not a larger number. Raising it is a PM ruling recorded in DR-350."
    )


def test_lane_roster_only_shrinks():
    added = set(publish_lane.PUBLISH_LANE_OPS) - _RATIFIED_LANE_OPS
    assert not added, (
        f"Ops admitted to the publish lane without ratification: {sorted(added)}. A "
        f"row here lifts BOTH the ceremony clamp and the suspension refusal for that "
        f"op — add it to _RATIFIED_LANE_OPS in the same commit so the carve-out's "
        f"growth is visible."
    )


def test_every_lane_op_is_one_the_publish_path_actually_reaches():
    """A row that no publish CLI names is a widening with a lane's paperwork on it."""
    for op in publish_lane.PUBLISH_LANE_OPS:
        assert op_budget_suspension.is_suspended(op) or ipc.is_ceremony_method(op), (
            f"{op} is in the publish lane but is neither suspended nor a ceremony op "
            f"— it was never capped, so the lane grants it nothing and the row is "
            f"noise that will read as precedent."
        )


# ---------------------------------------------------------------------------
# OFF by default — the ceremony and suspension rules are untouched
# ---------------------------------------------------------------------------

def test_lane_is_inactive_with_no_declaration():
    assert not publish_lane.is_active()
    assert publish_lane.budget_for("ceremony.scoped_git_commit") is None


def test_lane_op_outside_a_round_still_resolves_at_the_ceremony_budget():
    resolved = ipc._timeout_for("ceremony.scoped_git_commit")
    assert resolved <= ipc.CEREMONY_BUDGET_SECS, (
        f"A lane op resolved to {resolved}s outside a declared round. The carve-out "
        f"must not leak into the close ceremony, which is the caller the 2s budget "
        f"was written for."
    )


def test_lane_op_outside_a_round_is_still_refused_at_dispatch():
    ipc.allow_unstamped_dispatch()
    response = asyncio.run(ipc.dispatch_message({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "ceremony.scoped_git_commit",
        "_origin_worktree": ".",
        "params": {},
    }))
    error = response.get("error")
    assert error is not None and error["code"] == ipc.OP_SUSPENDED_ERROR


def test_lane_op_outside_a_round_is_still_refused_in_process():
    with pytest.raises(op_budget_suspension.OpSuspendedError):
        ipc.get_op_handler("ceremony.scoped_git_commit")


# ---------------------------------------------------------------------------
# ON inside a round — and only for what the list names
# ---------------------------------------------------------------------------

def test_lane_op_inside_a_round_gets_the_lane_budget(in_lane):
    assert ipc._timeout_for("ceremony.scoped_git_commit") == (
        publish_lane.PUBLISH_LANE_BUDGET_SECS
    )


def test_lane_op_inside_a_round_is_not_refused_in_process(in_lane):
    """The in-process door reads the environment because it IS the caller's process."""
    assert ipc.get_op_handler("ceremony.scoped_git_commit") is not None


def test_a_non_lane_ceremony_op_inside_a_round_is_still_clamped(in_lane):
    """The lane is per-op, not per-process. A round does not widen everything it does."""
    for op in ("ceremony.wsc_tail", "ceremony.wsc_commit"):
        assert ipc._timeout_for(op) <= ipc.CEREMONY_BUDGET_SECS, (
            f"{op} was widened by a publish-lane declaration despite not being in "
            f"PUBLISH_LANE_OPS. The lane is a closed list; a round that widens every "
            f"op it touches is the blanket lift this design exists to avoid."
        )


def test_a_non_lane_suspended_op_inside_a_round_is_still_refused(in_lane):
    """Same point at the other door: being in a round is not a general reinstatement."""
    with pytest.raises(op_budget_suspension.OpSuspendedError):
        ipc.get_op_handler("ceremony.wsc_tail")


# ---------------------------------------------------------------------------
# The environment carries a boolean, never a duration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "9999"])
def test_any_truthy_declaration_yields_exactly_the_lane_budget(monkeypatch, value):
    """`COORDINATOR_PUBLISH_LANE=9999` buys 600s, not 9999s.

    The variable is read for truthiness and never parsed as a number. This is the
    property that separates a lane declaration from the retired unbounded timeout
    knobs: a caller says WHICH lane it is in, never HOW LONG it wants.
    """
    monkeypatch.setenv(publish_lane.PUBLISH_LANE_ENV, value)
    assert ipc._timeout_for("ceremony.scoped_git_commit") == (
        publish_lane.PUBLISH_LANE_BUDGET_SECS
    )


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_an_explicitly_falsy_declaration_is_not_a_lane(monkeypatch, value):
    """Narrowing is always allowed: turning the lane off puts the op back under 2s."""
    monkeypatch.setenv(publish_lane.PUBLISH_LANE_ENV, value)
    assert not publish_lane.is_active()
    assert ipc._timeout_for("ceremony.scoped_git_commit") <= ipc.CEREMONY_BUDGET_SECS


# ---------------------------------------------------------------------------
# The wire signal — the warm server's only honest read
# ---------------------------------------------------------------------------

def test_the_envelope_field_carries_the_lane_without_the_environment():
    """A warm server's env is its spawner's, so the request has to say so itself."""
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "ceremony.scoped_git_commit",
        publish_lane.PUBLISH_LANE_FIELD: True,
        "params": {},
    }
    assert publish_lane.is_active(msg)
    assert ipc._timeout_for("ceremony.scoped_git_commit", msg) == (
        publish_lane.PUBLISH_LANE_BUDGET_SECS
    )


def test_the_envelope_field_does_not_widen_an_op_off_the_list():
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "ceremony.wsc_tail",
        publish_lane.PUBLISH_LANE_FIELD: True,
        "params": {},
    }
    assert ipc._timeout_for("ceremony.wsc_tail", msg) <= ipc.CEREMONY_BUDGET_SECS


def test_the_envelope_alone_carries_a_lane_op_through_BOTH_suspension_doors():
    """The warm-server shape: envelope says lane, environment does not.

    Regression guard for a live defect in the original wiring. `dispatch_message`
    yields to the lane at its own suspension check (which reads the envelope) and then
    calls `get_op_handler` to resolve the handler — and that second door was checking
    the ENVIRONMENT only. On the warm path this process is the server, whose
    `os.environ` is its spawner's, so a lane request admitted at door one was refused
    at door two, and the envelope field that exists precisely to carry the lane across
    the pipe was ignored where it mattered most.

    The cold path masked it end-to-end (env is inherited there), which is why a live
    9-row publish round did not surface it. Asserting on `dispatch_message` rather than
    on `get_op_handler` directly is deliberate: the bug lived in the SEAM between the
    two doors, and a unit test of either one alone passes while dispatch stays broken.
    """
    ipc.allow_unstamped_dispatch()
    response = asyncio.run(ipc.dispatch_message({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "ceremony.scoped_git_commit",
        "_origin_worktree": ".",
        publish_lane.PUBLISH_LANE_FIELD: True,
        "params": {},
    }))
    error = response.get("error") or {}
    assert error.get("code") != ipc.OP_SUSPENDED_ERROR, (
        "a lane request carrying the envelope field was still refused as suspended — "
        "one of the two doors is reading the environment instead of the request, and "
        "on a warm server the environment belongs to whoever spawned it."
    )


def test_a_request_without_the_field_is_not_in_the_lane():
    """Absence reads as 'older client, not in a round' — never as a default-on."""
    msg = {"jsonrpc": "2.0", "id": 1, "method": "ceremony.scoped_git_commit", "params": {}}
    assert not publish_lane.is_active(msg)


def test_a_malformed_envelope_is_not_a_lane_request():
    for msg in (None, "not-a-dict", 7, []):
        assert not publish_lane.request_declares_lane(msg)
