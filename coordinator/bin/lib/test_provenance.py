
from __future__ import annotations

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from provenance import get_provenance_completeness  # noqa: E402


def test_absent_system_key_resolves_unknown() -> None:
    assert get_provenance_completeness({}) == "unknown"


def test_system_without_provenance_completeness_resolves_unknown() -> None:
    assert get_provenance_completeness({"system": {"other_field": "value"}}) == "unknown"


def test_provenance_completeness_none_resolves_unknown() -> None:
    assert (
        get_provenance_completeness({"system": {"provenance_completeness": None}})
        == "unknown"
    )


def test_provenance_completeness_empty_string_resolves_unknown() -> None:
    assert (
        get_provenance_completeness({"system": {"provenance_completeness": ""}})
        == "unknown"
    )


def test_complete_value_is_returned() -> None:
    assert (
        get_provenance_completeness({"system": {"provenance_completeness": "complete"}})
        == "complete"
    )


def test_stored_unknown_value_is_returned() -> None:
    assert (
        get_provenance_completeness({"system": {"provenance_completeness": "unknown"}})
        == "unknown"
    )


def test_system_none_resolves_unknown() -> None:
    assert get_provenance_completeness({"system": None}) == "unknown"


def test_system_non_dict_scalar_resolves_unknown() -> None:
    assert get_provenance_completeness({"system": "not-a-dict"}) == "unknown"


def test_unrecognized_value_passes_through_unchanged() -> None:
    assert (
        get_provenance_completeness(
            {"system": {"provenance_completeness": "corrupted_value"}}
        )
        == "corrupted_value"
    )
