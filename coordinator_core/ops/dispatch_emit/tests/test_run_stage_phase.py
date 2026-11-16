"""The additive run-stage `phase`: set by an admit heartbeat and the stamp helper, never on a terminal record."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RUN_PHASES,
    RUN_STAGE_FILE,
    RUN_STAGES,
    stamp_plan_phase_at,
    stamp_run_phase_at,
    touch_run_activity,
)
from coordinator_core.session import record_homes
from pathlib import Path

T0 = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
RUN_ID, TOKEN, SCRIPT = "ask-20261105T000000-abcdef", "t" * 32, "/s/script.mjs"


def _stage(d, stage: str = "minted", **extra) -> None:
    d.mkdir(parents=True, exist_ok=True)
    rec = {"stage": stage, "last_activity_at": T0.strftime("%Y-%m-%dT%H:%M:%SZ"), "session_id": "s", **extra}
    (d / RUN_STAGE_FILE).write_text(json.dumps(rec), encoding="utf-8")


def _read(d):
    return json.loads((d / RUN_STAGE_FILE).read_text(encoding="utf-8"))


def test_first_touch_sets_executing_even_inside_the_throttle(tmp_path: Path) -> None:
    _stage(tmp_path)
    assert touch_run_activity(tmp_path, now=T0 + timedelta(seconds=1)) is True
    data = _read(tmp_path)
    assert data["phase"] == "executing" and data["stage"] == "minted"


def test_a_touch_never_overwrites_an_existing_phase(tmp_path: Path) -> None:
    _stage(tmp_path, phase="review")
    assert touch_run_activity(tmp_path, now=T0 + timedelta(hours=1)) is True
    assert _read(tmp_path)["phase"] == "review"
    _stage(tmp_path, phase="review")
    assert touch_run_activity(tmp_path, now=T0 + timedelta(seconds=1)) is False


def test_stamp_sets_phase_leaves_stage_and_is_idempotent(tmp_path: Path) -> None:
    _stage(tmp_path, "staged")
    assert stamp_run_phase_at(tmp_path, "plan-author", now="2026-11-05T12:05:00Z") is True
    data = _read(tmp_path)
    assert data["phase"] == "plan-author" and data["stage"] == "staged"
    assert data["last_activity_at"] == "2026-11-05T12:05:00Z"
    before = (tmp_path / RUN_STAGE_FILE).read_bytes()
    assert stamp_run_phase_at(tmp_path, "plan-author") is False
    assert (tmp_path / RUN_STAGE_FILE).read_bytes() == before
    assert stamp_run_phase_at(tmp_path, "review") is True and _read(tmp_path)["phase"] == "review"


@pytest.mark.parametrize("stage", ["halted", "landed", "complete"])
def test_a_terminal_record_is_never_rephased(tmp_path: Path, stage) -> None:
    _stage(tmp_path, stage)
    before = (tmp_path / RUN_STAGE_FILE).read_bytes()
    assert stamp_run_phase_at(tmp_path, "executing") is False
    assert touch_run_activity(tmp_path, now=T0 + timedelta(hours=1)) is False
    assert (tmp_path / RUN_STAGE_FILE).read_bytes() == before


def test_unknown_phase_absent_and_malformed_records_are_refused(tmp_path: Path) -> None:
    _stage(tmp_path)
    assert stamp_run_phase_at(tmp_path, "bogus") is False
    assert "phase" not in _read(tmp_path)
    assert stamp_run_phase_at(tmp_path / "nope", "review") is False
    (tmp_path / RUN_STAGE_FILE).write_text("{not json", encoding="utf-8")
    assert stamp_run_phase_at(tmp_path, "review") is False


def test_plan_stamp_keeps_stage_minted_and_stage_stays_in_enum(tmp_path: Path) -> None:
    _stage(tmp_path)
    assert stamp_plan_phase_at(tmp_path) is True
    assert _read(tmp_path)["stage"] == "minted"
    for phase in RUN_PHASES:
        stamp_run_phase_at(tmp_path, phase)
        assert _read(tmp_path)["stage"] in RUN_STAGES
    assert not set(RUN_PHASES) & set(RUN_STAGES)


def test_an_admit_sets_executing_without_a_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_token, "_read_rec", lambda root, rid, **k: {"run_id": rid, "script_path": SCRIPT})
    monkeypatch.setattr(run_token, "_matches", lambda rec, rid, tok, sp, *a: tok == TOKEN)
    monkeypatch.setattr(run_token, "TOKEN_RE", __import__("re").compile(r".+"))
    d = tmp_path / "w" / RUN_ID
    monkeypatch.setattr(record_homes, "home_dir", lambda root, kind: str(tmp_path / "w"))
    _stage(d)
    spawns = []

    def no_spawn(*a, **k):
        spawns.append(a)
        raise AssertionError("spawn on the admit path")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    monkeypatch.setattr(subprocess, "run", no_spawn)

    assert run_token.admits(tmp_path, RUN_ID, "wrong", SCRIPT) is False
    assert "phase" not in _read(d)
    assert run_token.admits(tmp_path, RUN_ID, TOKEN, SCRIPT) is True
    data = _read(d)
    assert data["phase"] == "executing" and data["stage"] == "minted"
    assert spawns == []
