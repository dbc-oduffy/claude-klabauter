"""A doubled-date `plan:` edge is never composed, and a refire resolves an existing one to the single-dated plan."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.sizing_fire import resumable_plan, single_dated_plan_edge
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import _plan

SINGLE = "docs/plans/2026-10-30-kane-skull.md"
DOUBLED = "docs/plans/2026-10-30-2026-10-30-kane-skull.md"


def test_date_prefixed_slug_yields_one_leading_date():
    assert single_dated_plan_edge(DOUBLED) == SINGLE
    assert single_dated_plan_edge("docs/plans/" + "2026-10-30-" + "2026-10-30-kane-skull.md") == SINGLE


def test_single_dated_and_inner_dates_untouched():
    assert single_dated_plan_edge(SINGLE) == SINGLE
    inner = "docs/plans/2026-10-30-foo-2026-10-30.md"
    assert single_dated_plan_edge(inner) == inner


def test_bind_plan_edge_writes_single_dated_edge(repo):  # noqa: F811
    import yaml

    from coordinator_core.ops.dispatch_emit.ask_gate import _bind_plan_edge
    from coordinator_core.ops.dispatch_emit.sizing_fire import load_sizing
    from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import _put

    (repo / ".git").mkdir(exist_ok=True)
    _put(repo, tshirt="S", route="spec-dispatch")
    sizing = load_sizing(repo, REL)
    assert _bind_plan_edge(repo, sizing, REL, DOUBLED) == SINGLE
    assert sizing["plan"] == SINGLE
    assert yaml.safe_load((repo / REL).read_text(encoding="utf-8"))["plan"] == SINGLE


def test_refire_resolves_doubled_edge_to_the_single_dated_plan(repo):  # noqa: F811
    _plan(repo, rel=SINGLE)
    plan = resumable_plan({"plan": DOUBLED}, REL, repo, "m_plus")
    assert plan is not None and plan.plan_rel == SINGLE


def test_doubled_edge_naming_a_real_file_is_used_as_is(repo):  # noqa: F811
    _plan(repo, rel=SINGLE)
    _plan(repo, rel=DOUBLED)
    plan = resumable_plan({"plan": DOUBLED}, REL, repo, "m_plus")
    assert plan is not None and plan.plan_rel == DOUBLED


def test_doubled_edge_with_no_plan_on_disk_stays_unresolved(repo):  # noqa: F811
    assert resumable_plan({"plan": DOUBLED}, REL, repo, "m_plus") is None
