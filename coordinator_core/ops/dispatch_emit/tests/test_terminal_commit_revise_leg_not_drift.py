"""A write by the run's own revise-leg agent (an executor, which the review-writer map skips) after the
review moves the commit expectation to its bytes; a write the run's agents did not make still refuses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.session_hub_path import hub_for_common, session_dir_for
from coordinator_core.ops.dispatch_emit import review_edits, terminal_commit_review
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_refuses_blob_drift import (  # noqa: F401 -- repo is a fixture
    _WITH_BASE,
    SID,
    _fire,
    _record_at,
    _review,
    _sha,
    repo,
)
from coordinator_core.session import touch_record

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_STARTED = 500.0
_REVIEWED_AT = 2000.0


def _agent(repo: Path, agent_id: str, events, *, owner: str = SID, subagent_type: str = "coordinator:executor") -> None:
    """A dispatched agent of the session whose own touch record holds ``events`` ((path, hash, ts))."""
    hub = hub_for_common(repo / ".git")
    adir = hub / ".agents" / agent_id
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "em-session-id.txt").write_text(owner + "\n", encoding="utf-8")
    with (hub / SID / "dispatched-agents.txt").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{agent_id}\thost\t{subagent_type}\t1\n")
    with (adir / touch_record.RECORD_FILENAME).open("ab") as fh:
        for path, digest, ts in events:
            fh.write(touch_record.encode_line(
                session_id=owner, agent_id=agent_id, verb=touch_record.VERB_TOUCH, path=path,
                timestamp=ts, content_hash=digest, kind="w",
            ))


def _setup(repo, monkeypatch, final: str) -> None:
    """The run wrote ``A`` (session write), the review read ``B``, and ``a.py`` now holds ``final``."""
    monkeypatch.setattr(review_edits, "review_writes", lambda root, sid, since: {})
    sub = repo / RUN_DIR_ROOT / "run-1" / "submission.json"
    sub.parent.mkdir(parents=True, exist_ok=True)
    sub.write_text(json.dumps({"submitted_at": _STARTED}), encoding="utf-8")
    (repo / "a.py").write_bytes(final.encode("utf-8"))
    _record_at(repo, "a.py", _sha("A\n"), 1000.0)
    _review(repo, {"a.py": _sha("B\n")}, _REVIEWED_AT)
    session_dir_for(repo / ".git", SID).mkdir(parents=True, exist_ok=True)


def test_a_revise_leg_write_after_the_review_lands_at_its_bytes(repo, monkeypatch):
    _setup(repo, monkeypatch, "C\n")
    _agent(repo, "rev1", [("a.py", _sha("C\n"), 3000.0)])
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["committed"] is True, out
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("C\n")}


def test_the_newest_of_several_revise_writes_is_the_expectation(repo, monkeypatch):
    _setup(repo, monkeypatch, "D\n")
    _agent(repo, "rev1", [("a.py", _sha("C\n"), 3000.0)])
    _agent(repo, "rev2", [("a.py", _sha("D\n"), 3500.0)])
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["committed"] is True, out
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("D\n")}


def test_a_foreign_write_after_the_revise_leg_still_refuses(repo, monkeypatch):
    _setup(repo, monkeypatch, "FOREIGN\n")
    _agent(repo, "rev1", [("a.py", _sha("C\n"), 3000.0)])
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["refused"] == "blob-drift"
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("C\n")}


def test_a_write_by_no_run_agent_still_refuses_at_the_review_hash(repo, monkeypatch):
    _setup(repo, monkeypatch, "C\n")
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["refused"] == "blob-drift"
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("B\n")}


def test_another_sessions_agent_write_is_not_the_runs_own(repo, monkeypatch):
    _setup(repo, monkeypatch, "C\n")
    _agent(repo, "peer1", [("a.py", _sha("C\n"), 3000.0)], owner="peer-session")
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["refused"] == "blob-drift"
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("B\n")}


def test_an_agent_write_before_the_review_keeps_the_reviewed_expectation_then_lands_as_own(repo, monkeypatch):
    """The first attempt still expects the reviewed bytes; the own-drift re-measure
    (`_own_drift_actuals`, unbounded by run start so a refire keeps the run's earlier own write)
    then accepts the run's own recorded write rather than refusing."""
    _setup(repo, monkeypatch, "C\n")
    _agent(repo, "rev1", [("a.py", _sha("C\n"), _REVIEWED_AT - 100.0)])
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("B\n")}
    assert out.get("refused") != "blob-drift"


def test_an_agent_write_before_the_run_began_is_no_expectation(repo, monkeypatch):
    _setup(repo, monkeypatch, "C\n")
    _agent(repo, "old1", [("a.py", _sha("C\n"), _STARTED - 100.0)])
    assert terminal_commit_review._run_agent_write_events(repo, SID, _STARTED, ["a.py"]) == {}


def test_a_hashless_newest_agent_write_has_no_event(repo, monkeypatch):
    _setup(repo, monkeypatch, "C\n")
    _agent(repo, "rev1", [("a.py", _sha("C\n"), 3000.0), ("a.py", None, 3100.0)])
    assert terminal_commit_review._run_agent_write_events(repo, SID, _STARTED, ["a.py"]) == {}


def test_an_unrequested_path_is_not_returned(repo, monkeypatch):
    _setup(repo, monkeypatch, "C\n")
    _agent(repo, "rev1", [("other.py", _sha("C\n"), 3000.0)])
    assert terminal_commit_review._run_agent_write_events(repo, SID, _STARTED, ["a.py"]) == {}
