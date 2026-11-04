"""A submit answered "nothing was taken" leaves no run behind once its worker drains; a run minted with no
runner past the grace is named resumable, never "already working"."""

from __future__ import annotations

import contextvars
import json
import threading
import time
from pathlib import Path

import pytest

from coordinator_core import warp_live
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real
from coordinator_core.ops.dispatch_emit import run_token

repo = real.repo
RUN_ID = "ask-20261102T200000-abc123"
SCRIPT = "scratch/warp/x.workflow.mjs"
NOTHING_TAKEN = "nothing was taken"


def _ask(root: Path) -> dict:
    return real._payload(root, {"ask": "build the thing", "source": "relayed"})


def _reason(env: dict) -> str:
    hso = env["hookSpecificOutput"]
    return hso.get("permissionDecisionReason", "") + hso.get("additionalContext", "")


def _delay(monkeypatch, name: str, seconds: float) -> None:
    """Slow the named pre-emit step of `_evaluate` so the answer deadline lands before the emit starts."""
    original = getattr(mod, name)

    def slow(*args, **kwargs):
        time.sleep(seconds)
        return original(*args, **kwargs)

    monkeypatch.setattr(mod, name, slow)


def _phantoms(root: Path) -> list:
    found = [p for p in (root / "state" / "warp").glob("*/run-stage.json")]
    found += list((root / "scratch" / "warp").glob("*.workflow.mjs"))
    found += list(run_token._record_path(root, "ask-20000101T000000-000000").parent.glob("ask-*.json"))
    return found


def test_slow_pre_emit_worker_reaching_the_emit_after_the_answer_mints_nothing(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "1.0")
    hold = threading.Event()
    drained = threading.Event()
    original = mod._premintable

    def blocked(*args, **kwargs):
        hold.wait(30)
        return original(*args, **kwargs)

    real_emit_within = mod._emit_within_budget

    def watched(*args, **kwargs):
        try:
            return real_emit_within(*args, **kwargs)
        finally:
            drained.set()

    monkeypatch.setattr(mod, "_premintable", blocked)
    monkeypatch.setattr(mod, "_emit_within_budget", watched)
    try:
        env = mod.evaluate(_ask(repo))
    finally:
        hold.set()
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny", env
    assert NOTHING_TAKEN in _reason(env)
    assert drained.wait(10)
    assert _phantoms(repo) == []


def _emit_blocking_then_real(release: threading.Event, done: threading.Event, seen: list, mint_first: bool):
    def emit(params: dict, root: Path) -> dict:
        try:
            if mint_first:
                run_token.mint(root, RUN_ID, SCRIPT, session_id=params.get("session_id") or None)  # as the real emit does
            release.wait(30)
            return mod._default_emit(params, root)
        except BaseException as exc:
            seen.append(exc)
            raise
        finally:
            done.set()

    return emit


def test_slow_emit_answered_nothing_taken_publishes_no_record_when_it_resumes(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "1.0")
    _delay(monkeypatch, "_call_preference", 0.7)  # the emit join falls past the answer deadline
    release, done, seen = threading.Event(), threading.Event(), []
    try:
        env = mod.evaluate(_ask(repo), emit=_emit_blocking_then_real(release, done, seen, mint_first=False))
    finally:
        release.set()
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny", env
    assert NOTHING_TAKEN in _reason(env)
    assert done.wait(10)
    assert [type(e) for e in seen] == [run_token.RecordWriteCancelled]
    assert _phantoms(repo) == []


def test_cancelled_gate_aborts_the_real_emit_before_it_writes_anything(repo):
    gate = run_token.WriteGate()
    assert gate.cancel() == "idle"

    def attempt() -> None:
        run_token.WRITE_GATE.set(gate)
        mod._default_emit({"ask": "build the thing", "ask_source": "relayed", "session_id": ""}, repo)

    with pytest.raises(run_token.RecordWriteCancelled):
        contextvars.copy_context().run(attempt)
    assert _phantoms(repo) == []
    assert not list((repo / "scratch" / "warp").glob("*")) if (repo / "scratch" / "warp").exists() else True


def test_an_emit_started_after_the_answer_never_runs(repo):
    ran: list = []
    call = {"t0": time.monotonic(), "wall0": time.time(), "roots": [repo], "minted": [], "gate": run_token.WriteGate(), "gates": []}
    mod._cancel_call_gates(call)
    token = mod._CALL.set(call)
    try:
        with pytest.raises(mod._EmitOverran) as caught:
            mod._emit_within_budget(lambda p, r: ran.append(1) or {}, {}, repo, 5.0)
    finally:
        mod._CALL.reset(token)
    assert ran == [] and not caught.value.minted and NOTHING_TAKEN in caught.value.message


def test_a_later_emit_of_the_call_gets_its_own_gate_that_the_answer_also_cancels(repo):
    call = {"t0": time.monotonic(), "wall0": time.time(), "roots": [repo], "minted": [], "gate": run_token.WriteGate(), "gates": []}
    token = mod._CALL.set(call)
    try:
        assert mod._emit_within_budget(lambda p, r: {"ok": 1}, {}, repo, 5.0) == {"ok": 1}
        assert mod._emit_within_budget(lambda p, r: {"ok": 2}, {}, repo, 5.0) == {"ok": 2}
    finally:
        mod._CALL.reset(token)
    assert len(call["gates"]) == 1
    assert mod._cancel_call_gates(call) == "idle"
    assert call["gates"][0].begin() is False


def test_committed_token_at_the_answer_names_the_run_and_never_says_nothing_taken(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "1.0")
    _delay(monkeypatch, "_call_preference", 0.7)
    release, done, seen = threading.Event(), threading.Event(), []
    try:
        env = mod.evaluate(_ask(repo), emit=_emit_blocking_then_real(release, done, seen, mint_first=True))
    finally:
        release.set()
    text = _reason(env)
    assert RUN_ID in text and "Do not re-submit" in text and NOTHING_TAKEN not in text, env
    assert done.wait(10)


SIZING = real.REL


@pytest.fixture
def launch_dirs(tmp_path, monkeypatch):
    managed, legacy = tmp_path / "managed", tmp_path / "legacy"
    managed.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: legacy / name)


def _stranded_token(root: Path, age_s: float) -> None:
    run_token.mint(root, RUN_ID, SCRIPT, session_id="sess-real-emit", sizing_rel=SIZING)
    warp_live.record_submission(
        root, RUN_ID, session_id="sess-real-emit", transcript_path=None, script_path=SCRIPT, sizing_rel=SIZING, tool_use_id=None
    )
    path = run_token._record_path(root, RUN_ID)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["minted_at"] = time.time() - age_s
    path.write_text(json.dumps(record), encoding="utf-8")


def test_a_sizing_whose_run_has_no_runner_past_the_grace_is_named_resumable(repo, launch_dirs):
    real._put_sizing(repo)
    _stranded_token(repo, warp_live.MINTED_RUNNER_GRACE_SECONDS + 60)
    env = mod.evaluate(real._payload(repo, {"sizing": SIZING}))
    text = _reason(env)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    assert RUN_ID in text and mod._MINTED_OP in text and "already working" not in text


def test_a_sizing_whose_minted_run_is_inside_the_grace_is_not_named_resumable(repo, launch_dirs):
    real._put_sizing(repo)
    _stranded_token(repo, 5)
    env = mod.evaluate(real._payload(repo, {"sizing": SIZING}))
    assert mod._MINTED_OP not in _reason(env) or RUN_ID not in _reason(env), env
