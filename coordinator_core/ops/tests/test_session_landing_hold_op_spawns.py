"""session.landing_hold: registration, set/status/clear, and the end-to-end defer-then-replay path."""

from __future__ import annotations

import pytest
from pathlib import Path

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.ops import session_landing_hold as op
from coordinator_core.ops.dispatch_emit import checkpoint_stamp
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_checkpoint_stamp import (
    PLAN,
    _git as _git_cp,
    _message,
    _repo,
    _rows,
)
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    RUN,
    _call,
    _git,
    _write_record,
    repo,
)
from coordinator_core.session import landing_hold

SID = "11111111-2222-4333-8444-555555555555"


def _op(root, **params):
    return op._session_landing_hold(params, repo_root=root / ".git")


@pytest.mark.spawns_process
def test_clear_replays_deferred_halted_record(repo) -> None:
    _write_record(repo, RUN)
    head = _git(["rev-parse", "HEAD"], repo).strip()
    _op(repo, action="set", session_id=SID)

    deferred = _call(repo, session_id=SID)

    assert deferred["deferred"] == "session-landing-hold"
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
    assert _op(repo, action="status", session_id=SID)["deferred_count"] == 1

    out = _op(repo, action="clear", session_id=SID)

    assert out["failed"] == [] and [r["kind"] for r in out["replayed"]] == ["halted_record"]
    new_head = _git(["rev-parse", "HEAD"], repo).strip()
    assert new_head != head and out["replayed"][0]["sha"]
    assert (repo / RECORD_DIR_ROOT / RUN / "run.json").is_file()
    assert not landing_hold.is_held(str(repo), SID)
    assert _git(["log", "--format=%s", f"{head}..HEAD"], repo).count("rows coded") == 0


@pytest.mark.spawns_process
def test_clear_replays_deferred_row_mark_through_land_stamp(tmp_path: Path) -> None:
    root, sha = _repo(tmp_path)
    _op(root, action="set", session_id=SID)

    out = checkpoint_stamp.stamp_checkpoint(root, _message(), sha, session_id=SID)

    assert out["stamp_deferred"] == "session-landing-hold"
    assert _git_cp(root, "rev-parse", "HEAD") == sha
    assert _rows(root)["C1"]["disposition"] == "coded"

    cleared = _op(root, action="clear", session_id=SID)

    assert cleared["failed"] == [] and [r["kind"] for r in cleared["replayed"]] == ["checkpoint_stamp"]
    assert _git_cp(root, "rev-parse", "HEAD") == cleared["replayed"][0]["sha"]
    assert _git_cp(root, "rev-parse", "HEAD~1") == sha
    assert _git_cp(root, "status", "--porcelain", "--", PLAN) == ""
    assert not landing_hold.is_held(str(root), SID)
