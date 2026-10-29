"""A dated sizing slug yields one date, and the planner is told the path `s_plan_path` returns."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.sizing_fire import DATE_HEAD_PATTERN, PLAN_PATH_JS, s_plan_path
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose


def test_doubled_stem_resolves_to_one_date_even_with_differing_dates():
    assert s_plan_path("state/sizings/2026-10-29-2026-10-29-foo-0c5fcc.yaml") == "docs/plans/2026-10-29-foo-0c5fcc.md"
    assert s_plan_path("state/sizings/2026-10-29-2026-10-20-foo.yaml") == "docs/plans/2026-10-29-foo.md"


def test_single_date_stem_and_existing_doubled_edge_untouched():
    assert s_plan_path("state/sizings/2026-10-29-foo.yaml") == "docs/plans/2026-10-29-foo.md"


def test_scout_slug_strips_leading_date():
    script = _compose()
    line = next(ln for ln in script.splitlines() if "const _slugOf" in ln)
    assert f".replace(/^{DATE_HEAD_PATTERN}/, '')" in line


def test_planner_is_told_the_resolver_path_not_a_stem_placeholder():
    script = _compose()
    assert PLAN_PATH_JS in script
    assert "<sizing-stem>" not in script
    assert "_planPathFor(_sizingRel)" in script
