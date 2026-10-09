"""The register rollup is the only shipped writer on a register-bearing sizing; a sizing with
no register ships through both writers as before.

Run: python -m pytest coordinator_core/ops/tests/test_rollup_sole_ship_writer.py -q -p no:cacheprovider
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.deliverable_cascade as cascade_mod
import coordinator_core.ops.sizing_ship as ship_mod
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted
from coordinator_core.ops.tests.test_requirement_register_fifa_replay import (
    ALL_SHIPPABLE,
    FIXTURE,
    PLAN,
    repo,  # noqa: F401 - fixture
    seed,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _no_register(repo: Path, name: str = "plain.yaml") -> Path:
    data = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    del data["requirement_register"]
    data["route"] = "plan"
    path = repo / "state" / "sizings" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _status(path: Path) -> str:
    return read_fm_field_unquoted(path.read_text(encoding="utf-8"), "status")


@pytest.mark.parametrize("row,fixed", [
    ("R-partial", "partial"),
    ("R-unreachable", "met but not wired"),
    ("R-unclaimed", "open"),
    ("R-deferred", "without a recorded ruling"),
])
def test_plan_routed_register_sizing_refused_by_both_writers(repo, row, fixed):
    fix = {k: v for k, v in ALL_SHIPPABLE.items() if k != row}
    path = seed(repo, route="plan", fix=fix)
    shipped = ship_mod._handler({"sizing_path": str(path)}, repo_root=repo)
    assert shipped["exit_code"] == 1 and "rollup" in shipped["error"]
    advanced, refusal = cascade_mod._advance_one_sizing(path, PLAN, repo / ".git")
    assert advanced is False and f"row {row}" in refusal and fixed in refusal
    assert _status(path) == "routed"


def test_non_register_sizing_ships_through_both_writers(repo):
    via_op = _no_register(repo)
    result = ship_mod._handler({"sizing_path": str(via_op)}, repo_root=repo)
    assert result["exit_code"] == 0 and result["applied"] is True
    assert _status(via_op) == "shipped"

    via_cascade = _no_register(repo, "plain-cascade.yaml")
    advanced, refusal = cascade_mod._advance_one_sizing(via_cascade, PLAN, repo / ".git")
    assert (advanced, refusal) == (True, None)
    assert _status(via_cascade) == "shipped"
