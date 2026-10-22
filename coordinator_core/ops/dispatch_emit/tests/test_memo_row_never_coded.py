"""A memo-send row is never stamped coded; a receipted one closes delivered_elsewhere."""

from __future__ import annotations

from types import SimpleNamespace

from coordinator_core.frontmatter.schema_validate import check_plan_tasks_source
from coordinator_core.ops.dispatch_emit import plan_row_coding, terminal_commit

_OUTBOX = ".coordinator-local/memo-outbox"

_PLAN = (
    "---\ntitle: p\n---\n\n# p\n\n## Goal\n\ng\n\n## Tasks\n\n```yaml plan-tasks\n"
    "- id: M1\n  title: send the memo\n  change_kind: doc-edit\n"
    f"  surface: {_OUTBOX}/topic.md\n  writes: []\n  memo_to: coordinator-content-repo\n  disposition: open\n"
    "  body: |\n    Send it.\n"
    "- id: C1\n  title: code it\n  change_kind: script-edit\n"
    "  surface: a.py\n  writes: []\n  disposition: open\n"
    "  body: |\n    Do it.\n"
    "```\n"
)


def _plan(tmp_path) -> None:
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "p.md").write_text(_PLAN, encoding="utf-8")


def _receipt(tmp_path) -> None:
    sent = tmp_path / _OUTBOX / "sent"
    sent.mkdir(parents=True, exist_ok=True)
    (sent / "topic.md").write_text("sent\n", encoding="utf-8")


def _request():
    return SimpleNamespace(plan_path="p.md", chunks=[SimpleNamespace(id="M1"), SimpleNamespace(id="C1")])


def test_flip_rows_coded_skips_a_memo_row():
    new, flipped = plan_row_coding.flip_rows_coded(_PLAN, {"M1", "C1"}, "abc1234")
    assert flipped == ["C1"]
    assert "id: M1" in new and "disposition: coded" in new
    new_only, none = plan_row_coding.flip_rows_coded(_PLAN, {"M1"}, "abc1234")
    assert none == [] and new_only == _PLAN


def test_receipted_memo_row_closes_delivered_elsewhere(tmp_path):
    _plan(tmp_path)
    _receipt(tmp_path)
    closes = terminal_commit._memo_rows_to_close(tmp_path, _request())
    assert list(closes["p.md"]) == ["M1"]
    new, closed = plan_row_coding.close_memo_rows_delivered(_PLAN, closes["p.md"])
    assert closed == ["M1"]
    for line in (
        "disposition: delivered_elsewhere",
        "delivered_via: memo",
        f"disposition_ref: {_OUTBOX}/sent/topic.md",
    ):
        assert line in new
    assert "coordinator-content-repo" in new
    assert check_plan_tasks_source(new) is None


def test_unreceipted_memo_row_stays_open_and_is_unsent(tmp_path):
    _plan(tmp_path)
    assert terminal_commit._memo_rows_to_close(tmp_path, _request()) == {}
    assert terminal_commit._memo_rows_without_receipt(tmp_path, _request()) == {"M1": "topic"}
    new, closed = plan_row_coding.close_memo_rows_delivered(_PLAN, {})
    assert closed == [] and new == _PLAN
