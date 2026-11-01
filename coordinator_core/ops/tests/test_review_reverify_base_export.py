"""review.reverify derives failing_at_base from a base_export_path file; an unmeasured base is indeterminate."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.tests.test_review_reverify_base_measured import (  # noqa: F401  (world is a fixture)
    FAILING, SIDECARS, _call, world,
)

OK = json.dumps({"baseline_export": {"sha": "a" * 40, "export": "ok", "why": None, "runner_exit": 1, "removed": True}})


def _export(root, body, tail=OK, name="base.txt"):
    path = root / name
    path.write_text(body + ("\n" + tail if tail else "") + "\n", encoding="utf-8")
    return str(path)


def test_export_failed_lines_are_pre_existing(world):
    node = FAILING[0]
    path = _export(world, f"PASSED tests/other.py::test_x\nFAILED {node} - AssertionError: boom")
    out = _call(world, failing=FAILING, sidecars=SIDECARS, base_export_path=path)
    assert out["error"] is None
    assert out["pre_existing"] == FAILING and out["reverted"] == [] and out["unattributed"] == []
    assert out["base_unmeasured"] is False and out["unmeasured"] == []
    assert out["passed_at_base"] == []


def test_export_error_line_and_explicit_list_union(world):
    path = _export(world, f"ERROR {FAILING[0]}")
    out = _call(world, failing=FAILING, sidecars=SIDECARS, base_export_path=path, failing_at_base=["x::y"])
    assert out["pre_existing"] == FAILING


def test_ids_absent_from_export_are_not_pre_existing(world):
    path = _export(world, "FAILED tests/other.py::test_x")
    out = _call(world, failing=FAILING, sidecars=SIDECARS, base_export_path=path)
    assert out["pre_existing"] == [] and out["base_unmeasured"] is False


def test_missing_file_is_unmeasured_not_own(world):
    out = _call(world, failing=FAILING, sidecars=SIDECARS, base_export_path=str(world / "nope.txt"))
    assert out["error"] is None
    assert out["base_unmeasured"] is True and out["pre_existing"] == []
    assert out["unattributed"] == [] and set(out["unmeasured"]) | set(r for e in out["reverted"] for r in e["tests"]) == set(FAILING)


@pytest.mark.parametrize("tail", [
    json.dumps({"baseline_export": {"export": "failed", "runner_exit": None}}),
    json.dumps({"baseline_export": {"export": "ok", "runner_exit": 2}}),
    "not json",
    "",
])
def test_bad_export_state_or_exit_is_unmeasured(world, tail):
    path = _export(world, f"FAILED {FAILING[0]}", tail=tail)
    out = _call(world, failing=FAILING, sidecars=SIDECARS, base_export_path=path)
    assert out["base_unmeasured"] is True and out["pre_existing"] == []


def test_unmeasured_ids_are_not_excluded_from_blockers(world):
    from coordinator_core.ops.review_test_blocker import from_reverify

    out = _call(world, failing=FAILING, sidecars=SIDECARS, base_export_path=str(world / "nope.txt"))
    assert [b.node_id for b in from_reverify({**out, "failing": FAILING})] == FAILING


def test_absent_param_keeps_legacy_shape(world):
    out = _call(world, failing=FAILING, sidecars=SIDECARS)
    assert "base_unmeasured" not in out
