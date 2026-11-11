"""
coordinator_core.ops.tests.test_group_em_goal_board_refresh_op -- tests for
"groupem.goal_board_refresh" (registration, flags at default arguments, refusals).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.group_em import gem_home, goal_board
from coordinator_core.op_scopes import OP_KEY_SCOPE
from coordinator_core.ops import _registry_map
from coordinator_core.ops import group_em_goal_board_refresh as op

pytestmark = pytest.mark.complete

SEED = Path(__file__).parents[2] / "group_em" / "tests" / "fixtures" / "fleet-goal-board-seed.json"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "group-em"
    h.mkdir()
    monkeypatch.setattr(gem_home, "resolve_home", lambda: h)
    monkeypatch.setattr(goal_board, "_registry_is_live", lambda sid: False)
    return h


def _seed_board() -> dict:
    return json.loads(SEED.read_text(encoding="utf-8"))


def test_registration_quad():
    assert ipc._REGISTRY.get("groupem.goal_board_refresh") is not None
    assert (
        _registry_map.OP_MODULE_MAP["groupem.goal_board_refresh"]
        == "coordinator_core.ops.group_em_goal_board_refresh"
    )
    assert OP_KEY_SCOPE["groupem.goal_board_refresh"] == "none"
    assert OP_CLASSIFICATION["groupem.goal_board_refresh"] is OpClass.MUTATING


def test_default_arguments_return_all_three_flags_and_write_the_board(home):
    board = _seed_board()
    board["goals"][0]["last_updated"] = "2020-01-01T00:00:00Z"
    board["task_map"] = [{"em": "e1", "task_id": "t1", "goal": None}]
    path = home / goal_board.BOARD_FILENAME
    path.write_text(json.dumps(board), encoding="utf-8")
    out = op._groupem_goal_board_refresh({})
    assert out["ok"] is True and out["errors"] == []
    assert out["path"] == str(path)
    kinds = {f["flag"] for f in out["flags"]}
    assert kinds == {"stale", "no-live-owner", "unmapped-task"}
    written = json.loads(path.read_text(encoding="utf-8"))
    assert {f["kind"] for f in written["flags"]} == kinds


def test_invalid_board_is_refused_and_file_untouched(home):
    board = _seed_board()
    board["goals"][0]["owners"] = []
    path = home / goal_board.BOARD_FILENAME
    path.write_text(json.dumps(board), encoding="utf-8")
    before = path.read_bytes()
    out = op._groupem_goal_board_refresh({})
    assert out["ok"] is False and out["errors"]
    assert path.read_bytes() == before


def test_missing_board_is_refused_without_creating_it(home):
    out = op._groupem_goal_board_refresh({})
    assert out["ok"] is False
    assert not (home / goal_board.BOARD_FILENAME).exists()


def test_home_refused_names_the_key(monkeypatch):
    def refuse():
        raise gem_home.HomeRefused("/x/home", "/x")

    monkeypatch.setattr(gem_home, "resolve_home", refuse)
    out = op._groupem_goal_board_refresh({})
    assert out["ok"] is False
    assert out["errors"][0]["field"] == "coordinator.group_em_home"


def test_unexpected_exception_degrades_to_a_refusal(monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(goal_board, "refresh", boom)
    out = op._groupem_goal_board_refresh({})
    assert out["ok"] is False and "RuntimeError" in out["errors"][0]["error"]
