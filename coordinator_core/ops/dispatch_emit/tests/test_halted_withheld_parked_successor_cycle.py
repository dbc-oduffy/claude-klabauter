"""A halted or resumed run, or a run of its sizing, parked behind the committer holds nothing against that commit."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_hold_release as hr
from coordinator_core.ops.dispatch_emit import stage_wait

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SIZING = "state/sizings/s.yaml"
OTHER = "state/sizings/other.yaml"
HELD = "coordinator_core/shared.py"


def _rid(age: int, tail: str) -> str:
    return f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - age))}-{tail}"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(root: Path, run_id: str, marker: str = "", sizing: str = SIZING, withheld: bool = False) -> None:
    rec = root / "state/warp" / run_id
    _write(rec / "run.json", json.dumps({"sizing_rel": sizing}))
    _write(rec / "manifest.json", json.dumps({"rows": [{"writes": [HELD]}]}))
    if marker:
        body = {"status": marker, "sizing_rel": sizing}
        if marker == "halted" and withheld:
            body["run_artifacts"] = {"withheld": {HELD: "withheld"}}
        _write(rec / "halted.json", json.dumps(body))


def _root(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    _write(tmp_path / SIZING, "status: open\n")
    _write(tmp_path / OTHER, "status: open\n")
    return tmp_path


def _park(root: Path, run_id: str, behind: str) -> None:
    assert stage_wait.park(root, run_id, [behind], [HELD]) is not None


def test_halted_holder_with_successor_parked_behind_the_committer_holds_nothing(tmp_path: Path) -> None:
    root = _root(tmp_path)
    halted, successor, committer = _rid(900, "aaaaaa"), _rid(300, "bbbbbb"), _rid(10, "cccccc")
    _run(root, halted, "halted")
    _run(root, successor)
    _run(root, committer, sizing=OTHER)
    assert hr.release_reason(root, halted, committer, None) is None
    _park(root, successor, committer)
    assert hr.release_reason(root, halted, committer, None) == hr.RELEASE_SUCCESSOR_PARKED
    assert hr.held_paths(root, halted, committer, None) == frozenset()


def test_halted_holder_itself_parked_behind_the_committer_holds_nothing(tmp_path: Path) -> None:
    root = _root(tmp_path)
    halted, committer = _rid(900, "aaaaaa"), _rid(10, "cccccc")
    _run(root, halted, "halted")
    _run(root, committer, sizing=OTHER)
    _park(root, halted, committer)
    assert hr.release_reason(root, halted, committer, None) == hr.RELEASE_SUCCESSOR_PARKED


def test_resumed_holder_with_successor_parked_behind_the_committer_holds_nothing(tmp_path: Path) -> None:
    root = _root(tmp_path)
    resumed, successor, committer = _rid(900, "aaaaaa"), _rid(300, "bbbbbb"), _rid(10, "cccccc")
    _run(root, resumed, "resumed")
    _run(root, successor)
    _run(root, committer, sizing=OTHER)
    assert hr.release_reason(root, resumed, committer, None) is None
    _park(root, successor, committer)
    assert hr.release_reason(root, resumed, committer, None) == hr.RELEASE_SUCCESSOR_PARKED


def test_run_of_another_sizing_parked_behind_the_committer_does_not_release(tmp_path: Path) -> None:
    root = _root(tmp_path)
    halted, stranger, committer = _rid(900, "aaaaaa"), _rid(300, "bbbbbb"), _rid(10, "cccccc")
    _run(root, halted, "halted")
    _run(root, stranger, sizing=OTHER)
    _run(root, committer, sizing=OTHER)
    _park(root, stranger, committer)
    assert hr.release_reason(root, halted, committer, None) is None


def test_successor_parked_behind_a_different_run_does_not_release(tmp_path: Path) -> None:
    root = _root(tmp_path)
    halted, successor, committer, third = _rid(900, "aaaaaa"), _rid(300, "bbbbbb"), _rid(10, "cccccc"), _rid(5, "dddddd")
    _run(root, halted, "halted")
    _run(root, successor)
    _run(root, committer, sizing=OTHER)
    _run(root, third, sizing=OTHER)
    _park(root, successor, third)
    assert hr.release_reason(root, halted, committer, None) is None
