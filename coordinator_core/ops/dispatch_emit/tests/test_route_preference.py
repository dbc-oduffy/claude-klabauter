"""Pins the route-preference leaf: validation, round trips, divergence, scout block."""

from coordinator_core.ops.dispatch_emit import route_preference as rp


def test_refusal_names_the_offending_key():
    assert "asks[0].route_preference" in rp.refusal("asks[0]", "shape", None)
    assert "ask.em_rationale" in rp.refusal("ask", "plan", "x" * 2001)
    assert "ask.em_rationale" in rp.refusal("ask", None, "   ")
    assert "ask.em_rationale" in rp.refusal("ask", None, 5)
    assert "ask.route_preference" in rp.refusal("ask", 3, None)


def test_refusal_none_when_absent_or_valid():
    assert rp.refusal("ask", None, None) is None
    assert rp.refusal("ask", "goal", "why") is None
    assert rp.refusal("ask", None, "x" * 2000) is None
    assert rp.refusal("ask", "roadmap", None) is None


def test_diverges():
    assert rp.diverges(rp.parse("goal", None), "goal-setting") is False
    assert rp.diverges(rp.parse("roadmap", None), "plan") is True
    assert rp.diverges(rp.parse(None, "why"), "plan") is False
    assert rp.diverges(rp.parse("plan", None), None) is True


def test_parse_strips_and_empties():
    assert rp.parse(" goal ", "  why ") == rp.RoutePreference("goal", "why")
    assert rp.parse(None, None).is_empty()
    assert rp.parse("", "  ").is_empty()


def test_json_round_trip_and_malformed():
    pref = rp.RoutePreference("roadmap", "spans two workstreams")
    assert rp.from_json(rp.to_json(pref)) == pref
    only = rp.RoutePreference(None, "why")
    assert rp.from_json(rp.to_json(only)) == only
    for bad in ("", "{not json", "[]", "null", '{"route_preference": "shape"}'):
        assert rp.from_json(bad).is_empty()


def test_em_analysis_round_trip_writes_only_non_null_keys():
    pref = rp.RoutePreference("plan", None)
    assert rp.to_em_analysis(pref) == {"route_preference": "plan"}
    assert rp.to_em_analysis(rp.RoutePreference(None, None)) == {}
    full = rp.RoutePreference("goal", "r")
    assert rp.from_em_analysis({"em_analysis": rp.to_em_analysis(full)}) == full
    assert rp.from_em_analysis({}).is_empty()
    assert rp.from_em_analysis({"em_analysis": "x"}).is_empty()


def test_scout_block_empty_and_populated():
    assert rp.scout_block(rp.RoutePreference(None, None)) == ""
    block = rp.scout_block(rp.RoutePreference("roadmap", "line one\nline two"))
    assert block.startswith(rp.SCOUT_BLOCK_HEADING)
    assert "roadmap" in block
    assert "> line one\n> line two" in block
    assert "route_override_reason" in block
    only_rationale = rp.scout_block(rp.RoutePreference(None, "why"))
    assert "> why" in only_rationale and "Route preference" not in only_rationale
