"""A holder run whose declared work is already at HEAD (hand-landed or recorded landed) holds no path."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import shared_path_landing as spl
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PRODUCT = "ops/product.py"
TESTS = "ops/test_product.py"
BASE = {PRODUCT: "a = 1\n", TESTS: "t = 1\n"}
KEPT = {PRODUCT: "a = 2\n", TESTS: "t = 2\n"}
OWN = "ask-20990101T000000-bbbbbb"


def _git(root: Path, *args: str, stdin: str = "") -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, input=stdin,
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _holder(root: Path, handlanded: bool) -> str:
    """Halted run H withholds PRODUCT and TESTS; with ``handlanded`` a hand commit puts the kept bytes at HEAD."""
    _git(root, "init", "-q")
    for p, text in BASE.items():
        _write(root / p, text)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    blobs = {p: _git(root, "hash-object", "-w", "--stdin", stdin=text) for p, text in KEPT.items()}
    for p, text in KEPT.items():
        _write(root / p, text)
    if handlanded:
        _git(root, "commit", "-q", "-am", "hand-landed")
    run_id = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - 300))}-7d7974"
    rec = root / "state/warp" / run_id
    sizing = "state/sizings/s.yaml"
    _write(root / sizing, "status: open\n")
    _write(rec / "run.json", json.dumps({"sizing_rel": sizing}))
    _write(rec / "halted.json", json.dumps({
        "status": "halted", "sizing_rel": sizing, "run_base_sha": base,
        "run_artifacts": {"withheld": {p: "halt-record-only" for p in KEPT}},
    }))
    _write(rec / "kept.json", json.dumps({
        "chunks": {"C1": {"paths": blobs}}, "plan_gate": None,
    }))
    return run_id


def _held(root: Path, holder: str) -> list:
    return spl.sibling_held_product(
        [("C1", PRODUCT, "peer-held")], {PRODUCT: [holder]}, root=root, own_run_id=OWN,
    )


def test_a_halted_holder_whose_work_is_not_at_head_still_holds(tmp_path):
    holder = _holder(tmp_path, handlanded=False)
    assert spl.holds_path(tmp_path, holder, PRODUCT, OWN)
    assert _held(tmp_path, holder) == [("C1", PRODUCT, [holder])]


def test_a_hand_landed_holder_with_no_record_does_not_hold(tmp_path):
    holder = _holder(tmp_path, handlanded=True)
    assert not spl.holds_path(tmp_path, holder, PRODUCT, OWN)
    assert _held(tmp_path, holder) == []


def test_a_holder_recorded_landed_does_not_hold(tmp_path):
    holder = _holder(tmp_path, handlanded=False)
    halted = tmp_path / "state/warp" / holder / "halted.json"
    marker = json.loads(halted.read_text(encoding="utf-8"))
    marker["status"] = "landed"
    _write(halted, json.dumps(marker))
    assert not spl.holds_path(tmp_path, holder, PRODUCT, OWN)
    assert _held(tmp_path, holder) == []


def test_the_refusal_that_remains_names_the_op_releasing_a_stale_hold(tmp_path):
    msg = spl.refusal_message([("C1", PRODUCT, ["run-x"])], OWN, tmp_path)
    assert "dispatch.record_halted_run_landed" in msg


def test_the_refusal_names_the_holder_run_in_the_release_call(tmp_path):
    msg = spl.refusal_message([("C1", PRODUCT, ["ask-20260101T000000-7d7974"])], OWN, tmp_path)
    assert "dispatch.record_halted_run_landed {run_id: ask-20260101T000000-7d7974," in msg


def test_a_live_holder_with_no_run_record_still_holds(tmp_path):
    assert spl.holds_path(tmp_path, "sess-1", PRODUCT, OWN)
