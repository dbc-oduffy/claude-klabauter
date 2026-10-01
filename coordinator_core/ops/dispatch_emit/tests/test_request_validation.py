"""Unit tests for the shared dispatch params validator."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.request_validation import Field, validate_params

SPEC = (
    Field("sizing_path", "nonempty_str", True),
    Field("writes", "str_list"),
    Field("run_id", "str"),
)


def test_well_formed_returns_none() -> None:
    assert validate_params("dispatch.x", {"sizing_path": "a", "writes": ["p"], "run_id": ""}, SPEC) is None


def test_optional_absent_or_none_is_ok() -> None:
    assert validate_params("dispatch.x", {"sizing_path": "a", "writes": None}, SPEC) is None


@pytest.mark.parametrize("params", [None, [], "s", 3])
def test_non_dict_refused(params: object) -> None:
    err = validate_params("dispatch.x", params, SPEC)
    assert err is not None and "must be an object" in err["error"]


def test_missing_required_refused() -> None:
    assert validate_params("dispatch.x", {}, SPEC) == {"error": "dispatch.x params.sizing_path is required"}


@pytest.mark.parametrize(
    "params,name",
    [
        ({"sizing_path": "  "}, "sizing_path"),
        ({"sizing_path": 1}, "sizing_path"),
        ({"sizing_path": "a", "writes": "p"}, "writes"),
        ({"sizing_path": "a", "writes": ["p", 2]}, "writes"),
        ({"sizing_path": "a", "run_id": 5}, "run_id"),
    ],
)
def test_wrong_type_refused_naming_field(params: dict, name: str) -> None:
    err = validate_params("dispatch.x", params, SPEC)
    assert err is not None and f"params.{name} must be" in err["error"]


def test_dict_kind() -> None:
    spec = (Field("d", "dict"),)
    assert validate_params("o", {"d": {}}, spec) is None
    assert validate_params("o", {"d": []}, spec) is not None
