"""review_receipt counts only a receipt stamped at or after the close's newest commit."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import coordinator_core.workstream_complete as wsc
from coordinator_core.subagent_sandbox import provision_report

_SID = "covsid123"
_STAMP = "2026-08-27T13:00:00+00:00"
_STAMP_EPOCH = int(datetime.fromisoformat(_STAMP).astimezone(timezone.utc).timestamp())


def _write_sidecar(tmp_path: Path, with_receipt: bool = True) -> Path:
    d = tmp_path / "state" / "subagent-share" / _SID
    d.mkdir(parents=True, exist_ok=True)
    p = d / "code-reviewer.deadbeef01.md"
    agent_type = "coordinator:code-reviewer"
    doc = (
        f"---\nstatus: complete\nagent_type: {agent_type}\nlead_session_id: {_SID}\n"
        "commits: []\n---\n\n## Findings\n\nReal review content here.\n"
    )
    if with_receipt:
        doc = provision_report._splice_review_receipt(doc, _SID, "deadbeef01", agent_type, _STAMP)
    p.write_text(doc, encoding="utf-8")
    return p


def _gate(tmp_path: Path, commits):
    return wsc._compute_review_receipt_gate(
        tmp_path, _SID, "implemented", None, None, close_commits=commits
    )


def test_stale_receipt_with_later_commit_blocks_and_names_sha(tmp_path):
    _write_sidecar(tmp_path)
    gate = _gate(tmp_path, (("aaaa111", _STAMP_EPOCH - 60), ("bbbb222", _STAMP_EPOCH + 60)))
    assert gate.blocks is True
    assert "bbbb222" in gate.detail
    assert "aaaa111" not in gate.detail


def test_receipt_stamped_after_newest_commit_clears(tmp_path):
    _write_sidecar(tmp_path)
    gate = _gate(tmp_path, (("aaaa111", _STAMP_EPOCH - 60), ("bbbb222", _STAMP_EPOCH)))
    assert gate.blocks is False


def test_no_known_close_commits_keeps_prior_behaviour(tmp_path):
    _write_sidecar(tmp_path)
    assert _gate(tmp_path, ()).blocks is False


def test_commit_of_reviewed_files_after_receipt_does_not_block(tmp_path):
    _write_sidecar(tmp_path)
    gate = _gate(
        tmp_path,
        (
            ("aaaa111", _STAMP_EPOCH - 60, ("src/a.py", "src/b.py")),
            ("bbbb222", _STAMP_EPOCH + 60, ("src/a.py",)),
        ),
    )
    assert gate.blocks is False


def test_bookkeeping_only_commits_after_receipt_do_not_block(tmp_path):
    _write_sidecar(tmp_path)
    gate = _gate(
        tmp_path,
        (
            ("aaaa111", _STAMP_EPOCH - 60, ("src/a.py",)),
            ("bbbb222", _STAMP_EPOCH + 60, ("state/cross-repo/inbox/m.md",)),
            ("cccc333", _STAMP_EPOCH + 90, ("state/review-trail/diffs/s.diff",)),
            ("dddd444", _STAMP_EPOCH + 99, ("state/warp/run/x.yaml",)),
        ),
    )
    assert gate.blocks is False


def test_memo_send_commits_after_receipt_do_not_block(tmp_path):
    # The paths memo.send commits: the sent draft and its sent-record.
    _write_sidecar(tmp_path)
    gate = _gate(
        tmp_path,
        (
            ("aaaa111", _STAMP_EPOCH - 60, ("src/a.py",)),
            ("bbbb222", _STAMP_EPOCH + 27, (".coordinator-local/memo-outbox/sent/topic.md",)),
            ("cccc333", _STAMP_EPOCH + 28, (".coordinator-local/memo-outbox/sent-records/topic/r.json",)),
        ),
    )
    assert gate.blocks is False


def test_later_commit_without_paths_still_blocks(tmp_path):
    _write_sidecar(tmp_path)
    gate = _gate(
        tmp_path,
        (
            ("aaaa111", _STAMP_EPOCH - 60, ("src/a.py",)),
            ("bbbb222", _STAMP_EPOCH + 60),
        ),
    )
    assert gate.blocks is True


def test_later_commit_outside_reviewed_files_still_blocks(tmp_path):
    _write_sidecar(tmp_path)
    gate = _gate(
        tmp_path,
        (
            ("aaaa111", _STAMP_EPOCH - 60, ("src/a.py",)),
            ("bbbb222", _STAMP_EPOCH + 60, ("src/a.py", "src/new.py")),
        ),
    )
    assert gate.blocks is True
    assert "bbbb222" in gate.detail


def test_without_receipt_block_the_gate_blocks_even_when_commits_precede(tmp_path):
    _write_sidecar(tmp_path, with_receipt=False)
    gate = _gate(tmp_path, (("aaaa111", _STAMP_EPOCH - 60),))
    assert gate.blocks is True
