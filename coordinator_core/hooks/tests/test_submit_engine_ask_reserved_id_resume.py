"""A fan-slice refire resumes the sizing's halted run whatever run id the slice reserved, and the
revisions queued under the reserved or parent ask id reach the halted run's record once."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask_resume_record import (
    HALTED_ID,
    RECORD_DIR_ROOT,
    SIZING_REL,
    _hso,
    _payload,
    _repo,
    seams,  # noqa: F401  (fixture)
)
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

RESERVED_ID = "ask-20261029T020202-ffffff"
PARENT_ID = "ask-20261029T030303-eeeeee"


def _queue(root: Path, run_id: str, *texts: str) -> None:
    d = root / RECORD_DIR_ROOT / run_id
    d.mkdir(parents=True, exist_ok=True)
    with (d / rl.INBOX_FILE).open("a", encoding="utf-8") as fh:
        for t in texts:
            fh.write(json.dumps({"text": t, "at": "2026-10-29T09:00:00Z"}) + "\n")


def _inbox_texts(root: Path, run_id: str) -> list[str]:
    return [e["text"] for e in rl._inbox(root / RECORD_DIR_ROOT / run_id)]


def _slice_payload(root: Path) -> dict:
    return _payload(root)


@pytest.mark.parametrize("reserved", [RESERVED_ID, HALTED_ID])
def test_halted_marker_wins_over_the_reserved_id(tmp_path, seams, reserved):  # noqa: F811
    spawned, emitted, emit = seams
    root = _repo(tmp_path, record=False)
    env, result = mod._resume_route(root, SIZING_REL, _payload(root), {}, "s1", None, reserved_run_id=reserved)
    assert env is not None and result is None
    assert HALTED_ID in Path(_hso(env)["updatedInput"]["scriptPath"]).read_text(encoding="utf-8")
    assert spawned == [HALTED_ID] and emitted == []


def test_revisions_under_reserved_and_parent_ids_carry_once(tmp_path, seams):  # noqa: F811
    root = _repo(tmp_path, record=False)
    _queue(root, HALTED_ID, "on the warp id")
    _queue(root, RESERVED_ID, "on the reserved id", "on the warp id")
    _queue(root, PARENT_ID, "on the parent id")
    for _ in range(2):
        env, _result = mod._resume_route(
            root, SIZING_REL, _payload(root), {}, "s1", None, reserved_run_id=RESERVED_ID, ask_parent_id=PARENT_ID
        )
        assert env is not None
    assert sorted(_inbox_texts(root, HALTED_ID)) == ["on the parent id", "on the reserved id", "on the warp id"]


def test_carry_keeps_the_sender_and_leaves_the_source(tmp_path):
    root = _repo(tmp_path, record=False)
    d = root / RECORD_DIR_ROOT / RESERVED_ID
    d.mkdir(parents=True)
    (d / rl.INBOX_FILE).write_text(json.dumps({"text": "x", "at": "t", "from": "peer"}) + "\n", encoding="utf-8")
    assert rl.carry_inbox(root, [RESERVED_ID, "../escape"], HALTED_ID) == 1
    assert rl._inbox(root / RECORD_DIR_ROOT / HALTED_ID)[0]["from"] == "peer"
    assert _inbox_texts(root, RESERVED_ID) == ["x"]


def test_resume_hook_process_time_stays_under_the_bar(tmp_path, seams):  # noqa: F811
    root = _repo(tmp_path, record=False)
    _queue(root, RESERVED_ID, "q")
    started = time.process_time()
    env, _ = mod._resume_route(root, SIZING_REL, _payload(root), {}, "s1", None, reserved_run_id=RESERVED_ID)
    assert env is not None
    assert time.process_time() - started < 0.5
