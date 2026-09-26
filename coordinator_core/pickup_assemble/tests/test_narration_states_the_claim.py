
from __future__ import annotations

import pytest

from coordinator_core.pickup_assemble import _CLASSIFICATION_NOUN, _ready_summary

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
    narration, _ = _ready_summary("wat", [{"id": "d1"}], [])

    assert narration.startswith("You hold this artifact."), narration
