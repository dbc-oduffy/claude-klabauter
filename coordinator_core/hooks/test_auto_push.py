"""
coordinator_core.hooks.test_auto_push — behavior-parity tests for the naked-Python
port of the DoE-owned `coordinator-auto-push` bash script (DR-059).

Tests assert:
  - classify_error() ordered-ladder parity, including the load-bearing ordering
    trap (non-fast-forward matched before gh-transient when both patterns hit).
  - branch_gate() work/*-proceed, migration|release|feature-skip-with-message,
    everything-else-skip-with-message.
  - extract_first_err()'s Trace-preamble-skipping fallback chain.
  - main() always exits 0, even on push failure or an injected internal
    exception (auto-push must never block a commit).
  - _attempts_for()/_backoff_seconds()'s per-class retry-budget table
    (ref-lock's own override, network/gh-transient's unchanged envelope).
    COORDINATOR_AUTO_PUSH_NO_SLEEP=1 is set for all retry tests so
    gh-transient's seconds-scale backoff is never actually paid.

  GRAVESTONED 2026-08-30 (docs/plans/2026-08-30-who-pushes-and-when.md C2):
  run_push_with_retry() and the durable pending-push write/drain subsystem
  (drain_pending_push, _write_pending_record, _drain_dead_ref_record) are
  deleted; their tests retired with them rather than retargeted. The read
  primitives (_read_pending_record, _pending_record_path, _record_is_stale)
  were restored the same day for orientation/regenerate_cache.py's health
  check -- see auto_push.py's module docstring for the full citation trail.

No test performs a real `git push` or touches a real remote --
subprocess/push_once and os.fork are monkeypatched throughout, and repo dirs
are tmp_path-scoped so failure-log writes are hermetic. The pipe-hold
regression tests are the one exception: they spawn a real, isolated `python3
-c` subprocess (never the pytest process itself) so `os.fork()` and stdio
redirection run for real, proving the fix at the OS level rather than via a
monkeypatch that could paper over the defect.

Spec backlink: state/handoffs/2026-07-15_164501_auto-push-naked-python-reimpl.md
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags, no_console_passthrough_kwargs

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Real spawn (python3, not git) is load-bearing: the pipe-hold regression
# tests spawn a real, isolated `python3 -c` subprocess so os.fork() and stdio
# redirection run for real at the OS level, proving the fix no monkeypatch
# could — see module docstring.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

from coordinator_core.hooks import auto_push  # noqa: E402
from coordinator_core.git.git_dir import resolve_git_common_dir  # noqa: E402

# The ACTUAL repo this test file lives in -- never a fixture's tmp_path.
# `push-failures.log` there is a real, append-only forensic record read by
# operators and by the Stop-time mid-session tripwire
# (`runtime-tripwire-em-check.py::_check_push_failures`, DoE-claude); a test
# that manufactures rows in it degrades that signal for every consumer, on
# every suite run (2026-08-19 incident: a fabricated "2 push failure(s)"
# diverted a live session that had none). Every test in this module must
# pass `--repo-root`/an explicit repo_root pointing at `tmp_path`, never let
# a fallback resolve against the real cwd -- this autouse fixture is the
# regression guard for that invariant, independent of any individual test's
# own tmp_path assertions.
_REAL_REPO_ROOT = Path(__file__).resolve().parents[2]
_REAL_GIT_COMMON_DIR = resolve_git_common_dir(_REAL_REPO_ROOT)


@pytest.fixture(autouse=True)
def _guard_real_push_failures_log():
    """Fail loudly if a test in this module wrote to the REAL repo's
    push-failures.log or dropped a push-stderr-*.log sidecar there, instead
    of staying inside its own tmp_path-scoped git common dir."""
    log_path = _REAL_GIT_COMMON_DIR / "push-failures.log"
    before_size = log_path.stat().st_size if log_path.exists() else None
    before_sidecars = set(_REAL_GIT_COMMON_DIR.glob("push-stderr-*.log"))

    yield

    after_size = log_path.stat().st_size if log_path.exists() else None
    assert after_size == before_size, (
        f"test wrote to the REAL repo's push-failures.log "
        f"({log_path}) -- it must target tmp_path instead"
    )
    after_sidecars = set(_REAL_GIT_COMMON_DIR.glob("push-stderr-*.log"))
    new_sidecars = after_sidecars - before_sidecars
    assert not new_sidecars, (
        f"test dropped push-stderr-*.log sidecar(s) in the REAL repo's git "
        f"dir instead of tmp_path: {sorted(p.name for p in new_sidecars)}"
    )


# ---------------------------------------------------------------------------
# classify_error
# ---------------------------------------------------------------------------

CLASSIFY_CASES = [
    (
        "gh-push-protection",
        "remote: error: GH013: Repository rule violations found\n"
        "remote: - Push cannot contain secrets\n",
    ),
    (
        "gh-size-limit",
        "remote: error: GH001: Large files detected.\n"
        "remote: error: File big.bin is 150.00 MB; this exceeds GitHub's file size limit of 100.00 MB\n",
    ),
    (
        "gh-lfs-quota",
        "batch response: This repository is over its data quota. GH008: Repository over quota\n",
    ),
    (
        "ref-lock",
        "error: cannot lock ref 'refs/heads/work/foo': is at abc123 but expected def456\n",
    ),
    (
        "non-fast-forward",
        "! [rejected]        work/foo -> work/foo (fetch first)\n"
        "error: failed to push some refs\n"
        "hint: Updates were rejected because the tip of your current branch is behind\n",
    ),
    (
        "dead-ref",
        "error: src refspec work/machine-a/2026-08-10 does not match any\n"
        "error: failed to push some refs to 'https://github.com/org/repo.git'\n",
    ),
    # Same class, the branch-shaped-refspec wording -- observed verbatim on
    # 2026-08-26 when a `work/*` branch was renamed while a push was in flight.
    (
        "dead-ref",
        "fatal: refs/heads/work/machine-b/2026-08-22 cannot be resolved to branch\n",
    ),
    (
        "gh-transient",
        "error: RPC failed; HTTP 502 curl 22 The requested URL returned error: 502\n"
        "send-pack: unexpected disconnect while reading sideband packet\n",
    ),
    (
        "network",
        "ssh: Could not resolve hostname github.com: Name or service not known\n"
        "fatal: Could not read from remote repository.\n",
    ),
    (
        "auth",
        "git@github.com: Permission denied (publickey).\n"
        "fatal: Could not read from remote repository.\n",
    ),
    (
        "gh-server-reject",
        "remote: error: Some org SSO policy rejected this push\n",
    ),
    (
        "unknown",
        "some completely unrecognized gibberish output\n",
    ),
    (
        "empty-stderr",
        "",
    ),
    (
        "timeout",
        "fatal: push exceeded 120s and was killed "
        "(Could not read from remote repository: timed out)\n",
    ),
    (
        "spawn-error",
        "fatal: git push failed to spawn: FileNotFoundError: "
        "[WinError 2] The system cannot find the file specified\n",
    ),
]


@pytest.mark.parametrize("expected_class,stderr_text", CLASSIFY_CASES)
def test_classify_error_table(expected_class, stderr_text):
    assert auto_push.classify_error(stderr_text) == expected_class


def test_classify_error_ordering_trap_non_fast_forward_before_gh_transient():
    # A stderr that matches BOTH non-fast-forward AND a disconnect/5xx pattern
    # must classify as non-fast-forward -- the bash ladder matches non-FF
    # before gh-transient specifically so a rejection that also closes the
    # connection can't be misread as a transient server failure.
    stderr_text = (
        "! [rejected]        work/foo -> work/foo (fetch first)\n"
        "error: failed to push some refs to 'origin'\n"
        "hint: tip of your current branch is behind its remote counterpart\n"
        "fatal: the remote end hung up unexpectedly\n"
    )
    assert auto_push.classify_error(stderr_text) == "non-fast-forward"


def test_classify_error_ordering_trap_push_protection_before_auth():
    # gh-push-protection must win over a generic "access denied" phrase that
    # could otherwise fall into `auth`.
    stderr_text = (
        "remote: error: GH013: Repository rule violations found\n"
        "remote: - Secret detected\n"
        "remote: access denied to push\n"
    )
    assert auto_push.classify_error(stderr_text) == "gh-push-protection"


def test_classify_error_dead_ref_not_in_retryable_classes():
    # AC2: a dead local branch ref cannot self-heal by resending the same
    # push -- it must never be retried.
    assert "dead-ref" not in auto_push._RETRYABLE_CLASSES


def test_classify_error_timeout_not_in_retryable_classes():
    # Defect 3 (2026-08-20 dispatch): "timeout" is a NEW label carved out of
    # what used to classify as "unknown" -- it must stay out of
    # _RETRYABLE_CLASSES so the retry/timing decision is unchanged, only the
    # banner-facing label improves.
    assert "timeout" not in auto_push._RETRYABLE_CLASSES


def test_push_once_timeout_message_classifies_as_timeout():
    # The exact message push_once() synthesizes on subprocess.TimeoutExpired
    # (GIT_PUSH_TIMEOUT_SECS-templated) must round-trip through classify_error
    # as "timeout", not fall through to "unknown".
    stderr_text = (
        f"fatal: push exceeded {auto_push.GIT_PUSH_TIMEOUT_SECS}s and was killed "
        "(Could not read from remote repository: timed out)"
    )
    assert auto_push.classify_error(stderr_text) == "timeout"


def test_classify_error_capitalized_ssh_failure_is_auth_not_unknown():
    # The single commonest real SSH push failure, verbatim from git. It fell to
    # "unknown" until _PAT_AUTH became IGNORECASE (2026-08-30) -- 52 of the 185
    # rows in example-retrieval-repo's push-failures.log, the largest unnamed class in it.
    stderr_text = (
        "fatal: Could not read from remote repository.\n"
        "\n"
        "Please make sure you have the correct access rights\n"
        "and the repository exists.\n"
    )
    assert auto_push.classify_error(stderr_text) == "auth"


def test_classify_error_capitalized_auth_change_is_label_only():
    # IGNORECASE must not move anything into or out of the retry ladder: "auth"
    # was already non-retrying and so was the "unknown" these rows used to land
    # in, so the operator-facing label is the only thing that changed.
    assert "auth" not in auto_push._RETRYABLE_CLASSES
    assert "unknown" not in auto_push._RETRYABLE_CLASSES


def test_classify_error_timeout_still_beats_case_insensitive_auth():
    # The ordering trap IGNORECASE would otherwise open: the synthesized timeout
    # message carries "Could not read from remote repository", which _PAT_AUTH
    # now matches. _PAT_TIMEOUT is tested first and must stay first.
    stderr_text = (
        f"fatal: push exceeded {auto_push.GIT_PUSH_TIMEOUT_SECS}s and was killed\n"
        "fatal: Could not read from remote repository.\n"
    )
    assert auto_push.classify_error(stderr_text) == "timeout"


def test_classify_error_spawn_error_not_in_retryable_classes():
    # "spawn-error" is a NEW label carved out of what used to classify as
    # "unknown" (2026-08-25 FileNotFoundError cluster) -- it must stay out of
    # _RETRYABLE_CLASSES so the retry/timing decision is unchanged, only the
    # banner-facing label improves.
    assert "spawn-error" not in auto_push._RETRYABLE_CLASSES


def test_push_once_spawn_failure_classifies_as_spawn_error(tmp_path, monkeypatch):
    # A git binary that cannot be exec'd raises OSError out of subprocess.run
    # BEFORE any git stderr exists. push_once must synthesize a message that
    # round-trips through classify_error as "spawn-error" rather than the
    # "unknown" that made the 2026-08-25 cluster read as an unclassified git
    # rejection.
    monkeypatch.setattr(auto_push, "git_exe", lambda: "git")

    def _raise(cmd, **kwargs):
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(auto_push.subprocess, "run", _raise)

    ok, stderr_text = auto_push.push_once(str(tmp_path), "work/some-branch", False, False)

    assert ok is False
    assert auto_push.classify_error(stderr_text) == "spawn-error"
    assert "FileNotFoundError" in stderr_text


def test_push_once_unresolvable_git_never_spawns(tmp_path, monkeypatch):
    # When git is not on PATH at all, push_once must report -- not raise, and
    # not attempt a spawn it already knows will fail with [WinError 2].
    monkeypatch.setattr(auto_push, "git_exe", lambda: None)

    def _must_not_run(cmd, **kwargs):
        raise AssertionError(f"spawned despite unresolvable git: {cmd!r}")

    monkeypatch.setattr(auto_push.subprocess, "run", _must_not_run)

    ok, stderr_text = auto_push.push_once(str(tmp_path), "work/some-branch", False, False)

    assert ok is False
    assert auto_push.classify_error(stderr_text) == "spawn-error"


def test_git_exe_resolves_once_per_process(monkeypatch):
    # The whole point of the resolver: three git spawns in one hook process
    # pay ONE PATH walk, and an unresolvable git is cached as such rather than
    # re-walked per spawn.
    calls = []

    import shutil

    def _counting_which(name, *args, **kwargs):
        calls.append(name)
        return "/usr/bin/git"

    monkeypatch.setattr(shutil, "which", _counting_which)
    monkeypatch.setattr(auto_push, "_GIT_EXE_CACHE", auto_push._GIT_EXE_UNRESOLVED)

    assert auto_push.git_exe() == "/usr/bin/git"
    assert auto_push.git_exe() == "/usr/bin/git"
    assert calls == ["git"]


def test_git_exe_falls_back_when_path_cannot_resolve_git(monkeypatch, tmp_path):
    """PATH is the thing that failed, so a PATH walk cannot be the whole answer.

    Regression for the 2026-08-25 cluster. Reproduced in a spike: a respawned
    child whose inherited PATH carries no Git directory gets
    `shutil.which("git") is None`, and every bare-`git` spawn raises the same
    `[WinError 2]`. Resolving through PATH alone renames that failure without
    preventing it -- the push still does not happen.
    """
    import shutil

    fake_git = tmp_path / "Git" / "cmd" / "git.exe"
    fake_git.parent.mkdir(parents=True)
    fake_git.write_text("", encoding="utf-8")

    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(auto_push.os, "name", "nt")
    monkeypatch.setenv("GIT_EXEC_PATH", "")
    monkeypatch.setenv("ProgramW6432", str(tmp_path))
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    # Registry rung must miss so the well-known rung is the one under test.
    monkeypatch.setattr(auto_push, "_GIT_EXE_CACHE", auto_push._GIT_EXE_UNRESOLVED)
    import builtins

    real_import = builtins.__import__

    def _no_winreg(name, *args, **kwargs):
        if name == "winreg":
            raise ImportError("no registry in this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_winreg)

    assert auto_push.git_exe() == str(fake_git)


def test_git_exe_prefers_git_exec_path_when_git_itself_supplied_it(monkeypatch, tmp_path):
    """When git invoked us, GIT_EXEC_PATH names the very git that did.

    Measured exported into a git-spawned subprocess, but NOT documented --
    `githooks(5)` names only "GIT_DIR, GIT_WORK_TREE, etc." -- so this is a
    first rung on evidence and never the only one.
    """
    import shutil

    exec_dir = tmp_path / "libexec" / "git-core"
    exec_dir.mkdir(parents=True)
    (exec_dir / "git.exe").write_text("", encoding="utf-8")

    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(auto_push.os, "name", "nt")
    # Forward slashes: git reports GIT_EXEC_PATH posix-style even on Windows.
    monkeypatch.setenv("GIT_EXEC_PATH", str(exec_dir).replace("\\", "/"))
    monkeypatch.setattr(auto_push, "_GIT_EXE_CACHE", auto_push._GIT_EXE_UNRESOLVED)

    assert auto_push.git_exe() == str(exec_dir / "git.exe")


def test_git_exe_off_path_is_a_noop_off_windows(monkeypatch):
    """The ladder is Windows-only -- POSIX keeps PATH resolution as the answer."""
    monkeypatch.setattr(auto_push.os, "name", "posix")
    assert auto_push._git_exe_off_path() is None


# ---------------------------------------------------------------------------
# branch_gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "branch,expect_push,expect_message",
    [
        ("work/machine-a/2026-07-15", True, False),
        ("work/x", True, False),
        ("feature/foo", False, True),
        ("release/1.0", False, True),
        ("migration/legacy", False, True),
        ("main", False, True),
        ("random-branch", False, True),
    ],
)
def test_branch_gate(branch, expect_push, expect_message):
    should_push, message = auto_push.branch_gate(branch)
    assert should_push == expect_push
    if expect_message:
        assert message is not None
        assert branch in message
    else:
        assert message is None


# Review: overengineering-reviewer Finding 4 -- the two main()-driven
# branch-gate-skip stderr tests retired with main(); branch_gate() itself
# stays covered by test_branch_gate above.


# ---------------------------------------------------------------------------
# resolve_branch -- case-canonicalization (the load-bearing Windows fix)
# ---------------------------------------------------------------------------

def test_resolve_branch_canonicalizes_to_for_each_ref_case(monkeypatch):
    # HEAD carries mixed-case, for-each-ref returns the lowercase canonical
    # ref -- resolve_branch must prefer the canonical (case-fold-matched)
    # form, matching the bash awk 'tolower($0)==tolower(b)' selection.
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("branch", "--show-current"): "work/MACHINE-A/2026-07-15",
        ("for-each-ref", "--format=%(refname:short)", "refs/heads/"): (
            "main\nwork/machine-a/2026-07-15\nother/branch"
        ),
    }.get(tuple(args)))

    assert auto_push.resolve_branch("/repo") == "work/machine-a/2026-07-15"


def test_resolve_branch_falls_back_to_raw_when_no_case_fold_match(monkeypatch):
    # for-each-ref succeeds but yields no case-fold match for the raw branch
    # -- resolve_branch must fall back to the raw name rather than error or
    # return an unrelated ref, matching the bash `[[ -z "$BRANCH" ]] &&
    # BRANCH="$RAW_BRANCH"` fallback.
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("branch", "--show-current"): "work/foo",
        ("for-each-ref", "--format=%(refname:short)", "refs/heads/"): "main\nother/branch",
    }.get(tuple(args)))

    assert auto_push.resolve_branch("/repo") == "work/foo"


def test_resolve_branch_falls_back_to_raw_when_for_each_ref_fails(monkeypatch):
    # for-each-ref lookup itself fails (returns None) -- must still fall back
    # to the raw branch name so Linux/macOS (where this hazard doesn't exist)
    # doesn't regress.
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("branch", "--show-current"): "work/foo",
    }.get(tuple(args)))

    assert auto_push.resolve_branch("/repo") == "work/foo"


# ---------------------------------------------------------------------------
# _detach_and_run / main -- Windows respawn-loop guard
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# extract_first_err -- Trace-preamble-skipping fallback chain
# ---------------------------------------------------------------------------

def test_extract_first_err_skips_trace_preamble():
    stderr_text = (
        "\n"
        "remote: error: Trace: 0123456789abcdef0123456789abcdef\n"
        "remote: error: GH013: Repository rule violations found for refs/heads/work/foo\n"
        "remote: - Push cannot contain secrets\n"
        "To github.com:org/repo.git\n"
        " ! [remote rejected] work/foo -> work/foo (push declined due to repository rule violations)\n"
    )
    result = auto_push.extract_first_err(stderr_text)
    assert "Trace:" not in result
    assert "GH013" in result


def test_extract_first_err_falls_back_to_first_nonblank_when_no_remote_reject_line():
    stderr_text = "\n\nfatal: unable to access 'https://example.com/': Could not resolve host\n"
    result = auto_push.extract_first_err(stderr_text)
    assert result == "fatal: unable to access 'https://example.com/': Could not resolve host"


def test_extract_first_err_truncates_to_200_chars_and_collapses_whitespace():
    long_line = "remote: error: " + ("x" * 300)
    stderr_text = f"{long_line}\r\n"
    result = auto_push.extract_first_err(stderr_text)
    assert len(result) == 200
    assert "\r" not in result


def test_extract_first_err_empty_stderr():
    assert auto_push.extract_first_err("") == ""


# Review: overengineering-reviewer Finding 4 -- main()'s exit-0-always and
# internal-error-logging contract was only exercised end-to-end through
# main() itself; all five tests retired with it. `log_failure` and
# `_module_provenance`'s own field-level behavior stay covered by the
# `log_failure` and `_module_provenance` sections elsewhere in this file.


@pytest.mark.skipif(
    os.name != "nt",
    reason="pins backslash-to-forward-slash normalization of a Windows-"
    "shaped COORDINATOR_HOST_PYTHON value via the host-native `Path."
    "as_posix()` -- on POSIX, Path is PosixPath, which treats a backslash "
    "as an ordinary filename character and performs no conversion, so a "
    "synthetic Windows-shaped literal round-trips unchanged; this is not "
    "reachable in real production use either (COORDINATOR_HOST_PYTHON only "
    "carries backslashes when set BY a Windows host, where Path is native "
    "WindowsPath at the same time).",
)
def test_module_provenance_prefers_host_python_env_when_set(monkeypatch):
    """A wrapper-launched hook reports the real host interpreter, not the
    wrapper's own `sys.executable`, when COORDINATOR_HOST_PYTHON is set."""
    monkeypatch.setenv(auto_push._ENV_HOST_PYTHON, r"C:\host\python.exe")

    provenance = auto_push._module_provenance()

    assert "interp=C:/host/python.exe" in provenance


def test_module_provenance_falls_back_to_sys_executable_when_unset(monkeypatch):
    """Absent COORDINATOR_HOST_PYTHON, the interpreter field is unchanged
    from `sys.executable`."""
    monkeypatch.delenv(auto_push._ENV_HOST_PYTHON, raising=False)

    provenance = auto_push._module_provenance()

    expected = Path(sys.executable).as_posix() if sys.executable else "<unknown>"
    assert f"interp={expected}" in provenance


def test_module_provenance_treats_blank_host_python_as_unset(monkeypatch):
    """Whitespace-only COORDINATOR_HOST_PYTHON must not be trusted -- it
    falls back to sys.executable exactly as if unset."""
    monkeypatch.setenv(auto_push._ENV_HOST_PYTHON, "   ")

    provenance = auto_push._module_provenance()

    expected = Path(sys.executable).as_posix() if sys.executable else "<unknown>"
    assert f"interp={expected}" in provenance


# ---------------------------------------------------------------------------
# log_failure -- git-dir topology resolution (2026-08-01: log_failure moved
# from a literal `<repo_root>/.git` join to `resolve_git_common_dir`, so
# `.git` FILE topologies -- linked worktree, `--separate-git-dir`, submodule
# -- no longer raise NotADirectoryError and silently drop the log).
# ---------------------------------------------------------------------------

def test_log_failure_plain_clone_writes_under_dot_git(tmp_path):
    """Regression guard: plain-clone `.git` dir, unchanged from pre-port
    behavior -- log lands at `<root>/.git/push-failures.log`."""
    repo_root = tmp_path
    (repo_root / ".git").mkdir()

    auto_push.log_failure(str(repo_root), "work/foo", "direct push", "auth", 1, "denied", "stderr text")

    log_path = repo_root / ".git" / "push-failures.log"
    assert log_path.exists()
    assert "work/foo" in log_path.read_text()


def test_log_failure_linked_worktree_writes_to_common_dir_not_private(tmp_path):
    """`.git` file with an ABSOLUTE gitdir pointing at a private worktree dir
    that itself contains a `commondir` file -- log must land in the resolved
    COMMON dir, not the private worktree gitdir."""
    repo_root = tmp_path / "worktree"
    repo_root.mkdir()
    common_dir = tmp_path / "main" / ".git"
    common_dir.mkdir(parents=True)
    private_gitdir = common_dir / "worktrees" / "worktree"
    private_gitdir.mkdir(parents=True)
    (private_gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (repo_root / ".git").write_text(f"gitdir: {private_gitdir}\n", encoding="utf-8")

    auto_push.log_failure(str(repo_root), "work/foo", "direct push", "auth", 1, "denied", "stderr text")

    log_path = common_dir / "push-failures.log"
    assert log_path.exists(), f"expected log at common dir {common_dir}, not private gitdir {private_gitdir}"
    assert not (private_gitdir / "push-failures.log").exists()


def test_log_failure_separate_git_dir_no_commondir_writes_to_private_gitdir(tmp_path):
    """`.git` file, absolute gitdir, NO `commondir` file -- the private
    gitdir IS the common dir (the `--separate-git-dir` case)."""
    repo_root = tmp_path / "worktree"
    repo_root.mkdir()
    gitdir = tmp_path / "elsewhere" / "repo.git"
    gitdir.mkdir(parents=True)
    (repo_root / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

    auto_push.log_failure(str(repo_root), "work/foo", "direct push", "auth", 1, "denied", "stderr text")

    log_path = gitdir / "push-failures.log"
    assert log_path.exists()


def test_log_failure_submodule_relative_gitdir_resolves_against_repo_root(tmp_path):
    """`.git` file with a RELATIVE `gitdir: ../.git/modules/<name>` pointer
    (the submodule form) -- must resolve relative to repo_root, not raise,
    and land the log at the correctly-resolved dir. This is the case the
    brief specifically warns gets silently mis-resolved by a naive
    absolute-only resolver."""
    superproject = tmp_path / "super"
    modules_dir = superproject / ".git" / "modules" / "sub"
    modules_dir.mkdir(parents=True)
    repo_root = superproject / "sub"
    repo_root.mkdir()
    (repo_root / ".git").write_text("gitdir: ../.git/modules/sub\n", encoding="utf-8")

    auto_push.log_failure(str(repo_root), "work/foo", "direct push", "auth", 1, "denied", "stderr text")

    log_path = modules_dir / "push-failures.log"
    assert log_path.exists(), f"expected log at resolved submodule gitdir {modules_dir}"


def test_log_failure_none_attempts_renders_explicit_unknown(tmp_path):
    # A caller that did not count its ladder legs must say so. Substituting a
    # number is what made three months of `cadence-sweep/... after 1` rows
    # assert a one-attempt ladder that does not exist.
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    auto_push.log_failure(str(repo_root), "work/foo", "cadence-sweep", "sweep-unconfirmed", None, "timed out", "")
    line = (repo_root / ".git" / "push-failures.log").read_text(encoding="utf-8")
    assert "(cadence-sweep/sweep-unconfirmed after ?)" in line


def test_log_failure_fail_open_on_garbage_dot_git_file(tmp_path):
    """`.git` file with unparseable content -- falls back to the literal
    `<root>/.git` join without raising."""
    repo_root = tmp_path
    (repo_root / ".git").write_text("not a gitdir pointer at all\n", encoding="utf-8")

    auto_push.log_failure(str(repo_root), "work/foo", "direct push", "auth", 1, "denied", "stderr text")

    # Fail-open target is the literal join -- which here is itself the
    # garbage FILE, so opening it for append raises NotADirectoryError and
    # log_failure degrades to stderr (matching the pre-port behavior for any
    # unwritable log target). The contract under test is "does not raise."


def test_log_failure_forensic_sidecar_lands_beside_log_in_non_plain_topology(tmp_path):
    """Non-plain topology: the forensic push-stderr sidecar must land beside
    the resolved log (not the private worktree gitdir), and the log row's
    stderr= field must cite the sidecar's actual resolved path."""
    repo_root = tmp_path / "worktree"
    repo_root.mkdir()
    gitdir = tmp_path / "elsewhere" / "repo.git"
    gitdir.mkdir(parents=True)
    (repo_root / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

    auto_push.log_failure(str(repo_root), "work/foo", "direct push", "auth", 1, "denied", "stderr text")

    sidecar_candidates = list(gitdir.glob("push-stderr-*.log"))
    assert len(sidecar_candidates) == 1
    sidecar = sidecar_candidates[0]
    assert sidecar.read_text(encoding="utf-8") == "stderr text"

    log_content = (gitdir / "push-failures.log").read_text(encoding="utf-8")
    assert sidecar.as_posix() in log_content


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------



def test_attempts_for_ref_lock_is_a_per_class_override_not_max_attempts():
    # Regression guard for DEC-1: ref-lock's budget must diverge from
    # MAX_ATTEMPTS, not merely equal it by coincidence of today's constants.
    assert auto_push.REF_LOCK_ATTEMPTS != auto_push.MAX_ATTEMPTS
    assert auto_push._attempts_for("network") == auto_push.MAX_ATTEMPTS
    assert auto_push._attempts_for("gh-transient") == auto_push.MAX_ATTEMPTS


def test_backoff_seconds_ref_lock_cumulative_reach_exceeds_longest_observed_burst():
    # Assert against the jitter FLOOR (random.uniform(0, 0.5) can be 0), not a
    # sampled value -- otherwise this test is flaky by construction. The
    # ceiling must clear the longer of the two observed bursts: doe-claude-em's
    # 45s and this repo's own 70s (.git/push-failures.log, 2026-08-30, 29
    # ref-lock ladders between 11:26:32Z and 11:27:42Z).
    floor_total = sum(min(2 ** n, 30) for n in range(1, 7))
    assert floor_total > 70
    for n in range(1, 7):
        floor = min(2 ** n, 30)
        assert auto_push._backoff_seconds("ref-lock", n) >= floor


def test_backoff_seconds_network_unchanged_envelope():
    for n in range(1, 4):
        low = 0.2 + n * 0.1
        high = 0.7 + n * 0.1
        value = auto_push._backoff_seconds("network", n)
        assert low <= value <= high


def test_backoff_seconds_gh_transient_unchanged_envelope():
    for n in range(1, 4):
        low = n * 2
        high = n * 2 + 0.5
        value = auto_push._backoff_seconds("gh-transient", n)
        assert low <= value <= high












# ---------------------------------------------------------------------------
# cockpit-contract release publish -- fires after a successful push, gated
# on a filesystem-only scoping guard (`_cockpit_publish_script`). See
# auto_push.py's "cockpit-contract release publish" section for the
# fleet-wide-firing defect this guard exists to prevent
# (cross-repo/archive/2026-07-25-claude-klabauter-em-cockpit-publish-use-a-github-action-not-a-claude-klabauter-directive.md,
# DoE-claude).
# ---------------------------------------------------------------------------













# Review: overengineering-reviewer Finding 4 -- the branch-gate-skip ->
# run_push_with_retry-never-called assertion was only reachable through
# main(); retired with it.


def test_cockpit_publish_nonzero_exit_does_not_fail_hook_and_warns(monkeypatch, tmp_path, capsys):
    repo_root = str(tmp_path)
    script_dir = tmp_path / ".github" / "scripts"
    script_dir.mkdir(parents=True)
    script_path = script_dir / "publish_cockpit_contract.py"
    script_path.write_text("# stub\n")

    class _FakeResult:
        returncode = 1

    monkeypatch.setattr(auto_push.subprocess, "run", lambda *a, **k: _FakeResult())
    monkeypatch.setattr(auto_push, "_resolve_python_exe", lambda: "python3")

    auto_push._invoke_cockpit_publish(repo_root, script_path)

    captured = capsys.readouterr()
    assert "[coordinator]" in captured.err
    assert "publish_cockpit_contract.py" in captured.err




# ---------------------------------------------------------------------------
# Pipe-hold regression (AC3) -- fork leg stdio disown + spawn_detached_push
# ---------------------------------------------------------------------------











# ---------------------------------------------------------------------------
# 2026-08-01/03 regression -- respawned child must actually IMPORT, not just
# have the right-shaped argv. The pre-fix bug (a6daf112e's top-level
# `from coordinator_core.git.git_dir import resolve_git_common_dir`) was
# invisible to every test above because they all monkeypatch
# `subprocess.Popen` and never let the child actually run -- an argv-shape
# assertion is exactly the blind spot that let a silently-dying respawn ship
# for two days. These tests spawn the REAL child process.
# ---------------------------------------------------------------------------













# Review: overengineering-reviewer Finding 4 -- main()'s --branch-flag
# bypass-resolve-and-gate contract was only reachable through main();
# retired with it (spawn_detached_push, main()'s sole caller of this
# leg, is itself already gravestoned per C8).


# ---------------------------------------------------------------------------
# The PowerShell fallback branch (once spike-gated) is DELETED, not merely
# disabled -- 2026-08-06 no-shell-spawns PM ruling. Native `git push` is the
# unconditional transport on every platform, including Windows+SSH.
# ---------------------------------------------------------------------------

def test_windows_ssh_powershell_fallback_constant_removed():
    # The gating constant itself must be gone, not just False -- a present-
    # but-disabled constant is exactly the regrowth surface the ruling closes.
    assert not hasattr(auto_push, "WINDOWS_SSH_POWERSHELL_FALLBACK")


def test_push_once_spawns_no_powershell_even_on_windows_ssh(monkeypatch, tmp_path):
    # Strengthened post-condition: for the Windows+SSH combination that used
    # to route through powershell.exe, push_once must invoke plain `git`,
    # never a `powershell`/`pwsh` binary.
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class _Result:
            returncode = 0
            stderr = ""

        return _Result()

    monkeypatch.setattr(auto_push.subprocess, "run", _fake_run)

    auto_push.push_once(str(tmp_path), "work/some-branch", True, True)

    assert captured["cmd"][0] == "git"
    lowered = [str(part).lower() for part in captured["cmd"]]
    assert not any("powershell" in part or part == "pwsh" for part in lowered)


@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(
            lambda root: auto_push.push_once(root, "work/some-branch", False, False),
            id="push_once",
        ),
        pytest.param(
            lambda root: auto_push._run_git(root, ["rev-parse", "HEAD"]),
            id="_run_git",
        ),
        pytest.param(
            lambda root: auto_push._is_ancestor(root, "HEAD", "refs/remotes/origin/x"),
            id="_is_ancestor",
        ),
    ],
)
def test_resolved_git_rides_executable_and_never_argv0(invoke, tmp_path, monkeypatch):
    # The split is load-bearing in BOTH directions and neither half is style.
    #
    # `executable=` is what actually fixes the 2026-08-25 [WinError 2] cluster:
    # the binary is located once, off PATH if need be, instead of re-derived by
    # a spawn whose PATH is the thing that failed.
    #
    # The literal `"git"` argv head is what keeps this module VISIBLE to
    # `coordinator_core/tests/test_shared_git_runner.py`, whose detector keys on
    # exactly that literal (`git/run.py`'s negative spec states the key). Moving
    # the resolved path into argv[0] silently drops a git-spawning module out of
    # that gate's inventory -- and both of its registers are shrink-only and at
    # their ceilings, so nothing catches the drop but this assertion.
    resolved = "/nowhere/bin/git-resolved"
    monkeypatch.setattr(auto_push, "git_exe", lambda: resolved)
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["executable"] = kwargs.get("executable")

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    monkeypatch.setattr(auto_push.subprocess, "run", _fake_run)

    invoke(str(tmp_path))

    assert captured["cmd"][0] == "git", (
        "the resolved binary belongs in executable=, not argv[0] -- argv[0] is "
        "what the shared-git-runner gate keys on"
    )
    assert captured["executable"] == resolved, (
        "git_exe()'s resolution must reach subprocess.run, or the spawn falls "
        "back to the PATH lookup that produced the [WinError 2] cluster"
    )



# Review: overengineering-reviewer Finding 4 -- auto_push.main(),
# _release_claims_for_head, _push_would_be_a_noop, and _ref_sha are all
# gravestoned (no production caller since C7 removed the post-commit
# hook's invocation of this module); their driving tests retired with
# them.
