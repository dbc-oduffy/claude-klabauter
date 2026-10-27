"""Replay for defect #9 (R11): one ask text must yield one tshirt and route across reps."""

import json

import pytest

import coordinator_core.sizing_assemble as sizing_assemble

NAME = "same-l-ask-sized-xs-s-m"


@pytest.fixture
def reps(load_fixture):
    fx = load_fixture(NAME)
    return json.loads((fx["dir"] / "reps.json").read_text(encoding="utf-8"))["reps"]


def test_fixture_carries_enough_reps(reps):
    assert len(reps) >= 5
    assert len({r["rep"] for r in reps}) == len(reps)


def assert_one_value(reps, key):
    values = {r[key] for r in reps}
    assert len(values) == 1, f"one ask yielded {key} {sorted(values)} across reps {[r['rep'] for r in reps]}"


# The two xfails below read the captured reps only; they document the defect and never run route().
# Engine determinism is pinned by test_route_replay_is_deterministic.
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="R11: the captured reps carry three distinct sizes")
def test_identical_ask_yields_one_tshirt_across_reps(reps):
    assert_one_value(reps, "tshirt")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="R11: the captured reps carry more than one route")
def test_identical_ask_yields_one_route_across_reps(reps):
    assert_one_value(reps, "route")


def test_assertion_accepts_uniform_and_refuses_varied_reps(reps):
    uniform = [dict(r, tshirt="M", route="plan") for r in reps]
    assert_one_value(uniform, "tshirt")
    assert_one_value(uniform, "route")
    varied = [dict(r, tshirt=("M" if i % 2 else "S")) for i, r in enumerate(uniform)]
    with pytest.raises(AssertionError, match="tshirt"):
        assert_one_value(varied, "tshirt")


def _routed(tshirt, **scope):
    out = sizing_assemble.route(estimate={"tshirt": tshirt}, **scope)
    return out["resolved_estimate"]["tshirt"], out["route"]


def test_route_replay_is_deterministic(reps):
    scope = {
        "scope_files": sizing_assemble.SCOPE_L_FLOOR_FILES,
        "scope_lines": sizing_assemble.SCOPE_L_FLOOR_LINES,
    }
    captured = {r["tshirt"] for r in reps}
    assert captured == {"XS", "S", "M"}
    outcomes = {_routed(t, **scope) for t in sorted(captured)}
    assert len(outcomes) == 1, f"one scope yielded {sorted(outcomes)} across scout estimates"
    assert outcomes == {("L", "plan")}


def test_route_replay_without_scope_still_diverges(reps):
    captured = {r["tshirt"] for r in reps}
    resolved = {t: _routed(t) for t in captured}
    assert len(set(resolved.values())) > 1, f"scope-absent estimates no longer diverge: {resolved}"
