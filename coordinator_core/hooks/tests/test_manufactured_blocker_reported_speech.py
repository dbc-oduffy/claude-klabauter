"""Reported speech and quotations do not trigger the manufactured-blocker
hand-up check; a handoff aimed at the reader still does."""

from __future__ import annotations

import pytest

from coordinator_core.hooks.guard_manufactured_blocker import _matches_manufactured_blocker


@pytest.mark.parametrize(
    "text",
    [
        "Angelique ruled the rollout stays staged; that was your call earlier, now settled.",
        "Per the APM ruling, option B ships, so it is your call only in the sense already made.",
        "The APM's call was to defer it, which was your call to delegate.",
        "That was your call last week and it is done.",
        'The log reads "this one is your call" from the other session.',
    ],
)
def test_reported_speech_is_silent(text):
    assert not _matches_manufactured_blocker(text)


@pytest.mark.parametrize(
    "text",
    [
        "Both options work. Your call.",
        "Tests pass. Up to you whether to ship; this is your call.",
        "Angelique ruled on A; but the second item is your call.",
        "Done with the edit. It now waits on you.",
    ],
)
def test_real_handoff_fires(text):
    assert _matches_manufactured_blocker(text)
