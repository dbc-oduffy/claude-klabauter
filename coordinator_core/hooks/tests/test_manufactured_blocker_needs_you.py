"""Turn-closers that hand the PM an EM call by saying it "needs you" fire; an
operator-only act stated as the act itself does not."""

from __future__ import annotations

import pytest

from coordinator_core.hooks.guard_manufactured_blocker import _matches_manufactured_blocker


@pytest.mark.parametrize(
    "text",
    [
        "The full release still needs you to look at the feature list.",
        "The one thing that genuinely needs you is the Firebase re-login.",
        "When you're back, two things need you.",
        "Releasing the engine will be a single yes or no for you.",
        "The merge only needs your go.",
        "It is ready for you to approve.",
    ],
)
def test_needs_you_hand_up_fires(text):
    assert _matches_manufactured_blocker(text)


@pytest.mark.parametrize(
    "text",
    [
        "To refresh the expired token, run `! firebase login --reauth`.",
        "Nothing here needs you; the publish landed.",
        'The peer wrote "this needs you" in its own report.',
    ],
)
def test_stated_act_or_negated_or_quoted_is_silent(text):
    assert not _matches_manufactured_blocker(text)
