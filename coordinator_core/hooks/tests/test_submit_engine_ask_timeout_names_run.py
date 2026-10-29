"""A submit whose emit outlives the caller's hook budget answers on its own: it names a run already
minted (so the caller never re-submits) and says nothing was taken when no run was minted."""

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


def test_overrun_before_mint_names_no_run(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "2.5")
    release = threading.Event()
    try:
        env = mod.evaluate(_payload(repo), emit=_slow_emit([], False, release))
    finally:
        release.set()
    reason = _deny_reason(env)
    assert RUN_ID not in reason and "nothing was taken" in reason


def test_in_budget_emit_still_allows(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "2.5")
    script = repo / "run.js"
    script.write_text("//", encoding="utf-8")
    env = mod.evaluate(_payload(repo), emit=lambda p, r: {"path": str(script), "run_id": RUN_ID})
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
