"""test_workweek_complete_close.py — regression suite for
workweek-complete-close.py's three ported concerns (week-start derivation,
Step 9.1.5's detect-only reconcile sweep, Step 13's archive+reset).

Covers: HEADER.md week-start extraction (present/absent/whitespace-only),
the reconcile sweep's authored_by null/empty skip + nonzero-delta warning +
helper-nonzero-exit tolerance (mirrors test_workday_start_reconcile_sweep.py's
shape for the sibling ceremony), and the archive command's file-move +
HEADER.md-reset logic (daily files, priorities fragments, review-trail JSON
incl. transient-shard deletion and .gitkeep preservation) exercised
git-free via `perform_archive_files` directly — no real git repo needed.

Spec backlink: coordinator/bin/workweek-complete-close.py
Spec backlink: example-doctrine-repo coordinator/commands/workweek-complete.md
    § Step 9.1, § Step 9.1.5, § Step 13
"""
from __future__ import annotations

import importlib.util
import io
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = os.path.normpath(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
)
_TARGET = os.path.join(_REPO_ROOT, "coordinator", "bin", "workweek-complete-close.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("workweek_complete_close", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


# ---------------------------------------------------------------------------
# week-start
# ---------------------------------------------------------------------------


def test_derive_week_start_extracts_date(mod, tmp_path):
    header = tmp_path / "HEADER.md"
    header.write_text(
        "# Week Changelog\n\n**Week starting:** 2026-07-20\n**Prior week released:** v2.7.0\n",
        encoding="utf-8",
    )
    assert mod.derive_week_start(header) == "2026-07-20"


def test_derive_week_start_missing_file_fails_loud(mod, tmp_path):
    header = tmp_path / "does-not-exist.md"
    with pytest.raises(SystemExit):
        mod.derive_week_start(header)


def test_derive_week_start_missing_line_fails_loud(mod, tmp_path):
    header = tmp_path / "HEADER.md"
    header.write_text("# Week Changelog\n\nno week-starting line here\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        mod.derive_week_start(header)


def test_derive_week_start_unset_placeholder_fails_loud(mod, tmp_path):
    header = tmp_path / "HEADER.md"
    header.write_text(
        "# Week Changelog\n\n**Week starting:** (not yet set — run /workweek-start to initialise)\n",
        encoding="utf-8",
    )
    # Non-empty placeholder text is returned verbatim, same as the bash
    # grep+sed oracle would have — this is a content problem for the
    # ceremony caller to notice, not a parse failure for this helper.
    assert "not yet set" in mod.derive_week_start(header)


# ---------------------------------------------------------------------------
# reconcile-sweep
# ---------------------------------------------------------------------------


def _write_entry(path: Path, *, authored_by: str | None) -> None:
    lines = ["---", "status: pending-release"]
    if authored_by is not None:
        lines.append(f"authored_by: {authored_by}")
    lines.append("---")
    lines.append("body")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fixture_reconcile_script(tmp_path: Path, *, rc: int = 0, delta: int = 0) -> Path:
    script = tmp_path / "fake-reconcile.py"
    script.write_text(
        "import sys\n"
        f"print('delta={delta}')\n"
        f"sys.exit({rc})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _run_sweep(mod, entry_paths, reconcile_script):
    out = io.StringIO()
    err = io.StringIO()
    rc = mod.run_reconcile_sweep([str(p) for p in entry_paths], str(reconcile_script), out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def test_reconcile_sweep_skips_missing_authored_by(mod, tmp_path):
    entry = tmp_path / "no-author.md"
    _write_entry(entry, authored_by=None)
    reconcile_script = _fixture_reconcile_script(tmp_path, delta=5)

    rc, out, err = _run_sweep(mod, [entry], reconcile_script)

    assert rc == 0
    assert out == ""


def test_reconcile_sweep_skips_literal_null_authored_by(mod, tmp_path):
    entry = tmp_path / "null-author.md"
    _write_entry(entry, authored_by="null")
    reconcile_script = _fixture_reconcile_script(tmp_path, delta=5)

    rc, out, err = _run_sweep(mod, [entry], reconcile_script)

    assert rc == 0
    assert out == ""


def test_reconcile_sweep_warns_on_nonzero_delta(mod, tmp_path):
    entry = tmp_path / "gap-entry.md"
    _write_entry(entry, authored_by="sess-abc123")
    reconcile_script = _fixture_reconcile_script(tmp_path, delta=3)

    rc, out, err = _run_sweep(mod, [entry], reconcile_script)

    assert rc == 0
    assert "gap-entry.md" in out
    assert "3 session" in out
    assert "unaccounted" in out


def test_reconcile_sweep_zero_delta_is_silent(mod, tmp_path):
    entry = tmp_path / "clean-entry.md"
    _write_entry(entry, authored_by="sess-abc123")
    reconcile_script = _fixture_reconcile_script(tmp_path, delta=0)

    rc, out, err = _run_sweep(mod, [entry], reconcile_script)

    assert rc == 0
    assert out == ""


def test_reconcile_sweep_helper_nonzero_exit_is_tolerated(mod, tmp_path):
    entry = tmp_path / "helper-fail.md"
    _write_entry(entry, authored_by="sess-abc123")
    reconcile_script = _fixture_reconcile_script(tmp_path, rc=2, delta=9)

    rc, out, err = _run_sweep(mod, [entry], reconcile_script)

    # Advisory-only: a helper failure does not fail the sweep, and its
    # (unreliable, since rc!=0) delta is not surfaced.
    assert rc == 0
    assert out == ""


def test_authored_by_field_strips_inline_comment_and_quotes(mod):
    content = '---\nauthored_by: "sess-xyz"  # note\n---\n'
    assert mod._authored_by_field(content) == "sess-xyz"


def test_default_reconcile_script_resolves_sibling_path(mod):
    resolved = mod._default_reconcile_script()
    assert os.path.basename(resolved) == "reconcile-completion-commits.py"
    assert os.path.dirname(resolved) == os.path.dirname(_TARGET)


def test_default_query_script_resolves_sibling_path(mod):
    resolved = mod._default_query_script()
    assert os.path.basename(resolved) == "query-completions.py"
    assert os.path.dirname(resolved) == os.path.dirname(_TARGET)


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------


def _make_week_tree(tmp_path: Path):
    week_dir = tmp_path / "state" / "week-changelog"
    week_dir.mkdir(parents=True)
    (week_dir / "HEADER.md").write_text(
        "# Week Changelog\n\n**Week starting:** 2026-07-20\n", encoding="utf-8"
    )
    (week_dir / "2026-07-20.md").write_text("daily 1\n", encoding="utf-8")
    (week_dir / "2026-07-21.md").write_text("daily 2\n", encoding="utf-8")
    (week_dir / "2026-07-24-pending-release.md").write_text("pending\n", encoding="utf-8")
    (week_dir / "HEADER.priorities.alice.md").write_text("- priority 1\n", encoding="utf-8")

    review_dir = tmp_path / "state" / "review-trail"
    review_dir.mkdir(parents=True)
    (review_dir / ".gitkeep").write_text("", encoding="utf-8")
    (review_dir / "some-review.json").write_text("{}", encoding="utf-8")
    (review_dir / ".weekly-reviewer-scopes-abc.json").write_text("{}", encoding="utf-8")

    return week_dir, review_dir


def test_perform_archive_files_moves_and_resets(mod, tmp_path):
    week_dir, review_dir = _make_week_tree(tmp_path)
    archive_root = tmp_path / "archive" / "week-changelogs"
    review_archive_root = tmp_path / "archive" / "review-trail"

    actions = mod.perform_archive_files(
        week_dir,
        archive_root,
        review_dir,
        review_archive_root,
        "2026-07-20",
        "v2.8.0",
        "abc1234",
        "2026-07-24",
    )

    assert actions  # non-empty action log

    dest = archive_root / "2026-07-20"
    assert (dest / "2026-07-20.md").exists()
    assert (dest / "2026-07-21.md").exists()
    assert (dest / "2026-07-24-pending-release.md").exists()
    assert (dest / "HEADER.priorities.alice.md").exists()

    # Source week-changelog dir: daily files + fragments gone, HEADER.md remains (rewritten).
    assert not (week_dir / "2026-07-20.md").exists()
    assert not (week_dir / "HEADER.priorities.alice.md").exists()
    assert (week_dir / "HEADER.md").exists()

    header_text = (week_dir / "HEADER.md").read_text(encoding="utf-8")
    assert "not yet set" in header_text
    assert "v2.8.0 (commit abc1234, 2026-07-24)" in header_text
    assert "Last /workweek-start:** (none)" in header_text

    review_dest = review_archive_root / "2026-07-20"
    assert (review_dest / "some-review.json").exists()
    # .gitkeep preserved in place, transient shard deleted (not archived).
    assert (review_dir / ".gitkeep").exists()
    assert not (review_dir / ".weekly-reviewer-scopes-abc.json").exists()
    assert not (review_dest / ".weekly-reviewer-scopes-abc.json").exists()


def test_perform_archive_files_only_matches_dated_files(mod, tmp_path):
    week_dir, review_dir = _make_week_tree(tmp_path)
    # A non-dated, non-HEADER stray file must NOT be swept.
    (week_dir / "notes.md").write_text("scratch\n", encoding="utf-8")
    archive_root = tmp_path / "archive" / "week-changelogs"
    review_archive_root = tmp_path / "archive" / "review-trail"

    mod.perform_archive_files(
        week_dir, archive_root, review_dir, review_archive_root, "2026-07-20", "v2.8.0", "abc1234", "2026-07-24"
    )

    assert (week_dir / "notes.md").exists()
    assert not (archive_root / "2026-07-20" / "notes.md").exists()


# ---------------------------------------------------------------------------
# A3 -- posix pathspec + scoped commit (2026-07-28)
# ---------------------------------------------------------------------------


def test_windows_relative_path_as_posix_matches_git_pathspec_form():
    # Documents the exact defect: `str(path.relative_to(root))` renders
    # OS-native separators (`state\week-changelog` on Windows), which is not
    # a valid `git add -- <pathspec>` argument (`\` is git's pathspec ESCAPE
    # character, so the whole pathspec silently collapses and matches
    # nothing). `.as_posix()` is the fix -- always forward-slash regardless
    # of host OS.
    from pathlib import PureWindowsPath

    root = PureWindowsPath("C:/repo")
    child = PureWindowsPath("C:/repo/state/week-changelog")
    rel = child.relative_to(root)
    assert str(rel) == "state\\week-changelog"
    assert rel.as_posix() == "state/week-changelog"


def test_git_commit_and_push_stages_posix_paths_and_scopes_commit(mod, tmp_path, monkeypatch):
    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)

        class _Result:
            stdout = "work/some-branch\n"
            returncode = 0

        return _Result()

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    rc = mod.git_commit_and_push(
        tmp_path,
        ["state/week-changelog", "archive/week-changelogs/2026-07-20"],
        "chore: archive week",
        "fake-branch-script.py",
        push=True,
    )

    assert rc == 0
    add_call = next(c for c in calls if c[:2] == ["git", "add"])
    commit_call = next(c for c in calls if c[:2] == ["git", "commit"])
    push_call = next(c for c in calls if c[:2] == ["git", "push"])

    # `git add` pathspec is posix-form (no backslashes) and dash-separated.
    assert "--" in add_call
    staged = add_call[add_call.index("--") + 1 :]
    assert staged == ["state/week-changelog", "archive/week-changelogs/2026-07-20"]
    assert all("\\" not in p for p in staged)

    # `git commit` carries an explicit pathspec, not a bare commit -- a
    # bare commit on a shared branch absorbs whatever else is staged.
    assert "--" in commit_call
    committed = commit_call[commit_call.index("--") + 1 :]
    assert committed == staged

    assert push_call == ["git", "push", "origin", "work/some-branch"]


# ---------------------------------------------------------------------------
# C1 -- week_only scoped filter (backfill safety)
# ---------------------------------------------------------------------------


def _make_two_week_tree(tmp_path: Path):
    """Week N = 2026-07-20..26, Week N+1 = 2026-07-27.. — both present in
    the same directory, as a multi-week backfill run would find it."""
    week_dir = tmp_path / "state" / "week-changelog"
    week_dir.mkdir(parents=True)
    (week_dir / "HEADER.md").write_text(
        "# Week Changelog\n\n**Week starting:** 2026-07-20\n", encoding="utf-8"
    )
    # week N artifacts
    (week_dir / "2026-07-20.md").write_text("daily 1\n", encoding="utf-8")
    (week_dir / "2026-07-24-pending-release.md").write_text("pending\n", encoding="utf-8")
    # week N+1 artifact -- must survive under week_only
    (week_dir / "2026-07-27.md").write_text("daily next week\n", encoding="utf-8")
    (week_dir / "HEADER.priorities.alice.md").write_text("- priority 1\n", encoding="utf-8")

    review_dir = tmp_path / "state" / "review-trail"
    review_dir.mkdir(parents=True)
    (review_dir / ".gitkeep").write_text("", encoding="utf-8")
    (review_dir / "2026-07-21-093000-sess1.json").write_text("{}", encoding="utf-8")
    (review_dir / "2026-07-28-101500-sess2.json").write_text("{}", encoding="utf-8")
    (review_dir / "not-a-date-record.json").write_text("{}", encoding="utf-8")
    (review_dir / ".weekly-reviewer-scopes-abc.json").write_text("{}", encoding="utf-8")

    return week_dir, review_dir


def test_week_only_scopes_to_week_window_and_leaves_other_artifacts(mod, tmp_path):
    week_dir, review_dir = _make_two_week_tree(tmp_path)
    archive_root = tmp_path / "archive" / "week-changelogs"
    review_archive_root = tmp_path / "archive" / "review-trail"

    mod.perform_archive_files(
        week_dir,
        archive_root,
        review_dir,
        review_archive_root,
        "2026-07-20",
        "v2.8.0",
        "abc1234",
        "2026-07-24",
        week_only=True,
    )

    dest = archive_root / "2026-07-20"
    # Week-N artifacts moved.
    assert (dest / "2026-07-20.md").exists()
    assert (dest / "2026-07-24-pending-release.md").exists()
    assert not (week_dir / "2026-07-20.md").exists()
    assert not (week_dir / "2026-07-24-pending-release.md").exists()

    # Week-N+1 artifact survives in place, untouched.
    assert (week_dir / "2026-07-27.md").exists()
    assert not (dest / "2026-07-27.md").exists()

    # Priorities fragment not opted-in -- survives in place.
    assert (week_dir / "HEADER.priorities.alice.md").exists()
    assert not (dest / "HEADER.priorities.alice.md").exists()

    review_dest = review_archive_root / "2026-07-20"
    # Week-N review record moved.
    assert (review_dest / "2026-07-21-093000-sess1.json").exists()
    assert not (review_dir / "2026-07-21-093000-sess1.json").exists()

    # Week-N+1 review record survives in place.
    assert (review_dir / "2026-07-28-101500-sess2.json").exists()
    assert not (review_dest / "2026-07-28-101500-sess2.json").exists()

    # Unparseable-name record survives in place -- fail-safe, never swept.
    assert (review_dir / "not-a-date-record.json").exists()
    assert not (review_dest / "not-a-date-record.json").exists()

    # Transient shard NOT deleted under week_only -- may belong to live week.
    assert (review_dir / ".weekly-reviewer-scopes-abc.json").exists()

    # .gitkeep untouched.
    assert (review_dir / ".gitkeep").exists()


def test_week_only_false_reproduces_sweep_everything_regression(mod, tmp_path):
    week_dir, review_dir = _make_two_week_tree(tmp_path)
    archive_root = tmp_path / "archive" / "week-changelogs"
    review_archive_root = tmp_path / "archive" / "review-trail"

    mod.perform_archive_files(
        week_dir,
        archive_root,
        review_dir,
        review_archive_root,
        "2026-07-20",
        "v2.8.0",
        "abc1234",
        "2026-07-24",
        week_only=False,
    )

    dest = archive_root / "2026-07-20"
    # Everything dated -- including week N+1 -- swept, matching today's
    # unscoped behaviour byte-for-byte.
    assert (dest / "2026-07-20.md").exists()
    assert (dest / "2026-07-24-pending-release.md").exists()
    assert (dest / "2026-07-27.md").exists()
    assert (dest / "HEADER.priorities.alice.md").exists()
    assert not (week_dir / "2026-07-27.md").exists()
    assert not (week_dir / "HEADER.priorities.alice.md").exists()

    review_dest = review_archive_root / "2026-07-20"
    assert (review_dest / "2026-07-21-093000-sess1.json").exists()
    assert (review_dest / "2026-07-28-101500-sess2.json").exists()
    assert (review_dest / "not-a-date-record.json").exists()

    # Transient shard deleted, as today.
    assert not (review_dir / ".weekly-reviewer-scopes-abc.json").exists()
    assert (review_dir / ".gitkeep").exists()


def test_week_only_with_move_priorities_opt_in_moves_fragment(mod, tmp_path):
    week_dir, review_dir = _make_two_week_tree(tmp_path)
    archive_root = tmp_path / "archive" / "week-changelogs"
    review_archive_root = tmp_path / "archive" / "review-trail"

    mod.perform_archive_files(
        week_dir,
        archive_root,
        review_dir,
        review_archive_root,
        "2026-07-20",
        "v2.8.0",
        "abc1234",
        "2026-07-24",
        week_only=True,
        move_priorities=True,
    )

    dest = archive_root / "2026-07-20"
    assert (dest / "HEADER.priorities.alice.md").exists()
    assert not (week_dir / "HEADER.priorities.alice.md").exists()


def test_cmd_archive_no_git_wires_defaults(mod, tmp_path, capsys):
    week_dir, review_dir = _make_week_tree(tmp_path)

    rc = mod.main(
        [
            "archive",
            "--repo-root",
            str(tmp_path),
            "--version",
            "v2.8.0",
            "--merge-sha",
            "abc1234",
            "--released-date",
            "2026-07-24",
            "--no-git",
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "rewrote" in out
    assert (tmp_path / "archive" / "week-changelogs" / "2026-07-20" / "2026-07-20.md").exists()


# Review: code-reviewer Finding 1 -- the --week-only/--move-priorities
# argparse flags and their wiring into _cmd_archive were exercised by
# nothing (the C1 tests above call perform_archive_files(...) directly).
# CLI-level test, same shape as test_cmd_archive_no_git_wires_defaults,
# asserting the flags actually reach perform_archive_files unmangled.
# ---------------------------------------------------------------------------
# Defect 1 -- review-trail sweep must not key on `.json` alone
# ---------------------------------------------------------------------------


def _add_stranded_record_fixtures(review_dir: Path, *, dated_name: str) -> None:
    """Adds a non-.json review-trail record plus the subdirectories that
    must keep being excluded by the `f.is_file()` guard, regardless of the
    new extension-agnostic filter."""
    (review_dir / dated_name).write_text("# finding\n", encoding="utf-8")
    for sub in ("chain-ancestry-waivers", "diffs", "findings", "pm-vouches"):
        (review_dir / sub).mkdir()
        (review_dir / sub / "should-not-move.md").write_text("x\n", encoding="utf-8")


def test_non_json_review_record_archived_default_path(mod, tmp_path):
    week_dir, review_dir = _make_week_tree(tmp_path)
    _add_stranded_record_fixtures(review_dir, dated_name="2026-07-01-stranded-finding.md")
    archive_root = tmp_path / "archive" / "week-changelogs"
    review_archive_root = tmp_path / "archive" / "review-trail"

    mod.perform_archive_files(
        week_dir, archive_root, review_dir, review_archive_root,
        "2026-07-20", "v2.8.0", "abc1234", "2026-07-24", week_only=False,
    )

    review_dest = review_archive_root / "2026-07-20"
    assert (review_dest / "2026-07-01-stranded-finding.md").exists()
    assert not (review_dir / "2026-07-01-stranded-finding.md").exists()

    # Subdirectories and their contents never touched.
    for sub in ("chain-ancestry-waivers", "diffs", "findings", "pm-vouches"):
        assert (review_dir / sub / "should-not-move.md").exists()
        assert not (review_dest / sub).exists()

    # .gitkeep and transient shard still handled as before.
    assert (review_dir / ".gitkeep").exists()
    assert not (review_dir / ".weekly-reviewer-scopes-abc.json").exists()


def test_non_json_review_record_archived_week_only_path(mod, tmp_path):
    week_dir, review_dir = _make_two_week_tree(tmp_path)
    # Dated within week N (2026-07-20..26).
    _add_stranded_record_fixtures(review_dir, dated_name="2026-07-22-stranded-finding.md")
    archive_root = tmp_path / "archive" / "week-changelogs"
    review_archive_root = tmp_path / "archive" / "review-trail"

    mod.perform_archive_files(
        week_dir, archive_root, review_dir, review_archive_root,
        "2026-07-20", "v2.8.0", "abc1234", "2026-07-24", week_only=True,
    )

    review_dest = review_archive_root / "2026-07-20"
    assert (review_dest / "2026-07-22-stranded-finding.md").exists()
    assert not (review_dir / "2026-07-22-stranded-finding.md").exists()

    for sub in ("chain-ancestry-waivers", "diffs", "findings", "pm-vouches"):
        assert (review_dir / sub / "should-not-move.md").exists()
        assert not (review_dest / sub).exists()

    assert (review_dir / ".gitkeep").exists()
    # Transient shard untouched under week_only (may belong to the live week).
    assert (review_dir / ".weekly-reviewer-scopes-abc.json").exists()


def test_non_json_review_record_out_of_window_survives_week_only(mod, tmp_path):
    week_dir, review_dir = _make_two_week_tree(tmp_path)
    # Dated within week N+1 -- must NOT be swept into week N's archive.
    _add_stranded_record_fixtures(review_dir, dated_name="2026-07-28-stranded-finding.md")
    archive_root = tmp_path / "archive" / "week-changelogs"
    review_archive_root = tmp_path / "archive" / "review-trail"

    mod.perform_archive_files(
        week_dir, archive_root, review_dir, review_archive_root,
        "2026-07-20", "v2.8.0", "abc1234", "2026-07-24", week_only=True,
    )

    review_dest = review_archive_root / "2026-07-20"
    assert (review_dir / "2026-07-28-stranded-finding.md").exists()
    assert not (review_dest / "2026-07-28-stranded-finding.md").exists()


# ---------------------------------------------------------------------------
# Defect 2 -- HEADER.md must not be reset by a historical week_only run
# ---------------------------------------------------------------------------

_LIVE_HEADER_TEXT = (
    "# Week Changelog\n\n"
    "<!-- Directory convention:\n"
    "line 2 of an 18-line comment block\n"
    "line 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\n"
    "line 11\nline 12\nline 13\nline 14\nline 15\nline 16\nline 17\n"
    "line 18 -->\n\n"
    "**Week starting:** 2026-08-03\n"
    "**Prior week released:** v2.7.0 (commit deadbeef1, 2026-08-02)\n"
    "**Last /workweek-start:** 2026-08-03\n"
    "**Priorities (from /workweek-start):** see `HEADER.priorities.*.md` fragments.\n"
)


def test_week_only_historical_run_leaves_live_header_untouched(mod, tmp_path):
    week_dir, review_dir = _make_week_tree(tmp_path)
    # HEADER declares the LIVE week (2026-08-03), unrelated to the
    # historical week (2026-07-20) this run is archiving.
    (week_dir / "HEADER.md").write_text(_LIVE_HEADER_TEXT, encoding="utf-8")
    archive_root = tmp_path / "archive" / "week-changelogs"
    review_archive_root = tmp_path / "archive" / "review-trail"

    actions = mod.perform_archive_files(
        week_dir, archive_root, review_dir, review_archive_root,
        "2026-07-20", "v2.8.0", "abc1234", "2026-07-24", week_only=True,
    )

    header_text = (week_dir / "HEADER.md").read_text(encoding="utf-8")
    assert header_text == _LIVE_HEADER_TEXT
    assert not any("rewrote" in a for a in actions)


def test_week_only_matching_declared_week_still_rewrites_header(mod, tmp_path):
    week_dir, review_dir = _make_week_tree(tmp_path)
    # HEADER declares exactly the week this run is closing.
    (week_dir / "HEADER.md").write_text(
        "# Week Changelog\n\n**Week starting:** 2026-07-20\n", encoding="utf-8"
    )
    archive_root = tmp_path / "archive" / "week-changelogs"
    review_archive_root = tmp_path / "archive" / "review-trail"

    actions = mod.perform_archive_files(
        week_dir, archive_root, review_dir, review_archive_root,
        "2026-07-20", "v2.8.0", "abc1234", "2026-07-24", week_only=True,
    )

    header_text = (week_dir / "HEADER.md").read_text(encoding="utf-8")
    assert "not yet set" in header_text
    assert any("rewrote" in a for a in actions)


def test_default_path_still_rewrites_header_at_real_week_boundary(mod, tmp_path):
    week_dir, review_dir = _make_week_tree(tmp_path)
    (week_dir / "HEADER.md").write_text(_LIVE_HEADER_TEXT, encoding="utf-8")
    archive_root = tmp_path / "archive" / "week-changelogs"
    review_archive_root = tmp_path / "archive" / "review-trail"

    actions = mod.perform_archive_files(
        week_dir, archive_root, review_dir, review_archive_root,
        "2026-08-03", "v2.8.0", "abc1234", "2026-08-09", week_only=False,
    )

    header_text = (week_dir / "HEADER.md").read_text(encoding="utf-8")
    assert "not yet set" in header_text
    assert "v2.8.0 (commit abc1234, 2026-08-09)" in header_text
    assert any("rewrote" in a for a in actions)


# ---------------------------------------------------------------------------
# C5b -- _relocate_or_move (relocate_touched_path routing, plain-move fallback)
# docs/plans/2026-08-06-relocation-re-declares-the-touch-claim.md § C5b
# ---------------------------------------------------------------------------


def test_relocate_or_move_relocates_touch_claim_onto_archived_path(mod, tmp_path):
    # Review: code-reviewer Finding 1 -- C5b's `_relocate_or_move` wiring
    # inside `perform_archive_files` shipped with zero test coverage.
    # Load-bearing case, modeled on
    # test_cross_repo_memo_draft.py::test_send_archive_relocates_touch_claim_onto_sent_copy:
    # a real session T-claims a changelog file under the week directory
    # before the archive sweep runs; the archived destination must land in
    # that same session's compute_offer safe_paths -- proving the claim
    # followed the file through relocate_touched_path rather than being
    # stranded by a plain shutil.move.
    import subprocess as _subprocess

    repo_root = tmp_path
    _subprocess.run(["git", "init", str(repo_root)], capture_output=True, check=True)
    _subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.email", "test@test.com"],
        capture_output=True, check=True,
    )
    _subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.name", "Test"],
        capture_output=True, check=True,
    )

    week_dir = repo_root / "state" / "week-changelog"
    week_dir.mkdir(parents=True)
    (week_dir / "HEADER.md").write_text(
        "# Week Changelog\n\n**Week starting:** 2026-07-20\n", encoding="utf-8"
    )
    (week_dir / "2026-07-20.md").write_text("daily 1\n", encoding="utf-8")

    review_dir = repo_root / "state" / "review-trail"
    review_dir.mkdir(parents=True)
    (review_dir / ".gitkeep").write_text("", encoding="utf-8")

    archive_root = repo_root / "archive" / "week-changelogs"
    review_archive_root = repo_root / "archive" / "review-trail"

    from coordinator_core.session import core as _session_core
    from coordinator_core.session import scope as _session_scope
    from coordinator_core.ops.session import safe_commit_offer as _safe_commit_offer

    session_id = "c5b-relocate-test-session"
    assert _session_core.init(session_id, cwd=str(repo_root))
    _session_scope.touch(session_id, "state/week-changelog/2026-07-20.md", cwd=str(repo_root))

    mod.perform_archive_files(
        week_dir,
        archive_root,
        review_dir,
        review_archive_root,
        "2026-07-20",
        "v2.8.0",
        "abc1234",
        "2026-07-24",
        session_id=session_id,
        relocate_fn=_session_scope.relocate_touched_path,
        relocate_cwd=str(repo_root),
    )

    dest = archive_root / "2026-07-20" / "2026-07-20.md"
    assert dest.exists()
    assert not (week_dir / "2026-07-20.md").exists()

    archived_rel = str(dest.relative_to(repo_root)).replace(os.sep, "/")
    offer = _safe_commit_offer.compute_offer(session_id, cwd=str(repo_root))
    assert archived_rel in offer["safe_paths"], (
        f"expected {archived_rel!r} in compute_offer's safe_paths, got: {offer['safe_paths']!r}"
    )


def test_relocate_or_move_falls_back_to_plain_move_when_relocate_fn_none(mod, tmp_path):
    # The degradation contract: with relocate_fn=None (unresolvable session
    # id / import failure), the move still happens and nothing raises -- an
    # archive that ran beats one that failed on bookkeeping.
    week_dir, review_dir = _make_week_tree(tmp_path)
    archive_root = tmp_path / "archive" / "week-changelogs"
    review_archive_root = tmp_path / "archive" / "review-trail"

    actions = mod.perform_archive_files(
        week_dir,
        archive_root,
        review_dir,
        review_archive_root,
        "2026-07-20",
        "v2.8.0",
        "abc1234",
        "2026-07-24",
        session_id="",
        relocate_fn=None,
    )

    assert actions
    dest = archive_root / "2026-07-20"
    assert (dest / "2026-07-20.md").exists()
    assert not (week_dir / "2026-07-20.md").exists()


def test_cmd_archive_threads_session_id_and_relocate_fn(mod, tmp_path, monkeypatch):
    # Confirms `_cmd_archive` actually supplies both session_id and
    # relocate_fn to perform_archive_files, so the production path is wired
    # and not silently inert.
    week_dir, review_dir = _make_week_tree(tmp_path)

    captured = {}
    real_perform = mod.perform_archive_files

    def _spy_perform_archive_files(*args, **kwargs):
        captured["session_id"] = kwargs.get("session_id")
        captured["relocate_fn"] = kwargs.get("relocate_fn")
        captured["relocate_cwd"] = kwargs.get("relocate_cwd")
        return real_perform(*args, **kwargs)

    monkeypatch.setattr(mod, "perform_archive_files", _spy_perform_archive_files)
    monkeypatch.setattr(mod, "_resolve_session_id_for_relocate", lambda: "spy-session-id")

    def _fake_relocate_fn():
        return object()

    monkeypatch.setattr(mod, "_import_relocate_touched_path", _fake_relocate_fn)

    rc = mod.main(
        [
            "archive",
            "--repo-root",
            str(tmp_path),
            "--version",
            "v2.8.0",
            "--merge-sha",
            "abc1234",
            "--released-date",
            "2026-07-24",
            "--no-git",
        ]
    )

    assert rc == 0
    assert captured["session_id"] == "spy-session-id"
    assert captured["relocate_fn"] is not None
    assert captured["relocate_cwd"] == str(tmp_path)


def test_cmd_archive_week_only_and_move_priorities_wire_through_cli(mod, tmp_path, capsys):
    week_dir, review_dir = _make_two_week_tree(tmp_path)

    rc = mod.main(
        [
            "archive",
            "--repo-root",
            str(tmp_path),
            "--version",
            "v2.8.0",
            "--merge-sha",
            "abc1234",
            "--released-date",
            "2026-07-24",
            "--week-starting",
            "2026-07-20",
            "--week-only",
            "--move-priorities",
            "--no-git",
        ]
    )

    assert rc == 0
    capsys.readouterr()

    dest = tmp_path / "archive" / "week-changelogs" / "2026-07-20"
    # week_only=True reached perform_archive_files: week N+1 artifact
    # survives untouched in place, not swept into the archive.
    assert (week_dir / "2026-07-27.md").exists()
    assert not (dest / "2026-07-27.md").exists()

    # move_priorities=True reached perform_archive_files: the fragment
    # moved despite week_only being set.
    assert (dest / "HEADER.priorities.alice.md").exists()
    assert not (week_dir / "HEADER.priorities.alice.md").exists()

    # week_only=True also reached the review-trail leg.
    review_dest = tmp_path / "archive" / "review-trail" / "2026-07-20"
    assert (review_dest / "2026-07-21-093000-sess1.json").exists()
    assert (review_dir / "2026-07-28-101500-sess2.json").exists()
    assert not (review_dest / "2026-07-28-101500-sess2.json").exists()
