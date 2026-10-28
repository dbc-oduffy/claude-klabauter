"""premise_close waives pm_approved only on a wont_do row whose premise edge matches."""
from __future__ import annotations

import jsonschema

from coordinator_core.frontmatter.schema_validate import (
    _PLAN_TASKS_SCHEMA_DICT,
    _cf_plan_tasks_disposition_shape,
)

_EDGE = {'chunk': 'C1', 'gate_kind': 'epistemic-premise'}
_CLOSE = {'chunk': 'C1', 'verdict': 'no', 'ref': 'state/audits/x.md'}


def _row(**kw):
    row = {
        'id': 'C2',
        'disposition': 'wont_do',
        'disposition_detail': 'premise C1 recorded no',
        'case_against': 'the premise failed',
        'depends_on': [dict(_EDGE)],
        'premise_close': dict(_CLOSE),
    }
    row.update(kw)
    return row


def test_matching_premise_close_waives_pm_approved():
    assert _cf_plan_tasks_disposition_shape(_row()) is None


def test_no_premise_close_still_requires_pm_approved():
    row = _row()
    del row['premise_close']
    assert _cf_plan_tasks_disposition_shape(row)['field'] == 'pm_approved'


def test_premise_close_naming_other_chunk_refused():
    row = _row(premise_close={**_CLOSE, 'chunk': 'C9'})
    assert _cf_plan_tasks_disposition_shape(row)['field'] == 'pm_approved'


def test_edge_of_other_gate_kind_refused():
    row = _row(depends_on=[{'chunk': 'C1', 'gate_kind': 'output-consumption-runtime'}])
    assert _cf_plan_tasks_disposition_shape(row)['field'] == 'pm_approved'


def test_backlogged_with_premise_close_refused():
    row = _row(disposition='backlogged', disposition_ref='docs/x.md')
    assert _cf_plan_tasks_disposition_shape(row)['field'] == 'pm_approved'


def test_governed_flag_unchanged():
    row = _row()
    del row['premise_close']
    assert _cf_plan_tasks_disposition_shape(row, governed=True) is None


def _schema_errors(row):
    v = jsonschema.Draft202012Validator(_PLAN_TASKS_SCHEMA_DICT)
    return [e.message for e in v.iter_errors(row)]


def _no_pm_required(errors):
    return not any("'pm_approved' is a required property" in m for m in errors)


def test_schema_waives_pm_approved_required_for_wont_do_premise_close():
    assert _no_pm_required(_schema_errors(_row()))


def test_schema_still_requires_pm_approved_without_premise_close():
    row = _row()
    del row['premise_close']
    assert not _no_pm_required(_schema_errors(row))


def test_schema_refuses_premise_close_verdict_yes():
    errors = _schema_errors(_row(premise_close={**_CLOSE, 'verdict': 'yes'}))
    assert errors
