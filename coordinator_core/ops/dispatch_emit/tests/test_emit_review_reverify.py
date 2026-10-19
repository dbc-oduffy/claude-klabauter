"""The plan route composes the Review re-verify phase before its scoped-test leg.

The composed script is pinned through the byte-parity golden (the same door invocation the parity
test drives), so a drift in either surface fails here with a named reason.
"""

from __future__ import annotations

from pathlib import Path

_FIXTURES = Path(__file__).resolve().parents[3] / "benchmarks" / "fixtures"
_GOLDEN = _FIXTURES / "emit_full_path_plan.expected.script.mjs"
_EMIT = Path(__file__).resolve().parents[1] / "emit.py"


def _script() -> str:
    return _GOLDEN.read_bytes().decode("utf-8").replace("\r\n", "\n")


def test_reverify_phase_follows_review_wave_and_precedes_verify():
    text = _script()
    titles = text.index("phases: [")
    phases = text[titles : text.index("]", titles)]
    assert phases.index("'Review wave'") < phases.index("'Review re-verify'") < phases.index("'Verify'")
    assert text.index("phase('Review wave')") < text.index("phase('Review re-verify')") < text.index("phase('Verify')")


def test_reverify_phase_names_runner_and_engine_op():
    text = _script()
    body = text[text.index("phase('Review re-verify')") : text.index("phase('Verify')")]
    assert "coordinator:test-runner" in body
    assert "review.reverify" in body
    assert "_reviewReverify" in body


def test_emit_turns_reverify_on():
    src = _EMIT.read_text(encoding="utf-8")
    call = src[src.index("compose_execute_review(\n        review,") :]
    assert "reverify=True," in call[: call.index("):")]
