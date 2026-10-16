"""terminal_commit's memo gate resolves inbox-surfaced slugs and never drops a slug-less open row."""

from __future__ import annotations

from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit import terminal_commit

_OUTBOX = ".coordinator-local/memo-outbox"
_INBOX = "coordinator-content-repo/state/cross-repo/inbox/topic.md"


def _row(extra: str, disposition: str = "open") -> str:
    return (
        "- id: M1\n  title: send the memo\n  change_kind: doc-edit\n"
        f"  surface: {_INBOX}\n  writes: []\n  disposition: {disposition}\n{extra}"
        "  body: |\n    Send it.\n"
    )


def _plan(tmp_path, row: str) -> None:
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "p.md").write_text(
        "---\ntitle: p\n---\n\n# p\n\n## Goal\n\ng\n\n## Tasks\n\n```yaml plan-tasks\n" + row + "```\n",
        encoding="utf-8",
    )


def _receipt(tmp_path, slug="topic") -> None:
    sent = tmp_path / _OUTBOX / "sent"
    sent.mkdir(parents=True, exist_ok=True)
    (sent / f"{slug}.md").write_text("sent\n", encoding="utf-8")


def _gate(tmp_path) -> dict:
    return terminal_commit._memo_rows_without_receipt(tmp_path, SimpleNamespace(plan_path="p.md"))


def test_inbox_row_with_receipt_field_maps_to_slug_until_receipt_exists(tmp_path):
    _plan(tmp_path, _row(f"  receipt: {_OUTBOX}/sent/topic.md\n"))
    assert _gate(tmp_path) == {"M1": "topic"}
    _receipt(tmp_path)
    assert _gate(tmp_path) == {}


def test_inbox_row_resolved_delivered_elsewhere_is_not_reported(tmp_path):
    _plan(
        tmp_path,
        _row(
            f"  receipt: {_OUTBOX}/sent/topic.md\n  delivered_via: memo\n"
            f"  disposition_ref: {_OUTBOX}/sent/topic.md\n",
            "delivered_elsewhere",
        ),
    )
    assert _gate(tmp_path) == {}


def test_inbox_row_without_slug_is_reported_not_dropped(tmp_path):
    _plan(tmp_path, _row(""))
    assert _gate(tmp_path) == {"M1": "no slug: declare receipt:"}
