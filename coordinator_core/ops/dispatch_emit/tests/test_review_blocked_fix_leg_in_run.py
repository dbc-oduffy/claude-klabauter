"""review_blocked closes in-ask findings, stale assertions and combined-run flakes in its halt-fix leg."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit.self_correction import HALT_FIX_LABEL, HALT_REVERIFY_LABEL

from . import test_ask_compose_halt_self_correction as _hs
from .test_ask_compose_halt_self_correction import _OWN, _OWNED, _drive, _done, _green, _labels, _red

_STALE_TEST = "other/tests/test_peer.py"
_WARN = [{"slice_id": "s1", "verdict": "WARN", "findings": 4, "applied": 0, "sidecar_path": "rev/s1.md"}]
_BLOCKED = [{"slice_id": "s1", "verdict": "BLOCKED", "findings": 1, "applied": 0, "sidecar_path": "rev/s1.md"}]
_CLEAR = {"test": _green(), "slices": [{"slice_id": "s1", "findings": [], "applied": True}]}

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_warn_findings_are_applied_by_the_fix_leg_and_the_run_lands():
    out = _drive(
        reverify=_green(), wave=_WARN, replies={HALT_FIX_LABEL: [_done(_OWN)], HALT_REVERIFY_LABEL: [_CLEAR]}
    )

    assert _labels(out) == [HALT_FIX_LABEL, HALT_REVERIFY_LABEL]
    assert "rev/s1.md" in out["calls"][0]["prompt"]
    assert out["halted"] is None
    assert out["wave"][0]["applied"] == 4


def test_a_stale_assertion_in_a_test_file_outside_the_footprint_is_a_widen_not_a_halt():
    out = _drive(
        reverify=_red(_OWNED),
        replies={HALT_FIX_LABEL: [_done(_OWN, _STALE_TEST)], HALT_REVERIFY_LABEL: [{"test": _green(), "slices": []}]},
    )

    assert out["halted"] is None
    assert "widened-paths.jsonl" in out["calls"][0]["prompt"]


def _drive_sc(**kw) -> dict:
    """`_drive` with `_selfCorrection` exposed, to read the recorded widen list."""
    original = _hs._HARNESS
    _hs._HARNESS = original.replace("foreign: _foreignReds,", "sc: _selfCorrection, foreign: _foreignReds,")
    try:
        return _drive(**kw)
    finally:
        _hs._HARNESS = original


def test_an_out_of_footprint_test_edit_is_carried_as_a_recorded_widen_not_a_halt():
    out = _drive_sc(
        reverify=_red(_OWNED),
        replies={HALT_FIX_LABEL: [_done(_OWN, _STALE_TEST)], HALT_REVERIFY_LABEL: [{"test": _green(), "slices": []}]},
    )

    assert out["halted"] is None
    assert out["sc"]["status"] == "fixed"
    assert out["sc"]["widened"] == [_STALE_TEST]


def test_a_review_path_that_does_not_exist_is_never_widened_only_the_resolved_real_file_is():
    ghost = "tests/test_launch.py"
    real = "tests/remote_compute/test_band_launch.py"
    out = _drive_sc(
        reverify=_red(f"{ghost}::test_a", reverted=[{"tests": [f"{ghost}::test_a"]}]),
        replies={HALT_FIX_LABEL: [_done(_OWN, real)], HALT_REVERIFY_LABEL: [{"test": _green(), "slices": []}]},
    )

    assert out["halted"] is None
    assert out["sc"]["widened"] == [real]
    assert ghost not in out["sc"]["widened"]


def test_a_non_test_write_outside_the_footprint_still_halts_as_scope():
    out = _drive(reverify=_red(_OWNED), replies={HALT_FIX_LABEL: [_done(_OWN, "tools/new_cli.py")]})

    assert out["halted"]["self_correction"]["status"] == "scope"
    assert out["halted"]["self_correction"]["needs_paths"] == ["tools/new_cli.py"]


def test_a_combined_run_only_red_is_recorded_flaky_and_does_not_halt():
    blocked_no_write = {"status": "BLOCKED", "paths": [], "reason": "passes alone and at base", "needs_paths": []}
    out = _drive(
        reverify=_red(_OWNED),
        replies={HALT_FIX_LABEL: [blocked_no_write], HALT_REVERIFY_LABEL: [{"test": _green(), "slices": []}]},
    )

    assert out["halted"] is None
    assert _labels(out) == [HALT_FIX_LABEL, HALT_REVERIFY_LABEL]


def test_a_red_that_still_fails_after_a_blocked_no_write_leg_halts():
    blocked_no_write = {"status": "BLOCKED", "paths": [], "reason": "not mine", "needs_paths": []}
    out = _drive(
        reverify=_red(_OWNED),
        replies={HALT_FIX_LABEL: [blocked_no_write], HALT_REVERIFY_LABEL: [{"test": _red(_OWNED), "slices": []}]},
    )

    assert out["halted"]["halted"] == "review_blocked"
    assert out["halted"]["self_correction"]["status"] == "no-progress"


def test_a_red_that_passes_alone_and_at_base_is_recorded_flaky_without_a_fix_leg():
    out = _drive(reverify=_red(_OWNED, passed_alone=[_OWNED], passed_at_base=[_OWNED]), replies={})

    assert out["halted"] is None
    assert HALT_FIX_LABEL not in _labels(out)
    assert out["reverify"]["flaky"] == [
        {"node_id": _OWNED, "run": "r1", "evidence": f"AssertionError in {_OWNED}; passed alone; passed at base"}
    ]


@pytest.mark.parametrize(
    "evidence",
    [
        {"passed_alone": [], "passed_at_base": [_OWNED]},
        {"passed_alone": [_OWNED], "passed_at_base": []},
        {},
    ],
    ids=["fails-alone", "fails-at-base", "no-evidence"],
)
def test_a_red_without_both_passes_still_halts_review_blocked(evidence):
    out = _drive(reverify=_red(_OWNED, **evidence), replies={})

    assert out["halted"]["halted"] == "review_blocked"
    assert [t["node_id"] for t in out["halted"]["failing"]] == [_OWNED]
    assert "flaky" not in out["reverify"]


def test_a_finding_contradicting_the_ask_still_halts_review_blocked():
    scope = {"status": "SCOPE", "paths": [], "reason": "contradicts the ask", "needs_paths": ["docs/x.md"]}
    out = _drive(reverify=_green(), wave=_BLOCKED, replies={HALT_FIX_LABEL: [scope]})

    assert out["halted"]["halted"] == "review_blocked"
    assert json.dumps(out["halted"]["self_correction"]["needs_paths"]) == '["docs/x.md"]'
