"""The composed grind script's land_run prompt carries the land_run reconcile clause once."""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit.land_run_contract import RECONCILE_CLAUSE

from .test_grind_compose import _compose


def _js_escaped(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def test_land_run_prompt_carries_clause_exactly_once():
    script = _compose()
    assert script.count(_js_escaped(RECONCILE_CLAUSE)) == 1
