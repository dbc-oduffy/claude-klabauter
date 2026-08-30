"""
Tests for coordinator_core.ops.rollup_derive.

Port of: rollup-derive.sh (DoE b5a4192c, 2026-07-20).
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.rollup_derive import main
from coordinator_core.win_portability import (
    no_console_creationflags,
    no_console_passthrough_kwargs,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    return repo


def _commit(repo, message: str) -> str:
    f = repo / "f.txt"
    f.write_text(f.read_text() + "x" if f.exists() else "x")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True, **no_console_passthrough_kwargs())
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
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
    subprocess.run(["git", "add", "f.txt"], cwd=git_repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=git_repo, check=True, **no_console_passthrough_kwargs())

    rc = main(["dlv-test-id-body-only"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "no-resolving-commits"


def test_malformed_trailer_drop_is_named_on_stderr(git_repo, monkeypatch, capsys):
    """A `Resolves:` line outside the final trailer block is the one drop reason
    the caller can act on -- stdout keeps the quiet token, stderr names the count
    and the SHA so the reader does not conclude the primitive is broken.
    """
    monkeypatch.chdir(git_repo)
    message = (
        "close out the deliverable\n\n"
        "Resolves: dlv-lvv-09\n\n"
        "Co-Authored-By: Someone <someone@example.com>\n"
    )
    sha = _commit(git_repo, message)

    rc = main(["dlv-lvv-09"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "no-resolving-commits"
    assert "1 commit(s) name" in captured.err
    assert sha in captured.err


def test_prefix_sharing_drop_is_not_reported_on_stderr(git_repo, monkeypatch, capsys):
    """The prefix-sharing narrowing is correct behaviour, not a defect -- reporting
    it would train the reader to ignore the malformed-trailer line.
    """
    monkeypatch.chdir(git_repo)
    _commit(git_repo, "commit with longer id\n\nResolves: hnd-abc-def456\n")

    rc = main(["hnd-abc"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "no-resolving-commits"
    assert captured.err == ""


def test_true_zero_candidates_emits_no_diagnostic(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    _commit(git_repo, "unrelated commit")

    rc = main(["never-mentioned-id"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "no-resolving-commits"
    assert captured.err == ""


def test_shipped_all_resolving_commits_on_origin_main(tmp_path, monkeypatch, capsys):
    bare = tmp_path / "bare_origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=work, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "Test"], cwd=work, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=work, check=True, **no_console_passthrough_kwargs())

    monkeypatch.chdir(work)
    sha = _commit(work, "resolving commit\n\nResolves: test-artifact-1\n")
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=work, check=True, **no_console_passthrough_kwargs())

    rc = main(["test-artifact-1"])
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert rc == 0
    assert lines[0] == "shipped"
    assert lines[1:] == [sha]


def test_not_shipped_resolving_commit_ahead_of_origin_main(tmp_path, monkeypatch, capsys):
    bare = tmp_path / "bare_origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=work, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "Test"], cwd=work, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=work, check=True, **no_console_passthrough_kwargs())

    monkeypatch.chdir(work)
    _commit(work, "base commit")
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=work, check=True, **no_console_passthrough_kwargs())
    sha = _commit(work, "local-only resolving commit\n\nResolves: test-artifact-2\n")

    rc = main(["test-artifact-2"])
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert rc == 0
    assert lines[0] == "not-shipped"
    assert lines[1:] == [sha]


def test_multiple_resolving_commits_batched_order_preserved(git_repo, monkeypatch, capsys):
    """C19: several candidates resolve the SAME artifact-id -- the batched
    primary-trailer lookup must still find every one of them and preserve
    the original (git log --all, reverse-chron) candidate order, exactly
    as the old per-candidate loop did.
    """
    monkeypatch.chdir(git_repo)
    sha1 = _commit(git_repo, "first resolving commit\n\nResolves: multi-artifact-1\n")
    _commit(git_repo, "unrelated commit in between")
    sha2 = _commit(git_repo, "second resolving commit\n\nResolves: multi-artifact-1\n")

    rc = main(["multi-artifact-1"])
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert rc == 0
    assert lines[0] == "unknown-error"
    assert lines[1:] == [sha2, sha1]


def test_resolving_shas_batches_one_git_log_call_not_per_candidate(git_repo, monkeypatch):
    """C19: the primary-trailer lookup across N candidates must cost ONE
    `git log` spawn, not one per candidate -- pinning the fix against
    regressing back to the N-spawn shape.
    """
    from coordinator_core.ops import rollup_derive

    monkeypatch.chdir(git_repo)
    _commit(git_repo, "resolving 1\n\nResolves: spawn-count-artifact\n")
    _commit(git_repo, "resolving 2\n\nResolves: spawn-count-artifact\n")
    _commit(git_repo, "resolving 3\n\nResolves: spawn-count-artifact\n")

    calls = []
    real_run_git = rollup_derive._run_git

    def _counting_run_git(args):
        calls.append(args)
        return real_run_git(args)

    monkeypatch.setattr(rollup_derive, "_run_git", _counting_run_git)

    resolving = rollup_derive._resolving_shas("spawn-count-artifact")

    assert len(resolving) == 3
    batch_calls = [c for c in calls if "--no-walk=unsorted" in c]
    assert len(batch_calls) == 1


def test_unknown_error_when_origin_main_missing(tmp_path, monkeypatch, capsys):
    """No 'origin' remote at all -> resolvers.main rc=2 -> propagated as unknown-error, not not-shipped."""
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=work, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "Test"], cwd=work, check=True, **no_console_passthrough_kwargs())

    monkeypatch.chdir(work)
    sha = _commit(work, "resolving commit\n\nResolves: test-artifact-3\n")

    rc = main(["test-artifact-3"])
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert rc == 0
    assert lines[0] == "unknown-error"
    assert lines[1:] == [sha]
