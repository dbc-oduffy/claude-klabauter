"""
coordinator_core.hooks.test_agent_postuse_dispatch -- tests for the PostToolUse(Agent)
fan-in dispatcher.

Two obligations, and neither is about what the legs do -- that is covered by their own
test files. This module owns only the composition:

1. **Both legs actually fire.** Timing cannot see a dropped leg and neither can a
   return-shape assertion: the fan-in returns `no_advisory()` whether it ran two legs,
   one, or none, because both legs are write ops. A fold that silently stopped calling
   one of its members would look identical in every other observable. That is the
   AC15-shaped check -- a before/after population count, expressed here as "each leg was
   invoked exactly once, with the params it was given".

2. **A raising leg cannot suppress its sibling.** This is the one property the fan-in
   can lose that the two separate processes it replaces had for free: a process boundary
   isolates a crash, a shared `asyncio.gather` does not. Without
   `return_exceptions=True` the first raising leg cancels the merge and the sibling's
   on-disk write is lost.

Spec backlink: coordinator_core/hooks/agent_postuse_dispatch.py (module under test).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.hooks import agent_postuse_dispatch as apd  # noqa: E402
from coordinator_core.hooks._envelope import no_advisory, post_advisory  # noqa: E402


@pytest.fixture
def legs(monkeypatch):
    """Replace both legs with recording stubs; yields the call log.

    Patches `_LEGS` rather than the leg modules' own `run`, so the test pins the
    composition this module performs and stays silent about how the legs are
    implemented.
    """
    calls: list[tuple[str, dict, object]] = []

    def _stub(label, result):
        async def _run(params, repo_root=None):
            calls.append((label, params, repo_root))
            if isinstance(result, BaseException):
                raise result
            return result

        return _run

    def _install(*specs):
        monkeypatch.setattr(
            apd, "_LEGS", tuple((label, _stub(label, result)) for label, result in specs)
        )
        return calls

    return _install


@pytest.mark.asyncio
async def test_both_legs_fire_once_with_the_handler_params(legs):
    """AC15 shape: a dropped leg is invisible in the return value, so count the calls."""
    calls = legs(("first", no_advisory()), ("second", no_advisory()))

    params = {"session_id": "test-session-fanin", "dispatched_agent_id": "abcdef123456"}
    result = await apd._handler(params, repo_root="/repo")

    assert [label for label, _p, _r in calls] == ["first", "second"]
    assert all(p is params for _l, p, _r in calls)
    assert all(r == "/repo" for _l, _p, r in calls)
    assert result == no_advisory()


@pytest.mark.asyncio
async def test_a_raising_leg_does_not_suppress_its_sibling(legs, capsys):
    """The property the process boundary gave for free and `gather` does not."""
    calls = legs(("raiser", RuntimeError("audit log unwritable")), ("sibling", no_advisory()))

    result = await apd._handler({}, repo_root=None)

    assert [label for label, _p, _r in calls] == ["raiser", "sibling"]
    assert result == no_advisory()
    stderr = capsys.readouterr().err
    assert "leg=raiser" in stderr
    assert "RuntimeError" in stderr


@pytest.mark.asyncio
async def test_advisory_text_from_one_leg_survives_the_merge(legs):
    """Both legs return `no_advisory()` today; the merge must not hardcode that.

    A leg that grows prose later has to be heard without this module being edited --
    the same silent-drop shape `_EAGER_HOOK_MODULES` closes off one level up.
    """
    legs(("quiet", no_advisory()), ("loud", post_advisory("something worth saying")))

    result = await apd._handler({}, repo_root=None)

    assert result["hookSpecificOutput"]["additionalContext"].endswith(
        "something worth saying"
    )


@pytest.mark.asyncio
async def test_two_advisory_texts_merge_in_leg_order(legs):
    legs(("first", post_advisory("alpha")), ("second", post_advisory("beta")))

    result = await apd._handler({}, repo_root=None)

    context = result["hookSpecificOutput"]["additionalContext"]
    assert context.index("alpha") < context.index("beta")
    assert "\n\n" in context


@pytest.mark.asyncio
async def test_a_leg_returning_an_unexpected_shape_is_treated_as_silent(legs):
    """A leg's own shape bug must not take down the fan-in or its sibling."""
    calls = legs(("odd", "not a dict"), ("sibling", no_advisory()))

    result = await apd._handler({}, repo_root=None)

    assert [label for label, _p, _r in calls] == ["odd", "sibling"]
    assert result == no_advisory()


def test_the_op_is_registered_and_eagerly_imported():
    """Present-but-dead is the failure mode: a hooks op needs its eager-import entry."""
    from coordinator_core import hooks, ipc

    assert "coordinator_core.hooks.agent_postuse_dispatch" in hooks._EAGER_HOOK_MODULES
    assert ipc.get_op_handler("hooks.agent_postuse_dispatch") is not None


def test_scope_and_class_are_the_union_of_the_legs():
    """A fan-in inherits the strictest member; this pins it as declared, not discovered."""
    from coordinator_core import op_scopes
    from coordinator_core.authz import classification

    scopes = op_scopes.OP_KEY_SCOPE
    assert scopes["hooks.agent_postuse_dispatch"] == "common_dir"
    assert scopes["hooks.agent_completion_log"] == "common_dir"
    assert scopes["hooks.track_dispatched_agents"] == "common_dir"

    classes = classification.OP_CLASSIFICATION
    assert (
        classes["hooks.agent_postuse_dispatch"]
        == classes["hooks.agent_completion_log"]
        == classification.OpClass.MUTATING
    )


def test_the_folded_legs_stay_registered_for_direct_callers():
    """The fold is additive. Deregistering either leg is a separate, louder decision."""
    from coordinator_core import ipc

    assert ipc.get_op_handler("hooks.agent_completion_log") is not None
    assert ipc.get_op_handler("hooks.track_dispatched_agents") is not None
