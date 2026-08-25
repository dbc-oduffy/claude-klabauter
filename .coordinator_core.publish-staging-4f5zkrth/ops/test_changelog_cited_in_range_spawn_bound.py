"""Spawn-count bound on `changelog_ops._cited_in_range_count`'s sha-token
resolution.

Guards the defect class where `_cited_in_range_count` spawned one `git
rev-parse --verify` subprocess per unique hex-looking token found in a
changelog body (fleet spawn census re-verification,
`state/audits/2026-08-15-fleet-census-reverification-at-head.md` §
"Rows surviving intact and unguarded" -- the strongest surviving unbounded
fan-out at that audit). This function is load-bearing, not diagnostic: its
count feeds `_content_gap_reason`, whose non-None return blocks changelog
injection -- so the FIX is a batched resolution shape, not a narrower scan.

Why a spawn COUNT and not a wall-clock threshold: this box averages 50-70
concurrent LLM sessions (CLAUDE.md load norm), so any elapsed-time assertion
is a flake generator. Spawn count is deterministic under load.

Spec backlink: coordinator_core/ops/changelog_ops.py::_cited_in_range_count,
::_batch_resolve_commits
"""
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
    """N distinct hex-looking tokens in the body (mix of real cited shas,
    real but out-of-range shas, and pure decoys) cost exactly ONE git
    subprocess, not one per token."""
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
    """Byte-parity: the batched result must equal what the old per-token
    `rev-parse --verify` loop would classify -- same cited set, decoys stay
    unresolved."""
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
    """Review: code-reviewer (F2, P2) — a non-zero `cat-file --batch-check`
    exit degrades the WHOLE token set to unresolved (`{tok: None for tok in
    tokens}`), not just the one that triggered the failure. This is the
    intended fail-closed direction (an entire-body cat-file hiccup should
    block changelog injection via `_content_gap_reason`, same as an
    unreadable body), but was previously untested — pins it."""
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
    """No hex-looking tokens at all: zero git subprocesses (`_batch_resolve_
    commits` short-circuits on an empty token list before spawning)."""
    repo, shas = _init_repo_with_commits(tmp_path, 1)

    def _run():
        return changelog_ops._cited_in_range_count(repo, "no tokens here", shas)

    n, cited_count = _count_subprocess_run_calls(_run)

    assert n == 0
    assert cited_count == 0
