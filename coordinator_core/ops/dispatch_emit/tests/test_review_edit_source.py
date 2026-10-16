"""session_review_writes reads review-stage writes from per-agent touch records."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import review_edit_source
from coordinator_core.session import touch_record

SID = "sess-main-0001"
SINCE = 1000.0


def _store(root: Path) -> Path:
    hub = root / ".git" / "coordinator-sessions"
    (hub / SID).mkdir(parents=True)
    return hub


def _agent(
    hub: Path, agent_id: str, subagent_type: str, events, *, owner: str = SID, ledger: str = SID
) -> None:
    adir = hub / ".agents" / agent_id
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "em-session-id.txt").write_text(owner + "\n", encoding="utf-8")
    ledger_file = hub / ledger / "dispatched-agents.txt"
    ledger_file.parent.mkdir(parents=True, exist_ok=True)
    with ledger_file.open("a", encoding="utf-8") as fh:
        fh.write(f"{agent_id}\thost\t{subagent_type}\t1\n")
    with (adir / touch_record.RECORD_FILENAME).open("ab") as fh:
        for path, ts, kind in events:
            fh.write(
                touch_record.encode_line(
                    session_id=owner,
                    agent_id=agent_id,
                    verb=touch_record.VERB_TOUCH,
                    path=path,
                    timestamp=ts,
                    kind=kind,
                )
            )


def test_reviewer_write_after_since_is_returned(tmp_path):
    hub = _store(tmp_path)
    _agent(hub, "a1", "coordinator:code-reviewer", [("pkg/outside.py", 1500.0, "w")])
    assert review_edit_source.session_review_writes(tmp_path, SID, SINCE) == {
        "pkg/outside.py": "a1"
    }


def test_executor_write_is_not_returned(tmp_path):
    hub = _store(tmp_path)
    _agent(hub, "a2", "coordinator:executor", [("pkg/exec.py", 1500.0, "w")])
    assert review_edit_source.session_review_writes(tmp_path, SID, SINCE) == {}


def test_pre_since_write_is_not_returned(tmp_path):
    hub = _store(tmp_path)
    _agent(hub, "a3", "coordinator:code-reviewer", [("pkg/old.py", 999.0, "w")])
    assert review_edit_source.session_review_writes(tmp_path, SID, SINCE) == {}


def test_read_kind_is_not_a_write(tmp_path):
    hub = _store(tmp_path)
    _agent(hub, "a4", "coordinator:code-reviewer", [("pkg/read.py", 1500.0, "r")])
    assert review_edit_source.session_review_writes(tmp_path, SID, SINCE) == {}


def test_another_sessions_agent_is_not_returned(tmp_path):
    hub = _store(tmp_path)
    _agent(hub, "a5", "coordinator:code-reviewer", [("pkg/theirs.py", 1500.0, "w")], owner="other-sess")
    assert review_edit_source.session_review_writes(tmp_path, SID, SINCE) == {}


def test_unrecognised_type_counts_as_review(tmp_path):
    hub = _store(tmp_path)
    _agent(hub, "a6", "coordinator:brand-new-reviewer", [("pkg/new.py", 1500.0, None)])
    assert review_edit_source.session_review_writes(tmp_path, SID, SINCE) == {"pkg/new.py": "a6"}


def test_latest_writer_names_the_path(tmp_path):
    hub = _store(tmp_path)
    _agent(hub, "a7", "coordinator:code-reviewer", [("pkg/x.py", 1200.0, "w")])
    _agent(hub, "a8", "coordinator:test-runner", [("pkg/x.py", 1300.0, "w")])
    assert review_edit_source.session_review_writes(tmp_path, SID, SINCE) == {"pkg/x.py": "a8"}


def test_degraded_store_gives_none(tmp_path):
    hub = _store(tmp_path)
    assert review_edit_source.session_review_writes(tmp_path, SID, SINCE) == {}
    _agent(hub, "a9", "coordinator:code-reviewer", [("pkg/y.py", 1500.0, "w")])
    with (hub / ".agents" / "a9" / touch_record.RECORD_FILENAME).open("ab") as fh:
        fh.write(b"not json\n")
    assert review_edit_source.session_review_writes(tmp_path, SID, SINCE) is None


def test_missing_since_or_session_gives_none(tmp_path):
    hub = _store(tmp_path)
    _agent(hub, "a10", "coordinator:code-reviewer", [("pkg/z.py", 1500.0, "w")])
    assert review_edit_source.session_review_writes(tmp_path, SID, None) is None
    assert review_edit_source.session_review_writes(tmp_path, None, SINCE) is None
