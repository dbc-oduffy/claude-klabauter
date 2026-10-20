"""Matcher cases DoE's `_fires` carried that the engine's `_pattern_matches`
lacked: n't negation, comma-bearing quoted spans, progressive status-line
waits, and the quantified plain-noun handoff exemption."""

from __future__ import annotations

import pytest

from coordinator_core.hooks.guard_manufactured_blocker import (
    _matches_manufactured_blocker,
    _plain_noun_handoff,
)


@pytest.mark.parametrize(
    "text",
    [
        "Nothing is waiting on you.",
        "The queue isn't waiting on you.",
        "It doesn't need your decision.",
        "That wasn't your call.",
    ],
)
def test_contraction_negation_is_silent(text):
    assert not _matches_manufactured_blocker(text)


def test_quoted_span_with_comma_is_silent():
    assert not _matches_manufactured_blocker('The peer wrote "ok, so it is your call" in its report.')


@pytest.mark.parametrize(
    "text",
    [
        "The merge gate is waiting on you.",
        "Build 4 is waiting on you to finish.",
    ],
)
def test_progressive_wait_status_line_is_silent(text):
    assert not _matches_manufactured_blocker(text)


@pytest.mark.parametrize(
    "text",
    [
        "The release is waiting on you: pick a date.",
        "Is the release waiting on you?",
        "I am waiting on you, please confirm.",
    ],
)
def test_progressive_wait_with_request_fires(text):
    assert _matches_manufactured_blocker(text)


def test_plain_noun_quantified_handoff_passes_altitude():
    text = "Keeping the old cache costs 40 GB of disk. It is your call."
    assert _matches_manufactured_blocker(text)
    assert _plain_noun_handoff(text)


@pytest.mark.parametrize(
    "text",
    [
        "Keeping the old cache is your call.",
        "Dropping hook_run.py saves 40 GB. It is your call.",
    ],
)
def test_plain_noun_requires_quantity_and_no_em_only_marker(text):
    assert _matches_manufactured_blocker(text)
    assert not _plain_noun_handoff(text)
