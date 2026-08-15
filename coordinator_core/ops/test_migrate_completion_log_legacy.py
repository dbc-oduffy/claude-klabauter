"""
Tests for coordinator_core.ops.migrate_completion_log_legacy.

Mirrors the bash oracle's own smoke-test suite — same three AC scenarios plus
additional edge cases, ported to pytest against the Python module's main()
directly (subprocess only for the `git` calls the module itself performs —
matching the oracle's own reliance on a real git repo).

Port of: test-migrate-completion-log-legacy.sh (DoE 290997c7, 2026-07-22)
"""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.ops.migrate_completion_log_legacy import main

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(args, cwd):
    result = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _make_fixture_repo(tmp_path, n=3):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)

    completed = repo / "archive" / "completed"
    completed.mkdir(parents=True)

    for i in range(n):
        month = f"{i + 1:02d}"
        fname = f"2026-{month}.md"
        (completed / fname).write_text(f"# Legacy monolith {fname}\n", encoding="utf-8")
        _git(["add", f"archive/completed/{fname}"], repo)

    if n > 0:
        _git(["commit", "-q", "-m", "fixture: initial monolith files"], repo)
    return repo


def test_three_monoliths_produce_three_moves(tmp_path, capsys):
    repo = _make_fixture_repo(tmp_path, n=3)

    rc = main(["--root", str(repo)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Moved:  3" in out

    staged = _git(["diff", "--cached", "--name-only"], repo).stdout
    staged_legacy = [
        line for line in staged.splitlines() if "archive/completed/legacy/" in line
    ]
    assert len(staged_legacy) == 3

    for m in ("01", "02", "03"):
        assert not (repo / "archive" / "completed" / f"2026-{m}.md").exists()
        assert (repo / "archive" / "completed" / "legacy" / f"2026-{m}.md").exists()


def test_rerun_after_commit_is_idempotent_noop(tmp_path, capsys):
    repo = _make_fixture_repo(tmp_path, n=3)
    main(["--root", str(repo)])
    _git(["commit", "-q", "-m", "migrate: move monoliths to legacy/"], repo)

    capsys.readouterr()
    rc = main(["--root", str(repo)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Nothing to migrate" in out or "no-op" in out or "Moved:  0" in out

    staged = _git(["diff", "--cached", "--name-only"], repo).stdout.strip()
    assert staged == ""


def test_missing_archive_completed_exits_1_with_remediation(tmp_path, capsys):
    repo = tmp_path / "bare_repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)

    rc = main(["--root", str(repo)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Remediation:" in err


def test_zero_monoliths_present_is_clean_noop(tmp_path, capsys):
    repo = _make_fixture_repo(tmp_path, n=0)

    rc = main(["--root", str(repo)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Nothing to migrate" in out


def test_nonexistent_root_path_exits_1(tmp_path, capsys):
    rc = main(["--root", str(tmp_path / "does-not-exist")])

    assert rc == 1
    err = capsys.readouterr().err
    assert "archive/completed/ not found" in err


def test_partial_reentrant_run_skips_already_moved_files(tmp_path, capsys):
    """Simulates a Ctrl-C-interrupted partial run: one file already at the
    legacy/ destination (but source not removed, unlike a real git mv — this
    exercises the destination-exists skip guard directly)."""
    repo = _make_fixture_repo(tmp_path, n=2)
    legacy = repo / "archive" / "completed" / "legacy"
    legacy.mkdir()
    # Pre-seed the destination for file 1 to simulate a prior partial move,
    # without removing the source (mirrors "git mv already ran, commit
    # didn't" more loosely — the module's skip guard only checks dst existence).
    (legacy / "2026-01.md").write_text("# already moved\n", encoding="utf-8")

    rc = main(["--root", str(repo)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "SKIP (already at destination): 2026-01.md" in out
    assert "MOVE: archive/completed/2026-02.md" in out


def test_empty_root_flag_value_is_usage_error(tmp_path, capsys):
    """Review: code-reviewer — `--root ""` must not silently fall through to
    env/git-auto-discovery; it's an explicit usage error."""
    rc = main(["--root", ""])

    assert rc == 1
    err = capsys.readouterr().err
    assert "--root requires a non-empty value" in err


def test_empty_root_equals_form_is_usage_error(tmp_path, capsys):
    rc = main(["--root="])

    assert rc == 1
    err = capsys.readouterr().err
    assert "--root requires a non-empty value" in err


def test_env_var_root_used_when_no_explicit_flag(tmp_path, capsys, monkeypatch):
    repo = _make_fixture_repo(tmp_path, n=1)
    monkeypatch.setenv("MIGRATION_REPO_ROOT", str(repo))

    rc = main([])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Moved:  1" in out
