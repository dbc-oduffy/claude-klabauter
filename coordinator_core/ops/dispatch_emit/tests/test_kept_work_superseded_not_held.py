"""kept_work.held_paths: a superseded halted run is not a holder; a live halted run still is."""

from __future__ import annotations

import json
import time
from pathlib import Path

from coordinator_core.ops.dispatch_emit import halted_hold_release as hr
from coordinator_core.ops.dispatch_emit import halted_withheld_index, kept_work

SIZING = "state/sizings/s.yaml"
PATH = "pkg/mod.py"


def _rid(age: int, tail: str) -> str:
    return f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - age))}-{tail}"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _halted_run(root: Path, run_id: str, *, marker: str, sizing: str) -> None:
    rec = root / "state/warp" / run_id
    _write(rec / "run.json", json.dumps({"sizing_rel": sizing}))
    _write(rec / "halted.json", json.dumps({"status": marker, "sizing_rel": sizing}))
    _write(rec / "kept.json", json.dumps({"chunks": {"C1": {"paths": {PATH: "a" * 40}}}, "plan_gate": None}))
    halted_withheld_index.add_run(root, run_id, [PATH])


def _root(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    _write(tmp_path / SIZING, "status: open\n")
    _write(tmp_path / "state/sizings/old.yaml", "status: superseded\n")
    return tmp_path


def test_sizing_superseded_run_is_not_a_holder(tmp_path):
    root = _root(tmp_path)
    run = _rid(300, "aaaaaa")
    _halted_run(root, run, marker="halted", sizing="state/sizings/old.yaml")
    assert hr.supersession_reason(root, run) == hr.RELEASE_SUPERSEDED
    assert kept_work.held_paths(root, [PATH]) == {}


def test_marker_superseded_run_is_not_a_holder(tmp_path):
    root = _root(tmp_path)
    run = _rid(300, "bbbbbb")
    _halted_run(root, run, marker="superseded", sizing=SIZING)
    assert hr.supersession_reason(root, run) == hr.RELEASE_MARKER_SUPERSEDED
    assert kept_work.held_paths(root, [PATH]) == {}


def test_live_halted_run_still_holds_while_superseded_peer_does_not(tmp_path):
    root = _root(tmp_path)
    sup, live = _rid(300, "aaaaaa"), _rid(290, "cccccc")
    _halted_run(root, sup, marker="halted", sizing="state/sizings/old.yaml")
    _halted_run(root, live, marker="halted", sizing=SIZING)
    assert hr.supersession_reason(root, live) is None
    assert kept_work.held_paths(root, [PATH]) == {PATH: live}
