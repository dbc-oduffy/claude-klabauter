"""
coordinator_core.write_guards.tests.test_evaluate_payload_json_forwards_aggregate

`evaluate_payload_json` is the seam the out-of-repo PreToolUse dispatchers call;
they cannot reach `evaluate()`. This pins that every keyword `evaluate()` accepts
is reachable through it — specifically `aggregate`.

WHY THIS IS A SAFETY TEST, NOT AN API-TIDINESS TEST. Until 2026-08-21 `aggregate`
stopped at `evaluate()`. A dispatcher doing the obvious thing
(`evaluate_payload_json(raw, aggregate=True)`) got `TypeError: unexpected keyword
argument`. Those dispatchers wrap this call in a fail-open `except Exception`, so
the TypeError would have disabled EVERY guard for that event — hard-denies
included — and the outcome is byte-indistinguishable from a clean allow. A silent
total guard bypass, triggered by a caller asking for MORE checking, discoverable
only by noticing that nothing ever fired again.

That came within one commit of shipping: this repo asked doe-claude-em to pass
the keyword at their call site, having verified the aggregation behaviour against
`evaluate()` directly and never through this seam. They caught it in review. This
test is the artifact that stops the gap reopening, since the two functions can
drift apart again silently.

negative-spec -- do not weaken `test_signature_parity` to an allowlist of
"keywords we currently care about". Its value is that it fails when the two
signatures diverge for ANY reason, including a keyword nobody has thought about
yet. That is the whole defect class.
"""

from __future__ import annotations

import inspect
import json

from coordinator_core.write_guards import engine


def _payload(content: str = "import os\n") -> str:
    return json.dumps(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "harmless.py", "content": content},
            "session_id": "test-forwards-aggregate",
        }
    )


def test_signature_parity_between_seam_and_callee() -> None:
    """Every keyword-only param of `evaluate()` must exist on the seam."""
    inner = inspect.signature(engine.evaluate).parameters
    outer = inspect.signature(engine.evaluate_payload_json).parameters

    inner_kw = {
        n for n, p in inner.items() if p.kind is inspect.Parameter.KEYWORD_ONLY
    }
    outer_kw = {
        n for n, p in outer.items() if p.kind is inspect.Parameter.KEYWORD_ONLY
    }

    missing = inner_kw - outer_kw
    assert not missing, (
        "evaluate() accepts keyword(s) that evaluate_payload_json does not forward: "
        f"{sorted(missing)}.\n"
        "Out-of-repo dispatchers can only reach the seam. A keyword that exists on "
        "the inner function but not here raises TypeError into their fail-open "
        "`except Exception`, silently disabling EVERY guard — hard-denies included — "
        "for that event. Forward it explicitly."
    )


def test_aggregate_keyword_is_accepted_without_raising() -> None:
    """The literal call shape a dispatcher would write must not raise."""
    engine.evaluate_payload_json(_payload(), aggregate=True)
    engine.evaluate_payload_json(_payload(), aggregate=False)


def test_aggregate_is_actually_forwarded_not_merely_accepted() -> None:
    """Swallowing the keyword would satisfy the test above and change nothing.

    `aggregate=True` must reach `evaluate()`, which is observable in the return
    SHAPE: a list under aggregate, never a list without it.
    """
    seen: dict = {}
    real = engine.evaluate

    def spy(payload, **kwargs):
        seen.update(kwargs)
        return real(payload, **kwargs)

    engine.evaluate = spy  # type: ignore[assignment]
    try:
        engine.evaluate_payload_json(_payload(), aggregate=True)
    finally:
        engine.evaluate = real  # type: ignore[assignment]

    assert seen.get("aggregate") is True, (
        f"aggregate did not reach evaluate(); kwargs seen: {sorted(seen)}"
    )


def test_default_shape_is_unchanged_for_existing_callers() -> None:
    """Every current caller omits the keyword and must stay byte-identical:
    the first advisory or None, never a list."""
    out = engine.evaluate_payload_json(_payload())
    assert out is None or isinstance(out, dict)


def test_aggregate_true_returns_a_list_shape() -> None:
    out = engine.evaluate_payload_json(_payload(), aggregate=True)
    assert isinstance(out, list)
