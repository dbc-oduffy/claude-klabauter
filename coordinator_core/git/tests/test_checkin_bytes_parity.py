"""checkin_sizes and the blobs commit_v2 lands equal real `git hash-object` on a CRLF autocrlf=true repo."""

from __future__ import annotations

import subprocess
from functools import partial
from pathlib import Path

import pytest

from coordinator_core.git.commit import checkin_sizes, hash_worktree_blobs_via_spawn
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

CRLF = b"line one\r\nline two\r\nline three\r\n"
MIXED = b"a\r\nb\nc\rd\r\n"

ARMS = {
    "no_attribute": (None, "f.txt", CRLF),
    "text_auto": (b"* text=auto\n", "f.txt", CRLF),
    "eol_crlf": (b"*.cmd text eol=crlf\n", "run.cmd", CRLF),
    "mixed_lone_cr": (None, "mixed.txt", MIXED),
    "mixed_lone_cr_text_auto": (b"* text=auto\n", "mixed.txt", MIXED),
}


def _git(args, cwd, **kw) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        **no_console_creationflags(), **kw,
    ).stdout.strip()


def _repo(tmp_path: Path, attrs, autocrlf: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["config", "core.autocrlf", autocrlf], repo)
    (repo / "seed.md").write_bytes(b"seed\n")
    _git(["add", "seed.md"], repo)
    _git(["commit", "-qm", "seed"], repo)
    if attrs is not None:
        (repo / ".gitattributes").write_bytes(attrs)
        _git(["add", ".gitattributes"], repo)
        _git(["commit", "-qm", "attrs"], repo)
    return repo


@pytest.mark.parametrize("arm", sorted(ARMS))
def test_sizes_and_landed_blob_equal_git(tmp_path, arm):
    attrs, name, data = ARMS[arm]
    repo = _repo(tmp_path, attrs, "true")
    (repo / name).write_bytes(data)

    want_sha = _git(["hash-object", "-w", name], repo)
    want_size = int(_git(["cat-file", "-s", want_sha], repo))

    sizes = checkin_sizes(
        repo, [name], blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=repo)
    )
    assert sizes == {name: want_size}

    result = commit_v2._handler({"paths": [name], "message": "land"}, repo_root=repo / ".git")
    assert result["committed"] is True
    landed = _git(["ls-tree", "HEAD", "--", name], repo).split()[2]
    assert landed == want_sha


def test_no_spawn_when_nothing_is_refused(tmp_path):
    repo = _repo(tmp_path, None, "true")
    (repo / "f.txt").write_bytes(CRLF)

    def boom(_paths):
        raise AssertionError("fallback spawned for an in-process shape")

    assert checkin_sizes(repo, ["f.txt"], blob_fallback=boom) == {"f.txt": len(CRLF.replace(b"\r\n", b"\n"))}


def test_missing_path_is_none(tmp_path):
    repo = _repo(tmp_path, None, "true")
    assert checkin_sizes(repo, ["gone.txt"], blob_fallback=lambda p: {}) == {"gone.txt": None}


def test_negative_arm_autocrlf_false_hashes_differently(tmp_path):
    on = _repo(tmp_path / "on", None, "true")
    off = _repo(tmp_path / "off", None, "false")
    (on / "f.txt").write_bytes(CRLF)
    (off / "f.txt").write_bytes(CRLF)
    assert _git(["hash-object", "f.txt"], on) != _git(["hash-object", "f.txt"], off)
