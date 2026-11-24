"""A PM hold set through the `run.hold` op reaches the submit_engine_ask hook, the halt-strand census and the
watch tick; a non-holder starts nothing, and a clear by the holder releases the run."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core import ipc
from coordinator_core.group_em import watch
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask_resume_record import (
    HALTED_ID, SIZING_REL, STORED_REL, _hso, _payload, _repo, seams,  # noqa: F401 (seams is a fixture)
)
from coordinator_core.ops.dispatch_emit import halt_strand_census, run_hold
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

pytestmark = pytest.mark.complete

HOLDER = "S"
OTHER = "T"
RELEASE = "the example-retrieval-repo tool gap closes"


def _run_hold_op(root: Path, session: str, **params) -> dict:
    """`run.hold` through the op registry, as a caller outside the process would reach it."""
    return ipc.dispatch_from_hook(
        "run.hold",
        {"run_id": HALTED_ID, "session_id": session, "repo_root": str(root), **params},
        origin_worktree=str(root),
    )


def _as(root: Path, session: str, **args) -> dict:
    payload = _payload(root)
    payload["session_id"] = session
    payload["tool_input"]["args"].update(args)
    return payload


def _stage_bytes(root: Path) -> bytes:
    return (root / RECORD_DIR_ROOT / HALTED_ID / "run-stage.json").read_bytes()


def _assert_held(env: dict) -> None:
    out = _hso(env)
    assert out["permissionDecision"] == "allow" and out["state"] == "held"
    assert HOLDER in out["additionalContext"] and RELEASE in out["additionalContext"]
    assert "updatedInput" not in out


def _held_lines(root: Path) -> list:
    stream = io.StringIO()
    with mock.patch.object(watch, "poll_once", return_value=({}, [], {}, 0)):
        assert watch.tick_once(str(root), caller_session_id="w", group_em_session_id="c", stream=stream) == 0
    return [line for line in stream.getvalue().splitlines() if line.startswith("HELD")]


def test_a_held_run_is_refused_to_a_non_holder_everywhere_and_released_by_the_holder(tmp_path: Path, seams) -> None:
    spawned, emitted, emit = seams
    root = _repo(tmp_path)
    warp = root / RECORD_DIR_ROOT
    stage_before = _stage_bytes(root)
    halted_before = (warp / HALTED_ID / "halted.json").read_bytes()

    out = _run_hold_op(root, HOLDER, pm_turn_ref="PM turn 9", release_condition=RELEASE)
    assert out["state"] == "held" and out["holder_session"] == HOLDER
    assert run_hold.read_hold(warp / HALTED_ID).release_condition == RELEASE

    refire = mod.evaluate(_as(root, OTHER), emit=emit)
    resume = mod.evaluate(_as(root, OTHER, resume_run_id=HALTED_ID), emit=emit)
    _assert_held(refire)
    _assert_held(resume)
    assert spawned == [] and emitted == []
    assert _stage_bytes(root) == stage_before
    assert (warp / HALTED_ID / "halted.json").read_bytes() == halted_before

    unrelated = _as(root, OTHER)
    unrelated["tool_input"]["args"] = {"ask": "an unrelated question, nothing to do with the held run"}
    plain = mod.evaluate(unrelated, emit=emit)
    assert _hso(plain).get("state") != "held"
    assert emitted and emitted[0]["session_id"] == OTHER
    assert _stage_bytes(root) == stage_before

    census = halt_strand_census.census(warp)
    assert census["held"] == [{"run_id": HALTED_ID, "holder_session": HOLDER, "release_condition": RELEASE}]
    assert [r["cause"] for r in census["rows"]] == ["held"]
    assert _held_lines(root) == [f"HELD {HALTED_ID} by {HOLDER} until {RELEASE}"]

    refused = _run_hold_op(root, OTHER, clear=True)
    assert refused["state"] == "held_by_other" and HOLDER in refused["reply"]
    assert run_hold.read_hold(warp / HALTED_ID) is not None

    assert _run_hold_op(root, HOLDER, clear=True)["state"] == "cleared"
    assert not (warp / HALTED_ID / run_hold.HELD_FILE).exists()
    assert halt_strand_census.census(warp)["held"] == []
    assert _held_lines(root) == []
    released = mod.evaluate(_as(root, OTHER), emit=emit)
    assert _hso(released)["updatedInput"]["scriptPath"] == str(root / STORED_REL)


def test_the_holder_refire_proceeds_while_held(tmp_path: Path, seams) -> None:
    _, _, emit = seams
    root = _repo(tmp_path)
    _run_hold_op(root, HOLDER, pm_turn_ref="PM turn 9", release_condition=RELEASE)
    env = mod.evaluate(_as(root, HOLDER), emit=emit)
    assert _hso(env)["updatedInput"]["scriptPath"] == str(root / STORED_REL)
    assert (root / RECORD_DIR_ROOT / HALTED_ID / run_hold.HELD_FILE).is_file()


def test_continue_held_proceeds_for_a_non_holder_and_records_it(tmp_path: Path, seams) -> None:
    _, _, emit = seams
    root = _repo(tmp_path)
    _run_hold_op(root, HOLDER, pm_turn_ref="PM turn 9", release_condition=RELEASE)
    env = mod.evaluate(_as(root, OTHER, continue_held=HALTED_ID), emit=emit)
    assert _hso(env)["updatedInput"]["scriptPath"] == str(root / STORED_REL)
    notes = (root / RECORD_DIR_ROOT / HALTED_ID / "notes.jsonl").read_text(encoding="utf-8")
    assert "Continued past the PM hold" in json.loads(notes.splitlines()[-1])["note"]
