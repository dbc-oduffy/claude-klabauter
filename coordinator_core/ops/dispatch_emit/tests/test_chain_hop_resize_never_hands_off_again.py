"""Incident: a chained launcher child that re-sized in-run and landed on M+ handed off a second time and the launcher refused it."""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ops.dispatch_emit import ask_compose, ask_roadmap
from coordinator_core.ops.dispatch_emit.ask_compose import AskComposeRefused, compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_plan_blitz import STAGE_FN, AskPlanBlitzRefused
from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_HAND_OFF_ARM = re.compile(r"_gate\.arm === \S+\) \{ return \{ " + CHAIN_HANDOFF_KEY + ": ")
_ANY_HAND_OFF = re.compile(r"return \{ " + CHAIN_HANDOFF_KEY + ": ")


_RB_TEXT = "export const meta = { name: 'rb', description: 'd', phases: [{ title: 'RB' }] };\nreturn {};\n"


def _compose(*, chain_hop: bool) -> str:
    return compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=lambda text: (f"async function {STAGE_FN}() {{ return null; }}", []),
        plan_blitz_text="stub", roadmap_blitz_text=_RB_TEXT, launcher_child=True, chain_hop=chain_hop, **REVIEW_KW,
    )


def test_a_chained_child_that_sizes_in_run_carries_plan_blitz_and_no_hand_off_arm():
    script = _compose(chain_hop=True)
    assert f"async function {STAGE_FN}(" in script
    assert _HAND_OFF_ARM.search(script) is None


def test_a_first_hop_child_still_hands_off_on_m_plus():
    script = _compose(chain_hop=False)
    assert f"async function {STAGE_FN}(" not in script
    assert _HAND_OFF_ARM.search(script) is not None


def test_a_chain_hop_script_returns_no_hand_off_on_any_arm():
    script = _compose(chain_hop=True)
    assert _ANY_HAND_OFF.search(script) is None
    assert _ANY_HAND_OFF.search(_compose(chain_hop=False)) is not None


def test_a_chain_hop_research_legs_keep_the_halt_instead_of_handing_off():
    script = _compose(chain_hop=True)
    assert "a chained run cannot chain the research sibling again" in script
    assert "a chained run cannot chain the research run again" in script
    assert "a chained run cannot chain" not in _compose(chain_hop=False)


def test_a_chain_hop_that_cannot_embed_the_roadmap_arm_refuses_and_a_first_hop_still_drops_it(monkeypatch):
    monkeypatch.setattr(ask_compose, "STAGES_EMBED_BYTE_BUDGET", 1)
    with pytest.raises(AskComposeRefused, match="roadmap arm"):
        _compose(chain_hop=True)
    assert _ANY_HAND_OFF.search(_compose(chain_hop=False)) is not None


def test_a_chain_hop_whose_roadmap_stage_will_not_wrap_halts_instead_of_handing_off(monkeypatch):
    def _boom(*_a, **_k):
        raise AskPlanBlitzRefused("gone")

    monkeypatch.setattr(ask_roadmap, "wrap_roadmap_stage", _boom)
    script = _compose(chain_hop=True)
    assert _ANY_HAND_OFF.search(script) is None
    assert "a chained run holds no roadmap stage and cannot hand off again" in script
    assert _ANY_HAND_OFF.search(_compose(chain_hop=False)) is not None
