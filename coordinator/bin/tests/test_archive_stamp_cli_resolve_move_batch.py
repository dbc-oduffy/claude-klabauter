"""resolve-move-memos: N inbox memos stamped with their own dispositions, moved to archive, no commit."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import unittest.mock
from pathlib import Path

import pytest

from coordinator_core import archive_stamp

_BIN_DIR = Path(__file__).resolve().parent.parent

_OPEN_MEMO = """\
---
kind: fyi
status: open
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""


def _load_cli():
    loader = importlib.machinery.SourceFileLoader(
        "archive_stamp_cli_resolve_move_test", str(_BIN_DIR / "archive-stamp-cli.py")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    inbox = root / "state" / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "work/test")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    for i in range(3):
        (inbox / f"m{i}.md").write_text(_OPEN_MEMO, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-1")
    return root


def _run(capsys, argv):
    with unittest.mock.patch.object(_cli, "_import_module", lambda: archive_stamp):
        rc = _cli.main(argv)
    return rc, capsys.readouterr()


def test_three_memos_stamped_moved_uncommitted_with_pathspec(repo, capsys):
    inbox = repo / "state" / "cross-repo" / "inbox"
    archive = repo / "state" / "cross-repo" / "archive"
    head = _git(repo, "rev-parse", "HEAD")
    argv = ["resolve-move-memos"]
    for i in range(3):
        argv += [
            "--memo", str(inbox / f"m{i}.md"),
            "--actioned-note", f"note number {i}",
            "--realized-by", f"abc123{i}",
        ]

    rc, out = _run(capsys, argv)

    assert rc == 0, out.err
    assert _git(repo, "rev-parse", "HEAD") == head
    assert not list(inbox.glob("*.md"))
    expected = []
    for i in range(3):
        text = (archive / f"m{i}.md").read_text(encoding="utf-8")
        assert "status: actioned" in text
        assert "decision: accepted" in text
        assert f"note number {i}" in text
        assert f"abc123{i}" in text
        expected += [f"state/cross-repo/inbox/m{i}.md", f"state/cross-repo/archive/m{i}.md"]
    assert out.out.splitlines() == expected


def test_pathspec_drives_one_scoped_commit(repo, capsys):
    inbox = repo / "state" / "cross-repo" / "inbox"
    _, out = _run(
        capsys,
        ["resolve-move-memos", "--memo", str(inbox / "m0.md"), "--actioned-note", "n", "--realized-by", "abc1234"],
    )
    _git(repo, "add", "-A", "--", *out.out.split())
    _git(repo, "commit", "-q", "-m", "land", "--", *out.out.split())
    assert _git(repo, "status", "--porcelain", "--", "state/cross-repo/archive/m0.md") == ""
    assert (inbox / "m1.md").exists()


def test_failed_memo_does_not_abort_others_and_exits_1(repo, capsys):
    inbox = repo / "state" / "cross-repo" / "inbox"
    archive = repo / "state" / "cross-repo" / "archive"
    rc, out = _run(
        capsys,
        [
            "resolve-move-memos",
            "--memo", str(inbox / "m0.md"), "--actioned-note", "n", "--realized-by", "abc1234",
            "--memo", str(inbox / "m1.md"),
            "--memo", str(inbox / "m2.md"), "--actioned-note", "n", "--realized-by", "abc1234",
        ],
    )
    assert rc == 1
    assert (archive / "m0.md").exists() and (archive / "m2.md").exists()
    assert (inbox / "m1.md").exists() and not (archive / "m1.md").exists()
    assert out.out.splitlines() == [
        "state/cross-repo/inbox/m0.md",
        "state/cross-repo/archive/m0.md",
        "state/cross-repo/inbox/m2.md",
        "state/cross-repo/archive/m2.md",
    ]


def test_memo_outside_inbox_is_refused_untouched(repo, capsys):
    stray = repo / "state" / "stray.md"
    stray.write_text(_OPEN_MEMO, encoding="utf-8")
    rc, out = _run(
        capsys,
        ["resolve-move-memos", "--memo", str(stray), "--actioned-note", "n", "--realized-by", "abc1234"],
    )
    assert rc == 1
    assert stray.read_text(encoding="utf-8") == _OPEN_MEMO
    assert out.out == ""


def test_requires_leading_memo_group(capsys):
    rc, _ = _run(capsys, ["resolve-move-memos", "a.md", "--actioned-note", "n"])
    assert rc == 2


def test_listed_in_usage_and_subcommand_table():
    assert "resolve-move-memos" in _cli._SUBCOMMANDS
    assert "resolve-move-memos" in _cli._SUBCOMMAND_USAGE
