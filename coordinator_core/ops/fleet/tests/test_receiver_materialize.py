"""materialize_memo: the shared receiver write + hook-free commit helper."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import _receiver_materialize as rm
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_MSG = "cross-repo memo delivered: x\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "receiver"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@claude-klabauter.test")
    _git(root, "config", "user.name", "ClaudeKlabauterTest")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "state" / "cross-repo").mkdir(parents=True)
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", ".gitkeep")
    _git(root, "commit", "-m", "init")
    return root


def test_materialized_file_is_on_disk_and_in_head_tree(tmp_path):
    repo = _make_repo(tmp_path)

    out = rm.materialize_memo(repo, "m.md", "body\n", commit_message=_MSG)

    assert out.status == rm.STATUS_MATERIALIZED
    assert out.rel_path == "state/cross-repo/inbox/m.md"
    assert out.reason is None
    assert (repo / out.rel_path).read_text(encoding="utf-8") == "body\n"
    assert _git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines().count(out.rel_path) == 1
    assert out.commit_sha == _git(repo, "rev-parse", "HEAD")


def test_existing_inbox_file_is_never_overwritten(tmp_path):
    repo = _make_repo(tmp_path)
    inbox = repo / "state" / "cross-repo" / "inbox"
    inbox.mkdir()
    (inbox / "m.md").write_text("original\n", encoding="utf-8")
    head_before = _git(repo, "rev-parse", "HEAD")

    out = rm.materialize_memo(repo, "m.md", "new\n", commit_message=_MSG)

    assert out.status == rm.STATUS_ALREADY_PRESENT
    assert (inbox / "m.md").read_text(encoding="utf-8") == "original\n"
    assert _git(repo, "rev-parse", "HEAD") == head_before


def test_filename_present_in_archive_is_already_present_for_inbox_dest(tmp_path):
    repo = _make_repo(tmp_path)
    archive = repo / "state" / "cross-repo" / "archive"
    archive.mkdir()
    (archive / "m.md").write_text("archived\n", encoding="utf-8")

    out = rm.materialize_memo(repo, "m.md", "new\n", commit_message=_MSG)

    assert out.status == rm.STATUS_ALREADY_PRESENT
    assert not (repo / "state" / "cross-repo" / "inbox" / "m.md").exists()


def test_archive_dest_writes_under_the_archive_root(tmp_path):
    repo = _make_repo(tmp_path)

    out = rm.materialize_memo(repo, "t.md", "done\n", dest="archive", commit_message=_MSG)

    assert out.status == rm.STATUS_MATERIALIZED
    assert out.rel_path == "state/cross-repo/archive/t.md"
    assert (repo / out.rel_path).read_text(encoding="utf-8") == "done\n"


def test_known_present_short_circuits_the_scan(tmp_path):
    repo = _make_repo(tmp_path)

    out = rm.materialize_memo(repo, "m.md", "x\n", commit_message=_MSG, known_present={"m.md"})

    assert out.status == rm.STATUS_ALREADY_PRESENT
    assert not (repo / out.rel_path).exists()


def test_declined_commit_names_the_status_and_leaves_no_file(tmp_path):
    repo = _make_repo(tmp_path)
    # A CR byte makes commit_authored_new_file refuse (a real decline, not a mock).
    out = rm.materialize_memo(repo, "crlf.md", "a\r\nb\n", commit_message=_MSG)

    assert out.status == rm.STATUS_COMMIT_DECLINED
    assert out.reason and "crlf.md" in out.reason
    assert out.commit_sha is None
    assert not (repo / out.rel_path).exists()


def test_unwritable_inbox_is_write_failed_not_already_present(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "state" / "cross-repo" / "inbox").write_text("not a dir", encoding="utf-8")

    out = rm.materialize_memo(repo, "m.md", "x\n", commit_message=_MSG)

    assert out.status == rm.STATUS_WRITE_FAILED
    assert out.reason


def test_unknown_dest_is_a_programming_error(tmp_path):
    repo = _make_repo(tmp_path)
    with pytest.raises(ValueError):
        rm.materialize_memo(repo, "m.md", "x\n", dest="elsewhere", commit_message=_MSG)
