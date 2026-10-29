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


def test_strict_refuses_unknown_key_naming_key_and_accepted() -> None:
    err = validate_params("dispatch.x", {"sizing_path": "a", "widened": []}, SPEC, strict=True)
    assert err == {
        "error": "dispatch.x params.widened is not accepted; accepted: run_id, sizing_path, writes"
    }


def test_default_stays_lenient_and_strict_accepts_known_keys() -> None:
    params = {"sizing_path": "a", "extra": 1}
    assert validate_params("dispatch.x", params, SPEC) is None
    assert validate_params("dispatch.x", {"sizing_path": "a"}, SPEC, strict=True) is None


def test_emit_refuses_unknown_top_level_key_and_writes_nothing(tmp_path) -> None:
    from coordinator_core.ops.dispatch_emit.op import _dispatch_emit

    out = tmp_path / "x.workflow.mjs"
    reply = _dispatch_emit(
        {"kind": "abandon_run", "run_id": "r1", "plan_path": "p.md", "output_path": str(out)}, tmp_path
    )
    assert "params.kind is not accepted" in reply["error"]
    assert '{"abandon_run": "<run_id>"}' in reply["error"]
    assert not out.exists()


def test_emit_accepts_every_typed_and_untyped_known_key() -> None:
    from coordinator_core.ops.dispatch_emit.op import (
        _PARAM_FIELDS,
        _UNTYPED_PARAM_KEYS,
        _unknown_param_refusal,
    )

    known = {f.name: None for f in _PARAM_FIELDS} | dict.fromkeys(_UNTYPED_PARAM_KEYS)
    assert _unknown_param_refusal(known) is None
    assert _unknown_param_refusal({"abandon_run": "r1"}) is None


def test_dict_kind() -> None:
    spec = (Field("d", "dict"),)
    assert validate_params("o", {"d": {}}, spec) is None
    assert validate_params("o", {"d": []}, spec) is not None
