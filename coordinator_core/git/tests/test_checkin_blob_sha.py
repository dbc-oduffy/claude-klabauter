"""checkin_blob_sha equals real `git hash-object` wherever it answers, and answers None where `_checkin_bytes` refuses."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.commit import checkin_blob_sha
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SHAPES = {
    "lf": b"one\ntwo\nthree\n",
    "crlf": b"one\r\ntwo\r\nthree\r\n",
    "mixed": b"a\r\nb\nc\rd\r\n",
    "lone_cr": b"a\rb\rc\r",
    "nul": b"bin\x00ary\x00\n",
    "nul_crlf": b"bin\x00\r\nary\r\n",
    "empty": b"",
}


def _git(args, cwd, **kw) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, **no_console_creationflags(), **kw
    ).stdout.decode().strip()


def _repo(tmp_path: Path, autocrlf, attrs=None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    if autocrlf is not None:
        _git(["config", "core.autocrlf", autocrlf], repo)
    if attrs is not None:
        (repo / ".gitattributes").write_bytes(attrs)
    return repo


def _real(repo: Path, name: str, data: bytes) -> str:
    return _git(["hash-object", "--stdin", f"--path={name}"], repo, input=data)


@pytest.mark.parametrize("autocrlf", [None, "false"], ids=["unset", "false"])
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_every_answer_equals_git_hash_object(tmp_path, autocrlf, shape):
    repo = _repo(tmp_path, autocrlf)
    (repo / "f.txt").write_bytes(SHAPES[shape])
    got = checkin_blob_sha(repo, "f.txt")
    if got is not None:
        assert got == _real(repo, "f.txt", SHAPES[shape])
    if b"\r" not in SHAPES[shape]:
        assert got is not None


@pytest.mark.parametrize("shape", ["crlf", "mixed", "lone_cr", "nul_crlf"])
def test_cr_bytes_under_a_not_true_autocrlf_give_none(tmp_path, shape):
    repo = _repo(tmp_path, "false")
    (repo / "f.txt").write_bytes(SHAPES[shape])
    assert checkin_blob_sha(repo, "f.txt") is None


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_text_auto_arm(tmp_path, shape):
    repo = _repo(tmp_path, "false", b"* text=auto\n")
    (repo / "f.txt").write_bytes(SHAPES[shape])
    got = checkin_blob_sha(repo, "f.txt")
    if got is not None:
        assert got == _real(repo, "f.txt", SHAPES[shape])
    if b"\r" not in SHAPES[shape]:
        assert got is not None


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_cmd_eol_pin_arm(tmp_path, shape):
    repo = _repo(tmp_path, "false", b"*.cmd text eol=crlf\n")
    (repo / "run.cmd").write_bytes(SHAPES[shape])
    got = checkin_blob_sha(repo, "run.cmd")
    if got is not None:
        assert got == _real(repo, "run.cmd", SHAPES[shape])
    if b"\r" in SHAPES[shape]:
        assert got is None
    else:
        assert got is not None


def test_clean_filter_path_gives_none(tmp_path):
    repo = _repo(tmp_path, "false", b"*.lfs filter=lfs\n")
    _git(["config", "filter.lfs.clean", "cat"], repo)
    (repo / "big.lfs").write_bytes(b"pointer me\n")
    assert checkin_blob_sha(repo, "big.lfs") is None


def test_unreadable_file_gives_none_and_nothing_is_written(tmp_path):
    repo = _repo(tmp_path, "false")
    (repo / "f.txt").write_bytes(b"x\n")
    before = sorted(p.relative_to(repo).as_posix() for p in (repo / ".git").rglob("*"))
    assert checkin_blob_sha(repo, "missing.txt") is None
    assert checkin_blob_sha(repo, "f.txt") is not None
    after = sorted(p.relative_to(repo).as_posix() for p in (repo / ".git").rglob("*"))
    assert before == after
