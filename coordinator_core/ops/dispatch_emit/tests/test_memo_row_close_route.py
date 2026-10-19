"""The memo-send closing route: receipt-ref slugs, receiver-inbox detection, performer-em memo rows."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.spine_read import (
    _is_memo_send_row,
    memo_close_route,
    memo_receipt_slug,
    memo_row_slug,
    read_spine,
)

_SENT = ".coordinator-local/memo-outbox/sent"
_INBOX_ROW = (
    "- id: M1\n  title: notify\n  change_kind: doc-edit\n"
    "  surface: coordinator-content-repo/state/cross-repo/inbox/\n  writes: []\n  disposition: open\n"
    "  body: |\n    Tell them.\n"
)


def _plan(tmp_path, *rows):
    (tmp_path / ".git").mkdir(exist_ok=True)
    path = tmp_path / "p.md"
    path.write_text(
        "---\ntitle: p\n---\n\n# p\n\n## Goal\n\ng\n\n## Tasks\n\n```yaml plan-tasks\n"
        + "".join(rows)
        + "```\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "ref",
    [f"{_SENT}/topic.md", f"{_SENT.replace('/', chr(92))}\\topic.md", "state/memo-outbox/sent/topic.md"],
)
def test_receipt_slug_accepts_canonical_and_legacy(ref):
    assert memo_receipt_slug(ref) == "topic"


@pytest.mark.parametrize(
    "ref",
    [
        ".coordinator-local/memo-outbox/topic.md",
        f"{_SENT}/a/topic.md",
        f"{_SENT}/topic.txt",
        f"{_SENT}/../topic.md",
        f"{_SENT}/../x/topic.md",
        None,
        7,
    ],
)
def test_receipt_slug_rejects_non_receipts(ref):
    assert memo_receipt_slug(ref) is None


def test_slug_precedence_receipt_then_disposition_ref_then_draft():
    base = {"surface": ".coordinator-local/memo-outbox/draft.md"}
    assert memo_row_slug(base) == "draft"
    assert memo_row_slug({**base, "disposition_ref": f"{_SENT}/ref.md"}) == "ref"
    assert memo_row_slug({**base, "disposition_ref": "abc123"}) == "draft"
    assert memo_row_slug({**base, "disposition_ref": f"{_SENT}/ref.md", "receipt": f"{_SENT}/rcpt.md"}) == "rcpt"


@pytest.mark.parametrize(
    "surface",
    [
        "coordinator-content-repo/state/cross-repo/inbox/",
        "state/cross-repo/inbox",
        "..\\coordinator-content-repo\\state\\cross-repo\\inbox\\",
        "/abs/coordinator-content-repo/state/cross-repo/inbox/x.md",
    ],
)
def test_inbox_surface_is_a_memo_send_row(surface):
    assert _is_memo_send_row({"surface": surface})
    assert _is_memo_send_row({"surface": "x.md", "writes_under": [surface]})


def test_near_miss_inbox_segment_is_not_a_memo_row():
    assert not _is_memo_send_row({"surface": "state/cross-repo/inbox-archive/"})


def test_inbox_row_without_slug_is_held_not_raised(tmp_path):
    exclusions: list = []
    assert read_spine(_plan(tmp_path, _INBOX_ROW), exclusions=exclusions) == []
    assert [(e["id"], e["reason"]) for e in exclusions] == [("M1", "memo-send")]
    assert "sent/<slug>.md" in exclusions[0]["detail"]


def test_inbox_row_with_receipt_reports_delivered_once_receipt_exists(tmp_path):
    row = _INBOX_ROW.replace("  writes: []\n", f"  writes: []\n  memo_to: coordinator-content-repo\n  receipt: {_SENT}/note.md\n")
    plan = _plan(tmp_path, row)
    exclusions: list = []
    read_spine(plan, exclusions=exclusions)
    assert exclusions[0]["reason"] == "memo-send"
    assert f"{_SENT}/note.md" in exclusions[0]["detail"]
    (tmp_path / _SENT).mkdir(parents=True)
    (tmp_path / _SENT / "note.md").write_text("sent\n", encoding="utf-8")
    exclusions = []
    read_spine(plan, exclusions=exclusions)
    assert [(e["id"], e["reason"]) for e in exclusions] == [("M1", "memo-delivered")]


def test_performer_em_memo_row_reads_memo_send_not_em_performed(tmp_path):
    row = (
        "- id: M1\n  title: send\n  change_kind: verification\n  performer: em\n  kind: memo-send\n  memo_to: coordinator-content-repo\n"
        f"  surface: docs/x.md\n  writes: []\n  receipt: {_SENT}/topic.md\n  disposition: open\n"
        "  body: |\n    Send.\n"
    )
    exclusions: list = []
    read_spine(_plan(tmp_path, row), exclusions=exclusions)
    assert [(e["id"], e["reason"]) for e in exclusions] == [("M1", "memo-send")]
    assert "em-discharge" not in exclusions[0]["detail"]
    assert "delivered_elsewhere" in exclusions[0]["detail"]


def test_close_route_names_the_receipt_or_the_placeholder():
    assert f"disposition_ref {_SENT}/topic.md" in memo_close_route("C6", "topic")
    assert "id C6" in memo_close_route("C6", "topic")
    assert "sent/<slug>.md" in memo_close_route("C6", None)
