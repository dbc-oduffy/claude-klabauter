"""diff_commit_trees against `git diff-tree -r`, read_commit, and the publish trailer codec."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.git.git_state import (  # noqa: E402
    diff_commit_trees,
    format_publish_trailers,
    parse_publish_trailers,
    read_commit,
)
from coordinator_core.win_portability import no_console_creationflags  # noqa: E402

pytestmark = [pytest.mark.spawns_process]

SHA_A = "a" * 40
SHA_B = "b" * 64


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "core.filemode", "true")
    return tmp_path


def _commit(repo: Path, msg: str = "c") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _put(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _expected(repo: Path, a: str, b: str, *paths: str):
    out = _git(repo, "diff-tree", "-r", "--no-renames", "--raw", "--no-abbrev", a, b, *(["--", *paths] if paths else []))
    rows = []
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        om, nm, osha, nsha, st = meta.lstrip(":").split(" ")
        rows.append((st, path, osha, nsha, om, nm))
    return sorted(rows, key=lambda r: r[1])


def _actual(changes):
    z = "0" * 40
    return sorted(
        [
            (
                c.status,
                c.path,
                c.old_sha or z,
                c.new_sha or z,
                f"{c.old_mode or 0:06o}",
                f"{c.new_mode or 0:06o}",
            )
            for c in changes
        ],
        key=lambda r: r[1],
    )


def _norm(rows):
    # git reports a pure mode flip as M; the engine's contract says T.
    return [
        ("T" if st == "M" and om != nm else st, p, o, n, om, nm)
        for st, p, o, n, om, nm in rows
    ]


def test_diff_matches_git_for_add_modify_delete_rename_mode_and_nested(repo: Path):
    _put(repo, "keep.txt", "k")
    _put(repo, "mod.txt", "1")
    _put(repo, "gone.txt", "g")
    _put(repo, "ren_old.txt", "r")
    _put(repo, "run.sh", "#!/bin/sh\n")
    _put(repo, "deep/a/b/c.txt", "c")
    _put(repo, "deep/a/b/same.txt", "s")
    _put(repo, "untouched/x/y.txt", "y")
    old = _commit(repo)

    _put(repo, "mod.txt", "2")
    (repo / "gone.txt").unlink()
    (repo / "ren_old.txt").rename(repo / "ren_new.txt")
    _put(repo, "new.txt", "n")
    _put(repo, "deep/a/b/c.txt", "c2")
    _put(repo, "deep/a/b/added/z.txt", "z")
    _git(repo, "add", "-A")
    _git(repo, "update-index", "--chmod=+x", "run.sh")
    _git(repo, "commit", "-q", "-m", "c2")
    new = _git(repo, "rev-parse", "HEAD")

    got = diff_commit_trees(repo, old, new)
    assert got is not None
    assert _actual(got) == _norm(_expected(repo, old, new))
    paths = {c.path: c.status for c in got}
    assert paths["run.sh"] == "T"
    assert paths["ren_old.txt"] == "D" and paths["ren_new.txt"] == "A"
    assert not any(p.startswith("untouched") for p in paths)


def test_diff_dir_replaced_by_file_matches_git(repo: Path):
    _put(repo, "x/inner.txt", "i")
    old = _commit(repo)
    _git(repo, "rm", "-rq", "x")
    _put(repo, "x", "now a file")
    new = _commit(repo)
    got = diff_commit_trees(repo, old, new)
    assert _actual(got) == _norm(_expected(repo, old, new))


def test_diff_gitlink_is_a_leaf(repo: Path):
    _put(repo, "f.txt", "f")
    old = _commit(repo)
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{SHA_A},sub")
    _git(repo, "commit", "-q", "-m", "gitlink")
    new = _git(repo, "rev-parse", "HEAD")
    got = diff_commit_trees(repo, old, new)
    assert [(c.status, c.path, c.new_mode) for c in got] == [("A", "sub", 0o160000)]


def test_diff_unreadable_object_returns_none(repo: Path):
    _put(repo, "f.txt", "f")
    old = _commit(repo)
    assert diff_commit_trees(repo, old, SHA_A) is None


def test_diff_spawns_nothing(repo: Path, monkeypatch):
    _put(repo, "d/f.txt", "f")
    old = _commit(repo)
    _put(repo, "d/f.txt", "g")
    new = _commit(repo)
    spawns = []
    real = subprocess.Popen

    def counting(*a, **k):
        spawns.append(a)
        return real(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", counting)
    assert diff_commit_trees(repo, old, new) is not None
    assert read_commit(repo, new) is not None
    assert spawns == []


def test_read_commit_matches_git(repo: Path):
    _put(repo, "f.txt", "f")
    first = _commit(repo, "first")
    _put(repo, "f.txt", "g")
    second = _commit(repo, "second\n\nbody line")
    info = read_commit(repo, second)
    assert info.tree == _git(repo, "rev-parse", f"{second}^{{tree}}")
    assert info.parents == (first,)
    assert info.message.startswith("second\n\nbody line")
    assert read_commit(repo, first).parents == ()
    assert read_commit(repo, SHA_A) is None
    assert read_commit(repo, _git(repo, "rev-parse", f"{second}^{{tree}}")) is None


def test_trailer_round_trip():
    block = format_publish_trailers(round_id="r1", source_head=SHA_A, signature=SHA_B)
    assert block.startswith("\n\n")
    assert parse_publish_trailers("publish\n" + block) == ("r1", SHA_A, SHA_B)


def test_trailer_omits_falsy_and_parse_defaults_none():
    block = format_publish_trailers(round_id="r1", source_head=None, signature=None)
    assert parse_publish_trailers("s" + block) == ("r1", None, None)
    assert parse_publish_trailers("just a subject") == (None, None, None)


def test_parse_tolerates_trailing_session_id():
    msg = "publish" + format_publish_trailers(round_id="r1", source_head=SHA_A, signature=SHA_B)
    msg += "\n\nSession-Id: abc-123\n"
    assert parse_publish_trailers(msg) == ("r1", SHA_A, SHA_B)
    joined = msg.replace("\n\nSession-Id", "\nSession-Id")
    assert parse_publish_trailers(joined) == ("r1", SHA_A, SHA_B)
