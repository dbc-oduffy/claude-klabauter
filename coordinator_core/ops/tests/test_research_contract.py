"""Pins the research contract tables: totality of TIER_TABLE and closed vocabularies."""

from coordinator_core.ops import _research_contract as rc


def test_tier_table_total_over_class_and_appetite():
    assert set(rc.TIER_TABLE) == set(rc.VALUE_CLASSES)
    for vc in rc.VALUE_CLASSES:
        assert set(rc.TIER_TABLE[vc]) == set(rc.APPETITES)
        for tier in rc.TIER_TABLE[vc].values():
            assert tier in rc.TIERS


def test_medium_appetite_is_as_classed():
    for vc in rc.VALUE_CLASSES:
        assert rc.TIER_TABLE[vc]["medium"] == vc


def test_floor_sources_exclude_web_and_are_sources():
    assert rc.FLOOR_SOURCES == {"repo", "structured", "notebooklm"}
    assert rc.FLOOR_SOURCES <= set(rc.SOURCES)
    assert "web" not in rc.FLOOR_SOURCES


def test_roster_and_limits():
    assert rc.UNBLOCK_ROLES == ("diagnose", "decompose", "challenge")
    assert set(rc.SOURCE_SPECIALIST) <= set(rc.SOURCES)
    assert rc.MAX_SCOUT_QUESTIONS == 2
