"""
Tests for coordinator_core.ops.migrate_cross_repo_layout.

Mirrors the bash oracle's own fixture-based test suite — same AC-8 scenarios
(standard migration, idempotent re-run, mixed tracked/untracked,
target-collision), ported to pytest against the Python module's main()
directly (subprocess only for the `git` calls the module itself performs —
matching the oracle's own reliance on a real git repo).

Port of: test-migrate-cross-repo-layout.sh (DoE 290997c7, 2026-07-22)
"""

from __future__ import annotations

import subprocess

from coordinator_core.ops.migrate_cross_repo_layout import main
from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope

import pytest
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(args, cwd):
    result = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True, **no_console_creationflags())
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _make_fixture_repo(tmp_path, n_flat=1, m_archive=2, include_dotfile=True):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)

    cross_repo = repo / "cross-repo"
    cross_repo.mkdir()
    (cross_repo / "README.md").write_text("# README\n", encoding="utf-8")
    for i in range(1, n_flat + 1):
        fname = f"2026-05-2{i}-test-memo-{i}.md"
        (cross_repo / fname).write_text(
            f"---\nfrom: other-em\nto: this-em\ntitle: Test memo {i}\nstatus: open\n---\nBody.\n",
            encoding="utf-8",
        )

    legacy_archive = repo / "archive" / "cross-repo"
    legacy_archive.mkdir(parents=True)
    for i in range(1, m_archive + 1):
        fname = f"2026-05-1{i}-archived-memo-{i}.md"
        (legacy_archive / fname).write_text(
            f"---\nfrom: other-em\nto: this-em\ntitle: Archived memo {i}\nstatus: actioned\n"
            "---\nBody.\n",
            encoding="utf-8",
        )
    if include_dotfile:
        (legacy_archive / ".dogfood-postscript.md").write_text(
            "# dogfood postscript\n", encoding="utf-8"
        )

    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "fixture: initial flat cross-repo layout"], repo)
    return repo


def test_standard_migration_moves_flat_and_archive_files(tmp_path, capsys):
    repo = _make_fixture_repo(tmp_path, n_flat=1, m_archive=2)

    rc = main(["--root", str(repo)])

    assert rc == 0
    out = capsys.readouterr().out

    flat_memo = "2026-05-21-test-memo-1.md"
    assert (repo / "cross-repo" / "inbox" / flat_memo).exists()
    assert not (repo / "cross-repo" / flat_memo).exists()
    assert (repo / "cross-repo" / "README.md").exists()

    for i in (1, 2):
        assert (repo / "cross-repo" / "archive" / f"2026-05-1{i}-archived-memo-{i}.md").exists()
    assert (repo / "cross-repo" / "archive" / ".dogfood-postscript.md").exists()

    assert not (repo / "archive" / "cross-repo").exists()

    assert "inbox moves:   1" in out
    assert "archive moves: 3" in out

    staged = _git(["diff", "--cached", "--name-only"], repo).stdout.strip()
    assert staged != ""


def test_idempotent_rerun_after_commit_is_noop(tmp_path, capsys):
    repo = _make_fixture_repo(tmp_path, n_flat=1, m_archive=2)
    main(["--root", str(repo)])
    _git(["commit", "-q", "-m", "migrate: cross-repo layout"], repo)

    capsys.readouterr()
    rc = main(["--root", str(repo)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "no-op" in out
    assert "inbox moves:   0" in out

    staged = _git(["diff", "--cached", "--name-only"], repo).stdout.strip()
    assert staged == ""


def test_mixed_tracked_and_untracked_archive_files(tmp_path, capsys):
    repo = _make_fixture_repo(tmp_path, n_flat=0, m_archive=1, include_dotfile=False)
    untracked = repo / "archive" / "cross-repo" / "2026-05-23-untracked-memo.md"
    untracked.write_text(
        "---\nfrom: other-em\nto: this-em\ntitle: Untracked memo\nstatus: actioned\n"
        "---\nBody.\n",
        encoding="utf-8",
    )
    # Deliberately NOT staged/committed — simulates the live central scenario
    # that regressed under a tracked-only assumption.

    rc = main(["--root", str(repo)])

    assert rc == 0
    assert (repo / "cross-repo" / "archive" / "2026-05-11-archived-memo-1.md").exists()
    assert (repo / "cross-repo" / "archive" / "2026-05-23-untracked-memo.md").exists()
    assert not (repo / "archive" / "cross-repo").exists()

    staged = _git(["diff", "--cached", "--name-only"], repo).stdout.strip()
    assert staged != ""


def test_target_collision_fails_loud_with_filename(tmp_path, capsys):
    repo = _make_fixture_repo(tmp_path, n_flat=1, m_archive=0, include_dotfile=False)

    inbox = repo / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    collision_file = inbox / "2026-05-21-test-memo-1.md"
    collision_file.write_text("pre-existing\n", encoding="utf-8")
    _git(["add", "cross-repo/inbox/2026-05-21-test-memo-1.md"], repo)
    _git(["commit", "-q", "-m", "fixture: pre-existing collision target"], repo)

    rc = main(["--root", str(repo)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "collision" in err or "already exists" in err
    assert "2026-05-21-test-memo-1.md" in err


def test_readme_never_moved(tmp_path, capsys):
    repo = _make_fixture_repo(tmp_path, n_flat=0, m_archive=0, include_dotfile=False)

    rc = main(["--root", str(repo)])

    assert rc == 0
    assert (repo / "cross-repo" / "README.md").exists()
    assert not (repo / "cross-repo" / "inbox" / "README.md").exists()


def test_not_a_git_repo_exits_1(tmp_path, capsys):
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()

    rc = main(["--root", str(plain_dir)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "not a git repository" in err


def test_nonexistent_root_exits_1(tmp_path, capsys):
    rc = main(["--root", str(tmp_path / "does-not-exist")])

    assert rc == 1
    err = capsys.readouterr().err
    assert "repo root does not exist" in err


def test_unknown_argument_exits_1(tmp_path, capsys):
    rc = main(["--bogus"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Unknown argument" in err


def test_root_flag_missing_value_exits_1(capsys):
    rc = main(["--root"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "--root requires a value" in err


def test_help_flag_exits_0(capsys):
    rc = main(["--help"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "migrate-cross-repo-layout" in out
    assert "Exit codes:" in out


def test_untracked_move_with_live_session_relocates_touch_claim(tmp_path, capsys, monkeypatch):
    """Review: code-reviewer — the other tests in this suite never resolve a
    live session, so they only exercise the plain-shutil.move fallback in
    `_move_one`'s untracked branch. This is the first to route through the
    real relocate_touched_path claiming path, modeled on
    coordinator_core/session/tests/test_claims.py::
    test_relocated_tracked_file_leaves_both_halves_claimed. Asserts through
    compute_offer, not touched.txt internals, so it genuinely fails if the
    untracked branch were reverted to a bare shutil.move.
    """
    repo = _make_fixture_repo(tmp_path, n_flat=0, m_archive=0, include_dotfile=False)
    src_rel = "archive/cross-repo/2026-05-23-untracked-memo.md"
    (repo / src_rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / src_rel).write_text(
        "---\nfrom: other-em\nto: this-em\ntitle: Untracked memo\nstatus: actioned\n"
        "---\nBody.\n",
        encoding="utf-8",
    )
    # Deliberately NOT staged/committed — the untracked branch of `_move_one`
    # is the one routed through `relocate_touched_path`; a tracked (`git mv`)
    # source never reaches it (that branch is out of scope for this finding).

    session_core.init("mine", cwd=str(repo))
    session_scope.touch("mine", src_rel, cwd=str(repo))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "mine")

    rc = main(["--root", str(repo)])

    assert rc == 0
    dest_rel = "cross-repo/archive/2026-05-23-untracked-memo.md"
    assert (repo / dest_rel).exists()
    assert not (repo / src_rel).exists()

    offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
    assert src_rel in offer["safe_paths"]
    assert dest_rel in offer["safe_paths"]


def test_no_op_when_nothing_to_migrate(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)

    rc = main(["--root", str(repo)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "no-op" in out


# ---------------------------------------------------------------------------
# Per-item git spawn amplification (coordinator_core/tests/
# test_no_unbatched_per_item_git_spawn.py _KNOWN_SITES:
# migrate_cross_repo_layout.py::main -> _move_one)
# ---------------------------------------------------------------------------


def test_process_count_does_not_grow_with_the_set(tmp_path, capsys, monkeypatch):
    """Model: test_schema_drift_watch.py::TestSchemaAdvisoryBatch::
    test_process_count_does_not_grow_with_the_set. Each phase's items share
    one constant destination directory, so the git spawn count for a phase
    must stay flat as its item count grows, not scale with it."""
    import coordinator_core.ops.migrate_cross_repo_layout as mcrl

    spawns: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
        spawns.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(mcrl.subprocess, "run", counting_run)

    (tmp_path / "small").mkdir()
    repo_small = _make_fixture_repo(tmp_path / "small", n_flat=1, m_archive=1)
    spawns.clear()
    rc_small = main(["--root", str(repo_small)])
    assert rc_small == 0
    spawns_for_small = len(spawns)

    (tmp_path / "large").mkdir()
    repo_large = _make_fixture_repo(tmp_path / "large", n_flat=4, m_archive=6)
    spawns.clear()
    rc_large = main(["--root", str(repo_large)])
    assert rc_large == 0
    spawns_for_large = len(spawns)

    assert spawns_for_large <= spawns_for_small, (
        f"spawn count grew with the item set: 2 items -> {spawns_for_small} spawns, "
        f"10 items -> {spawns_for_large} spawns"
    )
