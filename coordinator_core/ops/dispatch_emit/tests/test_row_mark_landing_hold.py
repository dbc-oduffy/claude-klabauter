"""A session landing hold defers both row-mark commits (checkpoint stamp, coded stamp): HEAD stays
unmoved, the plan stays stamped on disk, a deferred marker returns and no error key appears."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import checkpoint_stamp, terminal_commit_stamp
from coordinator_core.ops.dispatch_emit.checkpoint_stamp import stamp_checkpoint
from coordinator_core.ops.dispatch_emit.tests.test_checkpoint_stamp import (
    PLAN,
    _git,
    _message,
    _repo,
    _rows,
)
from coordinator_core.session import landing_hold

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

HELD = "s-held"


def test_checkpoint_stamp_defers_under_hold(tmp_path):
    root, sha = _repo(tmp_path)
    landing_hold.set_hold(str(root), HELD)

    out = stamp_checkpoint(root, _message(), sha, session_id=HELD)

    assert out["stamp_deferred"] == "session-landing-hold"
    assert out["stamp_sha"] is None and "stamp_error" not in out
    assert _git(root, "rev-parse", "HEAD") == sha
    assert _rows(root)["C1"]["disposition"] == "coded"
    (entry,) = landing_hold.pending(str(root), HELD)
    assert entry["kind"] == "checkpoint_stamp"
    assert entry["params"]["paths"] == [PLAN]
    assert entry["params"]["message"].startswith(f"mark 2 rows coded ({sha[:7]})")
    assert entry["params"]["session_id"] == HELD


def test_deferred_checkpoint_stamp_replays_through_land_stamp(tmp_path):
    root, sha = _repo(tmp_path)
    landing_hold.set_hold(str(root), HELD)
    stamp_checkpoint(root, _message(), sha, session_id=HELD)
    (entry,) = landing_hold.clear_hold(str(root), HELD)

    params = entry["params"]
    stamp_sha = checkpoint_stamp._land_stamp(root, params["paths"], params["message"], params["session_id"])

    assert stamp_sha and _git(root, "rev-parse", "HEAD") == stamp_sha
    assert _git(root, "rev-parse", "HEAD~1") == sha
    assert _git(root, "status", "--porcelain", "--", PLAN) == ""


@pytest.mark.parametrize("session_id", ["s-other", None])
def test_checkpoint_stamp_unheld_session_still_commits(tmp_path, session_id):
    root, sha = _repo(tmp_path)
    landing_hold.set_hold(str(root), HELD)

    out = stamp_checkpoint(root, _message(), sha, session_id=session_id)

    assert out["stamp_sha"] and "stamp_deferred" not in out
    assert _git(root, "rev-parse", "HEAD") == out["stamp_sha"]
    assert landing_hold.pending(str(root), HELD) == []


def _coded(root, session_id, commit_v2):
    return terminal_commit_stamp._stamp_coded_commit(commit_v2, root, root, {PLAN: {"C1"}}, "a" * 40, session_id)


def test_coded_stamp_defers_under_hold(tmp_path):
    root, sha = _repo(tmp_path)
    landing_hold.set_hold(str(root), HELD)
    calls = []

    out = _coded(root, HELD, lambda params, repo_root: calls.append(params))

    assert calls == []
    assert out == {"rows_coded": {PLAN: ["C1"]}, "coded_sha": None, "coded_deferred": "session-landing-hold"}
    assert "coded_stamp_error" not in out
    assert _git(root, "rev-parse", "HEAD") == sha
    assert _rows(root)["C1"]["disposition"] == "coded"
    (entry,) = landing_hold.pending(str(root), HELD)
    assert entry["kind"] == "coded_stamp"
    assert entry["params"]["paths"] == [PLAN]
    assert entry["params"]["session_id"] == HELD
    assert entry["params"]["message"].startswith("mark 1 rows coded (aaaaaaa)")


def test_coded_stamp_unheld_session_still_commits(tmp_path):
    root, _sha = _repo(tmp_path)
    landing_hold.set_hold(str(root), HELD)
    calls = []

    def fake(params, repo_root):
        calls.append(params)
        return {"committed": True, "sha": "b" * 40}

    out = _coded(root, "s-other", fake)

    assert len(calls) == 1 and out["coded_sha"] == "b" * 40
    assert "coded_deferred" not in out
    assert landing_hold.pending(str(root), HELD) == []
