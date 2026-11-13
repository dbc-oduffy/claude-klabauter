"""A refire's own lineage (resumed_from chain, same-sizing ask id) never withholds its paths; a foreign halted run still does."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.tests.test_finished_halted_holder_released_at_landing import (  # noqa: F401
    SIZING, _halt, _minted, repo,
)
from coordinator_core.ops.dispatch_emit.tests.test_land_run_declared_write_left_dirty import CODE, _write

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

OTHER_SIZING = "state/sizings/20260101-someone-else.yaml"


def _run_json(repo, run_id: str, **fields) -> None:
    _write(repo, f"state/warp/{run_id}/run.json", json.dumps(fields))


def test_resumed_from_chain_holders_never_withhold(repo):
    first, second, lander = _minted(900, "aaaaaa"), _minted(700, "bbbbbb"), _minted(100, "cccccc")
    _halt(repo, first)
    _halt(repo, second)
    _run_json(repo, second, sizing_rel=SIZING, resumed_from=first)
    _run_json(repo, lander, sizing_rel=OTHER_SIZING, resumed_from=second)
    assert land_run.foreign_halted_withheld(repo, lander, None, [CODE]) == {}


def test_same_sizing_ask_id_holder_never_withholds(repo):
    holder, lander = _minted(900, "aaaaaa"), _minted(100, "cccccc")
    _halt(repo, holder)
    _run_json(repo, lander, sizing_rel=SIZING)
    assert land_run.foreign_halted_withheld(repo, lander, None, [CODE]) == {}


def test_foreign_halted_run_still_withholds(repo):
    holder, lander = _minted(900, "aaaaaa"), _minted(100, "cccccc")
    _halt(repo, holder)
    _run_json(repo, lander, sizing_rel=OTHER_SIZING)
    assert land_run.foreign_halted_withheld(repo, lander, None, [CODE]) == {CODE: holder}


def test_lineage_holder_released_while_foreign_holder_still_withholds(repo):
    mine, foreign, lander = _minted(900, "aaaaaa"), _minted(800, "dddddd"), _minted(100, "cccccc")
    _halt(repo, mine)
    _halt(repo, foreign)
    _run_json(repo, mine, sizing_rel=OTHER_SIZING)
    _run_json(repo, foreign, sizing_rel="state/sizings/20260101-third.yaml")
    _run_json(repo, lander, sizing_rel=SIZING, resumed_from=mine)
    assert land_run.foreign_halted_withheld(repo, lander, None, [CODE]) == {CODE: foreign}
