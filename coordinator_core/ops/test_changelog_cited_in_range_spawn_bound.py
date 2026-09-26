from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import changelog_ops

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_repo_with_commits(tmp_path: Path, n: int) -> tuple[Path, list[str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    shas = []
    for i in range(n):
        (repo / "f.txt").write_text(f"v{i}\n", encoding="utf-8")
        _git(["add", "-A"], repo)
        _git(["commit", "-q", "-m", f"c{i}"], repo)
        shas.append(_git(["rev-parse", "HEAD"], repo).stdout.strip())
    return repo, shas


def _count_subprocess_run_calls(fn):
    calls = {"n": 0}
    orig = subprocess.run

    def _wrapper(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)

    changelog_ops.subprocess.run = _wrapper
    try:
        result = fn()
    finally:
        changelog_ops.subprocess.run = orig
    return calls["n"], result


def test_cited_in_range_count_spawns_once_regardless_of_token_count(tmp_path: Path) -> None:
    repo, shas = _init_repo_with_commits(tmp_path, 5)
    range_shas = shas[:3]
    decoys = ["deadbee", "cafebabe1234567", "0123456789abcdef0123456789abcdef01234567"]
    body = "cites " + " ".join(shas) + " and also " + " ".join(decoys)

    def _run():
        return changelog_ops._cited_in_range_count(repo, body, range_shas)

    n, cited_count = _count_subprocess_run_calls(_run)

    assert n == 1, "expected exactly one batched git subprocess, got %d" % n
    assert cited_count == 3, "expected all 3 in-range shas cited, got %d" % cited_count


def test_cited_in_range_count_matches_per_token_baseline(tmp_path: Path) -> None:
    repo, shas = _init_repo_with_commits(tmp_path, 4)
    range_shas = shas
    decoys = ["1234567", "abcdef0", "ffffffffffffffffffffffffffffffffffffff"]
    body = " ".join(shas + decoys)

    baseline_cited = set()
    import re

    tokens = {t.lower() for t in re.findall(r"\b[0-9a-fA-F]{7,40}\b", body)}
    range_set = set(range_shas)
    for tok in tokens:
        resolved = changelog_ops._git_lines_at(
            repo, ["rev-parse", "--verify", "-q", f"{tok}^{{commit}}"]
        )
        if resolved and resolved[0] in range_set:
            baseline_cited.add(resolved[0])

    batched_count = changelog_ops._cited_in_range_count(repo, body, range_shas)

    assert batched_count == len(baseline_cited)


def test_batch_resolve_commits_process_failure_unresolves_all_tokens(
    tmp_path: Path, monkeypatch
) -> None:
    repo, shas = _init_repo_with_commits(tmp_path, 2)

    orig = subprocess.run

    def _failing_run(*a, **kw):
        result = orig(*a, **kw)
        if a and a[0] and "cat-file" in a[0]:
            return subprocess.CompletedProcess(a[0], returncode=1, stdout="", stderr="simulated failure")
        return result

    monkeypatch.setattr(changelog_ops.subprocess, "run", _failing_run)

    resolved = changelog_ops._batch_resolve_commits(repo, [shas[0], shas[1]])

    assert resolved == {shas[0]: None, shas[1]: None}


def test_cited_in_range_count_empty_body_spawns_nothing(tmp_path: Path) -> None:
    repo, shas = _init_repo_with_commits(tmp_path, 1)

    def _run():
        return changelog_ops._cited_in_range_count(repo, "no tokens here", shas)

    n, cited_count = _count_subprocess_run_calls(_run)

    assert n == 0
    assert cited_count == 0
