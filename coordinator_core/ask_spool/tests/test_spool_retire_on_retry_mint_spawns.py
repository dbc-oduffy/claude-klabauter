"""A spooled ask whose retry mints is retired as superseded; the drain starts no second run for it."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ask_spool import drain, record
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

repo = real.repo
RUN_ID = "ask-20261113T190259-fe93c3"
ASK = "run the head_pass spike"


@pytest.fixture(autouse=True)
def _isolated_settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))


class CountingEngine:
    """Stands in for the submit hook inside the drain; counts the runs it is asked to start."""

    def __init__(self):
        self.calls = 0

    def __call__(self, payload):
        self.calls += 1
        raise AssertionError("the drain must not mint a retired ask")


def _withdrawn_header(root: Path, spool_id: str) -> dict:
    return record.read_entry(record.entry_path(str(root), spool_id, record.WITHDRAWN_SUFFIX))[0]


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
