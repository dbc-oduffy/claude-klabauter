"""review.reverify classes a failing id green in the clean export as peer_dirt; base reds persist as pre_existing."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.tests.test_review_reverify_base_measured import (  # noqa: F401  (world is a fixture)
    BASE, FAILING, PREFIX, SESSION, SIDECARS, _call, world,
)

OK = json.dumps({"baseline_export": {"sha": "a" * 40, "export": "ok", "why": None, "runner_exit": 1, "removed": True}})


def _export(root, body: str, tail: str = OK, name: str = "export.txt") -> str:
    path = root / name
    path.write_text(body + ("\n" + tail if tail else "") + "\n", encoding="utf-8")
    return str(path)


def _record(root, out: dict) -> dict:
    return json.loads((root / out["record"]).read_text(encoding="utf-8"))


def test_id_green_in_export_is_peer_dirt_never_reverted_or_unattributed(world) -> None:
    path = _export(world, "PASSED tests/other.py::test_x")
    out = rr._handler(
        {"base": BASE, "slice_prefix": PREFIX, "session_id": SESSION, "failing": FAILING,
         "sidecars": SIDECARS, "export_failing_path": path},
        world,
    )
    assert out["error"] is None
    assert out["peer_dirt"] == FAILING
    assert out["reverted"] == [] and out["unattributed"] == [] and out["pre_existing"] == []
    assert out["export_unmeasured"] is False
    assert (world / "pkg/mod.py").read_bytes() == b"VALUE = 1\n_unused = 2\n"
    assert _record(world, out)["peer_dirt"] == FAILING


def test_id_red_in_export_is_not_peer_dirt_and_keeps_todays_path(world) -> None:
    path = _export(world, f"FAILED {FAILING[0]} - AssertionError")
    out = _call(world, failing=FAILING, sidecars=SIDECARS, export_failing_path=path)
    assert out["peer_dirt"] == []
    assert [t for e in out["reverted"] for t in e["tests"]] == FAILING


@pytest.mark.parametrize("tail", [
    "not json",
    json.dumps({"baseline_export": {"export": "ok", "runner_exit": 2}}),
    "",
])
def test_invalid_export_is_unmeasured_and_ids_keep_todays_path(world, tail) -> None:
    path = _export(world, "PASSED tests/other.py::test_x", tail=tail)
    out = _call(world, failing=FAILING, sidecars=SIDECARS, export_failing_path=path)
    assert out["export_unmeasured"] is True and out["peer_dirt"] == []
    assert [t for e in out["reverted"] for t in e["tests"]] == FAILING


def test_missing_export_file_is_unmeasured(world) -> None:
    out = _call(world, failing=FAILING, sidecars=SIDECARS, export_failing_path=str(world / "nope.txt"))
    assert out["export_unmeasured"] is True and out["peer_dirt"] == []
    assert [t for e in out["reverted"] for t in e["tests"]] == FAILING


def test_absent_param_keeps_legacy_shape(world) -> None:
    out = _call(world, failing=FAILING, sidecars=SIDECARS)
    assert "export_unmeasured" not in out and out["peer_dirt"] == []


def test_base_red_wins_over_export_and_persists_in_pre_existing(world) -> None:
    base = _export(world, f"FAILED {FAILING[0]}", name="base.txt")
    export = _export(world, "PASSED tests/other.py::test_x")
    out = _call(world, failing=FAILING, sidecars=SIDECARS, base_export_path=base, export_failing_path=export)
    assert out["pre_existing"] == FAILING and out["peer_dirt"] == [] and out["reverted"] == []
    assert _record(world, out)["pre_existing"] == FAILING


def test_invalid_base_with_green_export_is_peer_dirt_not_unmeasured(world) -> None:
    export = _export(world, "PASSED tests/other.py::test_x")
    out = _call(
        world, failing=FAILING, sidecars=SIDECARS, base_export_path=str(world / "nope.txt"),
        export_failing_path=export,
    )
    assert out["base_unmeasured"] is True and out["peer_dirt"] == FAILING
    assert out["unmeasured"] == [] and out["pre_existing"] == []


def test_invalid_base_with_red_export_stays_unmeasured(world) -> None:
    export = _export(world, f"FAILED {FAILING[0]}")
    out = _call(
        world, failing=FAILING, sidecars=SIDECARS, base_export_path=str(world / "nope.txt"),
        export_failing_path=export,
    )
    assert out["base_unmeasured"] is True and out["peer_dirt"] == [] and out["pre_existing"] == []
    assert out["unattributed"] == []
    assert set(out["unmeasured"]) | {t for e in out["reverted"] for t in e["tests"]} == set(FAILING)
