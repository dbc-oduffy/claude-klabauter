
from __future__ import annotations

import pytest

from coordinator_core.ops.docgen.tests import test_conformance_canary as canary


def _resolver(doe_present: bool):
    def _resolve() -> None:
        if not doe_present:
            raise RuntimeError("synthetic: DoE clone not resolvable")

    return _resolve


def test_absent_and_unset_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_KLABAUTER_REQUIRE_DOE_CONFORMANCE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(pytest.skip.Exception):
        canary.test_doe_clone_conformance_lane_ran_or_environment_is_documented_optional(
            "synthetic-lane", "synthetic lane", _resolver(doe_present=False)
        )


@pytest.mark.parametrize("env_var", ["CLAUDE_KLABAUTER_REQUIRE_DOE_CONFORMANCE", "CI"])
@pytest.mark.parametrize("truthy_value", ["1", "true", "yes", "True", "YES"])
def test_absent_and_set_fails_loud(
    monkeypatch: pytest.MonkeyPatch, env_var: str, truthy_value: str
) -> None:
    monkeypatch.delenv("CLAUDE_KLABAUTER_REQUIRE_DOE_CONFORMANCE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv(env_var, truthy_value)
    with pytest.raises(pytest.fail.Exception):
        canary.test_doe_clone_conformance_lane_ran_or_environment_is_documented_optional(
            "synthetic-lane", "synthetic lane", _resolver(doe_present=False)
        )


def test_present_and_unset_still_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_KLABAUTER_REQUIRE_DOE_CONFORMANCE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(pytest.skip.Exception):
        canary.test_doe_clone_conformance_lane_ran_or_environment_is_documented_optional(
            "synthetic-lane", "synthetic lane", _resolver(doe_present=True)
        )


@pytest.mark.parametrize("env_var", ["CLAUDE_KLABAUTER_REQUIRE_DOE_CONFORMANCE", "CI"])
def test_present_and_set_passes(monkeypatch: pytest.MonkeyPatch, env_var: str) -> None:
    monkeypatch.delenv("CLAUDE_KLABAUTER_REQUIRE_DOE_CONFORMANCE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv(env_var, "1")
    canary.test_doe_clone_conformance_lane_ran_or_environment_is_documented_optional(
        "synthetic-lane", "synthetic lane", _resolver(doe_present=True)
    )


@pytest.mark.parametrize(
    "falsy_value", ["0", "false", "no", "", "False", "random-garbage"]
)
def test_falsy_env_values_do_not_trigger_requirement(
    monkeypatch: pytest.MonkeyPatch, falsy_value: str
) -> None:
    monkeypatch.setenv("CLAUDE_KLABAUTER_REQUIRE_DOE_CONFORMANCE", falsy_value)
    monkeypatch.delenv("CI", raising=False)
    assert canary._environment_requires_doe_clone() is False


def test_lanes_registry_is_non_empty_and_covers_contract_blocks_lane() -> None:
    lane_ids = {lane_id for lane_id, _description, _resolver in canary.LANES}
    assert "contract-blocks-header-style" in lane_ids
    assert "docgen-byte-identity" in lane_ids
