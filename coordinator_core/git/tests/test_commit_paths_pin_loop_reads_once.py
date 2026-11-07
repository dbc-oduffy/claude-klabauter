"""One commit_paths pass opens each attribute file once, however many CR-bearing text-pinned paths it lands."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import call_io_scope, checkin_attrs
from coordinator_core.git.commit import commit_paths

pytestmark = pytest.mark.complete


def _git(root: Path, *args: str) -> None:
    env = dict(
        os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t.example", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t.example",
    )
    subprocess.run(
        ["git", "-C", str(root), *args], env=env, check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _repo(tmp_path: Path, n: int, attrs: str) -> list[str]:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "core.autocrlf", "false")
    (root / ".gitattributes").write_text(attrs, encoding="ascii", newline="\n")
    names = [f"d{i % 4}/f{i}.txt" for i in range(n)]
    for i, name in enumerate(names):
        (root / name).parent.mkdir(exist_ok=True)
        (root / name).write_bytes(f"a {i}\nb\n".encode())
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    for i, name in enumerate(names):
        (root / name).write_bytes(f"a {i}\r\nb\r\nc\r\n".encode())
    return names


def _count_attr_reads(monkeypatch) -> list[str]:
    reads: list[str] = []
    orig = call_io_scope._read

    def counting(path: str):
        if path.endswith("attributes") or path.endswith(".gitattributes"):
            reads.append(path)
        return orig(path)

    monkeypatch.setattr(call_io_scope, "_read", counting)
    return reads


@pytest.mark.parametrize("attrs", ["* text=auto\n", "* text\n", "* -text\n"])
def test_attribute_file_reads_do_not_grow_with_path_count(tmp_path, monkeypatch, attrs):
    names = _repo(tmp_path, 12, attrs)
    reads = _count_attr_reads(monkeypatch)
    commit_paths(tmp_path / "repo", names, "pin loop")
    small = len(reads)

    reads.clear()
    names2 = _repo(tmp_path / "again", 48, attrs)
    commit_paths(tmp_path / "again" / "repo", names2, "pin loop")
    # Slack for per-directory .gitattributes files the larger fixture adds (d0..d3 are shared); never per path.
    assert len(reads) <= small + 4, (small, len(reads))


def test_index_snapshot_requested_once_per_pass_not_per_path(tmp_path, monkeypatch):
    from coordinator_core.git import commit as commit_mod

    names = _repo(tmp_path, 24, "* text=auto\n")
    calls: list[int] = []
    orig = commit_mod.read_index

    def counting(*args, **kwargs):
        calls.append(1)
        return orig(*args, **kwargs)

    monkeypatch.setattr(commit_mod, "read_index", counting)
    commit_paths(tmp_path / "repo", names, "pin loop")
    assert len(calls) == 1, (len(calls), len(names))


def test_commit_blob_stays_the_git_blob(tmp_path):
    names = _repo(tmp_path, 3, "* text=auto\n")
    root = tmp_path / "repo"
    commit_paths(root, names, "pin loop")
    for name in names:
        done = subprocess.run(
            ["git", "-C", str(root), "show", f"HEAD:{name}"], capture_output=True, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert b"\r" not in done.stdout


def test_dispositions_unchanged_inside_a_scope(tmp_path):
    root = tmp_path / "r"
    (root / ".git").mkdir(parents=True)
    (root / "sub").mkdir()
    (root / ".gitattributes").write_text("* text=auto\n*.bin -text\n", newline="\n")
    (root / "sub" / ".gitattributes").write_text("*.cmd eol=crlf\n", newline="\n")
    cases = {"a.txt": checkin_attrs.TEXT, "a.bin": checkin_attrs.BINARY, "sub/x.cmd": checkin_attrs.TEXT}
    bare = {p: checkin_attrs.checkin_disposition(root, p) for p in cases}
    with call_io_scope.call_io_scope():
        scoped = {p: checkin_attrs.checkin_disposition(root, p) for p in cases}
        pin = checkin_attrs.resolve_text_pin(root, "sub/x.cmd")
    assert bare == scoped == cases
    assert pin == checkin_attrs.TextPin(checkin_attrs.TEXT_AUTO, "crlf")


@pytest.mark.parametrize(
    "attrs",
    [
        "* text=auto\n",
        "*.png filter=lfs diff=lfs merge=lfs -text\n",
        "*.txt filter=lfs\n",
        "[attr]lfs filter=lfs -text\n*.bin lfs\n",
        "sub/*.dat filter=x\n",
    ],
)
@pytest.mark.parametrize("rel", ["a.txt", "b.png", "sub/c.dat", "sub/deep/d.txt"])
def test_clean_filter_may_apply_matches_content_hash(tmp_path, attrs, rel):
    from coordinator_core.git import content_hash

    root = tmp_path / "r"
    (root / ".git").mkdir(parents=True)
    (root / "sub" / "deep").mkdir(parents=True)
    (root / ".gitattributes").write_text(attrs, newline="\n")
    (root / "sub" / ".gitattributes").write_text("*.txt filter=y\n", newline="\n")
    want = content_hash._clean_filter_may_apply(root, rel)
    assert checkin_attrs.clean_filter_may_apply(root, rel) == want
    with call_io_scope.call_io_scope():
        assert checkin_attrs.clean_filter_may_apply(root, rel) == want
