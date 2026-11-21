"""An ask the engine cannot mint now (HandoverPending, -32004, reach budget with no run) is spooled, not refused."""

from __future__ import annotations

import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.ask_spool import record
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.requirements_ledger import HandoverPending

repo = real.repo


def _ask(root: Path, verbatim: str = "build the thing") -> dict:
    payload = real._payload(root, {"ask": verbatim, "source": "pm"})
    payload["tool_use_id"] = "toolu_spool_1"
    return payload


def _spooled(root: Path) -> list:
    return record.list_pending(str(root))


def _assert_spooled_allow(env: dict, root: Path) -> str:
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", env
    (spool_id,) = _spooled(root)
    assert f"SPOOLED as {spool_id}" in hso["additionalContext"], env
    assert hso["updatedInput"]["scriptPath"].endswith(f"spooled-{spool_id}.workflow.mjs")
    return spool_id


@pytest.mark.deliberate_wall_clock(reason="bounds the hook window so a handover-pending emit spools instead of waiting")
def test_handover_pending_raised_by_the_emit_spools_a_0600_entry_holding_the_ask(repo):
    def emit(_p, _r):
        raise HandoverPending(5)

    t0 = time.monotonic()
    env = mod.evaluate(_ask(repo), emit=emit)
    elapsed = time.monotonic() - t0
    spool_id = _assert_spooled_allow(env, repo)
    path = record.entry_path(str(repo), spool_id)
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    header, payload = record.read_entry(path)
    assert header["reason"] == "handover-pending" and header["session_id"] == "sess-real-emit"
    assert b"build the thing" in payload
    assert elapsed < 1.0
    assert not list((repo / "state" / "warp").glob("*/run-stage.json"))


def test_handover_pending_reply_with_no_run_spools(repo):
    env = mod.evaluate(_ask(repo), emit=lambda _p, _r: HandoverPending(3).reply)
    _assert_spooled_allow(env, repo)


def test_dispatch_timeout_32004_with_no_minted_run_spools(repo):
    class Indeterminate(Exception):
        code = -32004

    def emit(_p, _r):
        raise Indeterminate("warm dispatch indeterminate")

    env = mod.evaluate(_ask(repo), emit=emit)
    spool_id = _assert_spooled_allow(env, repo)
    header, _ = record.read_entry(record.entry_path(str(repo), spool_id))
    assert header["reason"] == "dispatch-timeout"


def test_the_same_ask_resubmitted_spools_once(repo):
    def emit(_p, _r):
        raise HandoverPending(5)

    first = mod.evaluate(_ask(repo), emit=emit)
    second = mod.evaluate(_ask(repo), emit=emit)
    assert len(_spooled(repo)) == 1
    assert first["hookSpecificOutput"]["permissionDecision"] == second["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_ordinary_emit_refusal_is_still_denied_and_not_spooled(repo):
    def emit(_p, _r):
        raise RuntimeError("bad sizing")

    env = mod.evaluate(_ask(repo), emit=emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert _spooled(repo) == []


def test_32004_after_a_run_was_minted_is_not_spooled(repo):
    class Indeterminate(Exception):
        code = -32004

    def emit(_p, root):
        mod._note_minted("ask-20261104T000000-aaaaaa")
        raise Indeterminate("indeterminate")

    env = mod.evaluate(_ask(repo), emit=emit)
    assert _spooled(repo) == []
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_reach_budget_with_no_run_minted_spools_and_the_late_emit_mints_nothing(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "1.0")
    release, done = threading.Event(), threading.Event()

    def emit(params, root):
        try:
            release.wait(30)
            return mod._default_emit(params, root)
        finally:
            done.set()

    original = mod._call_preference

    def slow(*args, **kwargs):
        time.sleep(0.7)
        return original(*args, **kwargs)

    monkeypatch.setattr(mod, "_call_preference", slow)
    try:
        env = mod.evaluate(_ask(repo), emit=emit)
    finally:
        release.set()
    _assert_spooled_allow(env, repo)
    assert done.wait(10)
    assert not list((repo / "state" / "warp").glob("*/run-stage.json"))


def test_refire_of_a_halted_run_that_outruns_the_budget_is_refused_not_spooled(tmp_path, monkeypatch):
    # A spool of a refire drains into a second launch of the run the retry already resumes.
    sizing_rel = "state/sizings/2026-10-29-resume-fixture.yaml"
    plan_rel = "docs/plans/2026-10-29-resume-fixture.md"
    halted_id = "warp-2026-10-29-resume-fixture-20261029T010101-abc123"
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "1.0")
    root = tmp_path
    (root / ".git").mkdir()
    for rel, text in ((sizing_rel, "schema: sizing-object\n"), (plan_rel, "# plan\n")):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    write_halt_marker(
        root / RECORD_DIR_ROOT / halted_id, halted="ledger-check", sizing_rel=sizing_rel,
        plan_rel=plan_rel, run_base_sha="a" * 40,
    )
    payload = {
        "tool_name": "Workflow", "cwd": str(root), "session_id": "s1", "tool_use_id": "toolu_x",
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": {"sizing": sizing_rel}},
    }
    release = threading.Event()

    def stalled_spawn(_root, _run_id):
        release.wait(30)
        return True

    monkeypatch.setattr(mod, "_spawn_recompose", stalled_spawn)
    try:
        env = mod.evaluate(payload, emit=lambda _p, _r: {})
    finally:
        release.set()
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny", env
    assert halted_id in hso["permissionDecisionReason"]
    assert "SPOOLED as" not in hso["permissionDecisionReason"]
    assert _spooled(root) == []


def test_unspoolable_disk_keeps_the_original_refusal(repo, monkeypatch):
    def emit(_p, _r):
        raise HandoverPending(5)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(record, "spool_ask", boom)
    env = mod.evaluate(_ask(repo), emit=emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_unwritable_placeholder_script_denies_but_names_the_spooled_entry(repo, monkeypatch):
    def emit(_p, _r):
        raise HandoverPending(5)

    real_write = Path.write_bytes

    def write_bytes(self, *a, **k):
        if self.name.startswith("spooled-"):
            raise OSError("read-only")
        return real_write(self, *a, **k)

    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    env = mod.evaluate(_ask(repo), emit=emit)
    (spool_id,) = _spooled(repo)
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny", env
    assert f"SPOOLED as {spool_id}" in hso["permissionDecisionReason"]
    assert f"coordinator-invoke ask_spool.withdraw '{{\"spool_id\":\"{spool_id}\"" in hso["permissionDecisionReason"]


def test_allow_envelope_names_the_withdraw_verb_with_the_spool_id(repo):
    def emit(_p, _r):
        raise HandoverPending(5)

    env = mod.evaluate(_ask(repo), emit=emit)
    spool_id = _assert_spooled_allow(env, repo)
    context = env["hookSpecificOutput"]["additionalContext"]
    assert f"coordinator-invoke ask_spool.withdraw '{{\"spool_id\":\"{spool_id}\",\"reason\":\"...\"}}'." in context


def _handover(_p, _r):
    raise HandoverPending(5)


def test_spooled_reply_leads_with_the_state_and_forbids_a_resubmit(repo):
    env = mod.evaluate(_ask(repo), emit=_handover)
    spool_id = _assert_spooled_allow(env, repo)
    context = env["hookSpecificOutput"]["additionalContext"]
    assert context.removeprefix("[coordinator] ").startswith(
        f"SPOOLED as {spool_id}: it fires automatically once the engine is warm. Do not resubmit."
    )
    assert "unavailable" not in context.lower()


def test_an_identical_resubmit_spools_no_second_entry_and_names_the_first(repo):
    first = mod.evaluate(_ask(repo), emit=_handover)
    spool_id = _assert_spooled_allow(first, repo)
    again = _ask(repo, "  Build   the THING ")
    again["tool_use_id"] = "toolu_spool_2"
    second = mod.evaluate(again, emit=_handover)
    assert _spooled(repo) == [spool_id]
    assert second["hookSpecificOutput"]["additionalContext"].removeprefix("[coordinator] ").startswith(f"SPOOLED as {spool_id}: ")


def test_a_distinct_ask_spools_its_own_entry(repo):
    mod.evaluate(_ask(repo), emit=_handover)
    other = _ask(repo, "build another thing")
    other["tool_use_id"] = "toolu_spool_3"
    env = mod.evaluate(other, emit=_handover)
    ids = _spooled(repo)
    assert len(ids) == 2
    named = [i for i in ids if f"SPOOLED as {i}" in env["hookSpecificOutput"]["additionalContext"]]
    assert len(named) == 1
