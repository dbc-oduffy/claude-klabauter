"""Pins the research contract tables: totality of TIER_TABLE and closed vocabularies."""

import pytest

from coordinator_core.ops import _research_contract as rc


def test_tier_table_total_over_class_and_appetite():
    assert set(rc.TIER_TABLE) == set(rc.VALUE_CLASSES) - {rc.SPIKE_CLASS}
    for vc in rc.TIER_TABLE:
        assert set(rc.TIER_TABLE[vc]) == set(rc.APPETITES)
        for tier in rc.TIER_TABLE[vc].values():
            assert tier in rc.TIERS


def test_medium_appetite_is_as_classed():
    for vc in set(rc.TIER_TABLE) & set(rc.TIERS):
        assert rc.TIER_TABLE[vc]["medium"] == vc


def test_spike_has_no_tier_row():
    assert rc.SPIKE_CLASS in rc.VALUE_CLASSES
    assert rc.SPIKE_CLASS not in rc.TIER_TABLE


def test_floor_sources_exclude_web_and_are_sources():
    assert rc.FLOOR_SOURCES == {"repo", "structured", "notebooklm"}
    assert rc.FLOOR_SOURCES <= set(rc.SOURCES)
    assert "web" not in rc.FLOOR_SOURCES


def test_roster_and_limits():
    assert rc.UNBLOCK_ROLES == ("diagnose", "decompose", "challenge")
    assert set(rc.SOURCE_SPECIALIST) <= set(rc.SOURCES)
    assert rc.MAX_SCOUT_QUESTIONS == 8


def test_scout_target_and_mode_vocabulary():
    assert rc.SCOUT_TARGETS == ("web", "repo", "local")
    assert rc.DEFAULT_SCOUT_TARGET == "web"
    assert rc.SCOUT_MODES == ("split", "combine")
    assert rc.DEFAULT_SCOUT_MODE == "split"
    assert rc.COMBINED_SCOUT_ITEM == "question"
    assert rc.TARGET_LINE_PREFIX == "Target: "
    assert rc.SCOUT_QUESTIONS_PARAM == "scout_questions"
    assert rc.SCOUT_MODE_PARAM == "scout_mode"
    assert set(rc.ScoutQuestion.__annotations__) == {"text", "targets"}
    assert set(rc.ScoutTarget.__annotations__) == {"target", "ref"}
    assert rc.PROBES_LIST == "probes"
    assert rc.LOCAL_PROBE_FLAG == "local_probe"
    assert rc.TARGET_ENTRY_SEP == ":"
    assert rc.PROBE_ARG_PATTERN == r"[A-Za-z0-9._-]+"
    assert rc.SCOUTS_ASK_FRAGMENT == "contract/scouts-ask-fragment.json"
    assert rc.PROBE_ALLOWLIST_KEY == "probe_allowlist"
    assert rc.PROBE_NO_REF == "none"
    assert rc.QUESTION_MODE_KEY == "question_mode"
    assert rc.QUESTION_TARGETS_KEY == "question_targets"


def test_every_seed_kind_maps_to_a_source():
    assert set(rc.SEED_KIND_SOURCE) == set(rc.SEED_KINDS)
    assert set(rc.SEED_KIND_SOURCE.values()) <= set(rc.SOURCES)
    assert rc.SEED_KIND_SOURCE == {"repo": "repo", "video": "notebooklm", "doc": "web", "paper": "web"}


def test_seed_sources_first_seen_order_without_repeats():
    seeds = [
        {"kind": "video", "url": "u1"},
        {"kind": "doc", "url": "u2"},
        {"kind": "paper", "url": "u3"},
        {"kind": "video", "url": "u4"},
        {"kind": "repo", "url": "u5"},
    ]
    assert rc.seed_sources(seeds) == ["notebooklm", "web", "repo"]


def test_seed_sources_empty_or_absent_is_empty():
    assert rc.seed_sources([]) == []
    assert rc.seed_sources(None) == []


def test_seed_sources_unknown_kind_raises():
    with pytest.raises(ValueError):
        rc.seed_sources([{"kind": "podcast", "url": "u"}])
