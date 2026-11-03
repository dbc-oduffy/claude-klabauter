"""A halted run whose sibling run of the same sizing already landed holds nothing, whichever was minted first."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.ops import warp_run_deliverable as wrd
from coordinator_core.ops.dispatch_emit import halted_hold_release as hr
from coordinator_core.ops.dispatch_emit import run_token

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SIZING = "state/sizings/s.yaml"
HELD = "coordinator_core/shared.py"


def _rid(age: int, tail: str) -> str:
    return f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - age))}-{tail}"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(root: Path, run_id: str, marker: str = "", sizing: str = SIZING, withheld: bool = True) -> None:
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
    return tmp_path


def test_halted_twin_minted_before_the_landed_run_is_released(tmp_path):
    root = _root(tmp_path)
    twin, landed, third = _rid(900, "aaaaaa"), _rid(300, "bbbbbb"), _rid(10, "cccccc")
    _run(root, twin, "halted")
    _run(root, landed, "landed")
    _run(root, third, sizing="state/sizings/other.yaml")
    assert hr.release_reason(root, twin, third, None) == hr.RELEASE_TWIN_LANDED
    assert hr.held_paths(root, twin, third, None) == frozenset()
    assert wrd.halted_run_write_claims(root, [HELD], exclude_run=third) == {}


def test_halted_twin_minted_after_the_landed_run_is_released(tmp_path):
    root = _root(tmp_path)
    landed, twin = _rid(900, "aaaaaa"), _rid(300, "bbbbbb")
    _run(root, landed, "landed")
    _run(root, twin, "halted")
    assert hr.release_reason(root, twin, None, None) == hr.RELEASE_TWIN_LANDED


def test_spent_landed_receipt_of_the_sibling_releases_the_halted_twin(tmp_path):
    root = _root(tmp_path)
    twin, landed = _rid(900, "aaaaaa"), _rid(300, "bbbbbb")
    _run(root, twin, "halted")
    _run(root, landed)
    _write(run_token._record_path(root, landed, spent=True), json.dumps({"landed": {"sha": "f" * 40}}))
    assert hr.release_reason(root, twin, None, None) == hr.RELEASE_TWIN_LANDED


def test_halted_run_with_no_landed_sibling_still_holds(tmp_path):
    root = _root(tmp_path)
    held, sibling, third = _rid(900, "aaaaaa"), _rid(300, "bbbbbb"), _rid(10, "cccccc")
    _run(root, held, "halted", withheld=False)
    _run(root, sibling)
    _run(root, third, sizing="state/sizings/other.yaml")
    assert hr.release_reason(root, held, third, None) is None


def test_landed_run_of_another_sizing_does_not_release(tmp_path):
    root = _root(tmp_path)
    held, other = _rid(900, "aaaaaa"), _rid(300, "bbbbbb")
    _run(root, held, "halted", withheld=False)
    _run(root, other, "landed", sizing="state/sizings/other.yaml")
    assert hr.release_reason(root, held, None, None) is None


def test_resumed_run_is_not_released_by_an_earlier_landed_sibling(tmp_path):
    root = _root(tmp_path)
    landed, resumed = _rid(900, "aaaaaa"), _rid(300, "bbbbbb")
    _run(root, landed, "landed")
    _run(root, resumed, "resumed")
    assert hr.release_reason(root, resumed, None, None) is None
