"""test_percolate_round_commit_pathspec — pins the publish-round commit-
pathspec's safety filtering (docs/plans/2026-08-13-the-publish-round-commits-
the-names-it-a.md § Tasks C3-C5).

REVISED 2026-08-23 (chunk C4, docs/plans/2026-08-23-rebuild-the-percolate-
round-as-six-steps.md AC4/AC5): `_build_commit_pathspec` and
`_split_stdout_by_row_dest` are DELETED -- the commit pathspec is now built
from a `RoundManifest` publish.py's real run persists to disk
(`_read_fresh_round_manifest`/`_pathspec_from_manifest`), never from a
re-parse of that run's stdout. Renames need no resolution machinery under
this shape (a rename is a real-file-presence REMOVE+NEW pair by
construction), so every test below that pinned `_build_commit_pathspec`'s
rename resolution, row attribution, or stdout-derived dedup/containment
logic is REMOVED with it — that mechanism is retired by design, not merely
relocated, and porting a test for code that no longer exists would just
re-describe the deletion.

REVISED AGAIN 2026-08-23 (PM ruling, in-session, "I don't want a dry run, I
never asked for a dry run"): `--dry-run-first` -- the one caller AC5
originally carved `_extract_change_lines` out for -- is ALSO retired
outright, taking `_extract_change_lines` and its block-prefix/rename-tag
parsing tests with it (`_split_stdout_by_row_dest` alone survives, for
`percolate-mirror.py`'s scan-secrets row attribution, unrelated to any of
this). Nothing in this file exercises stdout parsing any more.

What survives, and why: `_filter_commit_pathspec`'s three safety filters
(gitignored-at-dest, already-absent-deletion, repo-root-relative pathing)
are UNCHANGED code -- `_pathspec_from_manifest` reuses it verbatim, only
re-sourcing its `seen` input from the manifest instead of from
`_build_commit_pathspec`. Every test pinning that filtering behaviour is
exercised here by calling `_filter_commit_pathspec` directly via
`_seen_from_change_lines` (a tiny local stand-in for what
`_build_commit_pathspec` used to build internally, minus the rename
resolution and containment check neither of which a manifest-sourced `seen`
dict can ever need: `wire_paths.rel_id` cannot produce a `../`-bearing
entry).

Run: python -m pytest coordinator/bin/tests/test_percolate_round_commit_pathspec.py -q
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round_commit_pathspec", _BIN_DIR / "percolate-round.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _seen_from_change_lines(dest: "str", change_lines) -> dict:
    """Stands in for what `_build_commit_pathspec` used to build internally
    before handing off to `_filter_commit_pathspec` -- first-seen-wins,
    dest-relative `rel` joined onto `dest`. No rename resolution and no
    containment check: neither is a manifest-sourced caller's concern (§
    module docstring)."""
    dest_root = Path(dest)
    seen: dict = {}
    for tag, rel in change_lines:
        seen.setdefault(str(dest_root / rel), (tag, rel))
    return seen


# ---------------------------------------------------------------------------
# Pathspec pre-filtering (docs/plans/2026-08-14-the-publish-round-commits-
# the-names-it-a.md follow-up): the 100-declined-path deadlock. Fix 2 drops
# two knowable-before-committing benign-decline classes from the derived
# pathspec so `scoped-git-commit` is never asked to land a path that cannot.
# `_filter_commit_pathspec` itself is UNCHANGED by chunk C4 -- only its
# caller (`_pathspec_from_manifest`, not exercised directly here per this
# file's own Anti-scope, "do not re-run a real publish to test") changed.
# ---------------------------------------------------------------------------


def test_gitignored_path_dropped_from_pathspec(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [("NEW", "__pycache__/foo.pyc")]

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(
                cmd, 0, "__pycache__/foo.pyc\0", ""
            )
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    pathspec = _mod._filter_commit_pathspec(
        dest, str(dest), _seen_from_change_lines(str(dest), change_lines)
    )[0]
    assert pathspec == []


def test_already_absent_deletion_intent_dropped_from_pathspec(tmp_path, monkeypatch):
    """A `DELETE`/`REMOVE` tag for a path absent from both dest's worktree
    and its index has nothing left to commit -- the desired end state
    (absent) already holds."""
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [("REMOVE", "gone-already.sh")]

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 1, "", "")
        if "ls-files" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 1, "", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    pathspec = _mod._filter_commit_pathspec(
        dest, str(dest), _seen_from_change_lines(str(dest), change_lines)
    )[0]
    assert pathspec == []


def test_real_add_update_delete_still_appears_in_pathspec(tmp_path, monkeypatch):
    """A genuine deletion (still index-tracked, only worktree-removed by the
    real publish run) must NOT be filtered -- only the class of deletion
    whose path is absent from BOTH worktree and index is dropped."""
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [
        ("NEW", "added.md"),
        ("UPDATE", "changed.md"),
        ("REMOVE", "still-tracked.sh"),
    ]

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 1, "", "")
        if "ls-files" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 0, "still-tracked.sh\n", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    pathspec = _mod._filter_commit_pathspec(
        dest, str(dest), _seen_from_change_lines(str(dest), change_lines)
    )[0]
    assert pathspec == [
        str(dest / "added.md"),
        str(dest / "changed.md"),
        str(dest / "still-tracked.sh"),
    ]


def test_filter_summary_printed_to_stderr(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [
        ("NEW", "__pycache__/foo.pyc"),
        ("REMOVE", "gone-already.sh"),
        ("NEW", "kept.md"),
    ]

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(
                cmd, 0, "__pycache__/foo.pyc\0", ""
            )
        if "ls-files" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 1, "", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    pathspec = _mod._filter_commit_pathspec(
        dest, str(dest), _seen_from_change_lines(str(dest), change_lines)
    )[0]
    assert pathspec == [str(dest / "kept.md")]
    err = capsys.readouterr().err
    assert "filtered 2 path(s)" in err
    assert "1 gitignored" in err
    assert "1 deletion-intent" in err


def test_pathspec_filter_fails_open_on_undeterminable_dest_state(tmp_path, monkeypatch):
    """A path this filter cannot actually verify (probe failure, e.g. `dest`
    is not a git repo in this stub) is left in the pathspec -- a real change
    that should land is the failure mode this filter must never cause, so
    an uncertain case surfaces through `scoped-git-commit`'s own decline
    instead of being silently dropped here."""
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [("REMOVE", "maybe-gone.sh")]

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 128, "", "fatal: not a git repository")
        if "ls-files" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 128, "", "fatal: not a git repository")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    pathspec = _mod._filter_commit_pathspec(
        dest, str(dest), _seen_from_change_lines(str(dest), change_lines)
    )[0]
    assert pathspec == [str(dest / "maybe-gone.sh")]


def test_check_ignore_result_outside_rel_paths_raises_hard(tmp_path, monkeypatch):
    """`check-ignore --stdin` can only ever echo back a member of what it was
    fed once `rel_paths` and its output share one canonical (POSIX) form --
    a returned path absent from `rel_paths` means that invariant broke, and
    this must fail LOUD (hard raise) rather than silently filtering nothing:
    the corrupting direction is a narrowed filter missing a real gitignored
    path and committing gitignored content into the mirror."""
    dest = tmp_path / "dest"
    dest.mkdir()
    seen = {str(dest / "tracked.md"): ("NEW", "tracked.md")}

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 0, "not-in-rel-paths.md\n", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    with pytest.raises(ValueError, match="check-ignore reported"):
        _mod._filter_commit_pathspec(dest, str(dest), seen)


def test_check_ignore_result_subset_of_rel_paths_does_not_raise(tmp_path, monkeypatch):
    """Ordinary legitimate input -- every path `check-ignore` reports as
    ignored is drawn from what it was asked about -- must never trip the
    invariant-break raise; only a genuine mismatch does (§ the sibling
    hard-raise test above)."""
    dest = tmp_path / "dest"
    dest.mkdir()
    seen = {
        str(dest / "ignored.pyc"): ("NEW", "ignored.pyc"),
        str(dest / "kept.md"): ("NEW", "kept.md"),
    }

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 0, "ignored.pyc\0", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    kept = _mod._filter_commit_pathspec(dest, str(dest), seen)[0]
    assert kept == [str(dest / "kept.md")]


# ---------------------------------------------------------------------------
# Real-shape regression (docs/plans/2026-08-14-the-publish-round-commits-
# the-names-it-a.md follow-up, the 83-decline defect): a REAL git repo, with
# `dest` a `dest_subdir` beneath the actual `scoped-git-commit --repo` root.
# A REAL subprocess `git` is used deliberately here (never mocked) -- the
# defect is in exactly what a real `git ls-files`/`git diff --cached`
# reports for a `dest_subdir`, which a hand-rolled `_fake_run` cannot stand
# in for without begging the question.
# ---------------------------------------------------------------------------


import subprocess as _subprocess

_NO_WINDOW = {"creationflags": getattr(_subprocess, "CREATE_NO_WINDOW", 0)}


def _git_run(args, **kwargs):
    return _subprocess.run(args, capture_output=True, text=True, **_NO_WINDOW, **kwargs)


def _init_real_repo(repo_root: Path) -> None:
    _git_run(["git", "init", "-q"], cwd=str(repo_root), check=True)
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q",
         "--allow-empty", "-m", "init"],
        cwd=str(repo_root), check=True,
    )


def test_already_absent_deletion_intent_dropped_from_pathspec_real_repo_subdir(tmp_path):
    """Real-shape fidelity fix: `dest` is a `dest_subdir` under a real repo
    ROOT that never itself received the removed file (never existed at
    dest, matching one of the two `_dest_path_exists` "already gone"
    causes) -- `not _dest_path_exists(...)` must still resolve to True (drop
    it) when probed via a real `git`, not just a mocked one."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_real_repo(repo_root)
    dest_subdir = repo_root / "coordinator_core"
    dest_subdir.mkdir()
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q",
         "--allow-empty", "-m", "second"],
        cwd=str(repo_root), check=True,
    )

    change_lines = [("REMOVE", "ops/ceremony/tests/never_existed.py")]
    pathspec = _mod._filter_commit_pathspec(
        dest_subdir, str(dest_subdir), _seen_from_change_lines(str(dest_subdir), change_lines)
    )[0]
    assert pathspec == []


def test_unstaged_worktree_deletion_kept_but_repo_root_relative(tmp_path):
    """The other `_dest_path_exists` truth (still index-tracked, only
    worktree-removed): `_filter_commit_pathspec` must still KEEP it (§
    `test_real_add_update_delete_still_appears_in_pathspec` above, semantics
    unchanged by this fix) -- but with `repo_root` given, the kept entry
    must be `repo_root`-relative, not absolute under `dest_subdir`.

    Why this matters (the actual 83-decline root cause, not the filter's
    own drop/keep call): `scoped_git_commit.commit_pipeline.explicit_stage`
    classifies an unstaged deletion via `git_native.ls_files_deleted`, which
    runs with `cwd=worktree_root` (the `--repo` value, i.e. this test's
    `repo_root`) and reports matches CWD-relative. An absolute pathspec
    entry can never equality-match that CWD-relative name -- this is
    reproduced directly below via the same real-git call `explicit_stage`
    depends on, without touching `commit_pipeline.py` itself."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_real_repo(repo_root)
    dest_subdir = repo_root / "coordinator_core"
    tests_dir = dest_subdir / "ops" / "ceremony" / "tests"
    tests_dir.mkdir(parents=True)
    target_file = tests_dir / "test_claim_cli_remedy_invocations.py"
    target_file.write_text("x\n")
    _git_run(["git", "add", "-A"], cwd=str(repo_root), check=True)
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q",
         "-m", "seed"],
        cwd=str(repo_root), check=True,
    )

    # The real publish swap: physically removed, never staged (§ `publish.py
    # ::_swap_publish_staging_into_dest` -- a filesystem-level rename, never
    # a `git rm`).
    target_file.unlink()

    change_lines = [("REMOVE", "ops/ceremony/tests/test_claim_cli_remedy_invocations.py")]
    pathspec = _mod._filter_commit_pathspec(
        dest_subdir,
        str(dest_subdir),
        _seen_from_change_lines(str(dest_subdir), change_lines),
        repo_root=str(repo_root),
    )[0]
    assert pathspec == ["coordinator_core/ops/ceremony/tests/test_claim_cli_remedy_invocations.py"]

    # Reproduces the actual downstream classification `explicit_stage` runs
    # (`git_native.ls_files_deleted`) -- confirms the entry this call
    # produces is the form that probe will actually recognize.
    result = _git_run(["git", "-C", str(repo_root), "ls-files", "--deleted", "--", *pathspec])
    assert result.stdout.strip() == pathspec[0]

    # Pins the actual regression: an ABSOLUTE pathspec entry still scopes
    # `git ls-files --deleted` to the right file (git accepts an absolute
    # pathspec argument fine), but the reported match is ALWAYS CWD-relative
    # -- never byte-equal to the absolute input that named it. This is
    # exactly why `commit_pipeline.explicit_stage`'s `p in worktree_deleted`
    # containment check (comparing its caller's own pathspec string against
    # this CWD-relative output set) can never succeed for an absolute `p`,
    # regardless of whether the file is genuinely, unambiguously deleted.
    absolute_form = str(target_file)
    result_absolute = _git_run(
        ["git", "-C", str(repo_root), "ls-files", "--deleted", "--", absolute_form]
    )
    assert result_absolute.stdout.strip() == pathspec[0]
    assert result_absolute.stdout.strip() != absolute_form


def test_repo_root_relative_pathspec_uses_forward_slashes(tmp_path):
    """Review: coordinatorcode-reviewer-c58be590 -- `os.path.relpath` emits
    OS-native separators (backslash on Windows), which never byte-match
    git's own always-forward-slash CWD-relative output. Pins the expected
    string explicitly (never derived from `os.sep`) so this holds on any
    host, not just a Windows one."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_real_repo(repo_root)
    dest_subdir = repo_root / "coordinator_core" / "ops"
    tests_dir = dest_subdir / "ceremony" / "tests"
    tests_dir.mkdir(parents=True)
    target_file = tests_dir / "test_nested_deletion.py"
    target_file.write_text("x\n")
    _git_run(["git", "add", "-A"], cwd=str(repo_root), check=True)
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q",
         "-m", "seed"],
        cwd=str(repo_root), check=True,
    )
    target_file.unlink()

    change_lines = [("REMOVE", "ceremony/tests/test_nested_deletion.py")]
    pathspec = _mod._filter_commit_pathspec(
        dest_subdir,
        str(dest_subdir),
        _seen_from_change_lines(str(dest_subdir), change_lines),
        repo_root=str(repo_root),
    )[0]
    assert pathspec == ["coordinator_core/ops/ceremony/tests/test_nested_deletion.py"]
    assert "\\" not in pathspec[0]


def test_sibling_row_subtree_resolves_without_dotdot(tmp_path):
    """Review: coordinatorcode-reviewer-c58be590 (live-round follow-up) --
    a real multi-row round's manifest names entries from MANY sibling
    subtrees of one shared worktree (e.g. `coordinator_core`, `coordinator/
    bin`). Passing the actual worktree `<root>` as `repo_root` must resolve
    every entry relative to that shared root, never walking above it with
    `..`."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_real_repo(repo_root)

    row_a_dest = repo_root / "coordinator_core"
    row_a_dest.mkdir()
    row_b_dest = repo_root / "coordinator" / "bin"
    row_b_dest.mkdir(parents=True)
    (row_a_dest / "existing.py").write_text("x\n")
    (row_b_dest / "existing-tool").write_text("x\n")
    _git_run(["git", "add", "-A"], cwd=str(repo_root), check=True)
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q",
         "-m", "seed"],
        cwd=str(repo_root), check=True,
    )

    row_a_changes = [("NEW", "new-file.py")]
    row_b_changes = [("NEW", "new-tool")]

    pathspec_a = _mod._filter_commit_pathspec(
        row_a_dest,
        str(row_a_dest),
        _seen_from_change_lines(str(row_a_dest), row_a_changes),
        repo_root=str(repo_root),
    )[0]
    pathspec_b = _mod._filter_commit_pathspec(
        row_b_dest,
        str(row_b_dest),
        _seen_from_change_lines(str(row_b_dest), row_b_changes),
        repo_root=str(repo_root),
    )[0]
    combined = pathspec_a + pathspec_b

    assert combined == ["coordinator_core/new-file.py", "coordinator/bin/new-tool"]
    for entry in combined:
        assert ".." not in entry.split("/"), entry
