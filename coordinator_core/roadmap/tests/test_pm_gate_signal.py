"""Tests for ``coordinator_core.roadmap.pm_gate_signal`` -- detect-pm-gate-signal.

Covers: the vendored fragment loads and stays byte-identical to the DoE
source (skipped, not failed, when the sibling clone is unresolvable on this
machine -- the pin is a drift guard, not a hard cross-repo dependency for
CI); the three detection legs and the ``named-stakeholder`` judgment floor;
the ``blocked_by`` exclusion and the deprecated ``gate_dependency`` scan; and
the advisory, non-exhaustive shape of ``detect()``'s return value.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.doe_root_pointer import read_doe_root_pointer
from coordinator_core.roadmap.pm_gate_signal import detect, detect_stub, load_fragment

_VENDORED_FRAGMENT_PATH = (
    Path(__file__).resolve().parents[1] / "fragments" / "pm-gate-signal-fragment.json"
)
_DOE_FRAGMENT_RELPATH = "coordinator/contract/pm-gate-signal-fragment.json"


@pytest.fixture()
def fragment() -> dict:
    return load_fragment()


def test_load_fragment_reads_the_vendored_copy(fragment: dict) -> None:
    assert fragment["schema"] == "pm-gate-signal-fragment"
    assert fragment["emits"] == "recommendation"
    assert fragment["exhaustive"] is False


def test_vendored_fragment_is_byte_identical_to_doe_source() -> None:
    doe_root = read_doe_root_pointer()
    if not doe_root:
        pytest.skip("DoE-claude sibling root not resolvable on this machine")
    doe_path = Path(doe_root) / _DOE_FRAGMENT_RELPATH
    if not doe_path.is_file():
        pytest.skip(f"DoE source fragment not found at {doe_path}")
    doe_bytes = doe_path.read_bytes()
    vendored_bytes = _VENDORED_FRAGMENT_PATH.read_bytes()
    assert vendored_bytes == doe_bytes, (
        "vendored coordinator_core/roadmap/fragments/pm-gate-signal-fragment.json "
        "has drifted from the DoE source -- re-vendor a byte-identical copy, "
        "never re-derive the rule."
    )


def test_all_four_legs_present(fragment: dict) -> None:
    leg_ids = tuple(leg["id"] for leg in fragment["legs"])
    assert leg_ids == (
        "pm-prefix",
        "named-stakeholder",
        "decision-approval-policy-scope",
        "user-facing",
    )


def test_pm_prefix_leg_fires_on_case_sensitive_prefix(fragment: dict) -> None:
    stub = {"stub_id": "dscov-1", "blocking_notes": "PM decision on retry defaults"}
    result = detect_stub(fragment, stub)
    assert result is not None
    legs = [m["leg"] for m in result["matched_legs"]]
    assert "pm-prefix" in legs


def test_pm_prefix_leg_is_case_sensitive_and_does_not_fire_lowercase(
    fragment: dict,
) -> None:
    stub = {"stub_id": "dscov-2", "blocking_notes": "pm decision on retry defaults"}
    result = detect_stub(fragment, stub)
    if result is not None:
        legs = [m["leg"] for m in result["matched_legs"]]
        assert "pm-prefix" not in legs


def test_pm_prefix_leg_scans_deprecated_gate_dependency_field(fragment: dict) -> None:
    stub = {"stub_id": "dscov-3", "gate_dependency": "PM approval of scope change"}
    result = detect_stub(fragment, stub)
    assert result is not None
    fields = [m.get("field") for m in result["matched_legs"] if m["leg"] == "pm-prefix"]
    assert "gate_dependency" in fields


def test_decision_approval_policy_scope_leg_is_low_precision_by_design(
    fragment: dict,
) -> None:
    stub = {"stub_id": "dscov-4", "blocking_notes": "This work is out of scope for now"}
    result = detect_stub(fragment, stub)
    assert result is not None
    scope_matches = [
        m for m in result["matched_legs"] if m["leg"] == "decision-approval-policy-scope"
    ]
    assert scope_matches
    assert "scope" in scope_matches[0]["matched_terms"]


def test_user_facing_leg_fires_on_user_visible_language(fragment: dict) -> None:
    stub = {"stub_id": "dscov-5", "blocking_notes": "This changes user-facing behaviour"}
    result = detect_stub(fragment, stub)
    assert result is not None
    legs = [m["leg"] for m in result["matched_legs"]]
    assert "user-facing" in legs


def test_named_stakeholder_floor_surfaces_a_judgment_point_not_a_decision(
    fragment: dict,
) -> None:
    stub = {"stub_id": "dscov-6", "blocking_notes": "Needs sign-off from the VP"}
    result = detect_stub(fragment, stub)
    assert result is not None
    jp = result["judgment_point"]
    assert jp is not None
    assert jp["match_kind"] == "judgment"
    assert jp["judgment_required"] is True
    assert jp["surfaces_as"] == "judgment_point"
    assert "VP" in jp["matched_floor_terms"]


def test_named_stakeholder_floor_does_not_fire_on_a_peer_team_mention(
    fragment: dict,
) -> None:
    stub = {
        "stub_id": "dscov-7",
        "blocking_notes": "coordinate with claude-klabauter-em on rollout",
    }
    result = detect_stub(fragment, stub)
    if result is not None:
        assert result["judgment_point"] is None


def test_blocked_by_field_is_never_scanned(fragment: dict) -> None:
    stub = {
        "stub_id": "dscov-8",
        "blocked_by": "dscov-out-of-scope-item",
        "blocking_notes": "",
        "gate_dependency": "",
    }
    result = detect_stub(fragment, stub)
    assert result is None


def test_stub_with_no_signal_returns_none(fragment: dict) -> None:
    stub = {"stub_id": "dscov-9", "blocking_notes": "", "gate_dependency": ""}
    assert detect_stub(fragment, stub) is None


def test_detect_is_advisory_and_non_exhaustive(fragment: dict) -> None:
    stubs = [
        {"stub_id": "dscov-10", "blocking_notes": "PM decision needed"},
        {"stub_id": "dscov-11", "blocking_notes": "nothing to see here"},
    ]
    result = detect(fragment, stubs)
    assert result["emits"] == "recommendation"
    assert result["exhaustive"] is False
    candidate_ids = [c["stub_id"] for c in result["candidates"]]
    assert candidate_ids == ["dscov-10"]


def test_detect_echoes_emits_and_exhaustive_from_fragment_not_hardcoded() -> None:
    fragment = {
        "emits": "different-value",
        "exhaustive": True,
        "legs": [],
    }
    result = detect(fragment, [])
    assert result["emits"] == "different-value"
    assert result["exhaustive"] is True
    assert result["candidates"] == []
