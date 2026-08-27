"""coordinator_core.pickup_assemble.tests.test_narration_states_the_claim —
pins the EM-facing register of `_ready_summary`'s narration (PM ruling
2026-08-19, memo `2026-08-19-claude-klabauter-em-pickup-skill-leads-with-the-
assembler`).

The narration's first sentence is what the EM now HOLDS, never how that was
worked out. The prior string — "Computed the brief: N directive(s) ready to
run." — told the EM a brief had been computed, and therefore that something
computed it. The observed failure mode is an EM opening its pickup turn with
"I'll start by running the pickup assembler", then not running it and instead
hand-reading `cat <handoff> | head -60` — a truncated read of the artifact
whose routing, gates, and claim the brief had already resolved.

So the vocabulary of the machinery is banned outright rather than merely
de-emphasised: an EM that learns an assembler exists goes looking for it.
Offering it for inspection ("you can check it if you'd like") is the same
defect in a softer register, which is why this pins absence of the nouns and
not just their position in the sentence.
"""

from __future__ import annotations

import pytest

from coordinator_core.pickup_assemble import _CLASSIFICATION_NOUN, _ready_summary

#: Vocabulary that names the mechanism rather than its outcome. Substring
#: match, case-folded — "the brief"/"briefing" both land on `brief`.
_BANNED = ("assembl", "brief", "pre-comput", "precomput", "computed", "hook", "decision object")


@pytest.mark.parametrize("classification", sorted(_CLASSIFICATION_NOUN))
@pytest.mark.parametrize(
    "judgment_points",
    [[], [{"id": "j-kind"}]],
    ids=["coast-clear", "point-open"],
)
def test_narration_leads_with_the_claim(classification: str, judgment_points: list[dict]) -> None:
    narration, _ = _ready_summary(classification, [{"id": "d1"}], judgment_points)

    assert narration.startswith(f"You hold this {_CLASSIFICATION_NOUN[classification]}."), narration


@pytest.mark.parametrize("classification", sorted(_CLASSIFICATION_NOUN))
@pytest.mark.parametrize(
    "judgment_points",
    [[], [{"id": "j-kind"}]],
    ids=["coast-clear", "point-open"],
)
def test_narration_never_names_the_machinery(classification: str, judgment_points: list[dict]) -> None:
    narration, next_move = _ready_summary(classification, [{"id": "d1"}], judgment_points)

    for banned in _BANNED:
        assert banned not in narration.casefold(), f"{banned!r} leaked into narration: {narration}"
        assert banned not in next_move.casefold(), f"{banned!r} leaked into next_move: {next_move}"


def test_unknown_classification_still_states_a_claim() -> None:
    """The defensive default keeps the register — a classification this dict
    does not know still tells the EM it holds something, rather than falling
    back to describing what ran."""
    narration, _ = _ready_summary("wat", [{"id": "d1"}], [])

    assert narration.startswith("You hold this artifact."), narration
