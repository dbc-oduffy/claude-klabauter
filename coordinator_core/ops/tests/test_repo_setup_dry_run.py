"""repo.setup dry_run and undeclared-param refusal, driven through the handler on a scratch git repo."""

from __future__ import annotations


import pytest

from coordinator_core.ops import repo_setup as mod


def test_params_schema_is_strict() -> None:
    schema = mod._PARAMS_SCHEMA
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["root"]
    assert schema["properties"]["dry_run"] == {"type": "boolean"}
