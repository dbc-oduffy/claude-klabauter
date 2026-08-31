"""
coordinator_core.hooks.tests.test_agent_postuse_payload_adapter -- tests for the
engine-side hook-payload flattening adapter in `agent_postuse_dispatch`.

Three obligations, matching the C2 chunk body (docs/plans/2026-08-31-the-door-reads-
stdin-and-the-payload-lands-flat.md § C2):

1. A raw, nested `PostToolUse` payload of the real shape is flattened to the flat
   scalars both legs read, and both legs receive them.
2. A payload this adapter cannot interpret still returns `-32602` (INVALID_PARAMS)
   -- the adapter DERIVES, it does not widen the accepted shape.
3. The flat-union shape that already works (the `command`-transport caller-side
   stub) keeps working completely unchanged -- no caller is broken by this
   adapter's existence.

Spec backlink: docs/plans/2026-08-31-the-door-reads-stdin-and-the-payload-lands-flat.md § C2
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.hooks import agent_postuse_dispatch as apd  # noqa: E402
from coordinator_core.hooks._envelope import no_advisory  # noqa: E402
from coordinator_core.ipc import CallerFacingValidationError  # noqa: E402


@pytest.fixture
def legs(monkeypatch):
    """Replace both legs with recording stubs; yields the call log.

    Mirrors `test_agent_postuse_dispatch.py::legs` -- patches `_LEGS` so this
    module stays silent about how the legs are implemented and pins only the
    params each leg is actually handed.
    """
    calls: list[tuple[str, dict, object]] = []

    def _stub(label, result):
        async def _run(params, repo_root=None):
            calls.append((label, params, repo_root))
            return result

        return _run

    def _install(*specs):
        monkeypatch.setattr(
            apd, "_LEGS", tuple((label, _stub(label, result)) for label, result in specs)
        )
        return calls

    return _install


# ---------------------------------------------------------------------------
# 1. A nested payload of the real shape reaches both legs with the right fields.
# ---------------------------------------------------------------------------


def _nested_payload(**overrides):
    payload = {
        "session_id": "em-session-abc123",
        "tool_name": "Agent",
        "tool_input": {
            "description": "do the thing",
            "subagent_type": "coordinator:executor",
            "name": "worker-1",
        },
        "tool_response": {
            "agentId": "abcdef1234567890",
            "agent_id": "worker-1@session-abc123",
            "resolvedModel": "claude-sonnet-5",
        },
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_nested_payload_is_flattened_for_both_legs(legs):
    calls = legs(("first", no_advisory()), ("second", no_advisory()))

    result = await apd._handler(_nested_payload(), repo_root="/repo")

    assert result == no_advisory()
    assert [label for label, _p, _r in calls] == ["first", "second"]
    for _label, flat, _repo_root in calls:
        assert flat["session_id"] == "em-session-abc123"
        assert flat["description"] == "do the thing"
        assert flat["subagent_type"] == "coordinator:executor"
        assert flat["name"] == "worker-1"
        assert flat["dispatched_agent_id"] == "abcdef1234567890"
        assert flat["dispatched_agent_id_snake"] == "worker-1@session-abc123"
        assert flat["dispatched_model"] == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_dispatched_model_cascades_when_resolved_model_absent(legs):
    calls = legs(("only", no_advisory()))

    payload = _nested_payload(
        tool_response={"agentId": "abcdef1234567890", "model": "fallback-model"}
    )
    await apd._handler(payload, repo_root="/repo")

    _label, flat, _repo_root = calls[0]
    assert flat["dispatched_model"] == "fallback-model"


# ---------------------------------------------------------------------------
# 2. A malformed payload still returns -32602 -- the adapter derives, it does
#    not widen the accepted shape.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_object_tool_input_raises_caller_facing_validation_error(legs):
    legs(("first", no_advisory()), ("second", no_advisory()))

    payload = {"tool_input": "not-an-object", "tool_response": {}}

    with pytest.raises(CallerFacingValidationError):
        await apd._handler(payload, repo_root="/repo")


@pytest.mark.asyncio
async def test_non_object_tool_response_raises_caller_facing_validation_error(legs):
    legs(("first", no_advisory()))

    payload = {"tool_input": {}, "tool_response": ["not", "an", "object"]}

    with pytest.raises(CallerFacingValidationError):
        await apd._handler(payload, repo_root="/repo")


@pytest.mark.asyncio
async def test_non_dict_params_raises_caller_facing_validation_error(legs):
    legs(("first", no_advisory()))

    with pytest.raises(CallerFacingValidationError):
        await apd._handler(["not", "a", "dict"], repo_root="/repo")  # type: ignore[arg-type]


def test_caller_facing_validation_error_is_a_value_error():
    """-32602 preservation rides on CallerFacingValidationError subclassing ValueError
    (coordinator_core.ipc's own dispatch_message error-shaping) -- pin the inheritance
    so a future refactor of that class cannot silently drop this adapter to -32603."""
    assert issubclass(CallerFacingValidationError, ValueError)
    assert getattr(CallerFacingValidationError, "caller_facing_validation", False) is True


# ---------------------------------------------------------------------------
# 3. The flat-union shape that works today still works unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flat_union_shape_is_passed_through_unchanged(legs):
    """No `tool_input`/`tool_response` key -- today's caller-side-stub shape.

    Identity-checked (`is`), not just equality: the adapter must not even copy
    this shape, so a caller depending on object identity (none does today, but
    the composition test in test_agent_postuse_dispatch.py already asserts
    `is`) keeps working.
    """
    calls = legs(("first", no_advisory()), ("second", no_advisory()))

    params = {
        "description": "do the thing",
        "subagent_type": "coordinator:executor",
        "dispatched_agent_id": "abcdef1234567890",
    }
    await apd._handler(params, repo_root="/repo")

    assert all(p is params for _l, p, _r in calls)


@pytest.mark.asyncio
async def test_flat_union_shape_with_no_fields_at_all_still_works(legs):
    """Both legs already treat an empty/absent flat payload as fully optional
    (agent_completion_log/track_dispatched_agents default-and-drop) -- the
    adapter must not turn an all-absent flat payload into a validation error."""
    calls = legs(("first", no_advisory()), ("second", no_advisory()))

    result = await apd._handler({}, repo_root=None)

    assert result == no_advisory()
    assert [label for label, _p, _r in calls] == ["first", "second"]


@pytest.mark.parametrize("falsy_non_object", [[], "", 0, 0.0, False])
def test_a_falsy_non_object_is_refused_like_a_truthy_one(falsy_non_object):
    """The refusal must not depend on the junk being TRUTHY.

    Regression pin. The adapter first read these as
    `params.get("tool_input") or {}`, which coerced every falsy non-object --
    `[]`, `""`, `0` -- into `{}` BEFORE the isinstance check ran. The guard
    then fired only on truthy junk like `[1, 2]`, so the malformed payloads
    closest to "empty" were the ones that validated clean and reached both
    legs as a silently-empty flat shape. A guard whose blind spot has the same
    shape as the defect it screens for reads green forever."""
    with pytest.raises(CallerFacingValidationError):
        apd._flatten_hook_payload(
            {"tool_input": falsy_non_object, "tool_response": {}}
        )
    with pytest.raises(CallerFacingValidationError):
        apd._flatten_hook_payload(
            {"tool_input": {}, "tool_response": falsy_non_object}
        )


def test_an_absent_or_null_key_is_not_the_refused_case():
    """The discriminating control for the pin above.

    Absent and explicit-null are the legitimate shapes a real PostToolUse
    payload carries; only a PRESENT non-object is malformed. Without this leg
    the pin above would still pass if the adapter started refusing everything."""
    assert apd._flatten_hook_payload({"tool_input": {"name": "x"}})["name"] == "x"
    assert apd._flatten_hook_payload(
        {"tool_input": None, "tool_response": None}
    )["name"] is None
