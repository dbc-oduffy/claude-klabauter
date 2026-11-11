"""Tests for `group_em.goal_board` -- the fleet goal board's validation, flags and sole writer.

The vendored schema `contract/fleet-goal-board.schema.json` and the seed fixture
`fixtures/fleet-goal-board-seed.json` are byte copies of coordinator-content-repo commit `5f3e0cbb2f`
(`coordinator/schemas/fleet-goal-board.schema.json`,
`coordinator/schemas/fixtures/fleet-goal-board/seed-ecabf17.json`). Every case runs in-process on
`tmp_path`; no subprocess is spawned.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.group_em import gem_home, goal_board

pytestmark = pytest.mark.complete

SEED = Path(__file__).parent / "fixtures" / "fleet-goal-board-seed.json"
NOW = datetime(2026, 11, 11, 10, 0, 0, tzinfo=timezone.utc)


def _seed() -> dict:
    return json.loads(SEED.read_text(encoding="utf-8"))


def _all_live(_sid: str) -> bool:
    return True


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "group-em"
    h.mkdir()
    monkeypatch.setattr(gem_home, "resolve_home", lambda: h)
    return h


def _put(home: Path, board: dict) -> Path:
    p = home / goal_board.BOARD_FILENAME
    p.write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")
    return p


def test_board_path_is_under_resolved_home(home):
    assert goal_board.board_path() == home / "fleet-goal-board.json"


def test_seed_validates_clean():
    assert goal_board.validate(_seed()) == []


def test_validate_refuses_bare_zero_distance_and_unknown_key():
    board = _seed()
    board["goals"][0]["distance"] = {"figure": "0", "ref": "abc"}
    board["bogus"] = 1
    fields = {e["field"] for e in goal_board.validate(board)}
    assert "bogus" in fields
    assert any(f.startswith("goals[0]") for f in fields)


def test_validate_refuses_non_dict():
    assert goal_board.validate([]) != []


def test_fresh_board_with_live_owners_has_no_flags():
    assert goal_board.compute_flags(_seed(), NOW, _all_live) == []


def test_stale_pins_both_sides_of_the_cutoff():
    board = _seed()
    board["stale_after_hours"] = 1
    board["goals"][0]["last_updated"] = "2026-11-11T08:59:00Z"
    board["goals"][1]["last_updated"] = "2026-11-11T09:01:00Z"
    stale = [f["id"] for f in goal_board.compute_flags(board, NOW, _all_live) if f["flag"] == "stale"]
    assert stale == ["1a"]


def test_stale_defaults_to_24_hours_when_field_absent():
    board = _seed()
    del board["stale_after_hours"]
    board["goals"][2]["last_updated"] = "2026-11-10T09:00:00Z"
    stale = [f["id"] for f in goal_board.compute_flags(board, NOW, _all_live) if f["flag"] == "stale"]
    assert stale == ["2a"]


def test_no_live_owner_needs_every_owner_dead():
    board = _seed()
    board["goals"][1]["owners"] = ["steady-owner"]
    live = {"coordinator-content-repo-50": False, "claude-klabauter-c6": True}
    flags = goal_board.compute_flags(board, NOW, lambda sid: live.get(sid, True))
    assert flags == []
    live["claude-klabauter-c6"] = False
    flags = goal_board.compute_flags(board, NOW, lambda sid: live.get(sid, True))
    assert flags == [{"flag": "no-live-owner", "id": "1a"}]


def test_liveness_is_asked_once_per_owner():
    board = _seed()
    board["goals"][3]["owners"] = ["example-game-workbench-repo-19"]
    seen = []

    def probe(sid):
        seen.append(sid)
        return True

    goal_board.compute_flags(board, NOW, probe)
    assert seen.count("example-game-workbench-repo-19") == 1


def test_unmapped_task_null_goal_unknown_goal_mapped_and_fifa():
    board = _seed()
    board["task_map"] = [
        {"em": "e1", "task_id": "t1", "goal": None},
        {"em": "e1", "task_id": "t2", "goal": "nope"},
        {"em": "e2", "task_id": "t3", "goal": "1a"},
        {"em": "e2", "task_id": "t4", "goal": "FIFA"},
    ]
    del board["goals"][-1]  # FIFA stays mapped without a FIFA row
    unmapped = [f["id"] for f in goal_board.compute_flags(board, NOW, _all_live) if f["flag"] == "unmapped-task"]
    assert unmapped == ["e1/t1", "e1/t2"]


def test_default_liveness_reads_the_session_registry(monkeypatch):
    from coordinator_core.group_em import session_registry

    calls = []

    def fake(record, registry_dir=None):
        calls.append(record)
        return (record["session_id"] == "claude-klabauter-c6"), None

    monkeypatch.setattr(session_registry, "is_live", fake)
    board = _seed()
    flags = goal_board.compute_flags(board, NOW)
    assert {"session_id": "claude-klabauter-c6"} in calls
    ids = {f["id"] for f in flags if f["flag"] == "no-live-owner"}
    assert "1a" not in ids and "1b" in ids


def test_refresh_writes_validated_board_with_flags(home):
    board = _seed()
    board["goals"][0]["last_updated"] = "2026-11-01T00:00:00Z"
    board["task_map"] = [{"em": "e1", "task_id": "t1", "goal": None}]
    path = _put(home, board)
    result = goal_board.refresh(now=NOW, is_live=_all_live)
    assert result["ok"] is True and result["errors"] == []
    assert result["path"] == str(path)
    assert result["flags"] == [
        {"flag": "stale", "id": "1a"},
        {"flag": "unmapped-task", "id": "e1/t1"},
    ]
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["board_as_of"] == "2026-11-11T10:00:00Z"
    assert written["flags"] == [
        {"kind": "stale", "subject": "1a"},
        {"kind": "unmapped-task", "subject": "e1/t1"},
    ]
    assert goal_board.validate(written) == []
    assert list(home.glob("*.tmp")) == []


def test_refresh_is_idempotent_on_flags(home):
    _put(home, _seed())
    first = goal_board.refresh(now=NOW, is_live=_all_live)
    second = goal_board.refresh(now=NOW, is_live=_all_live)
    assert first["flags"] == second["flags"]


def test_refresh_refuses_invalid_board_and_leaves_file_untouched(home):
    board = _seed()
    board["goals"][0]["owners"] = []
    path = _put(home, board)
    before = path.read_bytes()
    result = goal_board.refresh(now=NOW, is_live=_all_live)
    assert result["ok"] is False and result["errors"]
    assert path.read_bytes() == before


def test_refresh_refuses_missing_board_without_creating_it(home):
    result = goal_board.refresh(now=NOW, is_live=_all_live)
    assert result["ok"] is False
    assert not (home / goal_board.BOARD_FILENAME).exists()


def test_refresh_refuses_non_json_board_untouched(home):
    path = home / goal_board.BOARD_FILENAME
    path.write_text("{not json", encoding="utf-8")
    result = goal_board.refresh(now=NOW, is_live=_all_live)
    assert result["ok"] is False
    assert path.read_text(encoding="utf-8") == "{not json"


def test_refresh_surfaces_home_refused_naming_the_key(monkeypatch):
    def refuse():
        raise gem_home.HomeRefused("/x/home", "/x")

    monkeypatch.setattr(gem_home, "resolve_home", refuse)
    result = goal_board.refresh(now=NOW, is_live=_all_live)
    assert result["ok"] is False and result["path"] is None
    assert result["errors"][0]["field"] == "coordinator.group_em_home"
    assert "coordinator.group_em_home" in result["errors"][0]["hint"]


def test_compute_flags_does_not_mutate_the_board():
    board = _seed()
    snapshot = copy.deepcopy(board)
    goal_board.compute_flags(board, NOW, _all_live)
    assert board == snapshot


def test_sole_writer_no_other_production_module_names_the_board_file():
    root = Path(goal_board.__file__).resolve().parent.parent
    own = Path(goal_board.__file__).resolve()
    offenders = []
    for py in root.rglob("*.py"):
        if py.resolve() == own or "tests" in py.relative_to(root).parts:
            continue
        if goal_board.BOARD_FILENAME in py.read_text(encoding="utf-8", errors="replace"):
            offenders.append(str(py.relative_to(root)))
    assert offenders == []
