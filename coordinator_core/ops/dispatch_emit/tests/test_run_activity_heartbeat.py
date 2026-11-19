"""touch_run_activity throttles a last_activity_at heartbeat; a run-token admit drives it."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HEARTBEAT_MIN_S,
    RUN_STAGE_FILE,
    record_dir,
    touch_run_activity,
)
from coordinator_core.session import record_homes
from pathlib import Path
import pytest

T0 = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)


def _stage(d, stage: str = "staged", at: datetime = T0) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / RUN_STAGE_FILE).write_text(
        json.dumps({"stage": stage, "phase": "executing", "last_activity_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "session_id": "s"}),
        encoding="utf-8",
    )


def _read(d):
    return json.loads((d / RUN_STAGE_FILE).read_text(encoding="utf-8"))


def test_advances_stamp_and_leaves_stage_and_other_keys(tmp_path: Path) -> None:
    _stage(tmp_path)
    later = T0 + timedelta(seconds=HEARTBEAT_MIN_S + 1)
    assert touch_run_activity(tmp_path, now=later) is True
    data = _read(tmp_path)
    assert data["last_activity_at"] == later.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert data["stage"] == "staged" and data["session_id"] == "s"


def test_a_second_touch_inside_the_throttle_writes_nothing(tmp_path: Path) -> None:
    _stage(tmp_path)
    assert touch_run_activity(tmp_path, now=T0 + timedelta(seconds=HEARTBEAT_MIN_S + 1)) is True
    before = (tmp_path / RUN_STAGE_FILE).read_bytes()
    assert touch_run_activity(tmp_path, now=T0 + timedelta(seconds=HEARTBEAT_MIN_S + 5)) is False
    assert (tmp_path / RUN_STAGE_FILE).read_bytes() == before


def test_young_stamp_is_refused_and_old_one_accepted(tmp_path: Path) -> None:
    _stage(tmp_path)
    assert touch_run_activity(tmp_path, now=T0 + timedelta(seconds=HEARTBEAT_MIN_S - 1)) is False
    assert touch_run_activity(tmp_path, now=T0 + timedelta(seconds=HEARTBEAT_MIN_S)) is True


def test_terminal_and_absent_stage_are_not_touched(tmp_path: Path) -> None:
    assert touch_run_activity(tmp_path / "nope", now=T0 + timedelta(hours=1)) is False
    for stage in ("halted", "landed", "complete"):
        _stage(tmp_path, stage)
        before = (tmp_path / RUN_STAGE_FILE).read_bytes()
        assert touch_run_activity(tmp_path, now=T0 + timedelta(hours=1)) is False
        assert (tmp_path / RUN_STAGE_FILE).read_bytes() == before


def test_a_token_admit_stamps_the_run_and_a_refused_admit_does_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id, token, script = "ask-20261105T000000-abcdef", "t" * 32, "/s/script.mjs"
    monkeypatch.setattr(run_token, "_read_rec", lambda root, rid, **k: {"run_id": rid, "script_path": script})
    monkeypatch.setattr(run_token, "_matches", lambda rec, rid, tok, sp, *a: tok == token)
    monkeypatch.setattr(run_token, "TOKEN_RE", __import__("re").compile(r".+"))
    d = tmp_path / "w" / run_id
    monkeypatch.setattr(record_homes, "home_dir", lambda root, kind: str(tmp_path / "w"))
    _stage(d, at=T0)

    assert run_token.admits(tmp_path, run_id, "wrong", script) is False
    assert _read(d)["last_activity_at"] == T0.strftime("%Y-%m-%dT%H:%M:%SZ")

    assert run_token.admits(tmp_path, run_id, token, script) is True
    assert _read(d)["last_activity_at"] != T0.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _read(d)["stage"] == "staged"


RUN = "ask-20261105T000000-abcdef"
FORTY_MIN_AGO = (datetime.now(timezone.utc) - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Reached(Exception):
    """Raised past the heartbeat call site to stop the op after it."""


def _minted(repo: Path, at: str) -> Path:
    d = record_dir(repo, RUN)
    d.mkdir(parents=True, exist_ok=True)
    (d / RUN_STAGE_FILE).write_text(
        json.dumps({"stage": "minted", "phase": "plan-author", "plan_started_at": at, "last_activity_at": at, "session_id": "s"}),
        encoding="utf-8",
    )
    return d


def _gate_up_to_the_heartbeat(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops.dispatch_emit import ask_gate, requirements_ledger

    def stop(*_a, **_k):
        raise _Reached

    monkeypatch.setattr(ask_gate, "main_worktree_root", lambda p: p)
    monkeypatch.setattr(ask_gate, "_clear_resume_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(ask_gate, "_clear_held_lock", lambda *a, **k: None)
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)
    monkeypatch.setattr(ask_gate, "_credit_split_predecessors", stop)
    monkeypatch.setattr(requirements_ledger, "_sizing_path", lambda root, rel: root / rel)


def test_a_second_ask_gate_call_40_min_into_plan_phase_advances_last_activity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops.dispatch_emit import ask_gate

    d = _minted(tmp_path, FORTY_MIN_AGO)
    _gate_up_to_the_heartbeat(tmp_path, monkeypatch)
    with pytest.raises(_Reached):
        ask_gate._handler({"sizing_path": "state/sizings/x.yaml", "run_id": RUN}, repo_root=tmp_path)
    data = _read(d)
    assert data["last_activity_at"] > FORTY_MIN_AGO and data["stage"] == "minted"
    assert data["plan_started_at"] == FORTY_MIN_AGO


def test_an_ask_gate_call_without_a_run_id_or_with_a_refused_bind_touches_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops.dispatch_emit import ask_gate

    d = _minted(tmp_path, FORTY_MIN_AGO)
    _gate_up_to_the_heartbeat(tmp_path, monkeypatch)
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: "refused")
    ask_gate._handler({"sizing_path": "state/sizings/x.yaml", "run_id": RUN}, repo_root=tmp_path)
    assert _read(d)["last_activity_at"] == FORTY_MIN_AGO


def test_a_second_ask_stage_call_40_min_into_plan_phase_advances_last_activity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops.dispatch_emit import ask_stage

    d = _minted(tmp_path, FORTY_MIN_AGO)
    monkeypatch.setattr(ask_stage, "main_worktree_root", lambda p: p)
    monkeypatch.setattr(ask_stage, "_escaping_write_refusal", lambda *a, **k: (_ for _ in ()).throw(_Reached()))
    with pytest.raises(_Reached):
        ask_stage._run_ask_stage({"run_id": RUN, "writes": []}, tmp_path)
    data = _read(d)
    assert data["last_activity_at"] > FORTY_MIN_AGO and data["stage"] == "minted"


def test_an_ask_stage_call_inside_the_throttle_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops.dispatch_emit import ask_stage

    young = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    d = _minted(tmp_path, young)
    before = (d / RUN_STAGE_FILE).read_bytes()
    monkeypatch.setattr(ask_stage, "main_worktree_root", lambda p: p)
    monkeypatch.setattr(ask_stage, "_escaping_write_refusal", lambda *a, **k: (_ for _ in ()).throw(_Reached()))
    with pytest.raises(_Reached):
        ask_stage._run_ask_stage({"run_id": RUN, "writes": []}, tmp_path)
    assert (d / RUN_STAGE_FILE).read_bytes() == before


def test_an_admit_still_succeeds_when_the_heartbeat_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id, token, script = "ask-20261105T000000-abcdef", "t" * 32, "/s/script.mjs"
    monkeypatch.setattr(run_token, "_read_rec", lambda root, rid, **k: {"run_id": rid, "script_path": script})
    monkeypatch.setattr(run_token, "_matches", lambda rec, rid, tok, sp, *a: tok == token)
    monkeypatch.setattr(run_token, "TOKEN_RE", __import__("re").compile(r".+"))

    def boom(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(record_homes, "home_dir", boom)
    assert run_token.admits(tmp_path, run_id, token, script) is True
    assert run_token.admits(tmp_path, run_id, "wrong", script) is False
