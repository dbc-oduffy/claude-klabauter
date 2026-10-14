from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import reconcile_manifest
from coordinator_core.ops import workday_start_step0_reconcile as step0
from coordinator_core.ops.workday_start_step0_reconcile import main
from coordinator_core.win_portability import no_console_creationflags, no_console_passthrough_kwargs

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )


def _make_origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True, **no_console_passthrough_kwargs())

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, **no_console_passthrough_kwargs())
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    _git(clone, "checkout", "-q", "-B", "main")
    (clone / "README.md").write_text("hello\n")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-q", "-m", "initial")
    _git(clone, "push", "-q", "-u", "origin", "main")
    return origin, clone


def test_not_a_git_repo_is_hard_error(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main([])
    err = capsys.readouterr().err
    assert rc != 0
    assert "not a git repository" in err.lower()


def test_already_current_when_origin_main_is_ancestor(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)
    _git(clone, "checkout", "-q", "-b", "work/testmachine/2026-01-01")

    monkeypatch.chdir(clone)
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ALREADY-CURRENT branch=work/testmachine/2026-01-01" in out


def test_reconciled_ff_when_origin_advances_cleanly(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)
    _git(clone, "checkout", "-q", "-b", "work/testmachine/2026-01-01")

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True, **no_console_passthrough_kwargs())
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    _git(other, "checkout", "-q", "-B", "main", "origin/main")
    (other / "upstream.txt").write_text("more\n")
    _git(other, "add", "upstream.txt")
    _git(other, "commit", "-q", "-m", "upstream advance")
    _git(other, "push", "-q", "origin", "main")

    monkeypatch.chdir(clone)
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "RECONCILED-FF branch=work/testmachine/2026-01-01" in out
    head = _git(clone, "rev-parse", "HEAD").stdout.strip()
    origin_head = _git(clone, "rev-parse", "origin/main").stdout.strip()
    assert head == origin_head


def test_reconciled_merge_when_both_sides_advance_without_conflict(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)
    _git(clone, "checkout", "-q", "-b", "work/testmachine/2026-01-01")
    (clone / "local-only.txt").write_text("local\n")
    _git(clone, "add", "local-only.txt")
    _git(clone, "commit", "-q", "-m", "local-only work")

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True, **no_console_passthrough_kwargs())
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    _git(other, "checkout", "-q", "-B", "main", "origin/main")
    (other / "upstream.txt").write_text("more\n")
    _git(other, "add", "upstream.txt")
    _git(other, "commit", "-q", "-m", "upstream advance")
    _git(other, "push", "-q", "origin", "main")

    monkeypatch.chdir(clone)
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "RECONCILED-MERGE branch=work/testmachine/2026-01-01" in out
    assert (clone / "upstream.txt").exists()
    assert (clone / "local-only.txt").exists()


def test_reconcile_conflict_holds_merge_open_and_writes_manifest(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)
    _git(clone, "checkout", "-q", "-b", "work/testmachine/2026-01-01")
    (clone / "README.md").write_text("local edit\n")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-q", "-m", "local conflicting edit")

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True, **no_console_passthrough_kwargs())
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    _git(other, "checkout", "-q", "-B", "main", "origin/main")
    (other / "README.md").write_text("upstream edit\n")
    _git(other, "add", "README.md")
    _git(other, "commit", "-q", "-m", "upstream conflicting edit")
    _git(other, "push", "-q", "origin", "main")

    monkeypatch.chdir(clone)
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 3
    assert captured.out.strip().splitlines()[-1] == (
        "RECONCILE-CONFLICT-OPEN branch=work/testmachine/2026-01-01"
    )
    assert "CONFLICT-PATHS\nREADME.md" in captured.err

    merge_head = clone / ".git" / "MERGE_HEAD"
    assert merge_head.exists()
    manifest = reconcile_manifest.read(clone)
    assert manifest is not None
    assert manifest.paths == ("README.md",)
    assert manifest.merge_head == merge_head.read_text().strip()
    assert manifest.branch == "work/testmachine/2026-01-01"


def test_reconcile_conflict_without_parsed_paths_aborts(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)
    _diverge(tmp_path, origin, clone, "README.md", "upstream edit\n")
    (clone / "README.md").write_text("local edit\n")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-q", "-m", "local conflicting edit")
    monkeypatch.setattr(step0, "_conflict_paths", lambda _output: [])

    monkeypatch.chdir(clone)
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 3
    assert "RECONCILE-CONFLICT branch=work/testmachine/2026-01-01" in captured.out
    assert "RECONCILE-CONFLICT-OPEN" not in captured.out
    assert "Branch Reconciliation Decision" in captured.err
    assert not (clone / ".git" / "MERGE_HEAD").exists()
    assert reconcile_manifest.read(clone) is None
    assert _git(clone, "status", "--porcelain=v1").stdout.strip() == ""


def _diverge(tmp_path: Path, origin: Path, clone: Path, filename: str, upstream: str) -> None:
    _git(clone, "checkout", "-q", "-b", "work/testmachine/2026-01-01")
    (clone / "local-only.txt").write_text("local\n")
    _git(clone, "add", "local-only.txt")
    _git(clone, "commit", "-q", "-m", "local-only work")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True, **no_console_passthrough_kwargs())
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    _git(other, "checkout", "-q", "-B", "main", "origin/main")
    (other / filename).write_text(upstream)
    _git(other, "add", filename)
    _git(other, "commit", "-q", "-m", "upstream advance")
    _git(other, "push", "-q", "origin", "main")


def test_clean_index_prints_no_spool_line(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)
    _diverge(tmp_path, origin, clone, "upstream.txt", "more\n")
    monkeypatch.chdir(clone)
    assert main([]) == 0
    assert "SPOOL-COMMITTED" not in capsys.readouterr().out


def test_staged_spool_is_committed_then_merge_proceeds(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)
    _diverge(tmp_path, origin, clone, "upstream.txt", "more\n")
    spool = clone / "state" / "block-discharge"
    spool.mkdir(parents=True)
    (spool / "s1.jsonl").write_text("{}\n")
    (spool / "s2.jsonl").write_text("{}\n")
    _git(clone, "add", "state/block-discharge")

    monkeypatch.chdir(clone)
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.splitlines()[0] == "SPOOL-COMMITTED 2"
    assert "RECONCILED-MERGE" in out
    assert _git(clone, "status", "--porcelain=v1").stdout.strip() == ""
    subjects = _git(clone, "log", "--format=%s", "-3", "--no-merges").stdout
    assert "block-discharge: commit staged spool so reconcile can merge" in subjects


def test_spool_appended_after_staging_commits_worktree_bytes(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)
    _diverge(tmp_path, origin, clone, "upstream.txt", "more\n")
    spool = clone / "state" / "block-discharge"
    spool.mkdir(parents=True)
    (spool / "s1.jsonl").write_text("{}\n")
    _git(clone, "add", "state/block-discharge")
    with (spool / "s1.jsonl").open("a") as handle:
        handle.write('{"later": 1}\n')

    monkeypatch.chdir(clone)
    rc = main([])
    assert rc == 0
    assert "SPOOL-COMMITTED 1" in capsys.readouterr().out
    assert _git(clone, "status", "--porcelain=v1").stdout.strip() == ""
    assert '{"later": 1}' in _git(clone, "show", "HEAD^1:state/block-discharge/s1.jsonl").stdout


def test_staged_non_spool_path_is_named_and_nothing_committed(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)
    _diverge(tmp_path, origin, clone, "upstream.txt", "more\n")
    spool = clone / "state" / "block-discharge"
    spool.mkdir(parents=True)
    (spool / "s1.jsonl").write_text("{}\n")
    (clone / "peer.txt").write_text("peer\n")
    _git(clone, "add", "state/block-discharge", "peer.txt")
    head_before = _git(clone, "rev-parse", "HEAD").stdout

    monkeypatch.chdir(clone)
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 3
    assert "RECONCILE-MERGE-NOT-STARTED" in captured.out
    assert "SPOOL-COMMITTED" not in captured.out
    listed = captured.err.split("STAGED-PATHS\n", 1)[1].splitlines()
    assert sorted(listed[:2]) == ["peer.txt", "state/block-discharge/s1.jsonl"]
    assert _git(clone, "rev-parse", "HEAD").stdout == head_before


def test_staged_deletion_is_seen_as_a_staged_path(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)
    _diverge(tmp_path, origin, clone, "upstream.txt", "more\n")
    _git(clone, "rm", "-q", "README.md")

    monkeypatch.chdir(clone)
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 3
    assert "STAGED-PATHS\nREADME.md" in captured.err


def test_conflict_paths_are_listed_on_stderr(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)
    _diverge(tmp_path, origin, clone, "README.md", "upstream edit\n")
    (clone / "README.md").write_text("local edit\n")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-q", "-m", "local conflicting edit")

    monkeypatch.chdir(clone)
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 3
    assert "RECONCILE-CONFLICT-OPEN" in captured.out
    assert captured.err.split("CONFLICT-PATHS\n", 1)[1].splitlines()[0] == "README.md"


def test_reconcile_merge_commit_refused_by_hook_aborts_and_exits_3(tmp_path, capsys, monkeypatch):
    """A merge that would apply cleanly but whose commit is refused by a
    planted `pre-merge-commit` hook -- reaches the commit step (unlike
    `test_reconcile_conflict_aborts_and_exits_3`'s genuine content conflict)
    and must be classified `RECONCILE-MERGE-COMMIT-REFUSED`, not
    `RECONCILE-CONFLICT`."""
    origin, clone = _make_origin_and_clone(tmp_path)
    _git(clone, "checkout", "-q", "-b", "work/testmachine/2026-01-01")
    (clone / "local-only.txt").write_text("local\n")
    _git(clone, "add", "local-only.txt")
    _git(clone, "commit", "-q", "-m", "local-only work")

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True, **no_console_passthrough_kwargs())
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    _git(other, "checkout", "-q", "-B", "main", "origin/main")
    (other / "upstream.txt").write_text("more\n")
    _git(other, "add", "upstream.txt")
    _git(other, "commit", "-q", "-m", "upstream advance")
    _git(other, "push", "-q", "origin", "main")

    hooks_dir = clone / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook_path = hooks_dir / "pre-merge-commit"
    hook_path.write_text("#!/bin/sh\nexit 1\n")
    hook_path.chmod(0o755)

    monkeypatch.chdir(clone)
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 3
    assert "RECONCILE-MERGE-COMMIT-REFUSED branch=work/testmachine/2026-01-01" in captured.out
    assert "not the A/B/C" in captured.err

    status = _git(clone, "status", "--porcelain=v1")
    assert status.stdout.strip() == ""
    merge_head = clone / ".git" / "MERGE_HEAD"
    assert not merge_head.exists()
