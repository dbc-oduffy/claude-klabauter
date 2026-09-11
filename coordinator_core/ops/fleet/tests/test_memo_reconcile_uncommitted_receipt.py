"""
coordinator_core.ops.fleet.tests.test_memo_reconcile_uncommitted_receipt

Purpose: `memo_reconcile_outbox._uncommitted_receipts` — a `sent/<topic>.md`
that HEAD does not know. `memo.send` writes that copy only AFTER reading the
receiver's own object store back and confirming the delivery commit is really
there, so an uncommitted one is a memo the receiver demonstrably has and this
repo has no committed record of. memo.send says so on an envelope that is gone
by the next session; nothing else ever looked (example-retrieval-repo-em, 2026-09-11).

This module spawns real git explicitly (one `git init` plus one commit per
test) because the check reads HEAD's tree spine. Its sibling
test_memo_reconcile_outbox.py deliberately runs without git and therefore
proves only the unreadable-HEAD arm.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/fleet/tests/test_memo_reconcile_uncommitted_receipt.py -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet.memo_reconcile_outbox import _memo_reconcile_outbox
from coordinator_core.win_portability import no_console_creationflags

# Declares a real external-process spawn (spawn ratchet Rule 2).
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_NO_CONSOLE = no_console_creationflags()

_SENT = (".coordinator-local", "memo-outbox", "sent")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, check=True, **_NO_CONSOLE
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "reconcile-receipt-test@claude-claude-klabauter.test")
    _git("config", "user.name", "Reconcile Receipt Test")
    _git("config", "commit.gpgsign", "false")
    (root / "README.md").write_text("skeleton\n", encoding="utf-8")
    _git("add", "README.md")
    _git("commit", "-m", "chore: initial skeleton")
    return root


def _archive(root: Path, name: str) -> Path:
    sent = root.joinpath(*_SENT)
    sent.mkdir(parents=True, exist_ok=True)
    path = sent / name
    path.write_text(
        '---\ntitle: "t"\nto: "example-retrieval-repo-em"\nstatus: sent\n---\n\nbody\n',
        encoding="utf-8",
    )
    return path


def _commit(root: Path, relpath: str) -> None:
    for args in (("add", "--", relpath), ("commit", "-m", f"receipt: {relpath}")):
        subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, check=True, **_NO_CONSOLE
        )


def _tracked_archive(repo: Path) -> None:
    """Establish that THIS repo tracks its archive.

    Not scaffolding: it is the precondition the check calibrates on. An
    archive HEAD knows nothing of means the bucket is untracked here
    (`.coordinator-local/` is gitignored), and absence carries no information
    — so every test of the positive case must first make presence the norm,
    or it is testing a repo where the correct verdict is silence.
    """
    _archive(repo, "landed.md")
    _commit(repo, ".coordinator-local/memo-outbox/sent/landed.md")


def _reports(result: dict) -> list:
    return [c for c in result["candidates"] if c["disposition"] == "report"]


def test_an_uncommitted_sent_copy_is_reported_as_a_receiptless_delivery(repo):
    _tracked_archive(repo)
    _archive(repo, "lost.md")

    result = _memo_reconcile_outbox({"dry_run": True}, repo_root=repo)

    reports = _reports(result)
    assert [r["filename"] for r in reports] == ["lost.md"]
    assert "no committed receipt" in reports[0]["note"]


def test_a_committed_sent_copy_is_not_a_finding(repo):
    """The discriminator is HEAD membership, not presence on disk — every
    healthy repo's archive is full of files this check must stay silent
    about (44 of 44 in claude-klabauter's own, 2026-09-11)."""
    _tracked_archive(repo)

    result = _memo_reconcile_outbox({"dry_run": True}, repo_root=repo)

    assert _reports(result) == []


def test_an_archive_head_knows_nothing_of_is_not_every_memo_lost(repo):
    """`.coordinator-local/` is gitignored fleet-wide, so a repo that never
    tracked the bucket has an archive with no HEAD entries at all. Reading
    that as a receiptless delivery PER MEMO would make this instrument
    loudest exactly where it is least informed."""
    _archive(repo, "a.md")
    _archive(repo, "b.md")
    _archive(repo, "c.md")

    result = _memo_reconcile_outbox({"dry_run": True}, repo_root=repo)

    assert _reports(result) == []


def test_the_report_names_the_pathspec_that_completes_the_receipt(repo):
    """A finding an operator cannot act on is a finding they will not act on.
    The receipt is the sent/ copy AND the ledger row memo.send appends
    alongside it, so both are named."""
    _tracked_archive(repo)
    _archive(repo, "lost.md")

    result = _memo_reconcile_outbox({"dry_run": True}, repo_root=repo)

    note = _reports(result)[0]["note"]
    assert ".coordinator-local/memo-outbox/sent/lost.md" in note
    assert ".coordinator-local/memo-outbox/sent-ledger.jsonl" in note


def test_act_mode_reports_it_and_moves_nothing(repo):
    """Act mode's envelope has no `candidates` key, so a finding that lived
    only there would vanish from exactly the run an operator makes."""
    _tracked_archive(repo)
    path = _archive(repo, "lost.md")

    result = _memo_reconcile_outbox({"dry_run": False}, repo_root=repo)

    assert result["acted"] == []
    assert [s["filename"] for s in result["skipped"]] == ["lost.md"]
    assert path.is_file(), "a sent/ copy is already where it belongs — the commit is what is missing"
