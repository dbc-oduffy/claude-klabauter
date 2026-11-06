"""The scout's research clause steers survey asks to `corpus` and reserves `deep` for the unblock team."""

from __future__ import annotations

import re

from coordinator_core.ops import _research_contract as rc
from coordinator_core.ops.dispatch_emit import ask_contract as ac


def test_clause_names_corpus_for_survey_compare_rank() -> None:
    clause = ac.RESEARCH_CLASSIFY_CLAUSE
    m = re.search(r"`corpus`[^`]*", clause)
    assert m is not None
    for word in ("survey", "compare", "rank"):
        assert word in m.group(0)


def test_clause_ties_deep_to_stuck_unblock() -> None:
    m = re.search(r"`deep`[^.]*", ac.RESEARCH_CLASSIFY_CLAUSE)
    assert m is not None
    assert "stuck" in m.group(0)
    assert "unblock" in m.group(0)


def test_every_backticked_class_is_a_value_class() -> None:
    clause = ac.RESEARCH_CLASSIFY_CLAUSE
    named = {c for c in rc.VALUE_CLASSES if f"`{c}`" in clause}
    assert named == set(rc.VALUE_CLASSES)


def test_clause_routes_named_urls_to_seeds_not_targets() -> None:
    clause = ac.RESEARCH_CLASSIFY_CLAUSE
    assert "`seeds` as `<kind>=<url>`" in clause
    assert "never in `targets`" in clause
    for kind in rc.SEED_KINDS:
        assert kind in clause


def test_clause_keeps_sibling_synthesis_research_and_census_a_file_change() -> None:
    clause = ac.RESEARCH_CLASSIFY_CLAUSE
    assert "synthesis over the research outputs of sibling asks" in clause
    assert "return `research` null" in clause


def test_bounded_single_write_excludes_sibling_synthesis_keeps_census() -> None:
    research = {"value_class": "corpus"}
    census = {"research": research, "intent": "Classify the 10 reports in the folder"}
    synth = {"research": research, "intent": "Synthesis over the ten outputs of the sibling asks, 10 reports"}
    assert ac.research_is_bounded_single_write(census, ["out.md"]) is True
    assert ac.research_is_bounded_single_write(synth, ["out.md"]) is False


def test_scout_schema_accepts_then_build_as_a_boolean() -> None:
    props = ac.ASK_SCOUT_SCHEMA["properties"]["research"]["properties"]
    assert props["then_build"] == {"type": "boolean"}
    assert "then_build" in ac.RESEARCH_CLASSIFY_CLAUSE


def test_halt_research_differs_from_room() -> None:
    assert ac.HALT_RESEARCH == "research"
    assert ac.HALT_RESEARCH != ac.HALT_ROOM
