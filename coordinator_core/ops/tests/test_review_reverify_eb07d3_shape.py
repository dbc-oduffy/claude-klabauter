"""The eb07d3 shape through `review.reverify` revert mode: Windows pytest printed the base-red ids with
backslash separators, and two more reds belong to a peer's dirt (green in the clean export). The base
export comes from `reverify_base_measure.measure` + `write_export`; the failing-ids export is green for the
peer-dirt ids. The op classes the first set `pre_existing` and the second `peer_dirt`, and the run's own
blockers are empty.

Trap: `_run` is stubbed (no real runner); `measure`, `write_export` and the file parse are real."""

from __future__ import annotations

import pytest

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops import reverify_base_measure as rbm
from coordinator_core.ops.review_test_blocker import own_diff_blockers
from coordinator_core.ops.tests.test_review_reverify import (  # noqa: F401  (world is a fixture)
    BASE, PREFIX, SESSION, world,
)

AT_BASE = [f"bench/tests/test_slow.py::test_red_{i}" for i in range(4)]
PEER_DIRT = [f"bench/tests/test_census.py::test_dirt[{i}]" for i in range(2)]
FAILING = [*AT_BASE, *PEER_DIRT]
OWN_PATHS = ["coordinator_core/ops/dispatch_emit/ask_contract.py"]
BASE_EXPORT = "scratch/reverify-base-export-run.txt"
EXPORT_FAILING = "scratch/reverify-export-failing-run.txt"


def _backslashed(node: str) -> str:
    path, sep, rest = node.partition("::")
    return path.replace("/", "\\") + sep + rest


@pytest.fixture
def exports(world, monkeypatch: pytest.MonkeyPatch):
    root, _orig, _declared = world

    def fake_run(sha, ids, cwd, timeout_s=None):
        if sha == "base":
            return "ok", 1, [f"FAILED {_backslashed(n)} - AssertionError" for n in AT_BASE], []
        return "ok", 0, [], []

    monkeypatch.setattr(rbm, "_run", fake_run)
    base = rbm.measure("base", "head", FAILING, str(root))
    rbm.write_export(base, root / BASE_EXPORT)
    rbm.write_export({"red": [], "export": "ok", "runner_exit": 0, "passed_at_base": []}, root / EXPORT_FAILING)
    return root, base


def _revert(root, **kw):
    return rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=FAILING, run_id="me", **kw
    )


def test_measure_lands_backslash_base_reds_in_red_not_passed_at_base(exports) -> None:
    _root, base = exports
    assert base["red"] == sorted(AT_BASE)
    assert base["passed_at_base"] == sorted(PEER_DIRT)


def test_base_red_is_pre_existing_peer_dirt_is_peer_dirt_and_no_own_blocker(exports) -> None:
    root, _base = exports
    out = _revert(root, base_export_path=BASE_EXPORT, export_failing_path=EXPORT_FAILING)
    assert out["error"] is None
    assert out["base_unmeasured"] is False and out["export_unmeasured"] is False
    assert out["pre_existing"] == sorted(AT_BASE)
    assert out["peer_dirt"] == sorted(PEER_DIRT)
    assert out["reverted"] == [] and out["unattributed"] == [] and out["unmeasured"] == []
    assert out["passed_at_base"] == []
    assert own_diff_blockers({**out, "failing": FAILING}, OWN_PATHS) == []


def test_without_the_two_exports_the_same_reds_are_not_classed(exports) -> None:
    root, _base = exports
    out = _revert(root)
    assert out["pre_existing"] == [] and out["peer_dirt"] == []
