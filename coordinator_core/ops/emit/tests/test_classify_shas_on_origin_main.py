
from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.ops.emit.resolvers import classify_shas_on_origin_main, sha_on_origin_main
from coordinator_core.win_portability import no_console_creationflags

import pytest

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "commit.gpgsign", "false")


def _commit(repo: Path, message: str, content: str) -> str:
    (repo / "file.txt").write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_empty_input_returns_empty_dict_no_git_io(tmp_path: Path) -> None:
    assert classify_shas_on_origin_main(tmp_path, []) == {}


def test_three_way_classification_matches_per_sha_oracle(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    on_main_sha = _commit(tmp_path, "base", "base\n")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", on_main_sha)
    off_main_sha = _commit(tmp_path, "unmerged", "unmerged\n")
    bad_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

    shas = [on_main_sha, off_main_sha, bad_sha]
    batched = classify_shas_on_origin_main(tmp_path, shas)

    assert batched == {
        on_main_sha: True,
        off_main_sha: False,
        bad_sha: None,
    }

    for sha in shas:
        assert batched[sha] == sha_on_origin_main(tmp_path, sha), (
            f"batched result for {sha!r} diverges from the per-SHA sha_on_origin_main oracle"
        )


def test_duplicate_shas_collapse_to_one_entry(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    on_main_sha = _commit(tmp_path, "base", "base\n")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", on_main_sha)

    result = classify_shas_on_origin_main(tmp_path, [on_main_sha, on_main_sha, on_main_sha])

    assert result == {on_main_sha: True}


def test_no_origin_main_ref_degrades_every_sha_to_none(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "solo", "solo\n")

    result = classify_shas_on_origin_main(tmp_path, [sha])

    assert result == {sha: None}, (
        "an unreachable origin/main must degrade every sha to None (indeterminate), "
        "never silently read as False ('definitely not shipped')"
    )


def test_only_bad_object_among_valid_shas_isolates_correctly(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    on_main_sha = _commit(tmp_path, "base", "base\n")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", on_main_sha)
    off_main_sha = _commit(tmp_path, "unmerged", "unmerged\n")
    bad_sha = "0000000000000000000000000000000000dead"

    result = classify_shas_on_origin_main(tmp_path, [on_main_sha, bad_sha, off_main_sha])

    assert result[on_main_sha] is True
    assert result[off_main_sha] is False
    assert result[bad_sha] is None
