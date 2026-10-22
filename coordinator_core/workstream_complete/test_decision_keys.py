"""Tests for completion decision-key alias resolution and refusal."""

from coordinator_core.workstream_complete.decision_keys import (
    COMPLETION_KEY_ALIASES,
    refusal_message,
    refused_decision_keys,
    resolve_entry_surfaces,
)


def test_alias_wins_per_surface():
    s = resolve_entry_surfaces(
        {
            "title": "t",
            "completion_title": "ct",
            "prose": "p",
            "completion_prose": "cp",
            "nature": "n",
            "completion_nature": "cn",
        }
    )
    assert (s.title, s.prose, s.nature_key_value) == ("ct", "cp", "cn")


def test_empty_alias_falls_through_to_bare_key():
    s = resolve_entry_surfaces({"title": "t", "completion_title": "  ", "prose": "p"})
    assert s.title == "t"
    assert s.prose == "p"
    assert s.nature_key_value is None
    assert s.aliased == ()


def test_aliased_ordering():
    s = resolve_entry_surfaces({"completion_nature": "n", "completion_title": "t"})
    assert s.aliased == ("completion_title", "completion_nature")


def test_refused_keys_sorted_and_judgment_ids_pass():
    d = {
        "completion_zeta": 1,
        "completion_bogus": 2,
        "completion_title": "x",
        "completion-nature-classification": "y",
        "title": "z",
    }
    assert refused_decision_keys(d) == ("completion_bogus", "completion_zeta")
    assert refused_decision_keys({"completion-nature-classification": "y"}) == ()


def test_message_names_every_key_and_accepted_set():
    msg = refusal_message(("completion_a", "completion_b"))
    assert "completion_a" in msg and "completion_b" in msg
    for k in COMPLETION_KEY_ALIASES:
        assert k in msg
    assert "\n" not in msg
