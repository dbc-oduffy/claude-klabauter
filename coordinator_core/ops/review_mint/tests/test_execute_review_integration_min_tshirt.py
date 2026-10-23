"""compose_execute_review: an integration agent carrying min_tshirt is gated exactly as a review-wave agent."""

import copy

import pytest

from coordinator_core.ops.dispatch_emit.wake_digest import INTEGRATION_NOT_RUN_VAR
from coordinator_core.ops.review_mint.execute_review import compose_execute_review
from coordinator_core.ops.review_mint.roster import parse_execute_review
from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _v5_fragment


def _roster(min_tshirt="L"):
    fragment = copy.deepcopy(_v5_fragment())
    for stage in fragment["execute_review"]["stages"]:
        if stage["kind"] == "integration":
            agent = {
                "agentType": "coordinator:overengineering-reviewer",
                "model": "opus",
                "effort": "low",
                "applies": "residue",
                "schema": "review-integration-result",
            }
            if min_tshirt:
                agent["min_tshirt"] = min_tshirt
            stage["agents"] = [agent]
    return parse_execute_review(fragment, signals={"named": ["coordinator:staff-eng"]})


def _integration_block(**extra):
    kwargs = dict(
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["a/one.py"],
        prompt_head="BRIEF",
        run_key="k",
        executor_rows=2,
    )
    roster = _roster(extra.pop("min_tshirt", "L"))
    kwargs.update(extra)
    return dict(compose_execute_review(roster, **kwargs))["Review integration"]


@pytest.mark.parametrize("size", ["XS", "S", "M"])
def test_known_small_size_composes_no_integration_call(size):
    block = _integration_block(tshirt=size)
    assert "agent(" not in block
    assert "coordinator:overengineering-reviewer" not in block
    assert f"const {INTEGRATION_NOT_RUN_VAR} = true;" in block
    assert "const _reviewIntegration = null;" in block


@pytest.mark.parametrize("size", ["L", "XL"])
def test_known_admitted_size_composes_the_call(size):
    block = _integration_block(tshirt=size)
    assert "coordinator:overengineering-reviewer" in block
    assert f"const {INTEGRATION_NOT_RUN_VAR} = false;" in block


def test_fire_time_size_gates_the_call_in_js():
    block = _integration_block(tshirt=None, tshirt_js="_tshirt")
    assert "coordinator:overengineering-reviewer" in block
    assert f"const {INTEGRATION_NOT_RUN_VAR} = !([" in block
    assert '"L"' in block and '"XS"' not in block
    assert "(_verifyOnly || " + INTEGRATION_NOT_RUN_VAR + ")" in block


def test_unknown_size_follows_the_wave_gate_and_is_skipped():
    block = _integration_block()
    assert "coordinator:overengineering-reviewer" not in block
    assert f"const {INTEGRATION_NOT_RUN_VAR} = true;" in block


def test_ungated_integration_still_runs_at_any_size():
    block = _integration_block(min_tshirt=None, tshirt="XS")
    assert "coordinator:overengineering-reviewer" in block
    assert f"const {INTEGRATION_NOT_RUN_VAR} = false;" in block


def test_digest_reports_gated_off_stage_as_not_run():
    from coordinator_core.ops.dispatch_emit import wake_digest as wd
    from coordinator_core.ops.dispatch_emit.tests.test_wake_digest_contract import _kwargs

    kwargs = _kwargs()
    kwargs["review_vars"] = {
        "prep": "_reviewPrep",
        "wave": "_reviewWave",
        "delivery": "_deliveryVerdict",
        "integration": "_reviewIntegration",
    }
    js = wd.completion_return_js(**kwargs)
    assert f"(typeof {INTEGRATION_NOT_RUN_VAR} !== 'undefined' && {INTEGRATION_NOT_RUN_VAR}) ? 'not_run'" in js
