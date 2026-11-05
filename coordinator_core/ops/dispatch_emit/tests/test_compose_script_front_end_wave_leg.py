"""compose_script: a plan declaring `front-end` in review_signals emits a front-end persona leg in the review wave."""

from __future__ import annotations

import copy
import re

import pytest

from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.ops.review_mint.roster import RosterFragmentError

from .conftest import REVIEW_KW

_LEG = "review:coordinator:senior-front-end"
_LABEL_RE = re.compile(r"label: '(review:[^']+)'")


def _roster_with_named_entry() -> dict:
    fragment = copy.deepcopy(REVIEW_KW["review_roster_fragment"])
    wave = next(s for s in fragment["execute_review"]["stages"] if s["kind"] == "review-wave")
    wave["agents"].append({"accepts_signals": "named", "per": "whole-diff", "schema": "wave"})
    return fragment


def _plan(signals: str) -> str:
    return f"---\ntitle: t\nreview_signals: {signals}\n---\n\n# t\n"


def _legs(plan_text: str) -> list[str]:
    row = WaveRow(id="C1", title="t", surface="s", writes=["pkg/c1.py"], reads=[], depends_on=[])
    script = compose_script(
        [[row]],
        name="wf",
        description="front-end signal",
        review_roster_fragment=_roster_with_named_entry(),
        review_stage_schemas=REVIEW_KW["review_stage_schemas"],
        plan_text=plan_text,
    )
    return _LABEL_RE.findall(script)


def test_front_end_signal_emits_the_senior_front_end_wave_leg():
    legs = _legs(_plan("[front-end]"))
    assert _LEG in legs, legs


def _ue_repo(tmp_path) -> str:
    (tmp_path / "Game.uproject").write_text("{}", encoding="utf-8")
    return str(tmp_path)


def test_ue_ask_composes_against_a_roster_with_no_named_entry(monkeypatch, tmp_path):
    from coordinator_core.ops.dispatch_emit import agent_route
    from coordinator_core.ops.dispatch_emit.ask_compose import _roster_has_named_wave_entry
    from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose

    monkeypatch.setattr(agent_route, "_model_of", lambda agent_type, defs, override=None: "opus")
    root = _ue_repo(tmp_path)
    assert _roster_has_named_wave_entry(REVIEW_KW["review_roster_fragment"]) is False
    script = _compose(repo_root=root)
    assert script
    with pytest.raises(RosterFragmentError):
        _compose(repo_root=root, required_reviewers=("game-dev:staff-game-dev",))


def test_roster_has_named_wave_entry_reads_the_wave_stage_only():
    from coordinator_core.ops.dispatch_emit.ask_compose import _roster_has_named_wave_entry

    assert _roster_has_named_wave_entry(_roster_with_named_entry()) is True
    assert _roster_has_named_wave_entry(REVIEW_KW["review_roster_fragment"]) is False
    assert _roster_has_named_wave_entry(None) is False


def test_no_front_end_signal_emits_no_senior_front_end_leg():
    assert _LEG not in _legs(_plan("[]"))
    assert _LEG not in _legs("---\ntitle: t\n---\n\n# t\n")
