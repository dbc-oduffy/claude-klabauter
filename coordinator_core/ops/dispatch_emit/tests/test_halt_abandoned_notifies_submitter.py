"""A run stale at `minted` or `staged` is halted and its submitting session (the `session_id` in
`run-stage.json`) receives one halt notice naming the run; a run with no recorded session is halted
without a notice."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.ask_spool import notice
from coordinator_core.ops import warp_run_deliverable
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir, write_run_stage_at
from coordinator_core.session import core as session_core

SELF = "5e1f0000-0000-4000-8000-0000000000a1"
PEER = "5e1f0000-0000-4000-8000-0000000000b2"
RUN = "run-peer"


def _stamp(age_seconds: float) -> str:
    return datetime.fromtimestamp(time.time() - age_seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    session_core.init(SELF, cwd=str(root))
    session_core.init(PEER, cwd=str(root))
    return root


def _run(repo: Path, *, stage: str, age: float, session_id: str | None) -> Path:
    peer = record_dir(repo, RUN)
    peer.mkdir(parents=True)
    (peer / "manifest.json").write_text(json.dumps({"rows": [], "doc_writes": []}), encoding="utf-8")
    run_token.mint(repo, RUN, "peer.mjs", session_id=PEER)
    write_run_stage_at(peer, stage, now=_stamp(age), session_id=session_id)
    when = time.time() - age
    for entry in peer.iterdir():
        os.utime(entry, (when, when))
    return peer


@pytest.mark.parametrize(
    "stage,bound,halted",
    [
        ("minted", warp_run_deliverable.MINTED_STALE_SECONDS, warp_run_deliverable.ABANDONED_AT_MINTED),
        ("staged", warp_run_deliverable.STAGED_STALE_SECONDS, warp_run_deliverable.ABANDONED_AT_STAGED),
    ],
)
def test_a_stale_run_notifies_its_submitting_session(repo, stage, bound, halted):
    peer = _run(repo, stage=stage, age=bound + 600, session_id=PEER)
    assert warp_run_deliverable._live_peer_tail(repo, RUN, PEER, str(repo)) is False
    marker = json.loads((peer / "halted.json").read_text(encoding="utf-8"))
    assert marker["halted"] == halted
    text = notice.advisory_text(str(repo), PEER)
    assert len(text.splitlines()) == 1
    assert RUN in text


def test_a_run_with_no_session_is_halted_without_a_notice(repo):
    peer = _run(
        repo, stage="minted", age=warp_run_deliverable.MINTED_STALE_SECONDS + 600, session_id=None,
    )
    stage_file = peer / "run-stage.json"
    data = json.loads(stage_file.read_text(encoding="utf-8"))
    data["session_id"] = None
    stage_file.write_text(json.dumps(data), encoding="utf-8")
    assert warp_run_deliverable._live_peer_tail(repo, RUN, PEER, str(repo)) is False
    assert (peer / "halted.json").exists()
    assert not os.path.exists(notice.notices_dir(str(repo))) or not os.listdir(notice.notices_dir(str(repo)))


def test_a_fresh_run_is_not_halted_and_not_notified(repo):
    peer = _run(repo, stage="minted", age=0, session_id=PEER)
    warp_run_deliverable._live_peer_tail(repo, RUN, PEER, str(repo))
    assert not (peer / "halted.json").exists()
    assert notice.consume_notices(str(repo), PEER) == []
