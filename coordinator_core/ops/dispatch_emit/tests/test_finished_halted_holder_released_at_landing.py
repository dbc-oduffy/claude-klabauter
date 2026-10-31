"""A halted run whose id was minted past the bound stops holding at landing even when its `halted.json` was
rewritten minutes ago; a live token, an unparseable id or a young mint stamp still holds."""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from coordinator_core.ops.dispatch_emit import halted_hold_release as hr
from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit.tests.test_land_run_declared_write_left_dirty import (  # noqa: F401
    CODE, _git, _write, repo,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

LANDER = "ask-20261030T234901-92c9cb"
SIZING = "state/sizings/20260101-finished-holder.yaml"
FRESH_MTIME_AGE = 480


def _minted(age: float, tail: str = "6a0ac6") -> str:
    return time.strftime("ask-%Y%m%dT%H%M%S", time.gmtime(time.time() - age)) + "-" + tail


def _halt(repo, run_id: str, mtime_age: float = FRESH_MTIME_AGE) -> None:
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=repo, input="a = 0\nb = 7\nc = 0\n", capture_output=True,
        text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()
    rec = f"state/warp/{run_id}"
    _write(repo, f"{rec}/run.json", json.dumps({"sizing_rel": SIZING}))
    _write(repo, f"{rec}/halted.json", json.dumps({
        "status": "halted", "sizing_rel": SIZING, "next_action": {"kind": "refire"},
        "run_base_sha": _git(repo, "rev-parse", "HEAD").strip(),
        "run_artifacts": {"withheld": {CODE: "halt-record-only"}},
    }))
    _write(repo, f"{rec}/kept.json", json.dumps({"chunks": {"X1": {"paths": {CODE: blob}}}, "plan_gate": None}))
    _write(repo, CODE, "a = 1\nb = 0\nc = 0\n")
    stamp = time.time() - mtime_age
    os.utime(repo / rec / "halted.json", (stamp, stamp))


def test_mint_epoch_parses_the_id_stamp_and_rejects_others():
    assert hr.run_id_mint_epoch("ask-20261030T135842-6a0ac6") == 1793368722.0
    assert hr.run_id_mint_epoch("ask-legacy-holder") is None


def test_old_mint_with_fresh_halt_mtime_is_released_and_lands(repo):
    holder = _minted(hr.STALE_HOLD_BOUND_SECONDS + 3600)
    _halt(repo, holder)
    assert hr.stale_release(repo, holder) == hr.RELEASE_STALE_NOT_LIVE
    assert land_run.foreign_halted_withheld(repo, LANDER, None, [CODE]) == {}


def test_old_mint_with_a_live_token_of_its_own_still_withholds(repo):
    holder = _minted(hr.STALE_HOLD_BOUND_SECONDS + 3600)
    _halt(repo, holder)
    run_token.mint(repo, holder, "wf/x.mjs", sizing_rel=SIZING)
    assert hr.stale_release(repo, holder) is None
    assert land_run.foreign_halted_withheld(repo, LANDER, None, [CODE]) == {CODE: holder}


def test_young_mint_still_withholds(repo):
    holder = _minted(600)
    _halt(repo, holder)
    assert hr.stale_release(repo, holder) is None
    assert land_run.foreign_halted_withheld(repo, LANDER, None, [CODE]) == {CODE: holder}


def test_unparseable_id_falls_back_to_the_mtime_and_holds_when_fresh(repo):
    holder = "ask-legacy-holder"
    _halt(repo, holder)
    assert hr.stale_release(repo, holder) is None


def test_unparseable_id_with_an_old_mtime_is_released(repo):
    holder = "ask-legacy-holder"
    _halt(repo, holder, mtime_age=hr.STALE_HOLD_BOUND_SECONDS + 600)
    assert hr.stale_release(repo, holder) == hr.RELEASE_STALE_NOT_LIVE
