"""
coordinator_core.ops.ceremony.tests.test_git_native

Tests for git_native.py -- the Windows-safe shared git-subprocess helper (AC3
foundation for the wsc_tail rebuild).

Coverage:
  (a) flags_present_on_every_wrapper -- mocks subprocess.run and asserts EVERY
                                         thin one-git-call-per-function public
                                         wrapper in the module (i.e. every public
                                         function except _COMPOSITE_ENTRYPOINTS --
                                         see that set's docstring) invokes it with
                                         creationflags carrying CREATE_NO_WINDOW
                                         (or the 0 no-op fallback on non-Windows)
                                         AND stdin=subprocess.DEVNULL AND
                                         capture_output=True AND text=True. This
                                         is the mechanical enforcement of AC3.
  (b) _git_returns_typed_result         -- GitResult.ok / returncode / stdout / stderr
                                         shape on a successful invocation.
  (c) _git_oserror_never_raises         -- OSError (git not on PATH) is converted to
                                         a returncode=-1 GitResult, never propagated.
  (d) _git_timeout_never_raises         -- TimeoutExpired is converted the same way.
  (e) _git_check_true_raises_on_nonzero -- check=True mirrors subprocess.run(check=True)
                                         semantics for a completed non-zero process.
  (f) no_bash_or_node_argv              -- grep guard: no wrapper's argv list contains
                                         "bash", "sh", "node", or any ".sh"/".js" token
                                         (AC2 mechanical enforcement at the C1 layer).

Spec backlink: pln-rebuild-the-wsc-commit-ceremon-f7c2a0 § C1.
"""

from __future__ import annotations

import inspect
import subprocess
from unittest.mock import MagicMock, patch

import pytest

# Real-git spawn is load-bearing for section (g) below: `reset_paths` is
# asserted against a real index/worktree so the "unstage-only, never a bare
# reset" hazard is proven against git's actual porcelain output, not a
# mocked call shape. Per-test repo fixtures since reset mutates the index.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

from coordinator_core.ops.ceremony import git_native


def _make_completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    completed = MagicMock()
    completed.returncode = returncode
    completed.stdout = stdout
    completed.stderr = stderr
    return completed


# ---------------------------------------------------------------------------
# (a) flags present on every wrapper
# ---------------------------------------------------------------------------


#: (function, args, kwargs) -- one entry per public wrapper in git_native.py.
#: Each call must reach subprocess.run exactly once with the Windows-safe flags.
_WRAPPER_INVOCATIONS = [
    (git_native.status_porcelain, ("/tmp/repo",), {}),
    (git_native.diff_cached_name_status, ("/tmp/repo",), {}),
    (git_native.diff_cached_name_only, ("/tmp/repo",), {}),
    (git_native.diff_quiet, ("/tmp/repo",), {}),
    (git_native.add_paths, ("/tmp/repo", ["a.txt", "b.txt"]), {}),
    (git_native.ls_files_deleted, ("/tmp/repo", ["a.txt", "b.txt"]), {}),
    (git_native.reset_paths, ("/tmp/repo", ["a.txt", "b.txt"]), {}),
    (git_native.commit_with_message_file, ("/tmp/repo", "/tmp/msg", ["a.txt"]), {}),
    (git_native.rev_parse_head, ("/tmp/repo",), {}),
    (git_native.log_grep, ("/tmp/repo", "Session-Id: abc"), {}),
    (git_native.log_diff_filter, ("/tmp/repo", "R"), {}),
    (git_native.remote, ("/tmp/repo",), {}),
    (git_native.push, ("/tmp/repo",), {}),
    (git_native.fetch, ("/tmp/repo", "origin"), {}),
    (git_native.rebase_onto, ("/tmp/repo", "origin/main", "abc123"), {}),
    (git_native.rebase_abort, ("/tmp/repo",), {}),
    (git_native.merge_base, ("/tmp/repo", "HEAD", "origin/main"), {}),
    (git_native.rev_parse_upstream, ("/tmp/repo",), {}),
    (git_native.rev_parse, ("/tmp/repo", "origin/main"), {}),
    (git_native.rev_list_count, ("/tmp/repo", "abc123..def456"), {}),
]


#: Public functions deliberately excluded from `_WRAPPER_INVOCATIONS` -- this
#: harness asserts each covered function reaches `subprocess.run` exactly
#: ONCE with the Windows-safe flag set (AC3), which only holds for the thin
#: one-git-call-per-function wrappers above. `commit_scoped` is a composite
#: orchestrator (C3, docs/plans/2026-07-27-computed-commit-mechanism-
#: selection.md) that itself calls several of the wrappers above -- each of
#: which is independently covered here -- and branches on real index/
#: worktree state via `diverging_paths()`, so it cannot be exercised through
#: a single mocked `subprocess.run` return value. It has its own dedicated
#: real-git test module: `test_commit_scoped.py`.
#:
#: `commit_authored_content` (DR-272 § 3.3, C2) is the same shape of
#: exclusion for the same reason -- it issues a whole private-index-seed /
#: hash-object / write-tree / commit-tree / update-ref sequence of `_git()`
#: calls internally (see its own docstring), not one. Its success/failure
#: real-git behavioural coverage lives in `test_commit_scoped.py`
#: (authored alongside C2); `test_commit_authored_content_issues_its_git_
#: sequence_through_the_shared_git_wrapper` below adds the AC3 mechanical
#: check this module exists to enforce -- that every one of ITS internal
#: `_git()` calls also carries the same Windows-safe flag set as the thin
#: single-call wrappers above -- which is genuinely new coverage, not a
#: restatement of the behavioural tests.
_COMPOSITE_ENTRYPOINTS = {"commit_scoped", "commit_authored_content"}

#: Public functions deliberately excluded from `_WRAPPER_INVOCATIONS` for a
#: DIFFERENT reason than `_COMPOSITE_ENTRYPOINTS`: `directory_pathspecs()`
#: and `directory_pathspec_diagnostic()` never call `subprocess.run` at all
#: -- they are pure, in-process predicates (`Path.is_dir()` / string
#: formatting), not `git` subprocess wrappers, so the (a) harness above
#: (which asserts `subprocess.run` was invoked exactly once with the
#: Windows-safe flag set) does not apply to them; running them through it
#: would assert `mock_run.call_count == 1` against a function that never
#: calls `subprocess.run`, which is a false claim, not a real test. Their
#: actual logic is covered directly below (`test_directory_pathspecs_*` /
#: `test_directory_pathspec_diagnostic_*`), not through this parametrized
#: harness -- see `directory_pathspecs()`'s own docstring for what incident
#: this predicate exists to close.
_NON_SUBPROCESS_HELPERS = {
    "directory_pathspecs",
    "directory_pathspec_diagnostic",
    "parse_check_ignore_stdin_z",
}

#: `check_ignore()` is a thin single-`git`-call wrapper like everything in
#: `_WRAPPER_INVOCATIONS`, but it feeds `input_data` through `_git()` (piped
#: stdin, never `subprocess.DEVNULL`) -- see `_git()`'s own docstring for why
#: that is NOT the "inherit the parent's stdin" hazard the (a) harness below
#: exists to catch, just a genuinely different, deliberate flag shape the
#: shared `stdin is subprocess.DEVNULL` assertion cannot express. Covered
#: directly by its own real-git tests instead (`test_check_ignore_*` below).
_STDIN_INPUT_WRAPPERS = {"check_ignore"}

#: `cat_file_batch()` is a public wrapper (promoted from
#: `ac27_differential_oracle._git_cat_file_batch`, C36) that -- like the
#: private `_hash_object_stdin_bytes()` above -- deliberately bypasses
#: `_git()`: it needs raw-bytes stdin/stdout (record boundaries are computed
#: from byte-length `size` fields in the `cat-file --batch` stream, not text
#: lines), so `_git()`'s `text=True` leg would mis-decode it. The (a) harness
#: above asserts `text=True`/`stdin=subprocess.DEVNULL` on every covered
#: wrapper, neither of which holds here -- covered directly instead by
#: `test_cat_file_batch_carries_the_windows_safe_creationflag` below, the
#: same pattern `test_hash_object_stdin_bytes_carries_the_windows_safe_
#: creationflag` already establishes for the other bytes-mode bypass.
#:
#: `cat_file_batch_objects()` is the same bypass one layer down: as of the
#: vendored-schema shape-sweep work it OWNS the `subprocess.run` call and
#: `cat_file_batch()` is a thin single-ref wrapper delegating to it, so both
#: are bytes-mode by the identical reasoning. It is listed here for the same
#: reason as `cat_file_batch` -- the (a) harness's `text=True`/`stdin=DEVNULL`
#: assertions cannot express this call shape -- NOT as an exemption from
#: coverage: `test_cat_file_batch_objects_*` below cover it directly,
#: including the creationflag, and the cross-rev property that is its whole
#: reason for existing.
_BYTES_MODE_WRAPPERS = {"cat_file_batch", "cat_file_batch_objects"}


def test_all_public_wrappers_are_covered():
    """Guard: every public function in git_native.py (besides `_git` itself,
    the `GitResult` dataclass, `_COMPOSITE_ENTRYPOINTS`, `_NON_SUBPROCESS_
    HELPERS`, and `_STDIN_INPUT_WRAPPERS`) has a corresponding entry in
    `_WRAPPER_INVOCATIONS` above -- a new thin `subprocess.run`-calling
    wrapper added without a test entry silently escapes AC3 enforcement.
    """
    public_funcs = (
        {
            name
            for name, obj in inspect.getmembers(git_native, inspect.isfunction)
            if not name.startswith("_") and obj.__module__ == git_native.__name__
        }
        - _COMPOSITE_ENTRYPOINTS
        - _NON_SUBPROCESS_HELPERS
        - _STDIN_INPUT_WRAPPERS
        - _BYTES_MODE_WRAPPERS
    )
    covered_funcs = {fn.__name__ for fn, _, _ in _WRAPPER_INVOCATIONS}
    assert public_funcs == covered_funcs, (
        f"wrapper coverage gap: {public_funcs - covered_funcs} untested, "
        f"{covered_funcs - public_funcs} tested-but-missing"
    )


# ---------------------------------------------------------------------------
# directory_pathspecs / directory_pathspec_diagnostic -- pure predicates,
# never call subprocess.run (see `_NON_SUBPROCESS_HELPERS` above for why
# they are not in `_WRAPPER_INVOCATIONS`); covered directly here instead.
# ---------------------------------------------------------------------------


def test_directory_pathspecs_returns_only_directory_entries(tmp_path):
    (tmp_path / "a_dir").mkdir()
    (tmp_path / "a_file.txt").write_text("content", encoding="utf-8")

    result = git_native.directory_pathspecs(
        tmp_path, ["a_dir", "a_file.txt", "missing.txt"]
    )

    assert result == ["a_dir"]


def test_directory_pathspecs_empty_input_returns_empty_list(tmp_path):
    assert git_native.directory_pathspecs(tmp_path, []) == []


def test_directory_pathspecs_preserves_input_order(tmp_path):
    (tmp_path / "dir_one").mkdir()
    (tmp_path / "dir_two").mkdir()

    result = git_native.directory_pathspecs(tmp_path, ["dir_two", "dir_one"])

    assert result == ["dir_two", "dir_one"]


def test_directory_pathspec_diagnostic_names_the_path():
    diagnostic = git_native.directory_pathspec_diagnostic("some/dir")

    assert "some/dir" in diagnostic
    assert "matches whatever is inside it AT COMMIT TIME" in diagnostic
    assert "Pass explicit file paths instead" in diagnostic


@pytest.mark.parametrize(
    "fn, args, kwargs",
    _WRAPPER_INVOCATIONS,
    ids=[fn.__name__ for fn, _, _ in _WRAPPER_INVOCATIONS],
)
def test_flags_present_on_every_wrapper(fn, args, kwargs):
    with patch.object(git_native.subprocess, "run", return_value=_make_completed()) as mock_run:
        fn(*args, **kwargs)

    assert mock_run.call_count == 1
    call_kwargs = mock_run.call_args.kwargs

    assert call_kwargs.get("stdin") is subprocess.DEVNULL
    assert call_kwargs.get("capture_output") is True
    assert call_kwargs.get("text") is True
    assert "creationflags" in call_kwargs
    assert call_kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)

    # argv[0] is always "git" -- no wrapper shells out to anything else.
    argv = mock_run.call_args.args[0]
    assert argv[0] == "git"


#: `--no-optional-locks` is PRE-SUBCOMMAND ONLY (`git status --no-optional-
#: locks` exits 129, "unknown option") -- see the cross-repo lock-retry
#: handoff spec this pins. `status_porcelain` is a bare (non-`--cached`) read
#: invocation measured taking `.git/index.lock` on this shared worktree; the
#: `diff_cached_*` siblings never took it and are deliberately NOT in this
#: list.
_NO_OPTIONAL_LOCKS_WRAPPERS = [
    (git_native.status_porcelain, ("/tmp/repo",), {}, "status"),
]

#: `diff_quiet` is the deliberate EXCLUSION, pinned here so a future
#: consistency-minded sweep does not "finish the job" and silently break the
#: EOL-phantom filter. It IS a bare worktree diff that takes the lock, but
#: suppressing that lock also suppresses the stat-cache write-back that lets a
#: phantom-dirty entry self-heal -- and `commit_gates`' phantom filter is this
#: wrapper's only production caller. See `git_native.diff_quiet`'s own comment.
_NO_OPTIONAL_LOCKS_EXCLUDED = [
    (git_native.diff_quiet, ("/tmp/repo",), {}, "diff"),
]


@pytest.mark.parametrize(
    "fn, args, kwargs, subcommand",
    _NO_OPTIONAL_LOCKS_WRAPPERS,
    ids=[fn.__name__ for fn, _, _, _ in _NO_OPTIONAL_LOCKS_WRAPPERS],
)
def test_no_optional_locks_precedes_subcommand(fn, args, kwargs, subcommand):
    with patch.object(git_native.subprocess, "run", return_value=_make_completed()) as mock_run:
        fn(*args, **kwargs)

    argv = mock_run.call_args.args[0]
    assert argv[0] == "git"
    assert argv[1] == "--no-optional-locks", (
        f"--no-optional-locks must sit immediately after 'git' and before "
        f"'{subcommand}' -- placed after the subcommand it fails with exit "
        f"129 ('unknown option'). Got argv={argv!r}"
    )
    assert argv[2] == subcommand


@pytest.mark.parametrize(
    "fn, args, kwargs, subcommand",
    _NO_OPTIONAL_LOCKS_EXCLUDED,
    ids=[fn.__name__ for fn, _, _, _ in _NO_OPTIONAL_LOCKS_EXCLUDED],
)
def test_phantom_clearing_readers_keep_the_optional_lock(fn, args, kwargs, subcommand):
    """The exclusion is load-bearing, not an oversight the next sweep should
    tidy up: these wrappers depend on git's stat-cache WRITE-BACK, which
    `--no-optional-locks` suppresses, to let a phantom-dirty entry self-heal.
    Adding the flag here leaves every phantom permanently dirty and re-filtered
    on each ceremony rather than converging."""
    with patch.object(git_native.subprocess, "run", return_value=_make_completed()) as mock_run:
        fn(*args, **kwargs)

    argv = mock_run.call_args.args[0]
    assert argv[0] == "git"
    assert "--no-optional-locks" not in argv, (
        f"{fn.__name__} must NOT carry --no-optional-locks — it relies on the "
        f"stat-cache write-back the flag suppresses. Got argv={argv!r}"
    )
    assert argv[1] == subcommand


# ---------------------------------------------------------------------------
# (b)-(e) _git() result-shape and error-handling contract
# ---------------------------------------------------------------------------


def test_git_returns_typed_result_on_success():
    with patch.object(
        git_native.subprocess, "run", return_value=_make_completed(0, "hello\n", "")
    ):
        result = git_native._git(["status"], cwd="/tmp/repo")

    assert isinstance(result, git_native.GitResult)
    assert result.ok is True
    assert result.returncode == 0
    assert result.stdout == "hello\n"
    assert result.stderr == ""


def test_git_oserror_never_raises():
    with patch.object(git_native.subprocess, "run", side_effect=OSError("git not found")):
        result = git_native._git(["status"], cwd="/tmp/repo")

    assert result.returncode == -1
    assert "git not found" in result.stderr


def test_git_timeout_never_raises():
    with patch.object(
        git_native.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(cmd=["git", "push"], timeout=60),
    ):
        result = git_native._git(["push"], cwd="/tmp/repo", timeout=60)

    assert result.returncode == -1
    assert "timed out" in result.stderr


def test_git_check_true_raises_on_nonzero():
    with patch.object(
        git_native.subprocess, "run", return_value=_make_completed(1, "", "fatal: boom")
    ):
        with pytest.raises(subprocess.CalledProcessError):
            git_native._git(["status"], cwd="/tmp/repo", check=True)


def test_git_check_false_does_not_raise_on_nonzero():
    with patch.object(
        git_native.subprocess, "run", return_value=_make_completed(1, "", "fatal: boom")
    ):
        result = git_native._git(["status"], cwd="/tmp/repo", check=False)

    assert result.returncode == 1
    assert result.stderr == "fatal: boom"


# ---------------------------------------------------------------------------
# (g) capture=True/False contract (C6a)
# ---------------------------------------------------------------------------


def test_git_capture_default_matches_hardcoded_prior_behaviour():
    with patch.object(
        git_native.subprocess, "run", return_value=_make_completed(0, "hello\n", "")
    ) as mock_run:
        result = git_native._git(["status"], cwd="/tmp/repo")

    assert mock_run.call_args.kwargs.get("capture_output") is True
    assert result.stdout == "hello\n"
    assert result.stderr == ""


def test_git_capture_true_explicit_matches_default():
    with patch.object(
        git_native.subprocess, "run", return_value=_make_completed(0, "hello\n", "")
    ) as mock_run:
        result = git_native._git(["status"], cwd="/tmp/repo", capture=True)

    assert mock_run.call_args.kwargs.get("capture_output") is True
    assert result.stdout == "hello\n"
    assert result.stderr == ""


def test_git_capture_false_omits_capture_output_and_result_is_empty():
    with patch.object(
        git_native.subprocess, "run", return_value=_make_completed(0, None, None)
    ) as mock_run:
        result = git_native._git(["status"], cwd="/tmp/repo", capture=False)

    assert "capture_output" not in mock_run.call_args.kwargs
    assert result.returncode == 0
    assert result.ok is True
    assert result.stdout == ""
    assert result.stderr == ""


def test_git_capture_false_still_carries_windows_safe_flags():
    with patch.object(
        git_native.subprocess, "run", return_value=_make_completed(0, None, None)
    ) as mock_run:
        git_native._git(["status"], cwd="/tmp/repo", capture=False)

    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs.get("stdin") is subprocess.DEVNULL
    assert call_kwargs.get("text") is True
    assert call_kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_git_capture_false_with_input_data_still_feeds_stdin_not_devnull():
    with patch.object(
        git_native.subprocess, "run", return_value=_make_completed(0, None, None)
    ) as mock_run:
        git_native._git(
            ["check-ignore"], cwd="/tmp/repo", capture=False, input_data="a.txt\0"
        )

    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs.get("input") == "a.txt\0"
    assert "stdin" not in call_kwargs
    assert "capture_output" not in call_kwargs


def test_git_capture_false_check_true_raises_with_none_output_not_synthesized_string():
    with patch.object(
        git_native.subprocess, "run", return_value=_make_completed(1, None, None)
    ):
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            git_native._git(["status"], cwd="/tmp/repo", check=True, capture=False)

    assert exc_info.value.output is None
    assert exc_info.value.stderr is None


# ---------------------------------------------------------------------------
# (f) no bash/node/script spawns anywhere in this module (AC2 mechanical guard)
# ---------------------------------------------------------------------------


def test_no_bash_or_node_argv():
    with patch.object(git_native.subprocess, "run", return_value=_make_completed()) as mock_run:
        for fn, args, kwargs in _WRAPPER_INVOCATIONS:
            fn(*args, **kwargs)

    banned_tokens = {"bash", "sh", "node"}
    for call in mock_run.call_args_list:
        argv = call.args[0]
        assert argv[0] == "git"
        for token in argv:
            assert token not in banned_tokens
            assert not str(token).endswith(".sh")
            assert not str(token).endswith(".js")


# ---------------------------------------------------------------------------
# (g) reset_paths -- scoped unstage-only rollback (session fb5fa766, 2026-07-31)
# ---------------------------------------------------------------------------


def _real_git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _real_porcelain(cwd) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(cwd), capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _init_real_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _real_git(["init", "-q"], repo)
    _real_git(["config", "user.email", "t@t.example"], repo)
    _real_git(["config", "user.name", "t"], repo)
    return repo


def test_reset_paths_empty_input_is_a_documented_noop_never_a_bare_reset(tmp_path):
    """`git reset -q HEAD --` with zero paths after `--` unstages the ENTIRE
    index -- the exact hazard this function's own docstring names. A caller
    passing an empty `paths` list (e.g. `explicit_stage` staged nothing this
    call) must get a true no-op, never a full-index reset."""
    repo = _init_real_repo(tmp_path)
    (repo / "a.txt").write_text("a")
    (repo / "b.txt").write_text("b")
    _real_git(["add", "--", "a.txt", "b.txt"], repo)

    result = git_native.reset_paths(repo, [])

    assert result.ok is True
    status_lines = _real_porcelain(repo)
    # Both files remain staged -- untouched by the empty-input no-op.
    assert any(line.startswith("A") and line.endswith("a.txt") for line in status_lines)
    assert any(line.startswith("A") and line.endswith("b.txt") for line in status_lines)


def test_reset_paths_unstages_only_the_given_pathspec(tmp_path):
    """A peer's own staged file OUTSIDE the given pathspec must survive
    `reset_paths()` untouched -- the scoped-rollback counterpart to
    `add_paths()`'s own explicit-pathspec contract."""
    repo = _init_real_repo(tmp_path)
    (repo / "a.txt").write_text("a")
    (repo / "sibling.txt").write_text("s")
    _real_git(["add", "--", "a.txt", "sibling.txt"], repo)

    result = git_native.reset_paths(repo, ["a.txt"])

    assert result.ok is True
    status_lines = _real_porcelain(repo)
    assert any(line.strip() == "?? a.txt" for line in status_lines)
    assert any(line.startswith("A") and line.endswith("sibling.txt") for line in status_lines)


def test_reset_paths_drops_directory_entries_but_still_resets_file_entries(tmp_path):
    """A directory entry in `paths` must never reach the `git reset`
    pathspec (it would match whatever is CURRENTLY inside it, including a
    peer's own file added since this call's own staging) -- it is dropped,
    while any co-supplied FILE entry in the same call is still rolled back."""
    repo = _init_real_repo(tmp_path)
    (repo / "dir").mkdir()
    (repo / "dir" / "inside.txt").write_text("d")
    (repo / "plain.txt").write_text("p")
    _real_git(["add", "--", "dir/inside.txt", "plain.txt"], repo)

    result = git_native.reset_paths(repo, ["dir", "plain.txt"])

    assert result.ok is True
    status_lines = _real_porcelain(repo)
    # The directory entry was dropped -- its file stays staged.
    assert any(
        line.startswith("A") and line.endswith("dir/inside.txt") for line in status_lines
    )
    # The co-supplied file entry was rolled back.
    assert any(line.strip() == "?? plain.txt" for line in status_lines)


def test_reset_paths_on_content_matching_head_is_a_true_noop(tmp_path):
    """Rolling back a path whose staged content already matches `HEAD` (the
    ordinary already-committed-no-op shape) must be silent and harmless --
    `git reset -q` never raises/prints for this case."""
    repo = _init_real_repo(tmp_path)
    (repo / "a.txt").write_text("seed")
    _real_git(["add", "--", "a.txt"], repo)
    _real_git(["commit", "-q", "-m", "seed"], repo)
    _real_git(["add", "--", "a.txt"], repo)  # re-add identical content

    result = git_native.reset_paths(repo, ["a.txt"])

    assert result.ok is True
    assert result.stderr == ""
    assert _real_porcelain(repo) == []


# ---------------------------------------------------------------------------
# ls_files_deleted -- explicit_stage's unstaged-deletion detection
# (2026-08-04 fix, defect A -- see that function's own "Deletion staging"
# docstring section).
# ---------------------------------------------------------------------------


def test_ls_files_deleted_reports_unstaged_deletion(tmp_path):
    repo = _init_real_repo(tmp_path)
    (repo / "gone.txt").write_text("content")
    _real_git(["add", "--", "gone.txt"], repo)
    _real_git(["commit", "-q", "-m", "seed"], repo)

    (repo / "gone.txt").unlink()

    result = git_native.ls_files_deleted(repo, ["gone.txt"])

    assert result.ok is True
    assert result.stdout.splitlines() == ["gone.txt"]


def test_ls_files_deleted_does_not_report_a_staged_deletion(tmp_path):
    """Once a deletion is staged (`git rm`), the index no longer holds the
    file's content -- `--deleted` (worktree vs INDEX) no longer reports it.
    This is the exact reason `explicit_stage()` needs a SECOND check
    (`diff_cached_name_status`'s `D` lines) for the staged case -- neither
    check alone covers both."""
    repo = _init_real_repo(tmp_path)
    (repo / "gone.txt").write_text("content")
    _real_git(["add", "--", "gone.txt"], repo)
    _real_git(["commit", "-q", "-m", "seed"], repo)

    _real_git(["rm", "-q", "gone.txt"], repo)

    result = git_native.ls_files_deleted(repo, ["gone.txt"])

    assert result.ok is True
    assert result.stdout.splitlines() == []


def test_ls_files_deleted_does_not_report_an_untracked_missing_path(tmp_path):
    """A path that was never tracked at all is not a deletion -- must not be
    misreported, even though it is equally absent from the worktree."""
    repo = _init_real_repo(tmp_path)
    (repo / "seed.txt").write_text("seed")
    _real_git(["add", "--", "seed.txt"], repo)
    _real_git(["commit", "-q", "-m", "seed"], repo)

    result = git_native.ls_files_deleted(repo, ["never/existed.txt"])

    assert result.ok is True
    assert result.stdout.splitlines() == []


def test_ls_files_deleted_empty_paths_is_a_documented_noop_never_a_whole_tree_scan(tmp_path):
    """Empty `paths` must never fall through to a bare `git ls-files
    --deleted` (whole-tree scan) -- the exact hazard `reset_paths()`'s own
    empty-input guard exists to close, one layer over."""
    repo = _init_real_repo(tmp_path)
    (repo / "gone.txt").write_text("content")
    _real_git(["add", "--", "gone.txt"], repo)
    _real_git(["commit", "-q", "-m", "seed"], repo)
    (repo / "gone.txt").unlink()

    result = git_native.ls_files_deleted(repo, [])

    assert result.ok is True
    assert result.stdout == ""


# ---------------------------------------------------------------------------
# check_ignore -- explicit_stage's ignored-path pre-filter (2026-08-03 fix,
# live `safe-commit-offer` incident; index-aware single-call form, also
# 2026-08-03 -- see `check_ignore()`'s own negative-spec for why the
# `--no-index` two-call decomposition was removed).
# ---------------------------------------------------------------------------


def test_check_ignore_tracked_path_matching_gitignore_is_not_ignored(tmp_path):
    """A tracked path that matches a (later-added) `.gitignore` pattern must
    NOT be classified ignored -- `git add` on an already-tracked path
    succeeds regardless of the pattern, so misreporting it here would
    silently stop committing real changes to a file the caller still owns
    (the `0ec3ca894` invariant, preserved through the index-aware default
    rather than a separate `ls_files_tracked()` gate)."""
    repo = _init_real_repo(tmp_path)
    (repo / "tracked.txt").write_text("t")
    _real_git(["add", "--", "tracked.txt"], repo)
    _real_git(["commit", "-q", "-m", "seed"], repo)
    (repo / ".gitignore").write_text("tracked.txt\n")

    result = git_native.check_ignore(repo, ["tracked.txt"])

    assert result.returncode == 1
    assert git_native.parse_check_ignore_stdin_z(result.stdout) == []


def test_check_ignore_untracked_path_matching_gitignore_is_ignored(tmp_path):
    """An untracked path matching `.gitignore` IS classified ignored -- the
    case `explicit_stage()`'s pre-filter must still drop from the stage
    set."""
    repo = _init_real_repo(tmp_path)
    (repo / ".gitignore").write_text("untracked.txt\n")
    (repo / "untracked.txt").write_text("u")

    result = git_native.check_ignore(repo, ["untracked.txt"])

    assert result.returncode == 0
    matches = git_native.parse_check_ignore_stdin_z(result.stdout)
    assert {m[3] for m in matches} == {"untracked.txt"}


def test_check_ignore_empty_input_is_a_documented_noop(tmp_path):
    repo = _init_real_repo(tmp_path)
    result = git_native.check_ignore(repo, [])
    # returncode == 1 -- "nothing matched" -- is the documented no-op shape,
    # never a bare `.ok` check (see `check_ignore`'s own docstring).
    assert result.returncode == 1
    assert result.stdout == ""


def test_check_ignore_flags_gitignore_blocked_path_only(tmp_path):
    repo = _init_real_repo(tmp_path)
    (repo / ".gitignore").write_text("ignored_dir/\n")
    (repo / "ignored_dir").mkdir()
    (repo / "ignored_dir" / "cache.txt").write_text("secret")
    (repo / "kept.txt").write_text("k")

    result = git_native.check_ignore(repo, ["ignored_dir/cache.txt", "kept.txt"])

    assert result.returncode == 0
    matches = git_native.parse_check_ignore_stdin_z(result.stdout)
    matched_paths = {m[3] for m in matches}
    assert matched_paths == {"ignored_dir/cache.txt"}


def test_check_ignore_no_matches_returns_returncode_one_not_a_failure(tmp_path):
    repo = _init_real_repo(tmp_path)
    (repo / "a.txt").write_text("a")
    (repo / "b.txt").write_text("b")

    result = git_native.check_ignore(repo, ["a.txt", "b.txt"])

    assert result.returncode == 1
    assert git_native.parse_check_ignore_stdin_z(result.stdout) == []


def test_parse_check_ignore_stdin_z_multiple_matches():
    stdout = (
        ".gitignore\x001\x00ignored_dir/\x00ignored_dir/cache.txt\x00"
        ".gitignore\x002\x00*.log\x00debug.log\x00"
    )
    matches = git_native.parse_check_ignore_stdin_z(stdout)
    assert matches == [
        (".gitignore", "1", "ignored_dir/", "ignored_dir/cache.txt"),
        (".gitignore", "2", "*.log", "debug.log"),
    ]


# ---------------------------------------------------------------------------
# commit_authored_content -- AC3 mechanical flag coverage (see
# `_COMPOSITE_ENTRYPOINTS`'s docstring above for why this is a real-git flag
# spy rather than a `_WRAPPER_INVOCATIONS` entry; behavioural success/failure
# coverage lives in `test_commit_scoped.py`).
# ---------------------------------------------------------------------------


def test_commit_authored_content_issues_its_git_sequence_through_the_shared_git_wrapper(tmp_path):
    """`commit_authored_content()` never reaches `subprocess.run` directly --
    every step of its `rev-parse`/`ls-tree`/`hash-object`/`read-tree`/
    `update-index`/`write-tree`/`commit-tree`/`update-ref` sequence goes
    through this module's own `_git()` (whose Windows-safe-flag contract is
    already independently covered by `test_git_returns_typed_result_on_
    success` and friends above), so patching `subprocess.run` directly here
    would double-count that contract and, worse, would also catch unrelated
    background subprocess activity elsewhere in the process (`git_native.
    subprocess` IS the shared stdlib module object, not a private import).
    This asserts the composite's actual call sequence instead -- the genuine
    coverage gap `test_all_public_wrappers_are_covered` flagged."""
    repo = _init_real_repo(tmp_path)
    (repo / "file.txt").write_text("original\n", encoding="utf-8")
    _real_git(["add", "--", "file.txt"], repo)
    _real_git(["commit", "-q", "-m", "baseline"], repo)

    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("a commit message\n", encoding="utf-8")

    real_git = git_native._git
    git_argvs = []

    def _spy(args, **kwargs):
        git_argvs.append(list(args))
        return real_git(args, **kwargs)

    with patch.object(git_native, "_git", side_effect=_spy):
        result = git_native.commit_authored_content(
            "file.txt", "AUTHORED CONTENT\n", msg_file, repo
        )

    assert result.ok, result.stderr
    verbs = [argv[0] for argv in git_argvs]
    assert verbs == [
        "rev-parse",
        "ls-tree",
        "read-tree",
        "update-index",
        "write-tree",
        "interpret-trailers",
        "commit-tree",
        "update-ref",
        "update-index",
    ]
    assert _committed_content_at_head(repo, "file.txt") == "AUTHORED CONTENT\n"


def _committed_content_at_head(repo, rel: str) -> str:
    result = subprocess.run(
        ["git", "show", f"HEAD:{rel}"], cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return result.stdout


def test_hash_object_stdin_bytes_carries_the_windows_safe_creationflag(tmp_path):
    """`_hash_object_stdin_bytes()` deliberately bypasses `_git()` (it needs
    raw-bytes stdin, not `_git()`'s `text=True` leg -- see its own
    docstring), so it re-implements the `creationflags=CREATE_NO_WINDOW`
    Windows-safe flag independently rather than inheriting it. The composite
    `commit_authored_content` test above spies on `git_native._git` only,
    which this function never calls, leaving this one call site unverified
    -- this test closes that gap directly against the real
    `subprocess.run` call."""
    repo = _init_real_repo(tmp_path)
    (repo / "file.txt").write_text("original\n", encoding="utf-8")
    _real_git(["add", "--", "file.txt"], repo)
    _real_git(["commit", "-q", "-m", "baseline"], repo)

    real_run = subprocess.run
    captured_kwargs = []

    def _spy(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return real_run(*args, **kwargs)

    with patch("subprocess.run", side_effect=_spy):
        result = git_native._hash_object_stdin_bytes(b"hashed content\n", "file.txt", cwd=repo)

    assert result.ok, result.stderr
    assert len(captured_kwargs) == 1
    kwargs = captured_kwargs[0]
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert kwargs["input"] == b"hashed content\n"
    assert "text" not in kwargs
    assert "encoding" not in kwargs


def test_cat_file_batch_carries_the_windows_safe_creationflag(tmp_path):
    """`cat_file_batch()` deliberately bypasses `_git()` (raw-bytes stdin/
    stdout, not `_git()`'s `text=True` leg -- see its own docstring), so it
    re-implements the `creationflags=CREATE_NO_WINDOW` Windows-safe flag
    independently rather than inheriting it -- mirrors
    `test_hash_object_stdin_bytes_carries_the_windows_safe_creationflag`
    above for the other bytes-mode bypass in this module."""
    repo = _init_real_repo(tmp_path)
    (repo / "file.txt").write_text("original\n", encoding="utf-8")
    _real_git(["add", "--", "file.txt"], repo)
    _real_git(["commit", "-q", "-m", "baseline"], repo)

    real_run = subprocess.run
    captured_kwargs = []

    def _spy(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return real_run(*args, **kwargs)

    with patch("subprocess.run", side_effect=_spy):
        result = git_native.cat_file_batch(repo, "HEAD", ["file.txt"])

    assert result == {"file.txt": "original\n"}
    assert len(captured_kwargs) == 1
    kwargs = captured_kwargs[0]
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert kwargs["input"] == b"HEAD:file.txt\n"
    assert "text" not in kwargs


def test_cat_file_batch_empty_paths_returns_empty_dict_no_spawn(tmp_path):
    """Empty `rel_paths` short-circuits to `{}` without spawning a subprocess
    -- mirrors every other empty-input guard in this module."""
    repo = _init_real_repo(tmp_path)
    with patch("subprocess.run") as mock_run:
        result = git_native.cat_file_batch(repo, "HEAD", [])
    assert result == {}
    mock_run.assert_not_called()


def test_cat_file_batch_missing_path_resolves_to_none(tmp_path):
    """A `rel_path` absent at `ref` resolves to `None`, never a raised
    error or a silently-dropped key -- the "missing" case in
    `git cat-file --batch`'s own header line."""
    repo = _init_real_repo(tmp_path)
    (repo / "file.txt").write_text("original\n", encoding="utf-8")
    _real_git(["add", "--", "file.txt"], repo)
    _real_git(["commit", "-q", "-m", "baseline"], repo)

    result = git_native.cat_file_batch(repo, "HEAD", ["file.txt", "does-not-exist.txt"])
    assert result == {"file.txt": "original\n", "does-not-exist.txt": None}


def _repo_with_two_commits(tmp_path):
    """Real repo where `file.txt` holds different content at two commits, and
    `second-only.txt` exists at the newer one alone. Returns
    (repo, first_sha, second_sha)."""
    repo = _init_real_repo(tmp_path)
    (repo / "file.txt").write_text("first\n", encoding="utf-8")
    _real_git(["add", "--", "file.txt"], repo)
    _real_git(["commit", "-q", "-m", "first"], repo)
    first_sha = git_native.rev_parse_head(repo).stdout.strip()

    (repo / "file.txt").write_text("second\n", encoding="utf-8")
    (repo / "second-only.txt").write_text("added later\n", encoding="utf-8")
    _real_git(["add", "--", "file.txt", "second-only.txt"], repo)
    _real_git(["commit", "-q", "-m", "second"], repo)
    second_sha = git_native.rev_parse_head(repo).stdout.strip()
    return repo, first_sha, second_sha


def test_cat_file_batch_objects_resolves_specs_across_different_revs_in_one_call(tmp_path):
    """THE property this function exists for, and the one most likely to break
    if the parser is later "simplified": many `<rev>:<path>` specs spanning
    DIFFERENT revs, resolved in a SINGLE spawn, each bound to its own slot.

    `cat_file_batch()` can only batch many paths at ONE ref, which forces a
    caller needing (rev, path) PAIRS into one spawn per rev -- a cold Windows
    `git` process per commit over a whole history walk. The vendored-schema
    full-history sweep is that caller. If this ever silently collapsed to
    per-rev behaviour, or mis-aligned records across the batch, the sweep
    would read one commit's blob as another's and mis-attribute schema debt.
    """
    repo, first_sha, second_sha = _repo_with_two_commits(tmp_path)

    specs = [
        f"{second_sha}:file.txt",
        f"{first_sha}:file.txt",
        f"{second_sha}:second-only.txt",
        f"{first_sha}:second-only.txt",
    ]
    real_run = subprocess.run
    spawns = []

    def _spy(*args, **kwargs):
        spawns.append(args)
        return real_run(*args, **kwargs)

    with patch("subprocess.run", side_effect=_spy):
        result = git_native.cat_file_batch_objects(repo, specs)

    assert result == {
        f"{second_sha}:file.txt": "second\n",
        f"{first_sha}:file.txt": "first\n",
        f"{second_sha}:second-only.txt": "added later\n",
        # Absent at the older commit -- `None`, never the other rev's blob and
        # never a dropped key.
        f"{first_sha}:second-only.txt": None,
    }
    assert len(spawns) == 1, "four specs across two revs must cost exactly one spawn"


def test_cat_file_batch_objects_carries_the_windows_safe_creationflag(tmp_path):
    """Same bytes-mode `_git()` bypass as `cat_file_batch` (which now delegates
    here), so the `creationflags=CREATE_NO_WINDOW` flag is re-implemented
    rather than inherited and must be asserted at this layer -- mirrors
    `test_cat_file_batch_carries_the_windows_safe_creationflag` above."""
    repo = _init_real_repo(tmp_path)
    (repo / "file.txt").write_text("original\n", encoding="utf-8")
    _real_git(["add", "--", "file.txt"], repo)
    _real_git(["commit", "-q", "-m", "baseline"], repo)

    real_run = subprocess.run
    captured_kwargs = []

    def _spy(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return real_run(*args, **kwargs)

    with patch("subprocess.run", side_effect=_spy):
        result = git_native.cat_file_batch_objects(repo, ["HEAD:file.txt"])

    assert result == {"HEAD:file.txt": "original\n"}
    assert len(captured_kwargs) == 1
    kwargs = captured_kwargs[0]
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert kwargs["input"] == b"HEAD:file.txt\n"
    assert "text" not in kwargs


def test_cat_file_batch_objects_empty_input_returns_empty_dict_no_spawn(tmp_path):
    """Empty `objects` short-circuits to `{}` without spawning -- mirrors every
    other empty-input guard in this module."""
    repo = _init_real_repo(tmp_path)
    with patch("subprocess.run") as mock_run:
        result = git_native.cat_file_batch_objects(repo, [])
    assert result == {}
    mock_run.assert_not_called()


def test_cat_file_batch_objects_bogus_rev_resolves_to_none(tmp_path):
    """An unresolvable REV (not merely a missing path) is `git cat-file
    --batch`'s "missing" header just the same, and must land as `None` in that
    spec's own slot without disturbing the resolvable specs beside it. The
    history sweep depends on this: a pruned or shallow-clone commit must read
    as "no data", never as an error and never as a neighbour's blob."""
    repo = _init_real_repo(tmp_path)
    (repo / "file.txt").write_text("original\n", encoding="utf-8")
    _real_git(["add", "--", "file.txt"], repo)
    _real_git(["commit", "-q", "-m", "baseline"], repo)

    result = git_native.cat_file_batch_objects(
        repo,
        ["no-such-rev:file.txt", "HEAD:file.txt", "HEAD:does-not-exist.txt"],
    )
    assert result == {
        "no-such-rev:file.txt": None,
        "HEAD:file.txt": "original\n",
        "HEAD:does-not-exist.txt": None,
    }


def test_commit_authored_content_explicit_deliverable_id_wins_over_session_resolved(tmp_path):
    """Bound 2's contract: an explicitly-passed `deliverable_id` is the
    tier-0 join key and must win over a `Deliverable-Id` trailer
    `compute_missing_trailer_args()` itself resolves from the session/
    claimed-plan tiers -- the caller-supplied value must never be silently
    dropped just because the session/claimed-plan tiers also had an answer."""
    repo = _init_real_repo(tmp_path)
    (repo / "file.txt").write_text("original\n", encoding="utf-8")
    _real_git(["add", "--", "file.txt"], repo)
    _real_git(["commit", "-q", "-m", "baseline"], repo)

    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("a commit message\n", encoding="utf-8")

    def _fake_compute_missing_trailer_args(_msg_file, _root, paths=None):
        return ["--trailer", "Session-Id: session-resolved-session-id", "--trailer",
                "Deliverable-Id: session-resolved-deliverable-id"]

    with patch.object(
        git_native, "compute_missing_trailer_args", side_effect=_fake_compute_missing_trailer_args
    ):
        result = git_native.commit_authored_content(
            "file.txt",
            "AUTHORED CONTENT\n",
            msg_file,
            repo,
            deliverable_id="caller-supplied-deliverable-id",
        )

    assert result.ok, result.stderr
    log_result = subprocess.run(
        ["git", "log", "-1", "--format=%B"], cwd=str(repo), capture_output=True, text=True, check=True,
    )
    assert "Deliverable-Id: caller-supplied-deliverable-id" in log_result.stdout
    assert "Deliverable-Id: session-resolved-deliverable-id" not in log_result.stdout
    assert "Session-Id: session-resolved-session-id" in log_result.stdout


def test_validate_explicit_deliverable_id_accepts_archived_spec_only(tmp_path):
    """AC3 (C1, docs/plans/2026-08-10-archived-specs-rejoin-the-scan-surface.md):
    a deliverable_id resolving ONLY from an artifact under archive/specs/<YYYY-MM>/
    (the fourth scan root `_scan_artifacts_by_deliverable_id` gained in C1) is
    ACCEPTED by `_validate_explicit_deliverable_id` -- returns None, not a
    rejection diagnostic. No control-flow change was made in this function; it
    delegates entirely to the scanner, so this pins that the widened scan
    surface is actually reachable through the guard."""
    root = tmp_path / "repo"
    specs_dir = root / "archive" / "specs" / "2026-08"
    specs_dir.mkdir(parents=True)
    (specs_dir / "2026-08-01-archived-plan.md").write_text(
        "---\ndeliverable_id: dlv-archived-spec-only-guard\n---\n\n# archived plan\n",
        encoding="utf-8",
    )

    result = git_native._validate_explicit_deliverable_id(
        "dlv-archived-spec-only-guard", root
    )

    assert result is None, (
        f"expected acceptance (None) for a deliverable_id resolving only from "
        f"archive/specs, got rejection: {result!r}"
    )
