from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.git.run import run_git  # noqa: E402
from percolate.peer_merge_regen import resolve_derived_after_merge  # noqa: E402
from percolate.round_derived import (  # noqa: E402
    EXPECTED_MANIFEST_REL,
    blob_sha,
    manifest_bytes,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

STAMP = "coordinator_core/_engine_stamp"
PUBLISHED = "coordinator_core/_engine_published_at"
CONTRACT = "coordinator_core/contract/a.md"


def _runner(repo: Path):
    def git(args):
        return run_git(
            ["-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
             "-c", "core.autocrlf=false", *args]
        )

    return git


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _manifest(repo: Path, source_head: str, paths: dict) -> None:
    _write(repo, EXPECTED_MANIFEST_REL, manifest_bytes(paths, source_head).decode("utf-8"))


def _commit(git, msg: str) -> None:
    assert git(["add", "-A"]).ok
    res = git(["commit", "-m", msg])
    assert res.ok, res.stderr


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git = _runner(r)
    assert git(["init", "-b", "main"]).ok
    _write(r, STAMP, "base\n")
    _write(r, PUBLISHED, "base\n")
    _write(r, CONTRACT, "base\n")
    _manifest(r, "base-head", {STAMP: blob_sha(b"base\n")})
    _commit(git, "base")
    assert git(["branch", "peer"]).ok
    return r, git


def _unmerged(git):
    return [p for p in git(["diff", "--name-only", "--diff-filter=U", "-z"]).stdout.split("\0") if p]


def test_derived_only_conflict_is_regenerated(repo):
    r, git = repo
    _write(r, STAMP, "ours\n")
    _write(r, PUBLISHED, "ours\n")
    _manifest(r, "ours-head", {STAMP: blob_sha(b"ours\n")})
    _commit(git, "ours")
    assert git(["checkout", "peer"]).ok
    _write(r, STAMP, "theirs\n")
    _write(r, PUBLISHED, "theirs\n")
    _manifest(r, "theirs-head", {STAMP: blob_sha(b"theirs\n")})
    _write(r, "peer.txt", "peer\n")
    _commit(git, "peer")
    assert git(["checkout", "main"]).ok
    assert git(["merge", "--no-commit", "--no-edit", "peer"]).returncode != 0
    assert set(_unmerged(git)) == {STAMP, PUBLISHED, EXPECTED_MANIFEST_REL}

    res = resolve_derived_after_merge(r, git)

    assert res.ok and res.unmerged_other == ()
    assert set(res.regenerated) == {STAMP, PUBLISHED, EXPECTED_MANIFEST_REL}
    assert (r / STAMP).read_bytes() == b"ours\n"
    assert (r / PUBLISHED).read_bytes() == b"ours\n"
    manifest = json.loads((r / EXPECTED_MANIFEST_REL).read_text("utf-8"))
    assert manifest["source_head"] == "ours-head"
    assert manifest["paths"]["peer.txt"] == blob_sha(b"peer\n")
    assert manifest["paths"][STAMP] == blob_sha(b"ours\n")
    assert EXPECTED_MANIFEST_REL not in manifest["paths"]
    assert _unmerged(git) == []
    assert git(["commit", "--no-edit"]).ok


def test_other_conflict_refuses_and_touches_nothing(repo):
    r, git = repo
    _write(r, STAMP, "ours\n")
    _write(r, CONTRACT, "ours\n")
    _commit(git, "ours")
    assert git(["checkout", "peer"]).ok
    _write(r, STAMP, "theirs\n")
    _write(r, CONTRACT, "theirs\n")
    _commit(git, "peer")
    assert git(["checkout", "main"]).ok
    assert git(["merge", "--no-commit", "--no-edit", "peer"]).returncode != 0
    before = git(["status", "--porcelain"]).stdout

    res = resolve_derived_after_merge(r, git)

    assert not res.ok
    assert res.unmerged_other == (CONTRACT,)
    assert git(["status", "--porcelain"]).stdout == before
    assert set(_unmerged(git)) == {STAMP, CONTRACT}


def test_clean_merge_still_regenerates_manifest(repo):
    r, git = repo
    _write(r, "ours.txt", "ours\n")
    _commit(git, "ours")
    assert git(["checkout", "peer"]).ok
    _write(r, "peer.txt", "peer\n")
    _commit(git, "peer")
    assert git(["checkout", "main"]).ok
    assert git(["merge", "--no-commit", "--no-edit", "peer"]).ok

    res = resolve_derived_after_merge(r, git)

    assert res.ok
    manifest = json.loads((r / EXPECTED_MANIFEST_REL).read_text("utf-8"))
    assert manifest["source_head"] == "base-head"
    assert manifest["paths"]["peer.txt"] == blob_sha(b"peer\n")
    assert manifest["paths"]["ours.txt"] == blob_sha(b"ours\n")
    assert git(["commit", "--no-edit"]).ok
