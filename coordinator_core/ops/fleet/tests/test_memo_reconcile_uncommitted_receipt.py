
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet.memo_reconcile_outbox import _memo_reconcile_outbox
from coordinator_core.win_portability import no_console_creationflags

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
    _tracked_archive(repo)

    result = _memo_reconcile_outbox({"dry_run": True}, repo_root=repo)

    assert _reports(result) == []


def test_an_archive_head_knows_nothing_of_is_not_every_memo_lost(repo):
    _archive(repo, "a.md")
    _archive(repo, "b.md")
    _archive(repo, "c.md")

    result = _memo_reconcile_outbox({"dry_run": True}, repo_root=repo)

    assert _reports(result) == []


def test_the_report_names_the_pathspec_that_completes_the_receipt(repo):
    _tracked_archive(repo)
    _archive(repo, "lost.md")

    result = _memo_reconcile_outbox({"dry_run": True}, repo_root=repo)

    note = _reports(result)[0]["note"]
    assert ".coordinator-local/memo-outbox/sent/lost.md" in note
    assert ".coordinator-local/memo-outbox/sent-ledger.jsonl" in note


def test_act_mode_reports_it_and_moves_nothing(repo):
    _tracked_archive(repo)
    path = _archive(repo, "lost.md")

    result = _memo_reconcile_outbox({"dry_run": False}, repo_root=repo)

    assert result["acted"] == []
    assert [s["filename"] for s in result["skipped"]] == ["lost.md"]
    assert path.is_file(), "a sent/ copy is already where it belongs — the commit is what is missing"
