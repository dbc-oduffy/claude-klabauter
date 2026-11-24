"""A refire or resume of a PM-held run from a non-holder returns a typed `held` allow envelope and starts
nothing; the holder and an explicit `continue_held` caller proceed, the continue recorded in the run's notes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask_resume_record import (
    HALTED_ID, SIZING_REL, STORED_REL, _hso, _payload, _repo, seams,  # noqa: F401 (seams is a fixture)
)
from coordinator_core.ops.dispatch_emit import run_hold
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

pytestmark = pytest.mark.complete

HOLDER = "holder-session"
OTHER = "s1"  # the session `_payload` carries


def _held_repo(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    run_hold.write_hold(
        root / RECORD_DIR_ROOT / HALTED_ID,
        run_hold.HeldRun(HOLDER, "PM turn 41", "the PM rules on the superseded plan", "2026-11-24T00:00:00Z"),
    )
    return root


def _with_args(payload: dict, **extra) -> dict:
    payload["tool_input"]["args"].update(extra)
    return payload


def _assert_held(env: dict) -> None:
    out = _hso(env)
    assert out["permissionDecision"] == "allow" and out["state"] == "held"
    text = out["additionalContext"]
    assert HOLDER in text and "PM turn 41" in text and "the PM rules on the superseded plan" in text
    assert f'args.continue_held: "{HALTED_ID}"' in text
    assert "updatedInput" not in out


def _halted_bytes(root: Path) -> bytes:
    return (root / RECORD_DIR_ROOT / HALTED_ID / "halted.json").read_bytes()


def test_a_non_holder_sizing_refire_gets_the_held_reply_and_nothing_starts(tmp_path: Path, seams) -> None:
    spawned, emitted, emit = seams
    root = _held_repo(tmp_path)
    stage = root / RECORD_DIR_ROOT / HALTED_ID / "run-stage.json"  # the halt marker stamped it `halted`
    before, stage_before = _halted_bytes(root), stage.read_bytes()
    env = mod.evaluate(_payload(root), emit=emit)
    _assert_held(env)
    assert spawned == [] and emitted == []
    assert _halted_bytes(root) == before
    assert stage.read_bytes() == stage_before
    assert (root / RECORD_DIR_ROOT / HALTED_ID / run_hold.HELD_FILE).is_file()


def test_a_non_holder_resume_run_id_gets_the_held_reply(tmp_path: Path, seams) -> None:
    spawned, emitted, emit = seams
    root = _held_repo(tmp_path)
    env = mod.evaluate(_with_args(_payload(root), resume_run_id=HALTED_ID), emit=emit)
    _assert_held(env)
    assert spawned == [] and emitted == []


def test_the_holder_proceeds_as_today(tmp_path: Path, seams) -> None:
    _, emitted, emit = seams
    root = _held_repo(tmp_path)
    payload = _payload(root)
    payload["session_id"] = HOLDER
    env = mod.evaluate(payload, emit=emit)
    assert _hso(env)["updatedInput"]["scriptPath"] == str(root / STORED_REL)
    assert emitted == []


def test_continue_held_proceeds_and_records_the_override(tmp_path: Path, seams) -> None:
    _, emitted, emit = seams
    root = _held_repo(tmp_path)
    env = mod.evaluate(_with_args(_payload(root), continue_held=HALTED_ID), emit=emit)
    assert _hso(env)["updatedInput"]["scriptPath"] == str(root / STORED_REL)
    notes = (root / RECORD_DIR_ROOT / HALTED_ID / "notes.jsonl").read_text(encoding="utf-8")
    note = json.loads(notes.splitlines()[-1])["note"]
    assert "Continued past the PM hold" in note and OTHER in note and HOLDER in note
    assert (root / RECORD_DIR_ROOT / HALTED_ID / run_hold.HELD_FILE).is_file()


def test_a_continue_naming_another_run_is_still_held(tmp_path: Path, seams) -> None:
    spawned, emitted, emit = seams
    root = _held_repo(tmp_path)
    env = mod.evaluate(_with_args(_payload(root), continue_held="warp-other-20261029T010101-abc123"), emit=emit)
    _assert_held(env)
    assert spawned == [] and emitted == []


def test_the_resume_route_answers_held_before_any_launch(tmp_path: Path, seams) -> None:
    spawned, _, _ = seams
    root = _held_repo(tmp_path)

    def launch(*a):
        raise AssertionError("a held run launched")

    env, result = mod._resume_route(root, SIZING_REL, _payload(root), {}, OTHER, launch)
    assert result is None
    _assert_held(env)
    assert spawned == []


def test_a_malformed_hold_does_not_hold(tmp_path: Path, seams) -> None:
    _, _, emit = seams
    root = _repo(tmp_path)
    (root / RECORD_DIR_ROOT / HALTED_ID / run_hold.HELD_FILE).write_text("{not json", encoding="utf-8")
    env = mod.evaluate(_payload(root), emit=emit)
    assert _hso(env)["updatedInput"]["scriptPath"] == str(root / STORED_REL)


def test_continue_held_is_an_accepted_args_key() -> None:
    assert "continue_held" in mod._ARGS_KEYS
