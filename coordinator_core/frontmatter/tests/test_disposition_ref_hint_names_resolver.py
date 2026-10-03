"""A plan-tasks row missing disposition_ref gets a hint naming plan-tasks-resolve.

The shared `_cf_disposition_shape` stays generic for its other callers
(carried_items, queue status); only the plan-tasks caller supplies the remedy.
"""

from __future__ import annotations

from coordinator_core.frontmatter.schema_validate import (
    _cf_disposition_shape,
    _cf_plan_tasks_disposition_shape,
)


def test_coded_without_ref_names_resolver():
    err = _cf_plan_tasks_disposition_shape({'id': 'A1', 'disposition': 'coded'})
    assert err is not None
    assert 'plan-tasks-resolve --id <row> --coded <sha>' in err['hint']


def test_other_ref_dispositions_name_matching_flag():
    spun = _cf_plan_tasks_disposition_shape(
        {'id': 'A1', 'disposition': 'spun_off', 'disposition_detail': 'x'}
    )
    back = _cf_plan_tasks_disposition_shape(
        {'id': 'A1', 'disposition': 'backlogged', 'disposition_detail': 'x'}
    )
    assert '--spun-off' in spun['hint']
    assert '--backlogged' in back['hint']


def test_generic_caller_hint_unchanged():
    err = _cf_disposition_shape(
        [{'disposition': 'coded'}],
        field_name='x',
        open_token='open',
        requires_ref=frozenset({'coded'}),
        detail_exempt=frozenset({'coded'}),
    )
    assert err['hint'] == "disposition 'coded' requires disposition_ref."
