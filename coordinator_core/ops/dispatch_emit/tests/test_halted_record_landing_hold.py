"""A halted-run record commit defers under a session landing hold and lands when no hold is set."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    RUN,
    _call,
    _git,
    _write_record,
    repo,
)
from coordinator_core.session import landing_hold

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SID = "11111111-2222-4333-8444-555555555555"


def test_held_session_defers_halt_commit_and_keeps_record_and_token(repo):
    _write_record(repo, RUN)
    token = run_token.mint(repo, RUN, "run.mjs")
    head = _git(["rev-parse", "HEAD"], repo).strip()
    assert landing_hold.set_hold(str(repo), SID)

    out = _call(repo, run_token=token, session_id=SID)

    assert not out.get("error") and not out.get("refused")
    assert out["committed"] is False
    assert out["deferred"] == "session-landing-hold"
    assert "push" not in out
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
    assert (repo / RECORD_DIR_ROOT / RUN / "run.json").is_file()
    assert run_token.halt_admits(repo, RUN, token, "run.mjs")
    entries = landing_hold.pending(str(repo), SID)
    assert [e["kind"] for e in entries] == ["halted_record"]
    assert entries[0]["params"]["run_id"] == RUN


def test_unheld_session_and_other_session_still_land(repo):
    _write_record(repo, RUN)
    assert landing_hold.set_hold(str(repo), SID)

    out = _call(repo, session_id="99999999-2222-4333-8444-555555555555")

    assert out["committed"] is True and "deferred" not in out
    assert landing_hold.pending(str(repo), SID) == []


def test_cleared_hold_lands_the_record(repo):
    _write_record(repo, RUN)
    landing_hold.set_hold(str(repo), SID)
    assert _call(repo, session_id=SID)["deferred"] == "session-landing-hold"

    landing_hold.clear_hold(str(repo), SID)
    out = _call(repo, session_id=SID)

    assert out["committed"] is True and "deferred" not in out
