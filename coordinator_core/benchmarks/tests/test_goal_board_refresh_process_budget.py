"""The goal-board refresh benchmark seeds a schema-valid board and reports flags, spawns and process ms.

Behaviour pinned: the fixture board validates against the vendored schema and carries a stale
goal, an unmapped task and a FIFA-mapped task; the measured op call succeeds, returns each of the
three flag kinds, spawns no git, and the unmapped-task flag names only the null-goal task.
"""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import goal_board_refresh_process_budget as bench
from coordinator_core.group_em import goal_board

pytestmark = pytest.mark.cadence


@pytest.fixture(scope="module")
def report():
    return bench.measure(runs=1)


def test_the_fixture_board_is_schema_valid_and_seeds_each_flag_shape():
    board = bench.build_board()
    assert goal_board.validate(board) == []
    assert board["goals"][0]["last_updated"] == bench._STALE_STAMP
    assert {t["goal"] for t in board["task_map"]} == {None, "FIFA"}


def test_the_seeded_home_holds_the_board_where_the_default_home_resolves(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    path = bench.seed_home(tmp_path)
    assert path.is_file() and path.name == goal_board.BOARD_FILENAME
    assert path.parent == tmp_path / "settings-home" / "group-em"


def test_the_op_call_succeeds_and_returns_every_flag_kind(report):
    assert report["ok"] is True, report["errors"]
    assert set(report["flag_kinds"]) == bench.EXPECTED_KINDS, report["flags"]
    unmapped = [f["id"] for f in report["flags"] if f["flag"] == "unmapped-task"]
    assert unmapped == ["bench-em/t-unmapped"]


def test_the_op_spawns_no_git_and_reports_process_time(report):
    assert report["git_spawns"] == 0, report["spawn_argv"]
    assert isinstance(report["process_ms"], float) and report["process_ms"] >= 0.0
    assert isinstance(report["cold_process_ms"], float)
