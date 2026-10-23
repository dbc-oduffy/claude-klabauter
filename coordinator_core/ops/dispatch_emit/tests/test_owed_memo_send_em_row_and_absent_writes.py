"""A memo-send row with unbuilt non-memo writes is owed as an EM perform row, not a bare memo send."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.spine_read import read_spine

_SENT = ".coordinator-local/memo-outbox/sent"


@pytest.fixture(autouse=True)
def _registered(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.ops.fleet.memo_draft._classify_receiver_for_draft", lambda to, dry_run: None
    )


def _plan(tmp_path, row):
    (tmp_path / ".git").mkdir(exist_ok=True)
    path = tmp_path / "p.md"
    path.write_text(
        "---\ntitle: p\n---\n\n# p\n\n## Goal\n\ng\n\n## Tasks\n\n```yaml plan-tasks\n" + row + "```\n",
        encoding="utf-8",
    )
    return path


def _row(writes, performer="em"):
    lines = "".join(f"    - {w}\n" for w in writes)
    return (
        f"- id: C1\n  title: probe\n  change_kind: verification\n  performer: {performer}\n"
        "  kind: memo-send\n  memo_to: coordinator-content-repo\n  surface: docs/x.md\n"
        f"  writes:\n{lines}  receipt: {_SENT}/topic.md\n  disposition: open\n  body: |\n    Probe.\n"
    )


def _excl(tmp_path, row):
    out: list = []
    read_spine(_plan(tmp_path, row), exclusions=out)
    return out


def test_absent_non_memo_write_is_an_em_perform_row(tmp_path):
    out = _excl(tmp_path, _row(["docs/verdict.md", ".coordinator-local/memo-outbox/topic.md"]))
    assert [(e["id"], e["reason"]) for e in out] == [("C1", "em-performed")]
    assert "docs/verdict.md" in out[0]["detail"]
    assert "delivered_elsewhere" not in out[0]["detail"]


def test_sent_memo_does_not_close_row_with_absent_writes(tmp_path):
    (tmp_path / _SENT).mkdir(parents=True)
    (tmp_path / _SENT / "topic.md").write_text("sent\n", encoding="utf-8")
    out = _excl(tmp_path, _row(["docs/verdict.md"]))
    assert [e["reason"] for e in out] == ["em-performed"]


def test_existing_non_memo_writes_stay_memo_send(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "verdict.md").write_text("v\n", encoding="utf-8")
    out = _excl(tmp_path, _row(["docs/verdict.md"]))
    assert [e["reason"] for e in out] == ["memo-send"]
    assert "delivered_elsewhere" in out[0]["detail"]


def test_no_non_memo_writes_stays_memo_send(tmp_path):
    assert [e["reason"] for e in _excl(tmp_path, _row([]).replace("  writes:\n", "  writes: []\n"))] == [
        "memo-send"
    ]
