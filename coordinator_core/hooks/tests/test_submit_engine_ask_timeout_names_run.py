"""A submit whose emit outlives the caller's hook budget answers on its own: it names a run already
minted (so the caller never re-submits) and is taken as a deferred emit when no run was minted."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_STAGE_FILE

repo = real.repo
RUN_ID = "ask-20261029T200000-e3414a"


def _payload(root: Path) -> dict:
    return real._payload(root, {"ask": "build the thing", "source": "relayed"})


def _slow_emit(root_seen: list, mint: bool, release: threading.Event):
    def emit(params: dict, root: Path) -> dict:
        if mint:
            d = root / RECORD_DIR_ROOT / RUN_ID
            d.mkdir(parents=True)
            (d / RUN_STAGE_FILE).write_text('{"stage": "minted"}', encoding="utf-8")
        root_seen.append(root)
        release.wait(30)
        return {"path": str(root / "never.js"), "run_id": RUN_ID}

    return emit


def _deny_reason(env: dict) -> str:
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny", env
    return hso["permissionDecisionReason"]


def test_overrun_after_mint_names_the_run(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "2.5")
    release = threading.Event()
    try:
        started = time.monotonic()
        env = mod.evaluate(_payload(repo), emit=_slow_emit([], True, release))
        assert time.monotonic() - started < 2.5
    finally:
        release.set()
    reason = _deny_reason(env)
    assert RUN_ID in reason and "Do not re-submit" in reason and "not taken" not in reason


def test_overrun_before_mint_is_taken_as_a_deferred_emit(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "2.5")
    release = threading.Event()
    try:
        env = mod.evaluate(_payload(repo), emit=_slow_emit([], False, release))
    finally:
        release.set()
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", env
    assert RUN_ID not in str(env) and "Do not re-submit" in hso["additionalContext"]
    assert Path(hso["updatedInput"]["scriptPath"]).parent == repo / "scratch" / "warp"


def _asks_payload(root: Path) -> dict:
    asks = [{"ask": f"job {i}", "label": f"job-{i}", "source": "relayed", "writes": [f"pkg/m{i}.py"]} for i in range(3)]
    return real._payload(root, {"asks": asks})


def _leftovers(root: Path) -> list:
    return sorted(p.name for p in (root / "state" / "sizings").glob("*.yaml")) + sorted(
        p.name for p in (root / "scratch" / "warp").glob("ask-parent-*") if p.is_dir()
    )


def test_fanout_outrunning_the_hook_budget_leaves_no_sizing_or_parent(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_fanout

    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "2.5")
    real_stage = ask_fanout._stage_text

    def slow_stage(target, text):
        real_stage(target, text)
        time.sleep(1.0)

    monkeypatch.setattr(ask_fanout, "_stage_text", slow_stage)
    env = mod.evaluate(_asks_payload(repo))
    assert "not taken" in _deny_reason(env)
    # At the deny moment, not after the worker's next poll: the process may exit with the worker mid-write.
    assert _leftovers(repo) == []
    deadline = time.monotonic() + 8
    while _leftovers(repo) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _leftovers(repo) == []


def _fanout_finishing_after_deadline(finish_s: float):
    def fake(params, root, abort=None, ledger=None):
        ledger.commit()  # past the last abort poll: sealed, cannot roll back
        while not abort():
            time.sleep(0.01)
        time.sleep(finish_s)
        return {"applied": True, "parent_id": "p"}

    return fake


def test_fanout_finishing_in_the_tail_after_the_deadline_is_taken(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_fanout

    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "1")
    monkeypatch.setattr(ask_fanout, "fanout", _fanout_finishing_after_deadline(0.05))
    assert mod._default_fanout({}, repo)["applied"] is True


def test_fanout_still_running_past_the_tail_grace_is_denied(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_fanout

    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "1")
    monkeypatch.setattr(ask_fanout, "fanout", _fanout_finishing_after_deadline(mod._FANOUT_TAIL_GRACE_S + 1.0))
    try:
        mod._default_fanout({}, repo)
    except RuntimeError as exc:
        assert str(exc) == ask_fanout.FANOUT_ABORTED
    else:
        raise AssertionError("expected a denial")


def test_fanout_within_the_hook_budget_still_writes_every_sizing(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "12")
    mod.evaluate(_asks_payload(repo))
    assert len(list((repo / "state" / "sizings").glob("*.yaml"))) == 3
    assert len([p for p in (repo / "scratch" / "warp").glob("ask-parent-*") if p.is_dir()]) == 1


def test_in_budget_emit_still_allows(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "2.5")
    script = repo / "run.js"
    script.write_text("//", encoding="utf-8")
    env = mod.evaluate(_payload(repo), emit=lambda p, r: {"path": str(script), "run_id": RUN_ID})
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env


def test_stall_before_the_emit_and_a_slow_emit_together_still_name_the_run(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "2.5")
    real_missing = mod._missing_context

    def slow_missing(root, paths):
        time.sleep(1.2)
        return real_missing(root, paths)

    monkeypatch.setattr(mod, "_missing_context", slow_missing)
    release = threading.Event()
    try:
        started = time.monotonic()
        env = mod.evaluate(_payload(repo), emit=_slow_emit([], True, release))
        assert time.monotonic() - started < 2.5
    finally:
        release.set()
    reason = _deny_reason(env)
    assert RUN_ID in reason and "Do not re-submit" in reason and "not taken" not in reason


def test_stall_after_the_emit_returns_names_the_run(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "2.5")
    release = threading.Event()
    script = repo / "run.js"
    script.write_text("//", encoding="utf-8")

    def stuck_research(root, ask, sizing_rel):
        release.wait(30)
        return []

    monkeypatch.setattr(mod, "_existing_research", stuck_research)
    try:
        started = time.monotonic()
        env = mod.evaluate(_payload(repo), emit=lambda p, r: {"path": str(script), "run_id": RUN_ID})
        assert time.monotonic() - started < 2.5
    finally:
        release.set()
    reason = _deny_reason(env)
    assert RUN_ID in reason and "Do not re-submit" in reason


def test_stall_before_any_mint_answers_inside_the_budget_and_says_nothing_was_taken(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "2.5")
    release = threading.Event()
    monkeypatch.setattr(mod, "_missing_context", lambda root, paths: release.wait(30))
    try:
        started = time.monotonic()
        env = mod.evaluate(_payload(repo))
        assert time.monotonic() - started < 2.5
    finally:
        release.set()
    assert "before any run was minted" in _deny_reason(env)


def test_fanout_blocked_inside_a_write_leaves_no_sizing_at_the_deny(repo, monkeypatch):
    """A writer stuck in os.write holds its file open (Windows refuses the unlink); no record home may show it."""
    import os

    from coordinator_core.ops.dispatch_emit import ask_fanout

    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "2.5")
    release = threading.Event()
    real_write = os.write

    def blocked_write(fd, data):
        release.wait(8)
        return real_write(fd, data)

    monkeypatch.setattr(ask_fanout.os, "write", blocked_write)
    try:
        env = mod.evaluate(_asks_payload(repo))
        assert "not taken" in _deny_reason(env)
        assert _leftovers(repo) == []
        assert not list((repo / "scratch" / "warp").glob("ask-parent-*"))
    finally:
        release.set()
    deadline = time.monotonic() + 8
    while any((repo / ".coordinator-local" / "fanout-staging").rglob("*.yaml")) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _leftovers(repo) == []
