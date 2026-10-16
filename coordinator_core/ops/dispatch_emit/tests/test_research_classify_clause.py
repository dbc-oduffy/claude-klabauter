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


def test_halt_research_differs_from_room() -> None:
    assert ac.HALT_RESEARCH == "research"
    assert ac.HALT_RESEARCH != ac.HALT_ROOM
