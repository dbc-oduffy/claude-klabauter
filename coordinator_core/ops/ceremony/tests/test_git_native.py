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

from coordinator_core.win_portability import leaf_spawn_creationflags

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
    (git_native.status_porcelain_scoped, ("/tmp/repo", ["sub"]), {}),
    (git_native.status_porcelain_v2, ("/tmp/repo",), {}),
    (git_native.patch_touched_paths, ("/tmp/patch.diff", "/tmp/repo"), {}),
    (git_native.diff_cached_name_status, ("/tmp/repo",), {}),
    (git_native.diff_cached_name_only, ("/tmp/repo",), {}),
    (git_native.diff_quiet, ("/tmp/repo",), {}),
    (git_native.add_paths, ("/tmp/repo", ["a.txt", "b.txt"]), {}),
    (git_native.ls_files_deleted, ("/tmp/repo", ["a.txt", "b.txt"]), {}),
    (git_native.reset_paths, ("/tmp/repo", ["a.txt", "b.txt"]), {}),
    (git_native.commit_with_message_file, ("/tmp/repo", "/tmp/msg", ["a.txt"]), {}),
    (git_native.add_paths_pathspec_file, ("/tmp/repo", ["a.txt", "b.txt"]), {}),
    (
        git_native.commit_with_message_file_pathspec_scoped,
        ("/tmp/repo", "/tmp/msg", ["a.txt"]),
        {},
    ),
    # rev_parse_head is deliberately ABSENT: it no longer spawns at all (it
    # reads `git_state.head_sha()`), so there is no subprocess call for this
    # list's flag assertions to inspect. Its replacement guard, which asserts
    # the STRONGER property, is
    # `test_rev_parse_head_spawns_nothing` below -- removing a wrapper from
    # this list without pinning zero-spawn elsewhere would let a future edit
    # quietly reintroduce the process with nothing to catch it.
    (git_native.log_grep, ("/tmp/repo", "Session-Id: abc"), {}),
    (git_native.log_diff_filter, ("/tmp/repo", "R"), {}),
    (git_native.remote, ("/tmp/repo",), {}),
    (git_native.push, ("/tmp/repo",), {}),
    (git_native.fetch, ("/tmp/repo", "origin"), {}),
    (git_native.rebase_onto, ("/tmp/repo", "origin/main", "abc123"), {}),
    (git_native.rebase_abort, ("/tmp/repo",), {}),
    (git_native.merge_base, ("/tmp/repo", "HEAD", "origin/main"), {}),
    (git_native.merge_base_is_ancestor, ("/tmp/repo", "HEAD", "origin/main"), {}),
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
#: `stage_from_patch` / `stage_from_patch_cas_refusal` are the same shape of
#: exclusion for the same reason (added 2026-08-19, closing a standing red in
#: `test_all_public_wrappers_are_covered`): `stage_from_patch` issues a whole
#: `apply --numstat` / `ls-files --cacheinfo` / `apply --cached` sequence of
#: `_git()` calls internally, and `stage_from_patch_cas_refusal` composes a
#: refusal envelope over `_head_blobs()` -- neither is the single-mocked-
#: return-value shape the (a) harness expresses. Their behavioural coverage
#: lives in `test_commit_pipeline.py` / `test_scoped_git_commit.py`; the AC3
#: mechanical property this module exists to enforce is covered for them by
#: `test_composite_entrypoints_never_call_subprocess_run_directly` below,
#: which is genuinely new coverage rather than a blanket exemption.
#: `commit_authored_new_file` is `commit_authored_content`'s creation
#: sibling and is excluded for the same reason -- not a thin single-call
#: wrapper the (a) harness can express. Its own AC3-shaped property is
#: STRONGER than the flag assertions, and is asserted in
#: `test_commit_authored_new_file.py` rather than here: at most ONE git
#: process for the whole entrypoint, counted at `subprocess.Popen` rather
#: than at the `_git()` seam, plus a hook canary proving no hook of the
#: destination repository fired on our behalf.
_COMPOSITE_ENTRYPOINTS = {
    "commit_scoped",
    "commit_authored_content",
    "commit_authored_new_file",
    "stage_from_patch",
    "stage_from_patch_cas_refusal",
}

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
#: `deferred_publisher_span` (added 2026-08-19, same standing-red closure as
#: the `_COMPOSITE_ENTRYPOINTS` additions above) belongs here rather than
#: there: it is a `@contextmanager` that sets and resets a ContextVar and
#: issues NO `git` call of any kind, composite or otherwise, so both the (a)
#: harness and the composite `subprocess.run`-absence check are inapplicable
#: — the former would assert a call count of 1 against a function that never
#: spawns. Its actual behaviour (including the nested-span reset contract) is
#: covered by `test_sole_publisher_suppression.py`.
#: `rev_parse_head` (2026-08-23) is the first entry here that was ONCE a real
#: spawning wrapper and stopped being one, rather than never having been one.
#: It now reads `git_state.head_sha()` -- `.git/HEAD` plus the loose ref or
#: `packed-refs`, the same resolution `git rev-parse HEAD` performs -- so the
#: (a) harness has no `subprocess.run` call to assert its flags against.
#: Listed as an exemption from THAT harness, never from coverage: it is
#: covered directly and more strictly by
#: `test_rev_parse_head_spawns_nothing` (patches `run` AND `Popen`, asserts
#: zero, and asserts the failure branch stays reachable for callers that
#: check `.ok`) and `test_rev_parse_head_returns_head_sha_with_trailing_
#: newline` (compares the in-process text byte-for-byte against a real `git
#: rev-parse HEAD`, which is the control the whole substitution rests on).
#: A wrapper that stops spawning must land in this set AND gain a zero-spawn
#: guard in the same commit -- moving it here alone would silently retire the
#: only assertion standing over it.
_NON_SUBPROCESS_HELPERS = {
    "directory_pathspecs",
    "directory_pathspec_diagnostic",
    "parse_check_ignore_stdin_z",
    "deferred_publisher_span",
    "rev_parse_head",
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


def test_rev_parse_head_spawns_nothing(tmp_path):
    """`rev_parse_head` must reach HEAD's sha WITHOUT a process.

    Replaces its row in `_WRAPPER_INVOCATIONS` (see the note there). That row
    asserted the Windows-safe flags on the spawn this wrapper used to make;
    the wrapper now reads `git_state.head_sha()` -- `.git/HEAD` plus the loose
    ref or `packed-refs`, the same resolution `git rev-parse HEAD` performs --
    so there is no call for those assertions to inspect.

    Dropping the row without this test would leave the wrapper unguarded in
    BOTH directions: nothing would notice a reintroduced spawn, and nothing
    would notice it going back to being wrong about the flags. Asserting zero
    is the stronger claim and the one worth pinning.

    Envelope shape is asserted alongside the spawn count deliberately: every
    caller branches on `.ok`/`.stdout`, and `stdout` must keep `git
    rev-parse`'s trailing newline, because a caller that sliced instead of
    stripping would otherwise see a silent off-by-one.
    """
    subprocess_module = git_native.subprocess
    with patch.object(subprocess_module, "run") as mock_run:
        with patch.object(subprocess_module, "Popen") as mock_popen:
            result = git_native.rev_parse_head(str(tmp_path))

    assert mock_run.call_count == 0, "rev_parse_head spawned a process"
    assert mock_popen.call_count == 0, "rev_parse_head spawned a process"

    # tmp_path is not a repo -- HEAD does not resolve, so this is the failure
    # branch, which must stay reachable for every caller that checks `.ok`.
    assert result.returncode != 0
    assert result.stdout == ""


def test_rev_parse_head_returns_head_sha_with_trailing_newline(tmp_path):
    """The success branch, against a real repo: same text `git rev-parse` emits."""
    repo = _init_real_repo(tmp_path)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _real_git(["add", "--", "seed.txt"], repo)
    _real_git(["commit", "-q", "-m", "seed"], repo)

    result = git_native.rev_parse_head(repo)

    assert result.returncode == 0
    assert result.stdout.endswith("\n"), "callers strip(); the newline is contract"
    assert len(result.stdout.strip()) == 40

    # Matches what the spawn used to return, byte for byte. This is the
    # control the whole change rests on: the in-process read is only a
    # legitimate substitution if it produces the same text git does.
    spawned = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, text=True, check=True,
    )
    assert result.stdout == spawned.stdout


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
    (git_native.status_porcelain_scoped, ("/tmp/repo", ["sub"]), {}, "status"),
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


def test_composite_entrypoints_never_call_subprocess_run_directly():
    """AC3 for every `_COMPOSITE_ENTRYPOINTS` member: a composite is exempt
    from the (a) harness because it issues MANY `_git()` calls, not one — it
    is NOT exempt from routing all of them through `_git()`.

    The (a) harness enforces the Windows-safe flag set per call. `_git()` is
    the single choke point that carries it (`CREATE_NO_WINDOW`, DEVNULL
    stdin, `text=True`), so a composite that reached `subprocess.run`
    directly would bypass that contract with nothing to catch it — the
    exemption would have become a hole. Asserted over the SET rather than
    per-named-function so a member added to `_COMPOSITE_ENTRYPOINTS` later
    inherits the check instead of silently escaping it.

    Source-level, not behavioural: a composite's real-git behaviour is
    covered in `test_commit_scoped.py` / `test_commit_pipeline.py` /
    `test_scoped_git_commit.py`. What no behavioural test can assert is the
    ABSENCE of a direct spawn on a branch that test happened not to take.
    """
    for name in sorted(_COMPOSITE_ENTRYPOINTS):
        source = inspect.getsource(getattr(git_native, name))
        assert "subprocess.run" not in source, (
            f"{name}() calls subprocess.run directly — every composite "
            "entrypoint must route through git_native._git(), which is the "
            "sole carrier of the Windows-safe flag set this module enforces."
        )


def test_commit_authored_content_issues_its_git_sequence_through_the_shared_git_wrapper(tmp_path):
    """`commit_authored_content()` never reaches `subprocess.run` directly --
    every step of its residual `_git()`-routed sequence (whose Windows-
    safe-flag contract is already independently covered by
    `test_git_returns_typed_result_on_success` and friends above) is
    asserted here, so patching `subprocess.run` directly would double-count
    that contract and, worse, would also catch unrelated background
    subprocess activity elsewhere in the process (`git_native.subprocess`
    IS the shared stdlib module object, not a private import). This
    asserts the composite's actual call sequence instead -- the genuine
    coverage gap `test_all_public_wrappers_are_covered` flagged.

    C4 (docs/plans/2026-08-22-a-commit-is-one-spawn-not-eleven.md): on the
    happy path exercised here (a plain, non-detached checkout with no
    concurrent lock holder and a resolvable commit identity), the whole
    tree-spine rewrite, the commit object, and the ref CAS are now built
    IN PROCESS (`_commit_via_head_spine`) -- the former `read-tree`/
    `update-index --cacheinfo`/`write-tree`/`commit-tree`/`update-ref`
    quintet is gone from this wrapper's own `_git()`-routed sequence
    entirely (AC1: at most `interpret-trailers` and the bound-6 shared
    `update-index` remain, alongside `_hash_object_stdin_bytes`'s own
    bytes-mode `hash-object` spawn, which bypasses `_git()` and is
    covered separately below). `rev-parse HEAD` is also gone -- `head_sha()`
    is a direct `.git/HEAD` file read (C3, asserted separately below by
    patching `_git` to fail loud on any `rev-parse` call).

    `ls-tree HEAD -- <path>` is gone TOO, and this assertion is the point of
    the test rather than a detail. An earlier revision pinned it at
    `== ["ls-tree"]` with the rationale "the one retained read spawn ...
    should route through git_state's shared `run_git`, not git_native
    `_git()`" -- a ROUTING property, written when that spawn was believed
    irreducible. It was not: `commit_authored_content` was calling
    `head_blobs` unconditionally BEFORE the fast/ladder fork, so the
    in-process arm paid an `ls-tree` it never needed. `_head_entry_for` now
    reads HEAD's tree spine in process and falls back to `head_blobs` only
    when the spine is unreadable -- which is the ladder condition anyway.
    Not spawning beats spawning through the right wrapper, so the assertion
    is inverted: ZERO `run_git` calls on this arm.

    Why this mattered more than one stale literal: `head_blobs` reaches git
    through `run_git` and never crosses the `git_native._git()` seam, so a
    spawn counter wrapped on `_git()` reported AC1's "<=3" while the leg
    genuinely issued FOUR processes (`ls-tree`, `hash-object`,
    `interpret-trailers`, `update-index`). No spawn assertion in this plan
    caught that; `test_ledger_leg_spawn_count_is_upper_bound_not_exact` did,
    by asserting a PAIR moved together. Hence the ladder-arm case below: an
    assertion that only ever sees the fast arm cannot tell "correctly not
    spawned" from "silently routed elsewhere"."""
    repo = _init_real_repo(tmp_path)
    (repo / "file.txt").write_text("original\n", encoding="utf-8")
    _real_git(["add", "--", "file.txt"], repo)
    _real_git(["commit", "-q", "-m", "baseline"], repo)

    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("a commit message\n", encoding="utf-8")

    real_git = git_native._git
    git_argvs = []

    def _spy(args, **kwargs):
        assert args[0] != "rev-parse", (
            "commit_authored_content must not spawn `git rev-parse HEAD` any "
            "more -- head_sha() is a direct file read (C3)"
        )
        assert args[0] not in ("read-tree", "write-tree", "commit-tree", "update-ref"), (
            f"commit_authored_content took the fast in-process path's "
            f"preconditions but still spawned `git {args[0]}` -- the "
            "spine/commit/CAS build must stay in process on this happy path "
            "(AC1)"
        )
        git_argvs.append(list(args))
        return real_git(args, **kwargs)

    from coordinator_core.git import git_state as _git_state_module

    real_run_git = _git_state_module.run_git
    run_git_argvs = []

    def _run_git_spy(args, **kwargs):
        run_git_argvs.append(list(args))
        return real_run_git(args, **kwargs)

    with patch.object(git_native, "_git", side_effect=_spy), \
         patch.object(_git_state_module, "run_git", side_effect=_run_git_spy):
        result = git_native.commit_authored_content(
            "file.txt", "AUTHORED CONTENT\n", msg_file, repo
        )

    assert result.ok, result.stderr
    assert run_git_argvs == [], (
        "the in-process arm must not spawn AT ALL for HEAD's tree entry -- "
        "_head_entry_for reads the spine in process. Got %r. `head_blobs` "
        "does not cross the _git() seam, so an `ls-tree` reappearing here is "
        "invisible to every spawn counter wrapped on that seam: this "
        "assertion is the only thing standing between a silent 4th process "
        "and an AC1 that reads <=3" % (run_git_argvs,)
    )
    verbs = [argv[0] for argv in git_argvs]
    assert verbs == ["update-index"], (
        "AC1: only the bound-6 shared update-index should remain through "
        "_git() on the fast, in-process path. The `interpret-trailers` spawn "
        "that used to sit ahead of it is gone -- `_apply_trailers` formats "
        "trailers in process for any message inside "
        "`commit_trailers.can_format_trailers_in_process` (no `#` comment "
        "line), which this fixture's message is. A message OUTSIDE that "
        "envelope still spawns git, so this list growing an "
        "`interpret-trailers` back means the envelope narrowed -- not that "
        "the replacement was reverted"
    )
    assert _committed_content_at_head(repo, "file.txt") == "AUTHORED CONTENT\n"
    fsck = subprocess.run(
        ["git", "fsck", "--strict"], cwd=str(repo), capture_output=True, text=True,
    )
    assert fsck.returncode == 0, fsck.stderr


def test_commit_authored_content_refuses_a_path_absent_from_head(tmp_path):
    """C3: `head_sha`/`head_blobs` replace the `rev-parse`/`ls-tree` reads,
    but the reserved-noun "does not exist in HEAD" refusal this entrypoint's
    own docstring names (`commit_authored_content` is for in-place mutation
    of an EXISTING file, not creating a new one) must still fire -- exercise
    it on a repo whose HEAD genuinely lacks the path, the easy case to hit:
    a fresh repo with one committed file and no `ledger.txt` at all."""
    repo = _init_real_repo(tmp_path)
    (repo / "other.txt").write_text("seed\n", encoding="utf-8")
    _real_git(["add", "--", "other.txt"], repo)
    _real_git(["commit", "-q", "-m", "baseline"], repo)

    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("a commit message\n", encoding="utf-8")

    result = git_native.commit_authored_content(
        "ledger.txt", "NEW LEDGER CONTENT\n", msg_file, repo
    )

    assert not result.ok
    assert "does not exist in HEAD" in result.stderr
    assert "ledger.txt" in result.stderr


def test_commit_authored_content_cas_still_fails_loud_on_concurrent_head_move(tmp_path):
    """C3's swap of `rev-parse HEAD` for `git_state.head_sha()` must not
    weaken the ref CAS: `old_head` (however it was obtained) is passed as
    the CAS's expected old value, and the write is refused atomically if
    the ref no longer matches it at call time. Force exactly that race by
    advancing HEAD (a concurrent sibling's commit) in the window between
    `commit_authored_content`'s own `old_head` read and the CAS itself, and
    assert it still refuses loud rather than silently orphaning the
    sibling's commit.

    C4: the CAS now lands via `cas_ref()` (in-process, `git_objects.py`),
    not a spawned `git update-ref` -- so the race is injected by wrapping
    `git_native.cas_ref` itself (the module-level name this file's own
    `_commit_via_head_spine` calls), not by intercepting an `update-ref`
    argv through `_git()`."""
    repo = _init_real_repo(tmp_path)
    (repo / "file.txt").write_text("original\n", encoding="utf-8")
    _real_git(["add", "--", "file.txt"], repo)
    _real_git(["commit", "-q", "-m", "baseline"], repo)

    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("a commit message\n", encoding="utf-8")

    real_cas_ref = git_native.cas_ref

    def _sibling_commits_first(*args, **kwargs):
        (repo / "sibling.txt").write_text("sibling\n", encoding="utf-8")
        _real_git(["add", "--", "sibling.txt"], repo)
        _real_git(["commit", "-q", "-m", "concurrent sibling commit"], repo)
        return real_cas_ref(*args, **kwargs)

    with patch.object(git_native, "cas_ref", side_effect=_sibling_commits_first):
        result = git_native.commit_authored_content(
            "file.txt", "AUTHORED CONTENT\n", msg_file, repo
        )

    assert not result.ok
    assert "compare-and-swap" in result.stderr
    assert "concurrently" in result.stderr
    # The sibling's own commit must still be HEAD's tip -- a silently
    # orphaned peer commit is exactly the hazard this CAS exists to prevent.
    head_now = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()
    subject_now = subprocess.run(
        ["git", "log", "-1", "--format=%s", head_now], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert subject_now == "concurrent sibling commit"


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
    re-implements its Windows-safe creationflag independently rather than
    inheriting it -- mirrors
    `test_hash_object_stdin_bytes_carries_the_windows_safe_creationflag`
    above for the other bytes-mode bypass in this module.

    LEAF, not merely windowless. `git cat-file --batch` spawns nothing of
    its own, so it takes `leaf_spawn_creationflags()` (DETACHED_PROCESS --
    no console object at all), never `no_console_creationflags()`
    (CREATE_NO_WINDOW -- a windowless console the subtree can inherit).
    Asserted against the primitive rather than a literal so the two cannot
    drift apart again: this assertion was left behind at CREATE_NO_WINDOW
    when the call site migrated, and read as a red for three sessions."""
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
    assert kwargs["creationflags"] == leaf_spawn_creationflags().get("creationflags", 0)
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
    here), so the creationflag is re-implemented rather than inherited and
    must be asserted at this layer -- mirrors
    `test_cat_file_batch_carries_the_windows_safe_creationflag` above,
    including its LEAF-not-merely-windowless rationale."""
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
    assert kwargs["creationflags"] == leaf_spawn_creationflags().get("creationflags", 0)
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

    def _fake_compute_missing_trailer_args(_msg_file, _root, paths=None, session_id_override=None):
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


def test_validate_explicit_deliverable_id_accepts_pln_prefix(tmp_path):
    """C10b leg (c): the citation convention now PREFERS citing a plan by its
    own `pln-` id (§ PM rulings, docs/plans/2026-08-13-spec-backlinks-cite-a-
    stable-deliverable-id.md; DoE's ratified doctrine at their commit
    fa72d1642). An author who follows that convention and passes the
    resulting `pln-` id to `--deliverable-id` must be ACCEPTED, not rejected
    for doing what the convention instructs."""
    root = tmp_path / "repo"
    plans_dir = root / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "2026-08-13-pln-guard.md").write_text(
        "---\nplan_id: pln-guard-accepts-pln-prefix\n---\n\n# plan\n",
        encoding="utf-8",
    )

    result = git_native._validate_explicit_deliverable_id(
        "pln-guard-accepts-pln-prefix", root
    )

    assert result is None, (
        f"expected acceptance (None) for a resolvable pln- id, got rejection: {result!r}"
    )


def test_validate_explicit_deliverable_id_rejects_bogus_prefix(tmp_path):
    """Regression: a shape neither `dlv-` nor `pln-` is STILL rejected before
    any existence scan -- widening the shape check to admit `pln-` must not
    also admit an arbitrary prefix."""
    root = tmp_path / "repo"
    root.mkdir()

    result = git_native._validate_explicit_deliverable_id(
        "bogus-neither-prefix", root
    )

    assert result is not None
    assert "'dlv-' or 'pln-' shape convention" in result


def test_validate_explicit_deliverable_id_rejects_unresolvable_pln(tmp_path):
    """Regression: a `pln-`-shaped id that resolves to no real artifact is
    STILL rejected -- shape acceptance does not bypass the existence scan."""
    root = tmp_path / "repo"
    root.mkdir()

    result = git_native._validate_explicit_deliverable_id(
        "pln-does-not-exist-anywhere", root
    )

    assert result is not None
    assert "does not resolve to any real artifact" in result


# ---------------------------------------------------------------------------
# (h) C2 (2026-08-21): `_index_blobs`/`_head_blobs`/`_resolve_mode_for_paths`
# re-pointed onto `coordinator_core.git.git_state` -- index side spawn-free,
# HEAD side keeps its single `ls-tree` behind `git_state.head_blobs`.
# state/dispatch-briefs/2026-08-21-the-commit-path-reads-git-state-without-
# spawning-git/C2.md
# ---------------------------------------------------------------------------


def test_index_blobs_reads_staged_sha_with_no_git_spawn(tmp_path, monkeypatch):
    """`_index_blobs` must answer from `git_state.read_index` alone -- no
    `_git()` subprocess call reachable from this function any more (the
    INDEX side becomes spawn-free per this chunk's own AC)."""
    repo = _init_real_repo(tmp_path)
    (repo / "a.txt").write_text("one\n")
    _real_git(["add", "--", "a.txt"], repo)

    def _fail(*_a, **_kw):
        raise AssertionError("_index_blobs must not spawn git")

    monkeypatch.setattr(git_native, "_git", _fail)

    blobs = git_native._index_blobs(repo, ["a.txt", "missing.txt"])

    assert blobs["missing.txt"] is None
    assert blobs["a.txt"] is not None
    assert blobs["a.txt"] != git_native._GIT_READ_FAILED
    assert blobs["a.txt"] != git_native._GIT_PATH_UNRECONCILED


def test_index_blobs_empty_paths_returns_empty_dict_no_read(tmp_path, monkeypatch):
    def _fail(*_a, **_kw):
        raise AssertionError("read_index must not be reached for empty paths")

    monkeypatch.setattr(git_native, "read_index", _fail)

    assert git_native._index_blobs(tmp_path, []) == {}


def test_index_blobs_maps_parse_failure_to_git_read_failed_sentinel(tmp_path, monkeypatch):
    """A `read_index` failure (`IndexParseError`) must degrade EVERY
    requested path to `_GIT_READ_FAILED`, never a plain `None` -- the exact
    "degraded reads" P1 this sentinel exists to close (see `_index_blobs`'s
    own docstring)."""
    repo = _init_real_repo(tmp_path)

    def _raise(*_a, **_kw):
        raise git_native.IndexParseError("boom")

    monkeypatch.setattr(git_native, "read_index", _raise)

    blobs = git_native._index_blobs(repo, ["a.txt", "b.txt"])

    assert blobs["a.txt"] is git_native._GIT_READ_FAILED
    assert blobs["b.txt"] is git_native._GIT_READ_FAILED


def test_head_blobs_reads_committed_sha_via_git_state_reader(tmp_path):
    repo = _init_real_repo(tmp_path)
    (repo / "a.txt").write_text("one\n")
    _real_git(["add", "--", "a.txt"], repo)
    _real_git(["commit", "-q", "-m", "seed"], repo)

    blobs = git_native._head_blobs(repo, ["a.txt", "untracked.txt"])

    assert blobs["a.txt"] is not None
    assert blobs["untracked.txt"] is None


def test_head_blobs_unborn_branch_reads_as_absent_not_read_failed(tmp_path):
    """No commits yet -- `git_state.head_blobs` folds the "not a valid
    object name HEAD" case into an ordinary empty answer; this must still
    read as `None` (absent), never `_GIT_READ_FAILED`, matching pre-C2
    behaviour for the unborn-branch case."""
    repo = _init_real_repo(tmp_path)
    (repo / "a.txt").write_text("one\n")
    _real_git(["add", "--", "a.txt"], repo)

    blobs = git_native._head_blobs(repo, ["a.txt"])

    assert blobs["a.txt"] is None


def test_head_blobs_infra_failure_maps_to_git_read_failed_sentinel(tmp_path, monkeypatch):
    """A genuine infrastructure failure below `git_state.read_tree_spine`
    (e.g. an unresolvable `.git` dir) must still surface as
    `_GIT_READ_FAILED`, never silently downgraded to `None`. C11 (`state/
    dispatch-briefs/2026-08-23-the-scoped-commit-rebuilt-from-first-
    principles/C11.md`) re-pointed `_head_blobs` off `git_state.head_blobs`
    (a `git ls-tree` spawn) onto `git_state.read_tree_spine` (in-process),
    so this test now monkeypatches the load-bearing dependency."""
    repo = _init_real_repo(tmp_path)

    def _raise(*_a, **_kw):
        raise OSError("boom")

    monkeypatch.setattr(git_native, "read_tree_spine", _raise)

    blobs = git_native._head_blobs(repo, ["a.txt"])

    assert blobs["a.txt"] is git_native._GIT_READ_FAILED


def test_resolve_mode_for_paths_prefers_index_over_head(tmp_path):
    repo = _init_real_repo(tmp_path)
    (repo / "exe.sh").write_text("#!/bin/sh\n")
    _real_git(["add", "--", "exe.sh"], repo)
    _real_git(["update-index", "--chmod=+x", "exe.sh"], repo)
    _real_git(["commit", "-q", "-m", "seed"], repo)

    modes = git_native._resolve_mode_for_paths(repo, ["exe.sh"])

    assert modes["exe.sh"] == "100755"


def test_resolve_mode_for_paths_falls_back_to_head_when_no_index_entry(tmp_path):
    repo = _init_real_repo(tmp_path)
    (repo / "exe.sh").write_text("#!/bin/sh\n")
    _real_git(["add", "--", "exe.sh"], repo)
    _real_git(["update-index", "--chmod=+x", "exe.sh"], repo)
    _real_git(["commit", "-q", "-m", "seed"], repo)
    _real_git(["rm", "--cached", "-q", "exe.sh"], repo)

    modes = git_native._resolve_mode_for_paths(repo, ["exe.sh"])

    assert modes["exe.sh"] == "100755"


def test_resolve_mode_for_paths_absent_path_omitted(tmp_path):
    repo = _init_real_repo(tmp_path)

    modes = git_native._resolve_mode_for_paths(repo, ["nowhere.txt"])

    assert "nowhere.txt" not in modes


def test_agree_branch_cas_refusal_second_index_read_is_a_fresh_observation(tmp_path):
    """THE LOAD-BEARING CONSTRAINT (C2 brief): `_agree_branch_cas_refusal`'s
    SECOND `_index_blobs` call, taken immediately before the agree branch's
    own `git add`, must remain a genuine fresh observation once re-pointed
    onto `git_state.read_index` -- not a cached/stale snapshot. Mutates the
    index strictly BETWEEN the pre-snapshot and the CAS re-check and asserts
    the refusal still fires."""
    repo = _init_real_repo(tmp_path)
    (repo / "a.txt").write_text("one\n")
    _real_git(["add", "--", "a.txt"], repo)
    _real_git(["commit", "-q", "-m", "seed"], repo)

    pre_index_blobs = git_native._index_blobs(repo, ["a.txt"])
    pre_head_blobs = git_native._head_blobs(repo, ["a.txt"])

    # A peer's own write lands strictly between the pre-snapshot above and
    # the CAS re-check below -- the exact check-then-act window this
    # function exists to close.
    (repo / "a.txt").write_text("two\n")
    _real_git(["add", "--", "a.txt"], repo)

    result = git_native._agree_branch_cas_refusal(
        repo, ["a.txt"], pre_index_blobs, pre_head_blobs
    )

    assert result is not None
    assert result.ok is False
    assert "a.txt" in result.stderr
    assert "index entry changed since this call's own snapshot" in result.stderr


def test_agree_branch_cas_refusal_no_mutation_between_reads_does_not_refuse(tmp_path):
    """Sibling control case: with nothing mutated between the two reads, the
    CAS re-check must NOT refuse -- proves the fresh-observation test above
    is actually detecting the mutation, not just always refusing."""
    repo = _init_real_repo(tmp_path)
    (repo / "a.txt").write_text("one\n")
    _real_git(["add", "--", "a.txt"], repo)
    _real_git(["commit", "-q", "-m", "seed"], repo)

    pre_index_blobs = git_native._index_blobs(repo, ["a.txt"])
    pre_head_blobs = git_native._head_blobs(repo, ["a.txt"])

    result = git_native._agree_branch_cas_refusal(
        repo, ["a.txt"], pre_index_blobs, pre_head_blobs
    )

    assert result is None


def test_agree_branch_cas_refusal_stays_fresh_under_an_open_index_cache_scope(tmp_path):
    """C2 AC3: `_agree_branch_cas_refusal`'s own `_index_blobs` call must
    remain a real disk read even while `git_state.index_read_cache_scope()`
    is open for the surrounding commit -- the compare-and-swap must never be
    served from the within-call cache the OTHER three `read_index` callers
    now share. Same mutate-in-the-window shape as the unscoped test above,
    run with the scope active to prove `fresh=True` actually bypasses it
    rather than merely happening to still work outside one."""
    from coordinator_core.git.git_state import index_read_cache_scope

    repo = _init_real_repo(tmp_path)
    (repo / "a.txt").write_text("one\n")
    _real_git(["add", "--", "a.txt"], repo)
    _real_git(["commit", "-q", "-m", "seed"], repo)

    with index_read_cache_scope():
        pre_index_blobs = git_native._index_blobs(repo, ["a.txt"])
        pre_head_blobs = git_native._head_blobs(repo, ["a.txt"])

        (repo / "a.txt").write_text("two\n")
        _real_git(["add", "--", "a.txt"], repo)

        result = git_native._agree_branch_cas_refusal(
            repo, ["a.txt"], pre_index_blobs, pre_head_blobs
        )

    assert result is not None
    assert result.ok is False
    assert "a.txt" in result.stderr
    assert "index entry changed since this call's own snapshot" in result.stderr


# ---------------------------------------------------------------------------
# (h) commit identity: a miss on the cheap sources costs ONE spawn, not the ladder
# ---------------------------------------------------------------------------


def test_commit_identity_prefers_env_vars_without_spawning(tmp_path, monkeypatch):
    """Env vars are git's own highest-precedence source, so a hit here must
    not spawn at all -- the common case on every commit this box makes."""
    repo = _init_real_repo(tmp_path)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Env Name")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "env@example.test")

    with patch.object(git_native, "_git") as spawn:
        assert git_native._resolve_commit_identity(repo) == ("Env Name", "env@example.test")

    spawn.assert_not_called()


def test_commit_identity_falls_back_to_one_git_var_spawn_not_the_ladder(tmp_path, monkeypatch):
    """The regression this test exists for: when the cheap config readers
    miss -- `includeIf`, system config, any shape they do not parse -- the
    identity must be recovered by ONE `git var GIT_COMMITTER_IDENT` spawn,
    NOT by returning None and dropping the whole commit to the ~9-spawn
    ladder. The identity is not worth trading the in-process win for."""
    repo = _init_real_repo(tmp_path)
    for var in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(git_native, "_read_config_user_section", lambda _path: (None, None))

    calls: list[list[str]] = []
    real_git = git_native._git

    def _counting_git(args, **kwargs):
        calls.append(list(args))
        return real_git(args, **kwargs)

    monkeypatch.setattr(git_native, "_git", _counting_git)
    identity = git_native._resolve_commit_identity(repo)

    assert identity is not None, "a configured identity must not fall through to the ladder"
    name, email = identity
    assert name and email
    assert calls == [["var", "GIT_COMMITTER_IDENT"]], "exactly one spawn, and only git var"


def test_commit_identity_returns_none_when_git_itself_cannot_resolve(tmp_path, monkeypatch):
    """`git var` failing is a genuine ladder condition -- git has no identity
    either, so the ladder's own commit-tree will produce git's diagnostic,
    which beats anything invented here."""
    repo = _init_real_repo(tmp_path)
    for var in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(git_native, "_read_config_user_section", lambda _path: (None, None))
    monkeypatch.setattr(
        git_native,
        "_git",
        lambda args, **kwargs: git_native.GitResult(
            returncode=128, stdout="", stderr="fatal: unable to auto-detect email address",
        ),
    )

    assert git_native._resolve_commit_identity(repo) is None


def test_commit_identity_never_returns_a_half_pair(tmp_path, monkeypatch):
    """A malformed `git var` line must be a ladder fall-back, never a commit
    object carrying `"None <None>"` in its author line."""
    repo = _init_real_repo(tmp_path)
    for var in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(git_native, "_read_config_user_section", lambda _path: (None, None))
    monkeypatch.setattr(
        git_native,
        "_git",
        lambda args, **kwargs: git_native.GitResult(
            returncode=0, stdout="no brackets here at all\n", stderr="",
        ),
    )

    assert git_native._resolve_commit_identity(repo) is None


def test_head_entry_falls_back_to_head_blobs_when_the_spine_is_unreadable(tmp_path, monkeypatch):
    """The sibling of the zero-`run_git` assertion above, and the reason that
    one can be trusted.

    An assertion that only ever observes the fast arm cannot distinguish
    "correctly did not spawn" from "silently stopped being called at all" --
    if `_head_entry_for` regressed to returning `None` unconditionally, the
    fast-arm test would still see zero `run_git` calls and pass while every
    commit quietly took the ladder. This pins the OTHER direction: when the
    spine is unreadable, `head_blobs` must still be consulted and its
    `ls-tree` must still spawn, because that is the fall-back the
    in-process read is allowed to lean on.

    Together the pair says: zero spawns when the spine reads, exactly one
    when it does not, and never a silent third state."""
    repo = _init_real_repo(tmp_path)
    (repo / "file.txt").write_text("original\n", encoding="utf-8")
    _real_git(["add", "--", "file.txt"], repo)
    _real_git(["commit", "-q", "-m", "baseline"], repo)

    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("ladder-arm commit\n", encoding="utf-8")

    monkeypatch.setattr(git_native, "read_tree_spine", lambda *a, **kw: None)

    from coordinator_core.git import git_state as _git_state_module

    # `head_blobs` reads the spine ITSELF now (C2b retired its `ls-tree` for
    # the ordinary case), so patching only `git_native`'s binding no longer
    # reaches the read this test exists to fail. Patch both bindings: the
    # assertion below is unchanged and means exactly what it always meant --
    # an unreadable spine must still produce exactly one `ls-tree`, never a
    # silent empty answer. `head_blobs` returning `{}` here is precisely the
    # "silent third state" the docstring names.
    monkeypatch.setattr(_git_state_module, "read_tree_spine", lambda *a, **kw: None)

    real_run_git = _git_state_module.run_git
    run_git_argvs = []

    def _run_git_spy(args, **kwargs):
        run_git_argvs.append(list(args))
        return real_run_git(args, **kwargs)

    with patch.object(_git_state_module, "run_git", side_effect=_run_git_spy):
        result = git_native.commit_authored_content(
            "file.txt", "LADDER CONTENT\n", msg_file, repo
        )

    assert result.ok, result.stderr
    assert [argv[0] for argv in run_git_argvs] == ["ls-tree"], (
        "with the spine unreadable, the HEAD tree-entry read must fall back "
        "to head_blobs' ls-tree -- got %r. Zero here would mean the mode and "
        "existence read had been dropped rather than relocated" % (run_git_argvs,)
    )
    assert _committed_content_at_head(repo, "file.txt") == "LADDER CONTENT\n"


def _spine_repo(tmp_path, files):
    """A real repo with `files` ({relpath: content}) committed, returned as
    (root, gitdir). Real git, never a fixture double -- the property under
    test is agreement WITH git, so a stand-in would assert nothing."""
    root = tmp_path / "spine"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", "--", rel], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
    return root, root / ".git"


def _git_out(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.mark.parametrize(
    "head, moved, dest, expect_gone",
    [
        # Source dir emptied by the move -- `a/b` must vanish, and with it
        # `a`, whose only child it was. The cascade is the half a
        # single-level prune would miss.
        (
            {"a/b/only.txt": "payload\n", "c/existing.txt": "stay\n", "keep.txt": "r\n"},
            "a/b/only.txt",
            "c/only.txt",
            ["a", "a/b"],
        ),
        # Source dir still occupied -- nothing may be pruned. Guards the
        # prune against over-firing.
        (
            {"a/one.txt": "1\n", "a/two.txt": "2\n", "c/existing.txt": "stay\n"},
            "a/one.txt",
            "c/one.txt",
            [],
        ),
    ],
)
def test_rewrite_head_spine_prunes_emptied_dirs_like_git(
    tmp_path, head, moved, dest, expect_gone
):
    """`_rewrite_head_spine`'s root sha equals `git write-tree`'s for the same
    content, including when a rename empties its source directory.

    Git has no empty directory: `write-tree` OMITS one rather than emitting an
    entry for the canonical empty tree. Before the prune, the rewrite wrote one
    anyway, so the root sha diverged from git's and the commit landed at rc=0
    carrying a tree git would never have produced -- no ladder, no refusal.
    Reported by claude-klabauter-15 against three live callers; the triggering
    shape is a rename that empties its source directory, i.e. an archival batch.

    Negative-spec: do NOT relax this to "the tree contains the right files".
    The whole property is SHA-IDENTITY with git, because a tree that merely
    holds the right blobs still diverges, and divergence is the defect.
    """
    root, gitdir = _spine_repo(tmp_path, head)
    blob = _git_out(root, "rev-parse", f"HEAD:{moved}")

    from coordinator_core.git import git_state

    spine = git_state.read_tree_spine(root, [moved, dest])
    assert spine is not None, "probe shape wrong -- spine unreadable"

    ours = git_native._rewrite_head_spine(
        gitdir, spine, {moved: git_native._ABSENT, dest: (0o100644, blob)}
    )

    # Ground truth: let git perform the identical rename and compute the tree.
    (root / dest).parent.mkdir(parents=True, exist_ok=True)
    (root / dest).write_text((root / moved).read_text(encoding="utf-8"), encoding="utf-8")
    (root / moved).unlink()
    subprocess.run(["git", "add", "--", dest], cwd=root, check=True)
    subprocess.run(["git", "add", "-u", "--", moved], cwd=root, check=True)
    theirs = _git_out(root, "write-tree")

    assert ours == theirs, (
        f"root tree sha diverges from git: ours={ours} git={theirs}. "
        f"ours:\n{_git_out(root, 'cat-file', '-p', ours)}\n"
        f"git:\n{_git_out(root, 'cat-file', '-p', theirs)}"
    )

    listing = _git_out(root, "ls-tree", "-r", "-t", "--name-only", ours).splitlines()
    for gone in expect_gone:
        assert gone not in listing, (
            f"{gone!r} survived as a phantom directory entry -- git prunes it, "
            f"so an entry here is the empty-tree bug. Tree:\n{listing}"
        )


# ---------------------------------------------------------------------------
# C3 (state/dispatch-briefs/2026-08-26-the-commit-becomes-a-warm-served-op/
# C3.md): the agree branch's own `git add` + `git commit -F` pair is gone,
# replaced by `_commit_scoped_private_index` -- the same in-process
# tree-spine rewrite + `cas_ref` landing route already proven for the
# diverged branch. These tests pin two of that dispatch's named hazards:
# a zero-spawn (or one-spawn, for a genuinely new worktree blob) landing on
# the ordinary loose-ref case, and correct behaviour immediately after
# `git pack-refs --all` packs the branch ref (the "packed-refs breaks it
# outright" hazard the spike verdict names -- here it is not a hazard at
# all, because `_resolve_cas_ref_target` already refuses a non-loose ref
# and `_commit_scoped_private_index` already falls back to its own
# `commit-tree`/`update-ref` ladder in that case).
# ---------------------------------------------------------------------------


def test_agree_branch_commits_at_zero_or_one_spawn_via_private_index_route(tmp_path, monkeypatch):
    """AC6: the agree branch (`diverged == []`) must land through
    `_commit_via_head_spine`'s fast path -- object writes plus a locked
    `cas_ref`, never a spawned `git add`/`git commit` pair. A brand-new,
    not-yet-tracked file still costs exactly ONE spawn (`git hash-object -w
    --stdin-paths`, through git's own clean-filter pipeline) -- the NAMED
    exception this dispatch's brief carries, never a silent violation of
    the <=1 budget."""
    repo = _init_real_repo(tmp_path)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _real_git(["add", "-A"], repo)
    _real_git(["commit", "-q", "-m", "seed"], repo)

    (repo / "new.txt").write_text("brand new content\n", encoding="utf-8")
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("agree branch commit\n", encoding="utf-8")

    argvs: list[list[str]] = []
    real_run = subprocess.run

    def _spy(args, *a, **kw):
        argvs.append(list(args))
        return real_run(args, *a, **kw)

    monkeypatch.setattr(git_native.subprocess, "run", _spy)

    result = git_native.commit_scoped(["new.txt"], msg_file, repo)

    assert result.ok, result.stderr
    # `commit_scoped`'s own pre-branch-selection divergence check (`git
    # status`/`git show` -- unrelated to this dispatch, unchanged by it)
    # still spawns; what this dispatch's AC6 bounds is the LANDING
    # mechanism itself -- never a bare `add`, never a real `commit`, and at
    # most one `hash-object` (the named worktree-blob exception).
    landing_spawns = [
        a for a in argvs
        if len(a) > 1 and a[1] in ("add", "commit", "hash-object")
    ]
    # AT MOST one `hash-object`, which is what the comment above has always
    # said and what the AC bounds -- not EXACTLY one.
    #
    # This assertion previously read `== [["git", "hash-object", ...]]` and so
    # failed the moment C3c/C3d made the blob write in-process: the op got
    # BETTER and the test went red on the improvement rather than on a
    # regression. That is the fourth instance of this shape on this
    # deliverable (the baton records three others asserting `len(calls) == 1`,
    # each pinning a previous optimisation as a FLOOR). A spawn-count
    # assertion must bound the ceiling; pinning the exact count converts every
    # future cut into a test failure and quietly discourages the next one.
    #
    # What must stay exact is the PROHIBITION -- never a bare `add`, never a
    # real `commit` -- because those name the landing mechanism this AC exists
    # to bound. Zero `hash-object` spawns is the better answer, not a
    # violation.
    forbidden = [a for a in landing_spawns if a[1] in ("add", "commit")]
    assert forbidden == [], (
        "the private-index landing route must never shell out to `git add` or "
        "`git commit`: %r" % (forbidden,)
    )
    hash_object_spawns = [a for a in landing_spawns if a[1] == "hash-object"]
    assert len(hash_object_spawns) <= 1, hash_object_spawns

    committed = subprocess.run(
        ["git", "show", "HEAD:new.txt"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    assert committed == "brand new content\n"


def test_agree_branch_commits_correctly_immediately_after_pack_refs(tmp_path):
    """Named hazard (spike verdict, "packed-refs breaks it outright"): after
    `git pack-refs --all` there is no loose ref file for the branch HEAD
    points at. `_resolve_cas_ref_target` refuses (returns `None`) rather
    than guessing, so `_commit_via_head_spine`'s fast path is skipped and
    `_commit_scoped_private_index` falls back to its own `commit-tree` +
    compare-and-swap `update-ref` ladder -- the commit must still land
    correctly, not merely fail safely."""
    repo = _init_real_repo(tmp_path)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _real_git(["add", "-A"], repo)
    _real_git(["commit", "-q", "-m", "seed"], repo)
    _real_git(["pack-refs", "--all"], repo)

    branch_ref = repo / ".git" / "refs" / "heads" / _real_git_out(repo, "branch", "--show-current")
    assert not branch_ref.exists(), "probe shape wrong -- branch ref is still loose after pack-refs"

    (repo / "after_pack.txt").write_text("landed after pack-refs\n", encoding="utf-8")
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("agree branch commit after pack-refs\n", encoding="utf-8")

    result = git_native.commit_scoped(["after_pack.txt"], msg_file, repo)

    assert result.ok, result.stderr
    committed = subprocess.run(
        ["git", "show", "HEAD:after_pack.txt"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    assert committed == "landed after pack-refs\n"


def _real_git_out(cwd, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_pathspec_file_is_nul_separated_and_never_dequoted(tmp_path):
    """A pathspec whose name starts with a quote is never C-dequoted.

    Regression guard (2026-08-26). `_write_pathspec_file` wrote
    newline-delimited on the premise that no staged path contains a literal
    newline. True, and beside the point: git's default pathspec-file reader
    also C-dequotes any line beginning with a double quote, so a line
    `"plain.txt"` staged `plain.txt` and `"pla\151n.txt"` decoded the octal
    escape and staged `plain.txt` too -- both at rc=0, which made the
    mis-stage invisible to every downstream check including the residue
    reconciliation. Under `--pathspec-file-nul` no dequoting occurs.

    Pins two things: the file is NUL-separated with no trailing separator,
    and every git invocation reading it passes `--pathspec-file-nul`.
    """
    paths = ["alpha.txt", "dir/beta.txt", '"quoted.txt"']
    spec = git_native._write_pathspec_file(tmp_path, paths)
    try:
        raw = spec.read_bytes()
        assert raw == b"alpha.txt\x00dir/beta.txt\x00" + '"quoted.txt"'.encode()
        assert b"\n" not in raw
        assert not raw.endswith(b"\x00"), "trailing NUL is an empty pathspec"
    finally:
        spec.unlink(missing_ok=True)

    source = inspect.getsource(git_native)
    for marker in (
        'f"--pathspec-from-file={pathspec_file}"',
    ):
        assert source.count(marker) == source.count('"--pathspec-file-nul"'), (
            "every --pathspec-from-file call site must pair with "
            "--pathspec-file-nul or git will C-dequote the file"
        )


# ---------------------------------------------------------------------------
# _hash_worktree_blobs -- C3, docs/plans/2026-08-26-the-commit-op-stops-
# asking-git-eleven-times.md. Adopts `commit_authored_new_file`'s already-
# shipped in-process `write_object` blob write into the
# `_commit_scoped_private_index` worktree-blob leg, retiring most
# `git hash-object -w --stdin-paths` spawns while falling back to that same
# spawn, byte-identically, for every case the in-process pre-check refuses.
# ---------------------------------------------------------------------------


def test_hash_worktree_blobs_clean_path_writes_in_process_no_spawn(tmp_path):
    """A plain, CR-free, unattributed path is written entirely in process --
    zero `git` spawns -- and its sha matches what `git hash-object -w` would
    have produced for the same bytes."""
    repo = _init_real_repo(tmp_path)
    # `write_bytes`, not `write_text` -- Windows text-mode writes translate a
    # bare `\n` to `\r\n`, which would make this "clean" fixture carry the
    # very CR byte the in-process path refuses on, defeating the test.
    (repo / "clean.txt").write_bytes(b"hello world\n")

    real_git = git_native._git
    spawned = []

    def _spy(args, **kwargs):
        spawned.append(list(args))
        return real_git(args, **kwargs)

    with patch.object(git_native, "_git", side_effect=_spy):
        result = git_native._hash_worktree_blobs(["clean.txt"], cwd=repo)

    assert result.ok is True
    assert spawned == [], f"clean path must be written in process, no spawn: {spawned}"

    expected_sha = _real_git_out(repo, "hash-object", "clean.txt")
    assert result.stdout.splitlines() == [expected_sha]


def test_hash_worktree_blobs_cr_content_falls_back_to_spawn_byte_identical(tmp_path):
    """Content carrying a CR byte cannot be reproduced by an in-process
    verbatim write (the clean pipeline's CRLF normalization might apply) --
    refused into the SAME `git hash-object -w --stdin-paths` spawn ladder
    used before this chunk, and the resulting sha is byte-identical to what
    that spawn always produced for this content."""
    repo = _init_real_repo(tmp_path)
    (repo / "crlf.txt").write_bytes(b"line one\r\nline two\r\n")

    result = git_native._hash_worktree_blobs(["crlf.txt"], cwd=repo)

    assert result.ok is True
    expected_sha = _real_git_out(repo, "hash-object", "crlf.txt")
    assert result.stdout.splitlines() == [expected_sha]


def test_hash_worktree_blobs_filter_attributed_path_falls_back_to_spawn_byte_identical(tmp_path):
    """A path a repo-local `.gitattributes` `filter=` pattern matches is
    refused for the in-process write (a `filter.*.clean` driver might apply)
    -- falls back to the spawn, byte-identical to `git hash-object -w`."""
    repo = _init_real_repo(tmp_path)
    (repo / ".gitattributes").write_text("*.bin filter=lfs\n", encoding="utf-8")
    (repo / "asset.bin").write_bytes(b"\x00\x01\x02binary")

    result = git_native._hash_worktree_blobs(["asset.bin"], cwd=repo)

    assert result.ok is True
    expected_sha = _real_git_out(repo, "hash-object", "asset.bin")
    assert result.stdout.splitlines() == [expected_sha]


def test_hash_worktree_blobs_mixed_paths_preserve_input_order(tmp_path):
    """A clean in-process path and a refused (CR-carrying) spawned path in
    the same call return shas in the SAME ORDER `paths` was given, matching
    `_hash_object_stdin_paths`'s own ordering contract."""
    repo = _init_real_repo(tmp_path)
    (repo / "clean.txt").write_bytes(b"clean\n")
    (repo / "crlf.txt").write_bytes(b"has\r\ncr\r\n")

    result = git_native._hash_worktree_blobs(["crlf.txt", "clean.txt"], cwd=repo)

    assert result.ok is True
    expected = [
        _real_git_out(repo, "hash-object", "crlf.txt"),
        _real_git_out(repo, "hash-object", "clean.txt"),
    ]
    assert result.stdout.splitlines() == expected


# ---------------------------------------------------------------------------
# _hash_worktree_blobs -- C3c autocrlf=true in-process checkin normalizer.
# docs/plans/2026-08-26-the-commit-op-stops-asking-git-eleven-times.md.
# `_autocrlf_checkin_normalize` was verified byte-identical to real
# `git hash-object` over a corpus of CRLF/LF/mixed/lone-CR/NUL/>8000-byte
# content BEFORE this branch was wired in (spike, not re-derived here).
# ---------------------------------------------------------------------------


def test_hash_worktree_blobs_autocrlf_true_crlf_content_writes_in_process_no_spawn(tmp_path):
    """Under `core.autocrlf=true` and no attribute pin, CR-carrying content
    is now written IN PROCESS -- zero spawns -- and matches real git's own
    checkin-side CRLF normalization exactly."""
    repo = _init_real_repo(tmp_path)
    _real_git(["config", "core.autocrlf", "true"], repo)
    (repo / "crlf.txt").write_bytes(b"line one\r\nline two\r\n")

    real_git = git_native._git
    spawned = []

    def _spy(args, **kwargs):
        spawned.append(list(args))
        return real_git(args, **kwargs)

    with patch.object(git_native, "_git", side_effect=_spy):
        result = git_native._hash_worktree_blobs(["crlf.txt"], cwd=repo)

    assert result.ok is True
    assert spawned == [], f"autocrlf=true CRLF content must be in process, no spawn: {spawned}"
    with open(repo / "crlf.txt", "rb") as fh:
        real_stdin_sha = subprocess.run(
            ["git", "hash-object", "--path=crlf.txt", "--stdin"],
            cwd=str(repo), input=fh.read(), capture_output=True, check=True,
        ).stdout.decode().strip()
    assert result.stdout.splitlines() == [real_stdin_sha]


def test_hash_worktree_blobs_autocrlf_true_binary_nul_content_left_verbatim(tmp_path):
    """A NUL byte anywhere marks content binary under `text=auto` -- left
    byte-for-byte untouched even though it also carries CRLF pairs."""
    repo = _init_real_repo(tmp_path)
    _real_git(["config", "core.autocrlf", "true"], repo)
    content = b"abc\r\n\x00def\r\n"
    (repo / "bin.dat").write_bytes(content)

    result = git_native._hash_worktree_blobs(["bin.dat"], cwd=repo)

    assert result.ok is True
    real_sha = subprocess.run(
        ["git", "hash-object", "--path=bin.dat", "--stdin"],
        cwd=str(repo), input=content, capture_output=True, check=True,
    ).stdout.decode().strip()
    assert result.stdout.splitlines() == [real_sha]


def test_hash_worktree_blobs_autocrlf_true_lone_cr_blocks_whole_buffer_conversion(tmp_path):
    """A lone CR (not followed by LF) ANYWHERE in the buffer blocks
    conversion of the entire buffer, including an earlier well-formed CRLF
    pair -- not merely the offending line."""
    repo = _init_real_repo(tmp_path)
    _real_git(["config", "core.autocrlf", "true"], repo)
    content = b"abc\r\ndef\r"
    (repo / "lonecr.txt").write_bytes(content)

    result = git_native._hash_worktree_blobs(["lonecr.txt"], cwd=repo)

    assert result.ok is True
    real_sha = subprocess.run(
        ["git", "hash-object", "--path=lonecr.txt", "--stdin"],
        cwd=str(repo), input=content, capture_output=True, check=True,
    ).stdout.decode().strip()
    assert result.stdout.splitlines() == [real_sha]


def test_hash_worktree_blobs_autocrlf_true_text_attribute_pin_falls_back_to_spawn(tmp_path):
    """A path with an explicit `.gitattributes` `eol=`/`text` pin is a
    disposition C3c's spike never measured (forced text ignores the NUL
    heuristic; `-text` never converts) -- refused to the spawn ladder even
    though `core.autocrlf=true` and the content carries CR."""
    repo = _init_real_repo(tmp_path)
    _real_git(["config", "core.autocrlf", "true"], repo)
    (repo / ".gitattributes").write_text("*.sha text eol=lf\n", encoding="utf-8")
    (repo / "digest.sha").write_bytes(b"abc\r\ndef\r\n")

    real_git = git_native._git
    spawned = []

    def _spy(args, **kwargs):
        spawned.append(list(args))
        return real_git(args, **kwargs)

    with patch.object(git_native, "_git", side_effect=_spy):
        result = git_native._hash_worktree_blobs(["digest.sha"], cwd=repo)

    assert result.ok is True
    assert any(a[:2] == ["hash-object", "-w"] for a in spawned), (
        f"attribute-pinned path must fall back to the spawn ladder: {spawned}"
    )
    expected_sha = _real_git_out(repo, "hash-object", "digest.sha")
    assert result.stdout.splitlines() == [expected_sha]


def test_repo_autocrlf_true_reads_repo_local_config(tmp_path, monkeypatch):
    """`_repo_autocrlf_true` resolves from repo-local config, case-
    insensitively, and is `False` for any other value or when unset.

    HERMETIC ON PURPOSE (C3d). This test previously asserted `False` for a
    fresh repo with the ambient machine config still in play, which passed
    only because the probe could not SEE the system layer -- on a stock Git
    for Windows box, `core.autocrlf=true` lives there and real git applies
    it, so `False` was the bug's own answer being pinned as the spec.
    `GIT_CONFIG_NOSYSTEM` isolates the layer actually under test here; the
    system layer gets its own test below."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(git_native.Path, "home", staticmethod(lambda: tmp_path / "nohome"))

    repo = _init_real_repo(tmp_path)
    assert git_native._repo_autocrlf_true(repo) is False

    _real_git(["config", "core.autocrlf", "input"], repo)
    assert git_native._repo_autocrlf_true(repo) is False

    _real_git(["config", "core.autocrlf", "true"], repo)
    assert git_native._repo_autocrlf_true(repo) is True


def test_repo_autocrlf_true_sees_the_system_layer(tmp_path, monkeypatch):
    """THE TEST THAT WOULD HAVE CAUGHT C3d. Git for Windows writes
    `core.autocrlf=true` into the SYSTEM config, not the repo config and not
    `~/.gitconfig`. A probe reading only the latter two resolves `False` on a
    stock install, so C3c's verified-correct in-process blob write never
    executed and `git hash-object` spawned on every commit -- a correct
    mechanism behind a precondition that never held."""
    system_config = tmp_path / "system-gitconfig"
    system_config.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_config))
    monkeypatch.setattr(git_native.Path, "home", staticmethod(lambda: tmp_path / "nohome"))

    repo = _init_real_repo(tmp_path)
    assert git_native._repo_autocrlf_true(repo) is True, (
        "autocrlf set ONLY in the system layer must resolve True -- real git "
        "applies it, so a spawn-free reader that cannot see it disagrees with "
        "the tool it is reproducing"
    )

    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    assert git_native._repo_autocrlf_true(repo) is False, (
        "GIT_CONFIG_NOSYSTEM must suppress the system layer, as it does for git"
    )


def test_repo_local_autocrlf_false_beats_a_system_true(tmp_path, monkeypatch):
    """Git's precedence is LAST WINS across layers -- system, then global,
    then repo-local. A first-hit walk in that order would return the system
    value and normalize content the repo explicitly asked to be left alone,
    which is a wrong blob rather than a missed optimisation."""
    system_config = tmp_path / "system-gitconfig"
    system_config.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_config))
    monkeypatch.setattr(git_native.Path, "home", staticmethod(lambda: tmp_path / "nohome"))

    repo = _init_real_repo(tmp_path)
    _real_git(["config", "core.autocrlf", "false"], repo)
    assert git_native._repo_autocrlf_true(repo) is False, (
        "a repo-local false must beat a system true"
    )


def test_autocrlf_checkin_normalize_matches_real_git_corpus(tmp_path):
    """Direct unit coverage of the pure transform against the exact corpus
    the C3c spike ran, independent of the `_hash_worktree_blobs` wiring."""
    repo = _init_real_repo(tmp_path)
    _real_git(["config", "core.autocrlf", "true"], repo)
    corpus = [
        b"line1\r\nline2\r\nline3\r\n",
        b"line1\nline2\nline3\n",
        b"line1\r\nline2\nline3\r\n",
        b"abc\r\n\x00def\r\n",
        b"abc\x00def",
        (b"a\r\n" * 5000),
        b"abc\r\ndef\r",
        b"",
        b"\r\n",
    ]
    for content in corpus:
        got = git_native._autocrlf_checkin_normalize(content)
        real_sha = subprocess.run(
            ["git", "hash-object", "--path=x.txt", "--stdin"],
            cwd=str(repo), input=content, capture_output=True, check=True,
        ).stdout.decode().strip()
        import hashlib
        got_sha = hashlib.sha1(b"blob " + str(len(got)).encode() + b"\0" + got).hexdigest()
        assert got_sha == real_sha, f"mismatch for {content!r}"


def test_hash_worktree_blobs_empty_paths_is_a_documented_noop_no_spawn(tmp_path):
    repo = _init_real_repo(tmp_path)

    with patch.object(git_native, "_git", side_effect=AssertionError("must not spawn")):
        result = git_native._hash_worktree_blobs([], cwd=repo)

    assert result == git_native.GitResult(returncode=0, stdout="", stderr="")


def test_commit_scoped_private_index_worktree_leg_uses_hash_worktree_blobs(tmp_path):
    """The rewire point itself: `_commit_scoped_private_index`'s worktree-
    sourced leg must call the new in-process-first helper, not the old
    unconditional-spawn one directly."""
    repo = _init_real_repo(tmp_path)
    (repo / "file.txt").write_text("original\n", encoding="utf-8")
    _real_git(["add", "--", "file.txt"], repo)
    _real_git(["commit", "-q", "-m", "baseline"], repo)

    (repo / "file.txt").write_text("changed\n", encoding="utf-8")

    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("edit\n\nSession-Id: t\n", encoding="utf-8")

    real_helper = git_native._hash_worktree_blobs
    calls = []

    def _spy(paths, **kwargs):
        calls.append(list(paths))
        return real_helper(paths, **kwargs)

    with patch.object(git_native, "_hash_worktree_blobs", side_effect=_spy):
        result = git_native._commit_scoped_private_index([], ["file.txt"], msg_file, repo)

    assert result.ok is True
    assert calls == [["file.txt"]]
