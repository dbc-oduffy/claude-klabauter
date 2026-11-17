"""Incident: a chained launcher child that re-sized in-run and landed on M+ handed off a second time and the launcher refused it."""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_plan_blitz import STAGE_FN
from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_HAND_OFF_ARM = re.compile(r"_gate\.arm === \S+\) \{ return \{ " + CHAIN_HANDOFF_KEY + ": ")


def _compose(*, chain_hop: bool) -> str:
    return compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=lambda text: (f"async function {STAGE_FN}() {{ return null; }}", []),
        plan_blitz_text="stub", launcher_child=True, chain_hop=chain_hop, **REVIEW_KW,
    )


def test_a_chained_child_that_sizes_in_run_carries_plan_blitz_and_no_hand_off_arm():
    script = _compose(chain_hop=True)
    assert f"async function {STAGE_FN}(" in script
    assert _HAND_OFF_ARM.search(script) is None


def test_a_first_hop_child_still_hands_off_on_m_plus():
    script = _compose(chain_hop=False)
    assert f"async function {STAGE_FN}(" not in script
    assert _HAND_OFF_ARM.search(script) is not None
