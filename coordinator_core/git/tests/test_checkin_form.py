"""checkin_form returns the bytes git's checkin filter hashes, and None for refused shapes."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.commit import checkin_form
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd, **kw) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, **no_console_creationflags(), **kw
    ).stdout.decode().strip()


def _repo(tmp_path: Path, attrs: bytes | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["config", "core.autocrlf", "true"], repo)
    if attrs is not None:
        (repo / ".gitattributes").write_bytes(attrs)
    return repo


def test_crlf_becomes_lf_and_matches_git(tmp_path):
    repo = _repo(tmp_path)
    data = b"one\r\ntwo\r\n"
    out = checkin_form(repo, "a.py", data)
    assert out == b"one\ntwo\n"
    sha = hashlib.sha1(b"blob %d\0" % len(out) + out).hexdigest()
    assert sha == _git(["hash-object", "--stdin", "--path=a.py"], repo, input=data)


def test_lf_unchanged(tmp_path):
    repo = _repo(tmp_path)
    assert checkin_form(repo, "a.py", b"one\ntwo\n") == b"one\ntwo\n"


def test_minus_text_path_keeps_raw_bytes(tmp_path):
    repo = _repo(tmp_path, attrs=b"*.dat -text\n")
    data = b"one\r\ntwo\r\n"
    assert checkin_form(repo, "x.dat", data) == data


def test_text_eol_crlf_with_cr_bytes_refused(tmp_path):
    repo = _repo(tmp_path, attrs=b"*.txt text eol=crlf\n")
    assert checkin_form(repo, "x.txt", b"one\r\ntwo\r\n") is None
