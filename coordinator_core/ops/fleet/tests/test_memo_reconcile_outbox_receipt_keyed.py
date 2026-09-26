"""
Tests for the R16 correction to coordinator_core.ops.fleet.memo_reconcile_outbox:
`_classify` keys "delivered" on the already-computed receipt check (a local
`sent/<name>` copy), never on `status != draft` alone.

Spec: docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-fyi-rest.md, row R16
("Item 16: memo_reconcile_outbox classifies delivered from the receipt
check, not status").

Before this fix, a `status: sent`/`open` entry with NO local `sent/<name>`
receipt was moved into `sent/` on the strength of its `status:` field alone
-- exactly the over-trust in a self-reported field this op's own module
docstring otherwise argues against. That silently pulls a still-undelivered
ask off the staleness-nudge surface. This module pins the corrected
behaviour: no local receipt means no move, regardless of what `status:`
claims.

No `git init` is needed -- see test_memo_reconcile_outbox.py's module
docstring for why a bare `.git` entry is a faithful worktree stand-in here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.fleet.memo_reconcile_outbox import _memo_reconcile_outbox

_NEW_OUTBOX = (".coordinator-local", "memo-outbox")
_NEW_SENT = (".coordinator-local", "memo-outbox", "sent")


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A worktree root as `main_worktree_root` recognises one: a directory
    with a `.git` entry beneath it. No `git init` -- nothing here runs git."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def _outbox_dir(root: Path) -> Path:
    d = root.joinpath(*_NEW_OUTBOX)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_memo(root: Path, name: str, status: str) -> Path:
    path = _outbox_dir(root) / name
    path.write_text(
        f'---\ntitle: "t"\nto: "example-retrieval-repo-em"\nstatus: {status}\n---\n\nbody\n',
        encoding="utf-8",
    )
    return path


def _dispositions(result: dict) -> dict:
    return {c["filename"]: c["disposition"] for c in result["candidates"]}


class TestReceiptKeyedClassification:
    def test_status_sent_with_no_receipt_is_not_moved_to_sent(self, worktree):
        """The row's own named test: a `status: sent` memo with no receipt
        is not moved to sent/."""
        _write_memo(worktree, "no-receipt.md", "sent")

        result = _memo_reconcile_outbox({"dry_run": False}, repo_root=worktree)

        assert result["acted"] == []
        outbox = worktree.joinpath(*_NEW_OUTBOX)
        assert (outbox / "no-receipt.md").is_file(), (
            "an undelivered ask stays on the staleness-nudge surface"
        )
        assert not (worktree.joinpath(*_NEW_SENT) / "no-receipt.md").exists()

    def test_status_open_with_no_receipt_is_not_moved_to_sent(self, worktree):
        """`open` is as much a non-draft claim as `sent` -- the discriminator
        is the receipt, not which terminal-looking spelling `status:` carries."""
        _write_memo(worktree, "no-receipt.md", "open")

        result = _memo_reconcile_outbox({"dry_run": False}, repo_root=worktree)

        assert result["acted"] == []
        assert worktree.joinpath(*_NEW_OUTBOX, "no-receipt.md").is_file()

    def test_dry_run_keeps_undelivered_non_draft_without_receipt(self, worktree):
        _write_memo(worktree, "no-receipt.md", "sent")

        result = _memo_reconcile_outbox({"dry_run": True}, repo_root=worktree)

        assert _dispositions(result) == {"no-receipt.md": "keep"}
        note = result["candidates"][0]["note"]
        assert "no sent/" in note or "receipt" in note

    def test_status_sent_with_a_local_receipt_is_treated_as_delivered(self, worktree):
        """The positive control: when a local `sent/<name>` receipt already
        exists, the non-draft entry is real archived history, not a bare
        claim -- it is left for the existing duplicate-skip path, never
        clobbering the authoritative archived copy, and never re-counted as
        acted work."""
        _write_memo(worktree, "has-receipt.md", "sent")
        sent = worktree.joinpath(*_NEW_SENT)
        sent.mkdir(parents=True)
        (sent / "has-receipt.md").write_text("authoritative archived copy\n", encoding="utf-8")

        result = _memo_reconcile_outbox({"dry_run": False}, repo_root=worktree)

        assert result["acted"] == []
        assert [s["filename"] for s in result["skipped"]] == ["has-receipt.md"]
        assert (sent / "has-receipt.md").read_text(encoding="utf-8") == (
            "authoritative archived copy\n"
        )

    def test_draft_status_is_unaffected_by_the_receipt_keying(self, worktree):
        """A live draft with no receipt still just stays a draft -- the
        receipt-keyed check only changes what happens to NON-draft status."""
        _write_memo(worktree, "live.md", "draft")

        result = _memo_reconcile_outbox({"dry_run": False}, repo_root=worktree)

        assert result["acted"] == []
        assert worktree.joinpath(*_NEW_OUTBOX, "live.md").is_file()
