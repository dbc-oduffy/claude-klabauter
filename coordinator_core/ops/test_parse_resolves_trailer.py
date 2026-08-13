"""
Tests for coordinator_core.ops.parse_resolves_trailer.

Mirrors the bash oracle case-for-case (zero / one / multiple / invalid-commit)
plus additional coverage for the case-insensitive fallback path and CLI
usage/exit-code parity.

Port of: parse-resolves-trailer.test.sh (example-doctrine-repo 3a561713, 2026-07-22)
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.parse_resolves_trailer import main, run

# Declared, not excused: this file spawns real git because the property under test
# is `Resolves:` trailer parsing off REAL commit metadata (`git log`/`git show`
# trailer extraction), which no mock stands in for -- the bash-oracle parity
# contract (parse-resolves-trailer.test.sh) it ports requires it. The `git_repo`
# fixture is per-test (function-scoped), and mutation (commit-per-case) needs that
# isolation, so it is not hoisted to module scope. The spawn ratchet's `_BASELINE`
# is shrink-only pre-existing residue and is explicitly not the route for this
# file -- coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    return repo


def _commit(repo, message: str) -> str:
    f = repo / "f.txt"
    f.write_text(message)
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def test_zero_trailers_empty_output_exit_0(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    sha = _commit(git_repo, "no trailer commit")
    lines, rc = run(sha)
    assert lines == []
    assert rc == 0


def test_one_trailer_extracted(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    sha = _commit(git_repo, "one trailer commit\n\nResolves: hnd-2026-07-08-abc123\n")
    lines, rc = run(sha)
    assert lines == ["hnd-2026-07-08-abc123"]
    assert rc == 0


def test_multiple_trailers_all_extracted(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    sha = _commit(
        git_repo,
        "multi trailer commit\n\nResolves: hnd-2026-07-08-abc123\nResolves: cmp-2026-07-08-def456\n",
    )
    lines, rc = run(sha)
    assert set(lines) == {"hnd-2026-07-08-abc123", "cmp-2026-07-08-def456"}
    assert len(lines) == 2
    assert rc == 0


def test_invalid_commit_nonzero_exit(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    lines, rc = run("not-a-real-commit-sha")
    assert lines == []
    assert rc == 2


def test_not_a_git_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lines, rc = run("HEAD")
    assert lines == []
    assert rc == 2


def test_fallback_case_insensitive_trailer(git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    sha = _commit(git_repo, "mixed case\n\nresolves: low-case-id\n")
    lines, rc = run(sha)
    assert lines == ["low-case-id"]
    assert rc == 0


def test_cli_no_args_usage_exit_1(capsys):
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 1
    assert "Usage: parse-resolves-trailer.sh <commit>" in captured.err


def test_cli_help_exit_0(capsys):
    rc = main(["--help"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Usage: parse-resolves-trailer.sh <commit>" in captured.out


def test_cli_prints_artifact_ids(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    sha = _commit(git_repo, "one trailer\n\nResolves: hnd-xyz\n")
    rc = main([sha])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == "hnd-xyz\n"
