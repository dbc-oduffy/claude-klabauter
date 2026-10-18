"""Tests for session_history: the bounded one-spawn reader and its public shape (pinned for Plan B)."""

from __future__ import annotations

import dataclasses
import inspect
import subprocess

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workstream_complete import session_history as sh

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SID = "11111111-2222-3333-4444-555555555555"
PEER = "99999999-8888-7777-6666-555555555555"


def _git(root, *args):
    out = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=root, capture_output=True, text=True, check=True, **no_console_creationflags(),
    )
    return out.stdout.strip()


def _commit(root, rel, text, sid=None, extra=""):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    msg = f"edit {rel}\n\nbody\n"
    if sid:
        msg += f"\nSession-Id: {sid}\n"
    msg += extra
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


def _floor(root, value, sid=SID):
    d = root / ".git" / "coordinator-sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "head_at_start").write_text(value + "\n", encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "seed.txt", "seed\n")
    return root


@pytest.fixture
def spawns(monkeypatch):
    calls = []
    real = subprocess.Popen

    class Counting(real):
        def __init__(self, args, *a, **k):
            calls.append(args)
            super().__init__(args, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", Counting)
    return calls


def test_own_commits_oldest_first_with_parents_trailers_and_renames(repo, spawns):
    floor = _git(repo, "rev-parse", "HEAD")
    _floor(repo, floor)
    a = _commit(repo, "docs/a.md", "one\ntwo\n", SID, extra="Deliverable-Id: D-1\n")
    _commit(repo, "peer.txt", "p\n", PEER)
    _git(repo, "mv", "docs/a.md", "docs/b.md")
    _git(repo, "commit", "-q", "-m", f"rename\n\nSession-Id: {SID}\n")
    spawns.clear()
    h = sh.read_session_history(repo, SID)
    assert len(spawns) == 1
    assert h.resolved and h.reason == "" and h.floor == floor
    assert len(h.window) == 3 and len(h.own) == 2
    first, _, last = h.window
    assert first.sha == a and first.parents == (floor,)
    assert first.session_ids == (SID,) and first.deliverable_id == "D-1"
    assert first.files == (sh.FileChange("docs/a.md", "A", 2, 0),)
    assert last.files[0].status == "R" and last.files[0].path == "docs/b.md"
    assert h.head == last.sha


def test_merge_carrying_sid_has_two_parents_and_no_files(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _floor(repo, base)
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "side.txt", "s\n", PEER)
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "main.txt", "m\n", SID)
    _git(repo, "merge", "-q", "--no-ff", "side", "-m", f"merge\n\nSession-Id: {SID}\n")
    h = sh.read_session_history(repo, SID)
    merge = h.window[-1]
    assert h.resolved and merge.is_merge and len(merge.parents) == 2
    assert merge.files == () and merge in h.own


@pytest.mark.parametrize(
    "value,reason",
    [(None, "floor-missing"), ("unknown", "floor-unknown"), ("abc123", "floor-malformed"),
     ("A" * 40, "floor-malformed")],
)
def test_floor_decides_with_zero_spawns(repo, spawns, value, reason):
    if value is not None:
        _floor(repo, value)
    else:
        (repo / ".git" / "coordinator-sessions" / SID).mkdir(parents=True)
    spawns.clear()
    h = sh.read_session_history(repo, SID)
    assert not h.resolved and h.reason == reason and h.window == ()
    assert spawns == []


def test_empty_sid_is_no_session_dir_without_spawn(repo, spawns):
    spawns.clear()
    h = sh.read_session_history(repo, "")
    assert (h.resolved, h.reason, h.window) == (False, "no-session-dir", ())
    assert spawns == []


def test_floor_not_an_ancestor_is_unresolved(repo):
    _git(repo, "checkout", "-q", "-b", "other")
    off = _commit(repo, "off.txt", "o\n")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "m.txt", "m\n", SID)
    _floor(repo, off)
    h = sh.read_session_history(repo, SID)
    assert not h.resolved and h.reason == "floor-not-ancestor" and h.window == ()


def test_floor_absent_from_object_store_is_git_exit(repo):
    _floor(repo, "d" * 40)
    _commit(repo, "m.txt", "m\n", SID)
    h = sh.read_session_history(repo, SID)
    assert not h.resolved and h.reason.startswith("git-exit-")


@pytest.mark.parametrize(
    "exc,reason",
    [(subprocess.TimeoutExpired("git", 15), "git-timeout"), (FileNotFoundError("git"), "git-spawn-failed")],
)
def test_spawn_failure_is_unresolved_never_an_empty_window(repo, monkeypatch, exc, reason):
    _floor(repo, _git(repo, "rev-parse", "HEAD"))
    _commit(repo, "m.txt", "m\n", SID)

    def _raise(*_a, **_k):
        raise exc

    monkeypatch.setattr(sh.subprocess, "run", _raise)
    h = sh.read_session_history(repo, SID)
    assert not h.resolved and h.reason == reason and h.window == ()


def test_empty_window_is_resolved_without_spawn(repo, spawns):
    _floor(repo, _git(repo, "rev-parse", "HEAD"))
    spawns.clear()
    h = sh.read_session_history(repo, SID)
    assert h.resolved and h.window == () and h.own == () and h.reason == ""
    assert spawns == []


def test_commit_before_head_at_start_is_absent(repo):
    _commit(repo, "early.txt", "e\n", SID)
    _floor(repo, _git(repo, "rev-parse", "HEAD"))
    late = _commit(repo, "late.txt", "l\n", SID)
    h = sh.read_session_history(repo, SID)
    assert [c.sha for c in h.window] == [late]


def test_no_backslash_path_and_crlf_output_parse_identically(repo):
    _floor(repo, _git(repo, "rev-parse", "HEAD"))
    _commit(repo, "a/b/c.txt", "x\n", SID)
    h = sh.read_session_history(repo, SID)
    assert all("\\" not in f.path for c in h.window for f in c.files)
    lf = "\x1fx\x1d\n\n:100644 100644 a b M\ta/b.txt\n\n1\t2\ta/b.txt\n"
    chunk = "s" * 40 + "\x1f\x1f5\x1f2026-01-01T00:00:00+00:00\x1fsub\x1fbody\n"
    assert sh._parse_commit(chunk + lf[2:]) == sh._parse_commit((chunk + lf[2:]).replace("\n", "\r\n"))


def test_binary_row_has_none_counts(repo):
    _floor(repo, _git(repo, "rev-parse", "HEAD"))
    p = repo / "bin.dat"
    p.write_bytes(b"\x00\x01\x02")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"bin\n\nSession-Id: {SID}\n")
    (f,) = sh.read_session_history(repo, SID).window[0].files
    assert (f.status, f.added, f.deleted) == ("A", None, None)


def test_demoted_session_id_line_is_still_attributed(repo):
    _floor(repo, _git(repo, "rev-parse", "HEAD"))
    _commit(repo, "x.txt", "x\n", SID, extra="\nTrailing paragraph.\n")
    assert len(sh.read_session_history(repo, SID).own) == 1


def test_public_shape_is_pinned():
    def names(cls):
        return [f.name for f in dataclasses.fields(cls)]

    assert names(sh.FileChange) == ["path", "status", "added", "deleted"]
    assert names(sh.SessionCommit) == [
        "sha", "parents", "committer_epoch", "committed_at", "subject", "session_ids",
        "deliverable_id", "files",
    ]
    assert names(sh.SessionHistory) == ["sid", "floor", "head", "window", "resolved", "reason"]
    assert isinstance(sh.SessionCommit.is_merge, property)
    assert isinstance(sh.SessionHistory.own, property)
    assert list(inspect.signature(sh.read_session_history).parameters) == ["root", "sid", "numstat"]
    for cls in (sh.FileChange, sh.SessionCommit, sh.SessionHistory):
        assert cls.__dataclass_params__.frozen
