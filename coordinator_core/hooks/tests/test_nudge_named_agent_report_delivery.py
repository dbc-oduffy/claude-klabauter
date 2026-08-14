"""
Tests for coordinator_core.hooks.nudge_named_agent_report_delivery.

Subject: the PreToolUse advisory that warns a NAMED Agent dispatch cannot deliver its
final text to the dispatcher (a named agent is an Agent-teams teammate; only SendMessage
to "main" reaches the parent). See the module docstring for the 2026-07-30 incident this
was written against — two named dispatches, four idle notifications, zero reports.

The invariants worth pinning, in priority order:
  1. It NEVER blocks. Advisory-only in every branch — a regression to deny/rewrite here
     would gate every named dispatch in the fleet.
  2. `name` absent → silence. This is the common path; a false positive here would
     attach an irrelevant wall of text to every ordinary dispatch.
  3. `name` present + no delivery instruction → advisory naming both working shapes.
  4. `name` present + brief already routes via SendMessage-to-main → silence. A nag that
     fires on the correct shape trains the reader to ignore it.
  5. Malformed input never raises.
"""

from __future__ import annotations

import pytest

from coordinator_core.hooks.nudge_named_agent_report_delivery import _handler


def _run(params: dict) -> dict:
    return _handler(params)


def _advisory_text(result: dict) -> str:
    return result["hookSpecificOutput"]["additionalContext"]


# --------------------------------------------------------------------------------------
# 1. Never blocks — the invariant that matters most.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tool_input",
    [
        {"prompt": "go", "name": "scout"},
        {"prompt": "go", "name": "scout", "run_in_background": True},
        {"prompt": "go", "name": "scout", "subagent_type": "general-purpose"},
    ],
)
def test_never_denies_or_rewrites(tool_input: dict) -> None:
    """Advisory-only in every firing branch: allow, no updatedInput, no deny."""
    result = _run({"tool_name": "Agent", "tool_input": tool_input})
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "updatedInput" not in hso, "must not rewrite a named dispatch"


# --------------------------------------------------------------------------------------
# 2. Silence on the common paths.
# --------------------------------------------------------------------------------------

def test_non_agent_tool_is_silent() -> None:
    assert _run({"tool_name": "Bash", "tool_input": {"command": "ls", "name": "x"}}) == {}


def test_unnamed_dispatch_is_silent() -> None:
    """The default shape — an unnamed background agent's report IS delivered."""
    assert _run({"tool_name": "Agent", "tool_input": {"prompt": "investigate X"}}) == {}


@pytest.mark.parametrize("name", ["", "   "])
def test_blank_name_is_silent(name: str) -> None:
    """An empty/whitespace name does not create a teammate, so there is nothing to warn."""
    assert _run({"tool_name": "Agent", "tool_input": {"prompt": "go", "name": name}}) == {}


# --------------------------------------------------------------------------------------
# 3. The firing case.
# --------------------------------------------------------------------------------------

def test_named_dispatch_without_delivery_instruction_advises() -> None:
    result = _run({
        "tool_name": "Agent",
        "tool_input": {"prompt": "Investigate the thing. Report back: findings.",
                       "name": "flag-emitter"},
    })
    text = _advisory_text(result)
    assert "flag-emitter" in text, "advisory must name the agent it is about"
    assert "SendMessage" in text
    assert '"main"' in text
    # Design-as-offers: both working shapes present, so the EM is redirected rather
    # than merely told off.
    assert "DROP `name`" in text
    assert "KEEP `name`" in text


def test_advisory_does_not_argue_against_naming() -> None:
    """Naming is legitimate (mid-flight addressability); the advisory must not forbid it."""
    text = _advisory_text(_run({
        "tool_name": "Agent", "tool_input": {"prompt": "go", "name": "scout"},
    }))
    assert "Either is fine" in text


# --------------------------------------------------------------------------------------
# 4. Suppression when the brief already has a delivery channel.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "prompt",
    [
        'When done, deliver your report by calling SendMessage to "main".',
        "Send your findings to main via SendMessage when finished.",
        'Finish by calling SendMessage({to: "main", message: ...}).',
        # Token order reversed — the check is ordering-independent by design.
        'Your report goes to main. Use SendMessage.',
    ],
)
def test_brief_with_sendmessage_to_main_is_silent(prompt: str) -> None:
    assert _run({"tool_name": "Agent", "tool_input": {"prompt": prompt, "name": "scout"}}) == {}


@pytest.mark.parametrize(
    "prompt",
    [
        # SendMessage mentioned but no main target — could be agent-to-agent messaging,
        # which does not deliver to the EM, so the advisory must still fire.
        "Use SendMessage to coordinate with the other worker.",
        # "main" mentioned but no SendMessage — e.g. discussing the main branch.
        "Compare the diff against main and report back.",
    ],
)
def test_partial_match_still_advises(prompt: str) -> None:
    """Both tokens are required to suppress — one alone is not a delivery instruction."""
    result = _run({"tool_name": "Agent", "tool_input": {"prompt": prompt, "name": "scout"}})
    assert result != {}, f"should still advise: {prompt!r}"


# --------------------------------------------------------------------------------------
# 5. Malformed input never raises.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "params",
    [
        {},
        {"tool_name": "Agent"},
        {"tool_name": "Agent", "tool_input": None},
        {"tool_name": "Agent", "tool_input": "not-a-dict"},
        {"tool_name": "Agent", "tool_input": []},
        {"tool_name": "Agent", "tool_input": {"name": 123}},
        {"tool_name": "Agent", "tool_input": {"name": "scout"}},           # no prompt key
        {"tool_name": "Agent", "tool_input": {"name": "scout", "prompt": None}},
    ],
)
def test_malformed_input_never_raises(params: dict) -> None:
    result = _run(params)
    assert isinstance(result, dict)


def test_named_dispatch_with_no_prompt_still_advises() -> None:
    """A missing prompt cannot contain a delivery instruction, so the advisory fires."""
    assert _run({"tool_name": "Agent", "tool_input": {"name": "scout"}}) != {}


# --------------------------------------------------------------------------------------
# Registration — the op must be reachable by its dotted id, since the DoE-side hook
# script dispatches it by name.
# --------------------------------------------------------------------------------------

def test_op_is_registered() -> None:
    import coordinator_core.hooks  # noqa: F401 — triggers registration side-effects
    from coordinator_core.ipc import get_op_handler

    assert get_op_handler("hooks.nudge_named_agent_report_delivery") is not None
