"""sizing.mark_routed, and sizing.ship's refusal of cascade-only routes."""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.ops.sizing_mark_routed as routed_mod
import coordinator_core.ops.sizing_ship as ship_mod
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted
from coordinator_core.ops.tests.test_sizing_ship import _init_repo, _sizing_body

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REL = "state/sizings/20261008-goal.yaml"


def _seed(repo: Path, *, status: str, route: str = "goal-setting") -> Path:
    path = repo / _REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _sizing_body(status=status).replace("route: spec-dispatch", f"route: {route}"),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    _init_repo(r)
    return r


@pytest.mark.parametrize("route", ["goal-setting", "roadmap", "shape"])
def test_ship_refuses_cascade_only_routes(repo, route):
    path = _seed(repo, status="routed", route=route)
    result = ship_mod._handler({"sizing_path": _REL}, repo_root=repo / ".git")
    assert result["exit_code"] == 1
    assert "sizing.mark_routed" in result["error"]
    assert read_fm_field_unquoted(path.read_text(encoding="utf-8"), "status") == "routed"


def test_ship_still_ships_spec_dispatch(repo):
    path = _seed(repo, status="routed", route="spec-dispatch")
    assert ship_mod._handler({"sizing_path": _REL}, repo_root=repo / ".git")["applied"] is True
    assert read_fm_field_unquoted(path.read_text(encoding="utf-8"), "status") == "shipped"


def test_mark_routed_from_sized_records_goal(repo):
    path = _seed(repo, status="sized")
    result = routed_mod._handler({"sizing_path": _REL, "goal_id": "goal-x"}, repo_root=repo / ".git")
    assert result["exit_code"] == 0 and result["applied"] is True, result
    text = path.read_text(encoding="utf-8")
    assert read_fm_field_unquoted(text, "status") == "routed"
    assert read_fm_field_unquoted(text, "goal_id") == "goal-x"


def test_mark_routed_same_goal_is_idempotent(repo):
    _seed(repo, status="sized")
    routed_mod._handler({"sizing_path": _REL, "goal_id": "goal-x"}, repo_root=repo / ".git")
    again = routed_mod._handler({"sizing_path": _REL, "goal_id": "goal-x"}, repo_root=repo / ".git")
    assert again["exit_code"] == 0 and again["applied"] is False


def test_mark_routed_refuses_a_second_goal(repo):
    _seed(repo, status="sized")
    routed_mod._handler({"sizing_path": _REL, "goal_id": "goal-x"}, repo_root=repo / ".git")
    other = routed_mod._handler({"sizing_path": _REL, "goal_id": "goal-y"}, repo_root=repo / ".git")
    assert other["exit_code"] == 1 and "duplicate" in other["error"]


@pytest.mark.parametrize("status", ["draft", "shipped", "declined", "superseded"])
def test_mark_routed_refuses_non_routable_status(repo, status):
    _seed(repo, status=status)
    result = routed_mod._handler({"sizing_path": _REL, "goal_id": "goal-x"}, repo_root=repo / ".git")
    assert result["exit_code"] == 1


def test_mark_routed_requires_goal_id(repo):
    _seed(repo, status="sized")
    result = routed_mod._handler({"sizing_path": _REL}, repo_root=repo / ".git")
    assert result["exit_code"] == 1 and "goal_id" in result["error"]
