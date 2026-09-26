
from __future__ import annotations

from coordinator_core._hook_envelope import (
    allow_advisory,
    capture_session,
    context_only,
    deny,
    no_advisory,
    post_advisory,
    rewrite_input,
)


def test_no_op_without_a_sink_installed():
    import coordinator_core._hook_envelope as hook_envelope

    assert hook_envelope._capture_sink.get() is None
    envelope = context_only("PreToolUse", "hello")
    assert envelope["hookSpecificOutput"]["additionalContext"].endswith("hello")
    assert hook_envelope._capture_sink.get() is None


def test_capture_session_accumulates_across_all_five_builders():
    with capture_session() as sink:
        allow_advisory("PreToolUse", "a")
        context_only("PreToolUse", "b")
        post_advisory("c")
        deny("PreToolUse", "d")
        rewrite_input("PreToolUse", {"key": "value"}, context="e")

    assert [name for name, _ in sink] == [
        "allow_advisory",
        "context_only",
        "post_advisory",
        "deny",
        "rewrite_input",
    ]
    assert len(sink) == 5
    assert all(isinstance(envelope, dict) for _, envelope in sink)


def test_no_advisory_is_not_captured():
    with capture_session() as sink:
        no_advisory()
        context_only("PreToolUse", "captured")

    assert len(sink) == 1
    assert sink[0][0] == "context_only"


def test_nested_capture_session_restores_prior_sink():
    with capture_session() as outer_sink:
        context_only("PreToolUse", "outer-before")

        with capture_session() as inner_sink:
            context_only("PreToolUse", "inner")

        assert len(inner_sink) == 1
        assert inner_sink[0][0] == "context_only"

        import coordinator_core._hook_envelope as hook_envelope

        assert hook_envelope._capture_sink.get() is outer_sink
        context_only("PreToolUse", "outer-after")

    assert len(outer_sink) == 2
    assert [
        ctx["hookSpecificOutput"]["additionalContext"].rsplit("] ", 1)[-1]
        for _, ctx in outer_sink
    ] == [
        "outer-before",
        "outer-after",
    ]

    import coordinator_core._hook_envelope as hook_envelope

    assert hook_envelope._capture_sink.get() is None
