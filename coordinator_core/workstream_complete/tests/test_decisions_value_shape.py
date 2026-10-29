"""workstream_complete decisions readers treat `{"value": v}` as `{"disposition": v}` and refuse unrecognized entries."""

from __future__ import annotations

import pytest

from coordinator_core import workstream_complete as wsc
from coordinator_core.workstream_complete import apply as wsc_apply


def test_session_shape_value_only_matches_disposition_form():
    by_disposition = wsc._session_shape_disposition_from_decisions(
        {"jp-session-shape": {"disposition": "single-session"}}
    )
    by_value = wsc._session_shape_disposition_from_decisions({"jp-session-shape": {"value": "single-session"}})
    assert by_value is not None
    assert by_value == by_disposition


def test_session_shape_unrecognized_entry_refuses_naming_key():
    with pytest.raises(ValueError, match="jp-session-shape") as exc:
        wsc._session_shape_disposition_from_decisions({"jp-session-shape": {"note": "x"}})
    assert '"value"' in str(exc.value)
    assert wsc._session_shape_disposition_from_decisions({}) is None


def test_apply_chosen_value_read_accepts_value():
    assert wsc_apply._decided_disposition("j", {"value": "go"}) == "go"
    assert wsc_apply._decided_disposition("j", {"disposition": "go"}) == "go"
    assert wsc_apply._decided_disposition("j", None) is None


def test_apply_nature_judgment_accepts_value_and_refuses_unrecognized():
    assert wsc_apply._decided_nature({"completion-nature-classification": {"value": "x"}}) == "x"
    with pytest.raises(ValueError, match="completion-nature-classification"):
        wsc_apply._decided_nature({"completion-nature-classification": {}})
