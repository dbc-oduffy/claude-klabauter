
from __future__ import annotations

import re
from datetime import date

import pytest
from pydantic import ValidationError

from coordinator_core.contract.cockpit_schema.common import _ISO_DATE_PATTERN
from coordinator_core.contract.cockpit_schema.entities.cross_repo_memo_summary import (
    CrossRepoMemoSummary,
)
from coordinator_core.ops.emit.sections.cross_repo_memos import _actioned_at

_ISO_DATE_RE = re.compile(_ISO_DATE_PATTERN)


def _base_summary_kwargs(**overrides) -> dict:
    kwargs = dict(
        repo="acme/widget",
        coordinator_root_path="/x/widget",
        title="a memo",
        **{"from": "sender"},
        to="receiver",
        status="actioned",
        created="2026-09-01",
        kind="ask",
        related=[],
        provenance={
            "source_kind": "local_fs",
            "repo": "acme/widget",
            "ref": None,
            "path": "cross-repo/inbox/memo.md",
            "observed_at": "2026-09-04T00:00:00Z",
            "derivation": "parsed",
            "entity_anchor": None,
        },
    )
    kwargs.update(overrides)
    return kwargs


def test_actioned_at_field_alone():
    assert _actioned_at({"actioned_at": "2026-09-04"}) == "2026-09-04"


def test_closed_at_field_alone():
    assert _actioned_at({"closed_at": "2026-08-01"}) == "2026-08-01"


def test_action_taken_at_field_alone():
    assert _actioned_at({"action_taken_at": "2026-07-15"}) == "2026-07-15"


def test_precedence_all_three_present_actioned_at_wins():
    fm = {
        "actioned_at": "2026-09-04",
        "closed_at": "2026-08-01",
        "action_taken_at": "2026-07-15",
    }
    assert _actioned_at(fm) == "2026-09-04"


def test_precedence_closed_at_beats_action_taken_at():
    fm = {"closed_at": "2026-08-01", "action_taken_at": "2026-07-15"}
    assert _actioned_at(fm) == "2026-08-01"


def test_rfc3339_datetime_truncates_to_date():
    assert _actioned_at({"actioned_at": "2026-09-04T12:34:56Z"}) == "2026-09-04"


def test_bare_date_passes_through():
    assert _actioned_at({"actioned_at": "2026-09-04"}) == "2026-09-04"


def test_datetime_date_object_returns_isoformat():
    assert _actioned_at({"actioned_at": date(2026, 9, 4)}) == "2026-09-04"


def test_picked_up_at_alone_returns_none():
    fm = {"picked_up_at": "2026-01-01T00:00:00Z"}
    assert _actioned_at(fm) is None


def test_picked_up_at_present_alongside_real_field_is_ignored():
    fm = {"picked_up_at": "2026-01-01T00:00:00Z", "actioned_at": "2026-09-04"}
    assert _actioned_at(fm) == "2026-09-04"


@pytest.mark.parametrize("bad_value", ["not-a-date", "2026-13-45", "", None, 42])
def test_malformed_first_field_falls_through_to_valid_later_field(bad_value):
    fm = {"actioned_at": bad_value, "closed_at": "2026-08-01"}
    assert _actioned_at(fm) == "2026-08-01"


@pytest.mark.parametrize("bad_value", ["not-a-date", "2026-13-45", "", None, 42])
def test_all_malformed_returns_none(bad_value):
    fm = {
        "actioned_at": bad_value,
        "closed_at": bad_value,
        "action_taken_at": bad_value,
    }
    assert _actioned_at(fm) is None


def test_no_closure_fields_at_all_returns_none():
    assert _actioned_at({}) is None


@pytest.mark.parametrize("fm", [
    {"actioned_at": "2026-09-04"},
    {"actioned_at": "2026-09-04T12:34:56Z"},
    {"closed_at": "2026-01-31"},
    {"action_taken_at": "2026-12-25"},
    {"actioned_at": date(2026, 9, 4)},
])
def test_every_non_none_return_matches_iso_date_pattern(fm):
    value = _actioned_at(fm)
    assert value is not None
    assert _ISO_DATE_RE.match(value)
    summary = CrossRepoMemoSummary(**_base_summary_kwargs(actioned_at=value))
    assert summary.actioned_at == value


def test_entity_accepts_valid_actioned_at():
    summary = CrossRepoMemoSummary(**_base_summary_kwargs(actioned_at="2026-09-04"))
    assert summary.actioned_at == "2026-09-04"


def test_entity_defaults_to_none_when_omitted():
    summary = CrossRepoMemoSummary(**_base_summary_kwargs())
    assert summary.actioned_at is None


def test_entity_rejects_non_date_string():
    with pytest.raises(ValidationError):
        CrossRepoMemoSummary(**_base_summary_kwargs(actioned_at="not-a-date"))
