"""`sizing-assemble --write` runs under Bash, so the sizing it writes must reach this
session's touch record itself: the dispatch gate reads that record for "sized"."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.sizing_assemble as sa
from coordinator_core.hooks import nudge_unrouted_sizing as sizing
from coordinator_core.hooks import watchdog_undischarged_next_move as watchdog

_SID = "11111111-2222-3333-4444-555555555555"
_REL = "state/sizings/2026-10-12-x.yaml"


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / _REL).write_text("schema: sizing-object\n", encoding="utf-8")
    monkeypatch.setattr(watchdog, "open_sizing_routed", lambda *a, **k: None)
    return tmp_path


def test_written_sizing_counts_as_this_sessions_sizing(repo, monkeypatch):
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    assert sizing._session_touched_sizing_files(_SID, str(repo)) == []

    sa._open_routed_obligation(str(repo / _REL))

    assert sizing._session_touched_sizing_files(_SID, str(repo)) == [_REL]


def test_no_session_id_records_nothing(repo, monkeypatch):
    for name in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)

    sa._open_routed_obligation(str(repo / _REL))

    assert sizing._session_touched_sizing_files(_SID, str(repo)) == []


def test_warm_request_identity_is_used_when_env_has_none(repo, monkeypatch):
    from coordinator_core.session.core import session_identity_override

    for name in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    with session_identity_override(_SID):
        sa._open_routed_obligation(str(repo / _REL))

    assert sizing._session_touched_sizing_files(_SID, str(repo)) == [_REL]
