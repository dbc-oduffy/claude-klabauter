"""An engine-run executor's BLOCKED dispatch report opens the sentinel escape for the session that fired the run."""

from __future__ import annotations

import json
import os
import tempfile
import time

import pytest

from coordinator_core.bash_guards import guard_post_executor_verification as g
from coordinator_core.hooks import dispatch_escape as de
from coordinator_core.hooks import nudge_em_code_dispatch as nd

SID = "sess-inrun-1"
FOREIGN = "sess-inrun-other"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(nd, "_is_bootstrap_or_out_of_repo", lambda p: False)
    monkeypatch.setattr(nd, "_session_sizing_state", lambda payload, sid: (True, False))
    monkeypatch.setattr(nd, "resolve_mode", lambda k, sid: False)
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "pkg").mkdir()
    (tmp_path / "repo" / "pkg" / "__init__.py").write_text("")
    g._PKG_CACHE.clear()


@pytest.fixture
def repo(tmp_path):
    return tmp_path / "repo"


def _run(repo, session: str, first_line: str = "status: BLOCKED", report_age: float = 0.0) -> None:
    run = repo / "scratch" / "warp" / f"ask-{session}"
    run.mkdir(parents=True)
    submitted = time.time() - 100
    (run / "submission.json").write_text(json.dumps({"session_id": session, "submitted_at": submitted}))
    (run / "manifest.json").write_text(json.dumps({"rows": [{"id": "C1"}]}))
    rep = repo / ".coordinator-local" / "subagent-share" / "dispatch-reports" / f"plan-{session}" / "C1.md"
    rep.parent.mkdir(parents=True)
    rep.write_text(first_line + "\n\nbody\n")
    stamp = submitted + 50 + report_age
    os.utime(rep, (stamp, stamp))


def _reason() -> None:
    with open(nd.dispatch_nudge_sentinel_path(SID), "w", encoding="utf-8") as fh:
        fh.write("conflicted merge\n")


def _payload(repo) -> dict:
    return {"session_id": SID, "cwd": str(repo)}


def _bash_armed(repo) -> dict:
    d = repo / ".git" / "coordinator-sessions" / SID
    d.mkdir(parents=True)
    (d / "dispatched-agents.txt").write_text("abcdef0123456789\tsonnet\tcoordinator:executor\t1\n")
    payload = {"tool_name": "Bash", "session_id": SID, "cwd": str(repo), "tool_input": {"command": "pytest"}}
    assert g.check(payload) is None
    return payload


@pytest.mark.parametrize("first", ["status: BLOCKED", "status: failed"])
def test_edit_route_honours_in_run_report_once(repo, first) -> None:
    _run(repo, SID, first)
    _reason()
    assert nd._take_escape(_payload(repo), SID, "x.py", "nudge-em-code-dispatch") is True
    assert not os.path.exists(nd.dispatch_nudge_sentinel_path(SID))
    assert nd._take_escape(_payload(repo), SID, "x.py", "nudge-em-code-dispatch") is False


def test_bash_route_honours_in_run_report_once(repo) -> None:
    payload = _bash_armed(repo)
    assert g.check(payload) is not None
    _run(repo, SID)
    _reason()
    assert g.check(payload) is None
    assert not os.path.exists(nd.dispatch_nudge_sentinel_path(SID))
    assert g.check(payload) is not None


def test_foreign_session_report_not_honoured(repo) -> None:
    _run(repo, FOREIGN)
    _reason()
    assert nd._take_escape(_payload(repo), SID, "x.py", "nudge-em-code-dispatch") is False
    assert os.path.exists(nd.dispatch_nudge_sentinel_path(SID))
    payload = _bash_armed(repo)
    assert g.check(payload) is not None
    assert os.path.exists(nd.dispatch_nudge_sentinel_path(SID))


def test_report_older_than_submission_not_honoured(repo) -> None:
    _run(repo, SID, report_age=-200)
    _reason()
    assert de._executor_failed(_payload(repo), SID) is False


def test_non_failed_report_not_honoured(repo) -> None:
    _run(repo, SID, "status: DONE")
    _reason()
    assert nd._take_escape(_payload(repo), SID, "x.py", "nudge-em-code-dispatch") is False
    assert os.path.exists(nd.dispatch_nudge_sentinel_path(SID))


def test_bare_reason_without_failed_executor_not_honoured(repo) -> None:
    _reason()
    assert nd._take_escape(_payload(repo), SID, "x.py", "nudge-em-code-dispatch") is False
    payload = _bash_armed(repo)
    assert g.check(payload) is not None
    assert os.path.exists(nd.dispatch_nudge_sentinel_path(SID))
