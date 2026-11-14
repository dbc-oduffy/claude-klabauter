"""A spooled ask whose retry mints is retired as superseded; the drain starts no second run for it."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ask_spool import drain, record
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real
from coordinator_core.ops.dispatch_emit.requirements_ledger import HandoverPending

repo = real.repo
RUN_ID = "ask-20261113T190259-fe93c3"
ASK = "run the head_pass spike"


@pytest.fixture(autouse=True)
def _isolated_settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))


def _submit(root: Path, tuid: str, emit, text: str = ASK) -> dict:
    payload = real._payload(root, {"ask": text, "source": "pm"})
    payload["tool_use_id"] = tuid
    return mod.evaluate(payload, emit=emit)


def _unavailable(_params, _root):
    raise HandoverPending(5)


def _minting_emit(root_script: Path):
    def emit(_params, _root):
        root_script.write_text("export const meta = {}\n", encoding="utf-8")
        return {"run_id": RUN_ID, "path": str(root_script), "ok": True}

    return emit


class CountingEngine:
    """Stands in for the submit hook inside the drain; counts the runs it is asked to start."""

    def __init__(self):
        self.calls = 0

    def __call__(self, payload):
        self.calls += 1
        raise AssertionError("the drain must not mint a retired ask")


def _withdrawn_header(root: Path, spool_id: str) -> dict:
    return record.read_entry(record.entry_path(str(root), spool_id, record.WITHDRAWN_SUFFIX))[0]


def test_spool_then_retry_mints_then_drain_starts_exactly_one_run(repo):
    _submit(repo, "toolu_first", _unavailable)
    (spool_id,) = record.list_pending(str(repo))

    env = _submit(repo, "toolu_retry", _minting_emit(repo / "retry.workflow.mjs"))

    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    assert record.list_pending(str(repo)) == []
    header = _withdrawn_header(repo, spool_id)
    assert header["superseded_by"] == RUN_ID and RUN_ID in header["withdraw_reason"]
    engine = CountingEngine()
    counts = drain.run(str(repo), evaluate=engine)
    assert engine.calls == 0 and counts["minted"] == 0


def test_a_retry_of_another_ask_leaves_the_pending_entry(repo):
    _submit(repo, "toolu_first", _unavailable)
    _submit(repo, "toolu_retry", _minting_emit(repo / "retry.workflow.mjs"), text="a different ask")
    assert len(record.list_pending(str(repo))) == 1


def test_a_claimed_entry_is_left_to_the_drain_guard(repo):
    _submit(repo, "toolu_first", _unavailable)
    (spool_id,) = record.list_pending(str(repo))
    assert record.claim(str(repo), spool_id)
    _submit(repo, "toolu_retry", _minting_emit(repo / "retry.workflow.mjs"))
    assert Path(record.entry_path(str(repo), spool_id, record.CLAIMED_SUFFIX)).is_file()


def test_a_spool_error_never_fails_the_mint(repo, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("spool unreadable")

    monkeypatch.setattr(record, "supersede_pending", boom)
    env = _submit(repo, "toolu_retry", _minting_emit(repo / "retry.workflow.mjs"))
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env


def _live_token_of_ask(root: Path, run_id: str, text: str, session: str) -> None:
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.extant_work import normalized_ask_hash

    subprocess.run(["git", "init", "-q", str(root)], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    run_token.mint(
        root, run_id, f"scratch/warp/{run_id}.workflow.mjs", session_id=session, marker=True,
        ask_sha256=normalized_ask_hash(text),
    )


def _spool_bare(root: Path, tuid: str, text: str, sid: str) -> str:
    payload = json.dumps(
        {"tool_name": "Workflow", "session_id": sid, "tool_use_id": tuid, "tool_input": {"name": "ask", "args": text}}
    ).encode()
    spool_id, created = record.spool_ask(str(root), payload, session_id=sid, tool_use_id=tuid, reason="x")
    assert created
    return spool_id


def test_drain_retires_an_entry_whose_ask_has_a_run_from_another_session(tmp_path):
    _live_token_of_ask(tmp_path, RUN_ID, ASK, session="other-session")
    spool_id = _spool_bare(tmp_path, "tuA", ASK, "sess1")
    engine = CountingEngine()

    counts = drain.run(str(tmp_path), evaluate=engine)

    assert engine.calls == 0 and counts["withdrawn"] == 1 and counts["minted"] == 0
    assert _withdrawn_header(tmp_path, spool_id)["superseded_by"] == RUN_ID


def test_drain_still_mints_an_entry_whose_ask_has_no_run(tmp_path):
    _live_token_of_ask(tmp_path, RUN_ID, "another ask entirely", session="other-session")
    _spool_bare(tmp_path, "tuA", ASK, "sess1")
    calls = []

    def evaluate(payload):
        calls.append(payload)
        raise RuntimeError("refused")

    counts = drain.run(str(tmp_path), evaluate=evaluate)
    assert len(calls) == 1 and counts["failed"] == 1 and counts["withdrawn"] == 0
