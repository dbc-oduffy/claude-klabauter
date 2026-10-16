"""compose_script: a plan declaring `front-end` in review_signals emits a front-end persona leg in the review wave."""

from __future__ import annotations

import copy
import re

from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

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


def test_no_front_end_signal_emits_no_senior_front_end_leg():
    assert _LEG not in _legs(_plan("[]"))
    assert _LEG not in _legs("---\ntitle: t\n---\n\n# t\n")
