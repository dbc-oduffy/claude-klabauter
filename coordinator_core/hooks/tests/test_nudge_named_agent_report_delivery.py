
from __future__ import annotations

import pytest

from coordinator_core.hooks.nudge_named_agent_report_delivery import _handler


def _run(params: dict) -> dict:
    return _handler(params)


def _advisory_text(result: dict) -> str:
    return result["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize(
    "tool_input",
    [
        {"prompt": "go", "name": "scout"},
        {"prompt": "go", "name": "scout", "run_in_background": True},
        {"prompt": "go", "name": "scout", "subagent_type": "general-purpose"},
    ],
)
def test_never_denies_or_rewrites(tool_input: dict) -> None:
    result = _run({"tool_name": "Agent", "tool_input": tool_input})
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "updatedInput" not in hso, "must not rewrite a named dispatch"


def test_non_agent_tool_is_silent() -> None:
    assert _run({"tool_name": "Bash", "tool_input": {"command": "ls", "name": "x"}}) == {}


def test_unnamed_dispatch_is_silent() -> None:
    assert _run({"tool_name": "Agent", "tool_input": {"prompt": "investigate X"}}) == {}


@pytest.mark.parametrize("name", ["", "   "])
def test_blank_name_is_silent(name: str) -> None:
    assert _run({"tool_name": "Agent", "tool_input": {"prompt": "go", "name": name}}) == {}


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
    assert "Drop `name`" in text
    assert "Keep `name`" in text


def test_advisory_does_not_argue_against_naming() -> None:
    text = _advisory_text(_run({
        "tool_name": "Agent", "tool_input": {"prompt": "go", "name": "scout"},
    }))
    assert "Either is fine" in text


@pytest.mark.parametrize(
    "prompt",
    [
        'When done, deliver your report by calling SendMessage to "main".',
        "Send your findings to main via SendMessage when finished.",
        'Finish by calling SendMessage({to: "main", message: ...}).',
        'Your report goes to main. Use SendMessage.',
    ],
)
def test_brief_with_sendmessage_to_main_is_silent(prompt: str) -> None:
    assert _run({"tool_name": "Agent", "tool_input": {"prompt": prompt, "name": "scout"}}) == {}


@pytest.mark.parametrize(
    "prompt",
    [
        "Use SendMessage to coordinate with the other worker.",
        "Compare the diff against main and report back.",
    ],
)
def test_partial_match_still_advises(prompt: str) -> None:
    result = _run({"tool_name": "Agent", "tool_input": {"prompt": prompt, "name": "scout"}})
    assert result != {}, f"should still advise: {prompt!r}"


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"tool_name": "Agent"},
        {"tool_name": "Agent", "tool_input": None},
        {"tool_name": "Agent", "tool_input": "not-a-dict"},
        {"tool_name": "Agent", "tool_input": []},
        {"tool_name": "Agent", "tool_input": {"name": 123}},
        {"tool_name": "Agent", "tool_input": {"name": "scout"}},
        {"tool_name": "Agent", "tool_input": {"name": "scout", "prompt": None}},
    ],
)
def test_malformed_input_never_raises(params: dict) -> None:
    result = _run(params)
    assert isinstance(result, dict)


def test_named_dispatch_with_no_prompt_still_advises() -> None:
    assert _run({"tool_name": "Agent", "tool_input": {"name": "scout"}}) != {}


def test_op_is_registered() -> None:
    import coordinator_core.hooks  # noqa: F401 — triggers registration side-effects
    from coordinator_core.ipc import get_op_handler

    assert get_op_handler("hooks.nudge_named_agent_report_delivery") is not None


def test_a_sidecar_brief_still_fires_even_when_it_names_sendmessage_main() -> None:
    result = _run({
        "tool_name": "Agent",
        "tool_input": {
            "name": "code-reviewer",
            "prompt": "Write findings to the sidecar, then SendMessage to main.",
        },
    })

    assert "NAMED DISPATCH" in _advisory_text(result)


def test_a_subagent_share_path_counts_as_a_sidecar() -> None:
    result = _run({
        "tool_name": "Agent",
        "tool_input": {
            "name": "code-reviewer",
            "prompt": "Findings go in state/subagent-share/x.md; SendMessage main after.",
        },
    })

    assert "NAMED DISPATCH" in _advisory_text(result)


def test_a_correct_non_sidecar_brief_is_still_suppressed() -> None:
    result = _run({
        "tool_name": "Agent",
        "tool_input": {
            "name": "scout",
            "prompt": "SendMessage to main with your answer when done.",
        },
    })

    assert result.get("hookSpecificOutput", {}).get("additionalContext") in (None, "")


def test_the_advisory_names_what_an_unfilled_scaffold_costs() -> None:
    result = _run({
        "tool_name": "Agent",
        "tool_input": {"name": "code-reviewer", "prompt": "review it"},
    })

    text = _advisory_text(result)
    assert "sidecar" in text
    assert "pointer" in text
