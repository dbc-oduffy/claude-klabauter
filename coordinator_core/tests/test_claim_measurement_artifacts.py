"""Falsifier for AC12/AC15 (docs/plans/2026-09-07-a-claim-is-written-twice-and-nothing-compares-them.md, P026-C10t).

Parses C10m's measurement artifact and asserts every AC12/AC15 named key is
present and typed, and that a null key carries its sibling
``*_unmeasured_reason``. Without this test both ACs are discharged by a
paragraph, which is the plan's own stated failure mode.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.frontmatter.schema_validate import parse_frontmatter

ARTIFACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "plans"
    / "2026-09-07-a-claim-is-written-twice-and-nothing-compares-them.measurement.md"
)

# AC12 shape clause: keys that may legitimately be null, each with a sibling
# "<key>_unmeasured_reason" string when null.
AC12_NULLABLE_KEYS = (
    "claims_observed",
    "by_resolver",
    "by_identity_axis",
    "warm_uncarried",
)

AC12_TYPES: dict[str, tuple[type, ...]] = {
    "measured_at": (str,),
    "box": (str,),
    "engine_provenance": (list,),
    "claims_observed": (int, type(None)),
    "by_resolver": (dict, type(None)),
    "by_identity_axis": (dict, type(None)),
    "warm_uncarried": (int, type(None)),
    "ledger_resolver_source_field_present": (bool,),
    # retroactive_reading is either null or a sub-map — checked separately.
}

AC15_MEASUREMENT_ENTRY_TYPES: dict[str, tuple[type, ...]] = {
    "surface": (str,),
    "class": (str,),
    # The repo's frontmatter YAML parser (schema_validate.parse_yaml) reads an
    # unquoted decimal like "0.140" as str rather than float (a documented
    # quirk shared with production frontmatter reads elsewhere in this repo),
    # so numeric-string values are accepted here too, checked for
    # float-parseability below.
    "process_time_ms": (int, float, str),
    "spawn_count": (int, str),
    "method": (str,),
    "verdict": (str,),
}

VALID_CLASSES = {"plan-delta", "pre-existing-baseline"}
VALID_VERDICTS = {"under-bar", "over-bar", "unmeasured"}


def _load_frontmatter() -> dict:
    if not ARTIFACT_PATH.exists():
        pytest.fail(f"C10m's measurement artifact is missing: {ARTIFACT_PATH}")
    content = ARTIFACT_PATH.read_text(encoding="utf-8")
    parsed = parse_frontmatter(content)
    frontmatter = parsed.get("frontmatter")
    if frontmatter is None:
        pytest.fail(f"{ARTIFACT_PATH} has no parseable YAML frontmatter")
    return frontmatter


@pytest.fixture(scope="module")
def frontmatter() -> dict:
    return _load_frontmatter()


def test_ac12_named_keys_present(frontmatter: dict) -> None:
    for key in AC12_TYPES:
        assert key in frontmatter, f"AC12 key {key!r} missing from artifact frontmatter"


def test_ac12_named_keys_typed(frontmatter: dict) -> None:
    for key, allowed_types in AC12_TYPES.items():
        value = frontmatter[key]
        assert isinstance(value, allowed_types), (
            f"AC12 key {key!r} has type {type(value).__name__}, "
            f"expected one of {[t.__name__ for t in allowed_types]}"
        )


def test_ac12_null_keys_carry_sibling_unmeasured_reason(frontmatter: dict) -> None:
    for key in AC12_NULLABLE_KEYS:
        if frontmatter.get(key) is None:
            reason_key = f"{key}_unmeasured_reason"
            assert reason_key in frontmatter, (
                f"AC12 key {key!r} is null but sibling {reason_key!r} is absent"
            )
            reason_value = frontmatter[reason_key]
            assert isinstance(reason_value, str) and reason_value.strip(), (
                f"AC12 sibling {reason_key!r} must be a non-empty string"
            )


def test_ac12_retroactive_reading_shape(frontmatter: dict) -> None:
    assert "retroactive_reading" in frontmatter, "AC12 key 'retroactive_reading' missing"
    retroactive = frontmatter["retroactive_reading"]
    if retroactive is None:
        return
    assert isinstance(retroactive, dict), "retroactive_reading must be null or a sub-map"
    for key, expected_type in (
        ("box", str),
        ("ledger_populated", bool),
        ("rows_read", int),
    ):
        assert key in retroactive, f"retroactive_reading.{key} missing"
        assert isinstance(retroactive[key], expected_type), (
            f"retroactive_reading.{key} has type {type(retroactive[key]).__name__}, "
            f"expected {expected_type.__name__}"
        )


def test_ac15_measurements_key_present_and_typed(frontmatter: dict) -> None:
    assert "measured_at" in frontmatter and isinstance(frontmatter["measured_at"], str)
    assert "box" in frontmatter and isinstance(frontmatter["box"], str)
    assert "measurements" in frontmatter, "AC15 key 'measurements' missing"
    measurements = frontmatter["measurements"]
    assert isinstance(measurements, list) and measurements, (
        "AC15 'measurements' must be a non-empty list"
    )
    for entry in measurements:
        assert isinstance(entry, dict), "each AC15 measurement entry must be a map"
        for key, allowed_types in AC15_MEASUREMENT_ENTRY_TYPES.items():
            assert key in entry, f"AC15 measurement entry missing key {key!r}: {entry}"
            value = entry[key]
            assert isinstance(value, allowed_types), (
                f"AC15 measurement entry key {key!r} has type {type(value).__name__}, "
                f"expected one of {[t.__name__ for t in allowed_types]}: {entry}"
            )
        try:
            float(entry["process_time_ms"])
        except (TypeError, ValueError):
            pytest.fail(
                f"AC15 measurement entry process_time_ms is not numeric-parseable: {entry}"
            )
        try:
            int(entry["spawn_count"])
        except (TypeError, ValueError):
            pytest.fail(
                f"AC15 measurement entry spawn_count is not int-parseable: {entry}"
            )
        assert entry["class"] in VALID_CLASSES, (
            f"AC15 measurement entry has unknown class {entry['class']!r}: {entry}"
        )
        assert entry["verdict"] in VALID_VERDICTS, (
            f"AC15 measurement entry has unknown verdict {entry['verdict']!r}: {entry}"
        )
        if entry["verdict"] == "unmeasured":
            reason_key = "unmeasured_reason"
            assert reason_key in entry and isinstance(entry[reason_key], str) and entry[
                reason_key
            ].strip(), (
                f"AC15 measurement entry has verdict 'unmeasured' but no "
                f"non-empty sibling {reason_key!r}: {entry}"
            )


def test_ac15_gating_plan_delta_entries_are_not_over_bar(frontmatter: dict) -> None:
    """plan-delta entries are gating: an over-bar verdict on one fails AC15."""
    measurements = frontmatter["measurements"]
    plan_delta_over_bar = [
        entry
        for entry in measurements
        if entry.get("class") == "plan-delta" and entry.get("verdict") == "over-bar"
    ]
    assert not plan_delta_over_bar, (
        f"AC15 is failed by over-bar plan-delta entries: {plan_delta_over_bar}"
    )
