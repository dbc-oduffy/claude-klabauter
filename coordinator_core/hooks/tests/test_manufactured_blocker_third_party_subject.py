"""A clause whose subject is a named third party does not trigger the
manufactured-blocker hand-up check; a first-person clause still does."""

from __future__ import annotations

import pytest

from coordinator_core.hooks.guard_manufactured_blocker import _matches_manufactured_blocker


@pytest.mark.parametrize(
    "text",
    [
        "coordinator-content-repo-55 is waiting on the PM's accept",
        "coordinator-content-repo-55 is waiting on you",
        "example-stats-repo-1f is waiting on you",
        "The peer is waiting on you",
        "peer needs your decision",
        "The Group EM is waiting on you",
    ],
)
def test_third_party_subject_is_silent(text):
    assert not _matches_manufactured_blocker(text)


@pytest.mark.parametrize(
    "text",
    [
        "I'm waiting on your call to proceed",
        "coordinator-content-repo-55 is idle and I'm waiting on you",
        "coordinator-content-repo-55 is done, so we're waiting on you",
        "The peer shipped it; my read is it now waits on you",
        "coordinator-content-repo-55 is done. Your call.",
        "The peer is blocked, and this session waits on you",
        "Our peer is waiting on you",
    ],
)
def test_first_person_or_fresh_clause_fires(text):
    assert _matches_manufactured_blocker(text)
