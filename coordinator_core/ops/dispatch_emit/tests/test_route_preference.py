"""Pins the route-preference leaf: validation, round trips, divergence, scout block."""

from coordinator_core.ops.dispatch_emit import route_preference as rp


def test_refusal_names_the_offending_key():
    assert "asks[0].route_preference" in rp.refusal("asks[0]", "shape", None)
    assert "ask.em_rationale" in rp.refusal("ask", "plan", "x" * 2001)
    assert "ask.em_rationale" in rp.refusal("ask", None, "   ")
    assert "ask.em_rationale" in rp.refusal("ask", None, 5)
    assert "ask.route_preference" in rp.refusal("ask", 3, None)


def test_over_cap_refusal_names_length_cap_and_context():
    msg = rp.refusal("ask", None, "x" * 2037)
    assert "2037" in msg and "2000" in msg and "`context`" in msg
    assert "non-empty string" not in msg


def test_refusal_none_when_absent_or_valid():
    assert rp.refusal("ask", None, None) is None
    assert rp.refusal("ask", "goal", "why") is None
    assert rp.refusal("ask", None, "x" * 2000) is None
    assert rp.refusal("ask", "roadmap", None) is None


def test_refusal_first_line_carries_a_valid_example():
    assert 'e.g. route_preference: "plan"' in rp.refusal("ask", "shape", None).splitlines()[0]
    assert "e.g. em_rationale:" in rp.refusal("ask", None, 5).splitlines()[0]


def test_normalize_repairs_every_well_meant_shape():
    assert rp.normalize("plan", "why") == ("plan", "why")
    assert rp.normalize({"route_preference": "plan", "em_rationale": "why"}, None) == ("plan", "why")
    assert rp.normalize({"preference": "Roadmap", "rationale": "why"}, None) == ("roadmap", "why")
    assert rp.normalize({"route_preference": "plan", "em_rationale": "n"}, "flat") == ("plan", "flat")
    assert rp.normalize("goal-setting", None) == ("goal", None)
    assert rp.normalize(None, ["a", "b"]) == (None, "a\nb")
    assert rp.normalize(None, 7) == (None, "7")
    assert rp.normalize(None, {"k": "v"}) == (None, '{"k": "v"}')
    assert rp.normalize("plan", "   ") == ("plan", None)
    assert rp.normalize(None, None) == (None, None)


def test_normalize_truncates_an_over_length_rationale_to_the_cap():
    _, why = rp.normalize(None, "x" * 5000)
    assert len(why) <= rp.EM_RATIONALE_MAX_CHARS and why.endswith("[truncated]")
    assert rp.refusal("ask", None, why) is None
    _, exact = rp.normalize(None, "y" * 2000)
    assert exact == "y" * 2000


def test_normalize_leaves_an_unusable_preference_for_refusal_to_name():
    pref, _ = rp.normalize("shape", None)
    assert pref == "shape" and "route_preference" in rp.refusal("ask", pref, None)
    pref, _ = rp.normalize({"route_preference": "shape"}, None)
    assert rp.refusal("ask", pref, None) is not None


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


def test_scout_block_is_advisory_not_a_strong_prior():
    block = rp.scout_block(rp.RoutePreference("roadmap", None))
    assert "strong prior" not in rp.SCOUT_BLOCK_HEADING + block
    assert "advisory" in rp.SCOUT_BLOCK_HEADING
    assert "does not override" in block


def test_route_verdict_none_without_preference_or_route():
    assert rp.route_verdict(rp.parse(None, "why"), "plan", reason="r", basis=None) is None
    assert rp.route_verdict(rp.parse("plan", None), None, reason=None, basis=None) is None
    assert rp.route_verdict(rp.parse("plan", None), "", reason=None, basis=None) is None


def test_route_verdict_pushback_on_divergence():
    v = rp.route_verdict(rp.parse("roadmap", None), "plan", reason="four small fixes", basis="e")
    assert v == {
        "verdict": "pushback",
        "em_preferred": "roadmap",
        "sized_route": "plan",
        "reason": "four small fixes",
        "message": "EM preferred roadmap; sized as plan because four small fixes.",
    }


def test_route_verdict_independent_on_match():
    v = rp.route_verdict(rp.parse("goal", None), "goal-setting", reason=None, basis="evidence")
    assert v["verdict"] == "independent" and v["basis"] == "evidence"
    assert v["em_preferred"] == "goal" and v["sized_route"] == "goal-setting"
    assert "did not decide it" in v["message"]
    none_basis = rp.route_verdict(rp.parse("plan", None), "plan", reason=None, basis=None)
    assert none_basis["basis"] is None
