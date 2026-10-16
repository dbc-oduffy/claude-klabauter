"""ask_compose: the ledger verdict relays met_by, wave_reviews paths are relativised, the review wave gets the design brief."""

from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose

_PREFIX = ask_compose._DESIGN_PREFIX_JS


def test_ledger_check_schema_admits_met_by():
    item = ask_compose._LEDGER_CHECK_SCHEMA["properties"]["items"]["items"]["properties"]
    assert item["met_by"] == {"type": ["string", "array"], "items": {"type": "string"}}


def test_ledger_rules_tell_the_checker_to_set_met_by_on_design_items():
    assert "design-registry" in ask_compose._LEDGER_ITEMS_RULES
    assert "met_by" in ask_compose._LEDGER_ITEMS_RULES


def test_ledger_verdict_relay_copies_met_by():
    assert "...(i.met_by ? { met_by: i.met_by } : {})" in _compose(in_run_commit=True)


def test_wave_reviews_sidecar_paths_are_relativised():
    script = _compose(in_run_commit=True)
    assert "_ir.wave_reviews = _ir.wave_reviews.map(" in script
    assert "sidecar_path: _rel(w.sidecar_path)" in script
    assert script.index("const _rel = ") < script.index("_ir.wave_reviews = ")


def test_review_wave_agents_are_prefixed_with_the_design_brief():
    script = _compose()
    assert "const _reviewOuterAgent = agent;" in script
    assert f"const _reviewAgent = (p, o) => _reviewOuterAgent({_PREFIX} + p, o);" in script
    start = script.index("const _reviewAgent")
    review = script[script.index("_reviewPrep = (() =>", start):]
    assert re.search(r"await _reviewAgent\(", review)
    end = review.index("_reviewWave = []; }")
    assert not re.search(r"(?<![\w.])agent\(", review[:end])
