"""The submit hook answers inside the door's hook read deadline, so the door relays its answer."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real

repo = real.repo

DOOR_DIR = Path(mod.__file__).resolve().parents[2] / "coordinator_core" / "warm" / "door"
DOOR_WINDOWS_C = DOOR_DIR / "door.c"
DOOR_POSIX_C = DOOR_DIR / "door_posix.c"

#: Door-side clock start to `evaluate`'s: connect, dispatch, payload parse; plus `_answer_overran` and the write-back.
DOOR_PRE_EVALUATE_AND_WRITEBACK_MARGIN_S = 0.3


def _define(source: Path, macro: str) -> int:
    match = re.search(rf"^#define\s+{macro}\s+(\d+)\s*$", source.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"{macro} missing from {source.name}"
    return int(match.group(1))


def test_the_hook_deadline_constant_is_the_doors_define_in_both_doors():
    for door in (DOOR_WINDOWS_C, DOOR_POSIX_C):
        assert _define(door, "DOOR_HOOK_SUBMIT_READ_DEADLINE_MS") == mod._DOOR_HOOK_READ_DEADLINE_S * 1000, door.name
        assert _define(door, "DOOR_HOOK_READ_DEADLINE_MS") == 500, door.name


def test_default_budget_answers_inside_the_door_deadline(monkeypatch):
    monkeypatch.delenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", raising=False)
    answer_at = mod._caller_budget_s() * mod._ANSWER_SHARE + mod._SCAN_GRACE_S
    assert answer_at + DOOR_PRE_EVALUATE_AND_WRITEBACK_MARGIN_S < mod._DOOR_HOOK_READ_DEADLINE_S


def test_budget_is_the_lesser_of_the_env_and_the_door_deadline(monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "30")
    assert mod._caller_budget_s() == mod._DOOR_HOOK_READ_DEADLINE_S
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "2.5")
    assert mod._caller_budget_s() == 2.5
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "junk")
    assert mod._caller_budget_s() == mod._DOOR_HOOK_READ_DEADLINE_S


def test_multi_ask_fanout_slower_than_the_budget_denies_naming_the_hook_budget(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "1")
    release = threading.Event()

    def slow_fanout(params, root, abort=None, ledger=None):
        release.wait(10)
        return {"applied": False}

    asks = [{"ask": f"job {i}", "label": f"job-{i}", "source": "relayed", "writes": [f"pkg/m{i}.py"]} for i in range(3)]
    try:
        started = time.monotonic()
        env = mod.evaluate(real._payload(repo, {"asks": asks}), fanout=slow_fanout)
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "spooled as" in hso["additionalContext"]
