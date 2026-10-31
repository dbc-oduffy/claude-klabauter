"""land_run.foreign_halted_withheld at default arguments: a halted run shaped like 977c47 (refire next_action,
no live token, halt older than the bound) no longer withholds a path; inside the bound it still does."""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from coordinator_core.ops.dispatch_emit import halted_hold_release, land_run, run_token
from coordinator_core.ops.dispatch_emit.tests.test_land_run_declared_write_left_dirty import (  # noqa: F401
    CODE, _git, _write, repo,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

LANDER = "ask-20261030T223203-aaaaaa"
HOLDER_SIZING = "state/sizings/20260101-halted-holder.yaml"
PAST = halted_hold_release.STALE_HOLD_BOUND_SECONDS + 600


def _halted_977c47(repo, age: int) -> str:
    """Plant the halt with its run id minted and its marker written `age` seconds ago; returns the run id."""
    holder = time.strftime("ask-%Y%m%dT%H%M%S", time.gmtime(time.time() - age)) + "-977c47"
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=repo, input="a = 0\nb = 7\nc = 0\n", capture_output=True,
        text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()
    rec = f"state/warp/{holder}"
    _write(repo, f"{rec}/run.json", json.dumps({"sizing_rel": HOLDER_SIZING}))
    _write(repo, f"{rec}/halted.json", json.dumps({
        "status": "halted", "sizing_rel": HOLDER_SIZING, "next_action": {"kind": "refire"},
        "run_base_sha": _git(repo, "rev-parse", "HEAD").strip(),
        "run_artifacts": {"withheld": {CODE: "halt-record-only"}},
    }))
    _write(repo, f"{rec}/kept.json", json.dumps({"chunks": {"X1": {"paths": {CODE: blob}}}, "plan_gate": None}))
    _write(repo, CODE, "a = 1\nb = 0\nc = 0\n")
    stamp = time.time() - age
    os.utime(repo / rec / "halted.json", (stamp, stamp))
    return holder


def test_a_halt_past_the_bound_with_no_live_token_does_not_withhold(repo):
    _halted_977c47(repo, PAST)
    assert land_run.foreign_halted_withheld(repo, LANDER, None, [CODE]) == {}


def test_a_halt_inside_the_bound_still_withholds(repo):
    holder = _halted_977c47(repo, 300)
    assert land_run.foreign_halted_withheld(repo, LANDER, None, [CODE]) == {CODE: holder}


def test_a_halt_past_the_bound_with_a_live_token_of_its_own_still_withholds(repo):
    holder = _halted_977c47(repo, PAST)
    run_token.mint(repo, holder, "wf/x.mjs", sizing_rel=HOLDER_SIZING)
    assert land_run.foreign_halted_withheld(repo, LANDER, None, [CODE]) == {CODE: holder}
