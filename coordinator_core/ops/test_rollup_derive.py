"""
Tests for coordinator_core.ops.rollup_derive.

Port of: rollup-derive.sh (example-doctrine-repo b5a4192c, 2026-07-20).
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.rollup_derive import main


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    return repo


def _commit(repo, message: str) -> str:
    f = repo / "f.txt"
    f.write_text(f.read_text() + "x" if f.exists() else "x")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def test_no_args_prints_usage_to_stdout_exit_1(capsys):
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Usage: rollup-derive.sh <artifact-id>" in out


def test_help_prints_usage_to_stdout_exit_0(capsys):
    rc = main(["--help"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Usage: rollup-derive.sh <artifact-id>" in out


def test_empty_artifact_id_errors_exit_1(capsys):
    rc = main([""])
    captured = capsys.readouterr()
    assert rc == 1
    assert "artifact-id must not be empty" in captured.err
    assert captured.out == ""


def test_not_a_git_repo_unknown_error_exit_0(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = main(["some-id"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "unknown-error"
    assert "not inside a git repository" in captured.err


def test_no_resolving_commits_vacuous_pass(git_repo, monkeypatch, capsys):
    """AC14: rollup_derive's own zero-match case is per-query, not a scan --
    this token must stay a distinct, quiet value (never collapsed into
    not-shipped), which is exactly what the downstream promoter's own AC14
    split (test_promote_shipped_in_flight_stubs.py) depends on being able to
    read off stdout.
    """
    monkeypatch.chdir(git_repo)
    _commit(git_repo, "unrelated commit")
    rc = main(["fake-artifact-id"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "no-resolving-commits"


def test_substring_prefix_is_not_a_false_positive_match(git_repo, monkeypatch, capsys):
    """hnd-abc must NOT match a commit carrying Resolves: hnd-abc-def456 (F1/P1 fix)."""
    monkeypatch.chdir(git_repo)
    _commit(git_repo, "commit with longer id\n\nResolves: hnd-abc-def456\n")
    rc = main(["hnd-abc"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "no-resolving-commits"


def test_body_mention_without_trailer_does_not_resolve(git_repo, monkeypatch, capsys):
    """(c) A commit whose BODY merely mentions the artifact id, with no actual
    `Resolves:` trailer at the true trailer position, does NOT resolve.

    Regression guard (docs/plans/2026-08-01-baton-spine-information-integrity.md
    § A1 test (c)): the candidate stage (`--grep --fixed-strings`) is a bare
    substring match and WILL find this commit as a candidate (the literal text
    "Resolves: dlv-test-id-body-only" appears in the message) -- but the verify
    stage (`parse_resolves_trailer.run`, structured `git interpret-trailers`
    parse) must reject it, because the mention sits mid-paragraph, not in the
    trailer block at the end of the message. rollup_derive must therefore still
    report `no-resolving-commits`, never a false-positive `shipped`/`not-shipped`.
    """
    monkeypatch.chdir(git_repo)
    message = (
        "chore: reference an id in prose, not as a trailer\n\n"
        "Saw an old note mentioning Resolves: dlv-test-id-body-only in passing; "
        "unrelated to this diff and not intended to close it.\n\n"
        "No other changes.\n"
    )
    _f = git_repo / "f.txt"
    _f.write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=git_repo, check=True)

    rc = main(["dlv-test-id-body-only"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "no-resolving-commits"


def test_shipped_all_resolving_commits_on_origin_main(tmp_path, monkeypatch, capsys):
    bare = tmp_path / "bare_origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=work, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=work, check=True)

    monkeypatch.chdir(work)
    sha = _commit(work, "resolving commit\n\nResolves: test-artifact-1\n")
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=work, check=True)

    rc = main(["test-artifact-1"])
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert rc == 0
    assert lines[0] == "shipped"
    assert lines[1:] == [sha]


def test_not_shipped_resolving_commit_ahead_of_origin_main(tmp_path, monkeypatch, capsys):
    bare = tmp_path / "bare_origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=work, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=work, check=True)

    monkeypatch.chdir(work)
    _commit(work, "base commit")
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=work, check=True)
    sha = _commit(work, "local-only resolving commit\n\nResolves: test-artifact-2\n")

    rc = main(["test-artifact-2"])
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert rc == 0
    assert lines[0] == "not-shipped"
    assert lines[1:] == [sha]


def test_unknown_error_when_origin_main_missing(tmp_path, monkeypatch, capsys):
    """No 'origin' remote at all -> envelope.main rc=2 -> propagated as unknown-error, not not-shipped."""
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=work, check=True)

    monkeypatch.chdir(work)
    sha = _commit(work, "resolving commit\n\nResolves: test-artifact-3\n")

    rc = main(["test-artifact-3"])
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert rc == 0
    assert lines[0] == "unknown-error"
    assert lines[1:] == [sha]
