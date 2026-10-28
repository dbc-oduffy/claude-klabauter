"""Pins the two landing refusal kinds: secret-in-diff (scan over added lines) and destructive-git."""

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import landing_refusal_kinds as kinds
from coordinator_core.ops.dispatch_emit import landing_secrets as ls
from coordinator_core.win_portability import no_console_creationflags

PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root, *args):
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "ok.txt").write_text("hello\n")
    (tmp_path / "old.txt").write_text(f"token = {GH}\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def test_kinds_are_in_the_closed_table():
    assert {ls.SECRET_IN_DIFF, ls.DESTRUCTIVE_GIT} == kinds.LANDING_REFUSAL_KINDS


def test_added_pem_key_is_refused(repo):
    (repo / "new.txt").write_text(f"{PEM}\nabc\n")
    found = ls.scan(repo, ["new.txt"])
    assert [(f.path, f.line, f.label) for f in found] == [("new.txt", 1, "private-key")]
    assert "secret-in-diff" in ls.refusal(found)


def test_added_token_line_in_tracked_file_is_refused(repo):
    (repo / "ok.txt").write_text(f"hello\nkey: {GH}\n")
    found = ls.scan(repo, ["ok.txt"])
    assert [(f.line, f.label) for f in found] == [(2, "github-token")]


def test_token_already_in_head_is_not_added(repo):
    (repo / "old.txt").write_text(f"token = {GH}\nmore\n")
    assert ls.scan(repo, ["old.txt"]) == []


def test_clean_diff_lands(repo):
    (repo / "ok.txt").write_text("hello\nworld\n")
    assert ls.scan(repo, ["ok.txt", "missing.txt"]) == []
    assert ls.refusal([]) is None


def test_binary_file_is_not_judged(repo):
    (repo / "b.bin").write_bytes(b"\0" + PEM.encode())
    assert ls.scan(repo, ["b.bin"]) == []


def test_oversize_file_is_not_judged(repo):
    (repo / "big.txt").write_text(f"{PEM}\n" + "x" * (ls._MAX_SCAN_BYTES + 1))
    assert ls.scan(repo, ["big.txt"]) == []


@pytest.mark.parametrize("argv", [
    ["git", "reset", "--hard", "HEAD~1"],
    ["git", "push", "--force", "origin", "main"],
    ["git", "branch", "-D", "x"],
    ["git", "update-ref", "-d", "refs/heads/x"],
    ["git", "filter-branch"],
    ["git", "commit", "--amend"],
    ["git", "-C", "/r", "reset", "--hard"],
    ["git", "reflog", "expire", "--all"],
])
def test_destructive_git_is_refused(argv):
    assert "destructive-git" in ls.destructive_refusal(argv)


@pytest.mark.parametrize("argv", [
    ["git", "commit", "-m", "x"], ["git", "reset", "--soft", "HEAD~1"], ["git", "push", "origin", "main"], ["git"],
])
def test_non_destructive_git_passes(argv):
    assert ls.destructive_refusal(argv) is None
