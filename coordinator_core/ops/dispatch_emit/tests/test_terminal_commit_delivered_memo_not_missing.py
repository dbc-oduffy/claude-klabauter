"""A DONE row's declared outbox memo draft that memo.send moved to its sent receipt is a met write
for terminal_commit; the same draft with no receipt still refuses declared-write-missing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = {
    "integration_stem": "rev-stem", "slices": 1, "fixes": 1,
    "wave_reviews": reviewed()["wave_reviews"],
}
_SID = "11111111-2222-3333-4444-555555555555"
PRESENT = "tools/names.py"
SLUG = "2026-11-22-claude-klabauter-to-coordinator-content-repo-tier-t-narrowed-to-test-runner"
DRAFTS = (
    f"state/cross-repo/outbox/{SLUG}.md",
    f".coordinator-local/memo-outbox/{SLUG}.md",
    f"state/memo-outbox/{SLUG}.md",
)


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / PRESENT).parent.mkdir(parents=True)
    (root / PRESENT).write_text("old\n", encoding="utf-8")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _stage_run(root: Path, draft: str) -> str:
    (root / PRESENT).write_text("new\n", encoding="utf-8")
    (root / "report-C2.md").write_text("DONE: built\n", encoding="utf-8")
    chunk = ChunkCommit(id="C2", title="names", paths=(PRESENT, draft), report="report-C2.md")
    other = ChunkCommit(id="C4", title="legs", paths=("legs.md",))
    request = CommitRequest(chunks=(chunk, other))
    (root / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return "run.mjs"


def _receipt(root: Path, sent_dir: str = ".coordinator-local/memo-outbox/sent") -> None:
    path = root / sent_dir / f"{SLUG}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nstatus: sent\n---\n", encoding="utf-8")


def _call(root: Path, script: str) -> dict:
    params = {
        "script_path": script, "incomplete_chunks": ["C4"], "inline_review": _REVIEWED,
        "session_id": _SID,
    }
    return terminal_commit._handler(params, repo_root=root / ".git")


@pytest.mark.parametrize("draft", DRAFTS)
def test_a_delivered_draft_with_a_receipt_is_a_met_write_and_the_row_lands(repo, draft):
    _receipt(repo)

    out = _call(repo, _stage_run(repo, draft))

    assert out.get("refused") != "declared-write-missing", out
    assert out["committed"] is True, out
    assert "new" in _git(["show", f"HEAD:{PRESENT}"], repo)


def test_a_legacy_sent_receipt_credits_the_draft(repo):
    _receipt(repo, "state/memo-outbox/sent")

    out = _call(repo, _stage_run(repo, DRAFTS[0]))

    assert out["committed"] is True, out


@pytest.mark.parametrize("draft", DRAFTS)
def test_an_absent_draft_with_no_receipt_still_refuses_naming_it(repo, draft):
    head = _git(["rev-parse", "HEAD"], repo)

    out = _call(repo, _stage_run(repo, draft))

    assert out["committed"] is False, out
    assert out["refused"] == "declared-write-missing", out
    assert out["missing_declared"] == {"C2": [draft]}, out
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_a_receipt_for_another_topic_does_not_credit_a_non_memo_absent_path(repo):
    _receipt(repo)

    out = _call(repo, _stage_run(repo, "tools/other.md"))

    assert out["refused"] == "declared-write-missing", out
    assert out["missing_declared"] == {"C2": ["tools/other.md"]}, out
