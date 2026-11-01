"""stranded_inbox_memos: open inbox memos on fetched sibling day refs, found without a git spawn."""

import subprocess

import pytest

from coordinator_core import memo_corpus

pytestmark = [pytest.mark.spawns_process]

_INBOX = "state/cross-repo/inbox"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _memo(status):
    return f"---\nstatus: {status}\n---\nbody\n"


def _write(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "work/me/2026-10-20")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _write(r, f"{_INBOX}/stranded.md", _memo("open"))
    _write(r, f"{_INBOX}/archived-here.md", _memo("open"))
    _write(r, f"{_INBOX}/closed.md", _memo("resolved"))
    _write(r, f"{_INBOX}/head-only.md", _memo("open"))
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "sibling")
    _git(r, "update-ref", "refs/remotes/origin/work/other/2026-10-29", "HEAD")
    _git(r, "rm", "-q", f"{_INBOX}/stranded.md", f"{_INBOX}/closed.md")
    _write(r, f"{_INBOX}/head-only.md", _memo("open"))
    _git(r, "commit", "-am", "mine")
    _git(r, "update-ref", "refs/remotes/origin/work/me/2026-10-20", "HEAD")
    (r / "state/cross-repo/archive").mkdir(parents=True)
    (r / "state/cross-repo/archive/archived-here.md").write_text(_memo("resolved"), encoding="utf-8")
    (r / _INBOX / "archived-here.md").unlink()
    return r


def test_finds_only_open_unseen_memo_with_zero_spawns(repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn during scan")

    with monkeypatch.context() as mp:
        mp.setattr(subprocess, "Popen", boom)
        found = memo_corpus.stranded_inbox_memos(str(repo))

    assert [(m.filename, m.ref) for m in found] == [
        ("stranded.md", "refs/remotes/origin/work/other/2026-10-29")
    ]
    assert len(found[0].blob_sha) == 40


def test_head_tracking_ref_is_skipped(repo):
    _git(repo, "update-ref", "-d", "refs/remotes/origin/work/other/2026-10-29")
    _write(repo, f"{_INBOX}/only-on-head.md", _memo("open"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "x")
    _git(repo, "update-ref", "refs/remotes/origin/work/me/2026-10-20", "HEAD")
    (repo / _INBOX / "only-on-head.md").unlink()
    assert memo_corpus.stranded_inbox_memos(str(repo)) == []


def test_packed_refs_are_read(repo):
    _git(repo, "pack-refs", "--all")
    found = memo_corpus.stranded_inbox_memos(str(repo))
    assert [m.filename for m in found] == ["stranded.md"]


def test_no_remote_refs_returns_empty(tmp_path):
    r = tmp_path / "bare"
    r.mkdir()
    _git(r, "init")
    assert memo_corpus.stranded_inbox_memos(str(r)) == []
