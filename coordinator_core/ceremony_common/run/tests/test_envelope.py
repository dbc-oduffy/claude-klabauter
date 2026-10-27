"""Envelope adapter: directive-to-step mapping and the fail-closed ready/held/closed split."""
from __future__ import annotations

import pytest

from coordinator_core.ceremony_common.run import envelope as env


def _d(id, **kw):
    return {"id": id, "cli": "x-cli", "args": ["--a"], "depends_on": None,
            "already_satisfied": False, "detail": "d", "stdin_from": None, **kw}


def _jp(id, resolves):
    return {"id": id, "dispositions": [{"value": "go", "resolves": resolves},
                                       {"value": "skip", "resolves": []}]}


def _steps(directives):
    return env.steps_from_envelope(
        {"directives": directives}, stage_of=lambda i: "s", costs={"a": "engine"})


def _split(directives, jps, decisions):
    steps = _steps(directives)
    r, h, c = env.ready(steps, {j["id"]: j for j in jps}, decisions,
                        directive_ids={d["id"] for d in directives})
    return [s.id for s in r], [s.id for s in h], [s.id for s in c]


def test_steps_in_order_with_carry_through_and_costs():
    steps = _steps([_d("a", stdin_from="b", already_satisfied=True), {"id": "b", "op": "some.op"}])
    assert [s.id for s in steps] == ["a", "b"]
    assert steps[0].stdin_from == "b" and steps[0].already_satisfied
    assert steps[0].cost_class == "engine" and steps[1].cost_class == "unmeasured"
    assert steps[1].kind == "op" and steps[1].target == "some.op"
    assert steps[0].heading == "a"


def test_heading_of_and_unknown_id_raises():
    out = env.steps_from_envelope({"directives": [_d("a")]}, stage_of=lambda i: "s",
                                  costs={}, heading_of=lambda i: "H")
    assert out[0].heading == "H"

    def stage_of(i):
        raise KeyError(i)

    with pytest.raises(KeyError):
        env.steps_from_envelope({"directives": [_d("z")]}, stage_of=stage_of, costs={})


def test_ungated_ready():
    assert _split([_d("a")], [], {}) == (["a"], [], [])


def test_decided_resolving_point_ready():
    assert _split([_d("a", depends_on="p")], [_jp("p", ["a"])], {"p": {"disposition": "go"}}) == (["a"], [], [])


def test_undecided_point_held():
    assert _split([_d("a", depends_on="p")], [_jp("p", ["a"])], {}) == ([], ["a"], [])


def test_decision_resolving_other_directive_closes_this_one():
    r = _split([_d("a", depends_on="p"), _d("b")], [_jp("p", ["b"])], {"p": {"disposition": "go"}})
    assert r == (["b"], [], ["a"])


def test_skip_disposition_closes():
    assert _split([_d("a", depends_on="p")], [_jp("p", ["a"])], {"p": {"disposition": "skip"}}) == ([], [], ["a"])


def test_unknown_dep_held_never_closed():
    r = _split([_d("a", depends_on=["p", "ghost"])], [_jp("p", ["a"])], {"p": {"disposition": "skip"}})
    assert r == ([], ["a"], [])


def test_sibling_directive_dep_does_not_gate():
    assert _split([_d("a"), _d("b", depends_on="a")], [], {}) == (["a", "b"], [], [])


def test_parse():
    assert env.parse('{"directives": []}') == {"directives": []}
    for bad in ("nope", "[1]", ""):
        with pytest.raises(env.EnvelopeError):
            env.parse(bad)


def test_value_only_decision_is_decided():
    jps = [_jp("p", ["b"])]
    ds = [_d("a", depends_on="p"), _d("b")]
    assert _split(ds, jps, {"p": {"value": "go"}}) == (["b"], [], ["a"])
    assert _split(ds, jps, {"p": {}}) == (["b"], ["a"], [])
