"""Pins corpus_fallback.fallback_refusal: refuses outage-plus-source-fallback, accepts stop wording."""

import pytest

from coordinator_core.ops.corpus_fallback import RULE, fallback_refusal


@pytest.mark.parametrize(
    "text",
    [
        "if example-retrieval-repo is unreachable, fall back to engine-source reads",
        "example-retrieval-repo ECONNREFUSED -- go ahead and do an engine-source read for the refire",
        "The corpus is down; read the engine source directly.",
        "rag drops again, so fall back to reading source",
        "example-retrieval-repo unavailable: grep the engine source instead.",
    ],
)
def test_refuses_fallback_instruction(text):
    reason = fallback_refusal(text)
    assert reason is not None and RULE in reason and "\n" not in reason


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        "example-retrieval-repo was ECONNREFUSED; stop and memo example-retrieval-repo-em",
        "corpus unreachable: return BLOCKED and report",
        "If example-retrieval-repo is down, do not fall back to engine source; stop.",
        "read the engine source for the UObject layout",
        "example-retrieval-repo is healthy; proceed",
    ],
)
def test_accepts_non_fallback_text(text):
    assert fallback_refusal(text) is None
