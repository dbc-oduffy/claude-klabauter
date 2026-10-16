"""Pins the research contract tables: totality of TIER_TABLE and closed vocabularies."""

from coordinator_core.ops import _research_contract as rc


def test_tier_table_total_over_class_and_appetite():
    assert set(rc.TIER_TABLE) == set(rc.VALUE_CLASSES)
    for vc in rc.VALUE_CLASSES:
        assert set(rc.TIER_TABLE[vc]) == set(rc.APPETITES)
        for tier in rc.TIER_TABLE[vc].values():
            assert tier in rc.TIERS


def test_medium_appetite_is_as_classed():
    for vc in set(rc.VALUE_CLASSES) & set(rc.TIERS):
        assert rc.TIER_TABLE[vc]["medium"] == vc


def test_spike_resolves_through_shape_as_corpus():
    from coordinator_core.ops import research_shape

    assert rc.TIER_TABLE["spike"] == rc.TIER_TABLE["corpus"]
    assert research_shape.shape({"value_class": "spike"})["tier"] == "corpus"
    assert research_shape.shape({"value_class": "spike", "appetite": "large"})["tier"] == "deep"


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
