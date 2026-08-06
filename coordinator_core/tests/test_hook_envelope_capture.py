"""
coordinator_core.tests.test_hook_envelope_capture — instrumentation seam
coverage for coordinator_core._hook_envelope's capture sink.

Review: coordinator:code-reviewer (Finding 1) — the capture_session()/_record()
seam introduced for the guard message-size discipline corpus had zero test
coverage: no test proved capture actually happens, nests correctly, or
restores the prior sink on exit. A broken restore leaks a sink across tests
and corrupts any corpus built afterwards after this module's tests run.

Spec backlink: docs/plans/2026-08-02-guard-message-size-discipline.md
"""

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
    """Calling a prose-carrying builder with no capture_session() active must
    not raise and must not have any observable side effect (the sink stays
    None — this is the hot-path no-measurement-work guarantee)."""
    import coordinator_core._hook_envelope as hook_envelope

    assert hook_envelope._capture_sink is None
    envelope = context_only("PreToolUse", "hello")
    assert envelope["hookSpecificOutput"]["additionalContext"].endswith("hello")
    assert hook_envelope._capture_sink is None


def test_capture_session_accumulates_across_all_five_builders():
    """Every one of the five prose-carrying builders must append to the
    active sink, in call order, as (builder_name, envelope) pairs."""
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
    """no_advisory() carries no prose and is explicitly not instrumented —
    it must never appear in the sink."""
    with capture_session() as sink:
        no_advisory()
        context_only("PreToolUse", "captured")

    assert len(sink) == 1
    assert sink[0][0] == "context_only"


def test_nested_capture_session_restores_prior_sink():
    """A nested capture_session() must restore the OUTER (possibly non-None)
    sink on exit, not clobber it back to None — a broken restore here leaks
    a sink across tests and corrupts any corpus built afterwards."""
    with capture_session() as outer_sink:
        context_only("PreToolUse", "outer-before")

        with capture_session() as inner_sink:
            context_only("PreToolUse", "inner")

        assert len(inner_sink) == 1
        assert inner_sink[0][0] == "context_only"

        # The outer sink must be active again (not None) after the inner
        # block exits, and must not have received the inner call.
        import coordinator_core._hook_envelope as hook_envelope

        assert hook_envelope._capture_sink is outer_sink
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

    assert hook_envelope._capture_sink is None
