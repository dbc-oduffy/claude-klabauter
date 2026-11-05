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


SHARED = "src/shared.txt"
DECOYS = 20


def _mint_id(age: int, tag: str) -> str:
    return time.strftime("ask-%Y%m%dT%H%M%S", time.gmtime(time.time() - age)) + "-" + tag


def _plant_halted(root, run_id: str, withheld: list) -> None:
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    sizing = f"state/sizings/{run_id}.yaml"
    rec = record_dir(root, run_id)
    halted_run.write_halt_marker(rec, halted="commit", sizing_rel=sizing)
    marker = json.loads((rec / "halted.json").read_text(encoding="utf-8"))
    marker["run_artifacts"] = {"withheld": {p: "halt-record-only" for p in withheld}}
    for name, body in (
        ("halted.json", marker), ("manifest.json", {"rows": [{"id": "C0", "writes": withheld or [SHARED]}]}),
        ("run.json", {"sizing_rel": sizing}),
    ):
        (rec / name).write_text(json.dumps(body), encoding="utf-8")


def _record_files_opened_by_the_release_leg(tmp_path) -> tuple:
    """`(landing run, holder, decoys, {run id: file names the leg opened})` for `_own_run_seniority` then
    `_halted_only_release` over a landing run, one halted holder of `SHARED` and `DECOYS` unrelated halted runs."""
    import sys
    from collections import defaultdict

    from coordinator_core.ops.dispatch_emit import halted_withheld_index

    (tmp_path / ".git").mkdir()
    lander, holder = _mint_id(60, "aaaaaa"), _mint_id(300, "bbbbbb")
    decoys = [_mint_id(400, f"{i:06x}") for i in range(DECOYS)]
    _plant_halted(tmp_path, lander, [])
    _plant_halted(tmp_path, holder, [SHARED])
    for i, decoy in enumerate(decoys):
        _plant_halted(tmp_path, decoy, [f"decoy/{i}.txt"])
    (tmp_path / "review.md").write_text("green", encoding="utf-8")
    outcome = {"inline_review": {"integration": {"sidecar": "review.md"}}}
    (tmp_path / "state/warp" / lander / "run-outcome.json").write_text(json.dumps(outcome), encoding="utf-8")
    halted_withheld_index.rebuild(tmp_path)  # steady state: the indexes are built before the leg runs
    halted_hold_release.rebuild_sizing_runs(tmp_path)

    seen: dict = defaultdict(set)
    armed = [True]
    prefix = os.path.normcase(str((tmp_path / "state/warp").resolve())) + os.sep

    def hook(event, args):
        if armed[0] and event == "open" and args and isinstance(args[0], (str, os.PathLike)):
            path = os.path.abspath(os.fspath(args[0]))
            if os.path.normcase(path).startswith(prefix):
                run_id, _, name = path[len(prefix):].partition(os.sep)
                seen[run_id].add(name)

    sys.addaudithook(hook)
    try:
        land_run._own_run_seniority(tmp_path, lander, None)
        land_run._halted_only_release(tmp_path, lander, None, {SHARED: holder}, [SHARED], None)
    finally:
        armed[0] = False
    return lander, holder, decoys, seen


def test_the_release_leg_reads_this_runs_and_its_holders_records(tmp_path):
    lander, holder, _, seen = _record_files_opened_by_the_release_leg(tmp_path)
    assert "halted.json" in seen[lander] and "halted.json" in seen[holder]


def test_the_release_leg_reads_no_marker_manifest_or_outcome_of_an_unrelated_run(tmp_path):
    _, _, decoys, seen = _record_files_opened_by_the_release_leg(tmp_path)
    assert {d: seen[d] - {"run.json"} for d in decoys if seen[d] - {"run.json"}} == {}


def test_the_release_leg_opens_no_record_file_of_a_run_other_than_its_own_and_its_holders(tmp_path):
    lander, holder, decoys, seen = _record_files_opened_by_the_release_leg(tmp_path)
    assert {r for r in seen if seen[r]} <= {lander, holder}


def test_a_landed_twin_of_the_holders_sizing_still_releases_it_from_the_sizing_index(tmp_path):
    """The other side: a cached binding is still honored, and a twin is found without scanning every dir."""
    (tmp_path / ".git").mkdir()
    holder, twin = _mint_id(600, "bbbbbb"), _mint_id(100, "cccccc")
    _plant_halted(tmp_path, holder, [SHARED])
    _plant_halted(tmp_path, twin, [])
    sizing = f"state/sizings/{holder}.yaml"
    rec = tmp_path / "state/warp" / twin
    (rec / "run.json").write_text(json.dumps({"sizing_rel": sizing}), encoding="utf-8")
    marker = json.loads((rec / "halted.json").read_text(encoding="utf-8"))
    marker["status"] = "landed"
    (rec / "halted.json").write_text(json.dumps(marker), encoding="utf-8")
    for _ in range(2):  # cold, then served from the index file
        assert halted_hold_release._successor_landed(tmp_path, holder, sizing, any_stamp=True) is True
    (rec / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/other.yaml"}), encoding="utf-8")
    assert halted_hold_release._successor_landed(tmp_path, holder, sizing, any_stamp=True) is False
