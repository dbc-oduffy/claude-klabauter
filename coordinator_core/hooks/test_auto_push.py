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
  - run_push_with_retry()'s retry policy: ref-lock retries (re-pushing) to
    MAX_ATTEMPTS then logs; non-fast-forward issues push_once exactly ONCE and
    instead polls a read-only fetch+ancestor "already superseded" test on each
    remaining attempt's backoff (XB-12) -- resolving as soon as a poll confirms
    the commit already reached origin and logging nothing, and logging FAILED
    only once every poll is exhausted with no supersession found.
    COORDINATOR_AUTO_PUSH_NO_SLEEP=1 is set for all retry tests so
    gh-transient's seconds-scale backoff is never actually paid.

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

def test_run_push_with_retry_ref_lock_retries_to_per_class_budget_then_logs(monkeypatch, tmp_path):
    # ref-lock gets its own per-class budget (DEC-1) -- MAX_ATTEMPTS is NOT
    # what bounds this class any more, so this asserts _attempts_for("ref-lock")
    # rather than the shared constant (regression guard: a literal here would
    # silently pass even if the per-class table stopped being consulted).
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv(auto_push._ENV_NO_SLEEP, "1")

    call_count = {"n": 0}

    def _fake_push_once(root, branch, windows_bash, ssh_remote):
        call_count["n"] += 1
        return False, "error: cannot lock ref 'refs/heads/work/foo': is at abc but expected def\n"

    monkeypatch.setattr(auto_push, "push_once", _fake_push_once)
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    auto_push.run_push_with_retry(repo_root, "work/foo")

    expected_attempts = auto_push._attempts_for("ref-lock")
    assert call_count["n"] == expected_attempts
    log_path = tmp_path / ".git" / "push-failures.log"
    assert log_path.exists()
    content = log_path.read_text()
    assert f"after {expected_attempts}" in content
    assert "ref-lock" in content


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


def test_run_push_with_retry_non_fast_forward_exhausts_when_never_superseded(monkeypatch, tmp_path):
    # A genuine, still-diverged non-fast-forward: _is_ancestor never confirms
    # our commit reached origin by any path. push_once is issued exactly
    # once (never re-pushed); the remaining attempts poll supersession on
    # the existing backoff, and once every poll comes back negative this
    # fails loud, logging after MAX_ATTEMPTS.
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv(auto_push._ENV_NO_SLEEP, "1")

    call_count = {"n": 0}

    def _fake_push_once(root, branch, windows_bash, ssh_remote):
        call_count["n"] += 1
        return False, "! [rejected] work/foo -> work/foo (fetch first)\nnon-fast-forward\n"

    monkeypatch.setattr(auto_push, "push_once", _fake_push_once)
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
        ("rev-parse", "work/foo"): "abc123",
        ("fetch", "origin", "work/foo"): "",
    }.get(tuple(args)))
    ancestor_calls = {"n": 0}

    def _fake_is_ancestor(root, sha, ref):
        ancestor_calls["n"] += 1
        return False

    monkeypatch.setattr(auto_push, "_is_ancestor", _fake_is_ancestor)
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    auto_push.run_push_with_retry(repo_root, "work/foo")

    assert call_count["n"] == 1
    # MAX_ATTEMPTS, deliberately, not `_attempts_for("non-fast-forward")`: this
    # is DEC-1's independence guard. The non-FF poll budget is a fail-loud
    # mechanism kept separate from the per-class RETRY table, so rebinding this
    # bound to the table -- the obvious "simplification" -- must fail here.
    assert ancestor_calls["n"] == auto_push.MAX_ATTEMPTS
    log_path = tmp_path / ".git" / "push-failures.log"
    assert log_path.exists()
    content = log_path.read_text()
    assert f"after {auto_push.MAX_ATTEMPTS}" in content
    assert "non-fast-forward" in content


def test_run_push_with_retry_non_fast_forward_resolves_immediately_when_superseded(monkeypatch, tmp_path, capsys):
    # The commit is already on origin (a peer's push, or our own
    # out-of-order async sibling, landed it first) -- must NOT write to
    # push-failures.log at all, must retry exactly once (the supersession
    # check runs before any retry decision), and must print an info-level
    # trace instead (XB-12: this is the false-positive PUSH FAILED the row
    # exists to eliminate).
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv(auto_push._ENV_NO_SLEEP, "1")

    call_count = {"n": 0}

    def _fake_push_once(root, branch, windows_bash, ssh_remote):
        call_count["n"] += 1
        return False, "! [rejected] work/foo -> work/foo (fetch first)\nnon-fast-forward\n"

    monkeypatch.setattr(auto_push, "push_once", _fake_push_once)
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
        ("rev-parse", "work/foo"): "abc123",
        ("fetch", "origin", "work/foo"): "",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "_is_ancestor", lambda root, sha, ref: True)
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    auto_push.run_push_with_retry(repo_root, "work/foo")

    assert call_count["n"] == 1
    log_path = tmp_path / ".git" / "push-failures.log"
    assert not log_path.exists()
    captured = capsys.readouterr()
    assert "race resolved" in captured.err
    assert "work/foo" in captured.err


def test_run_push_with_retry_non_fast_forward_polls_then_resolves(monkeypatch, tmp_path, capsys):
    # First supersession check (right after the sole push_once call): not yet
    # superseded (peer's push hasn't landed on origin from our vantage point
    # yet). Second check, on the next attempt's poll: confirms our commit
    # reached origin -- must stop polling immediately (not grind to
    # MAX_ATTEMPTS), must NOT re-issue push_once, and must not log a failure.
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv(auto_push._ENV_NO_SLEEP, "1")

    call_count = {"n": 0}
    ancestor_calls = {"n": 0}

    def _fake_push_once(root, branch, windows_bash, ssh_remote):
        call_count["n"] += 1
        return False, "! [rejected] work/foo -> work/foo (fetch first)\nnon-fast-forward\n"

    def _fake_is_ancestor(root, sha, ref):
        ancestor_calls["n"] += 1
        return ancestor_calls["n"] >= 2

    monkeypatch.setattr(auto_push, "push_once", _fake_push_once)
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
        ("rev-parse", "work/foo"): "abc123",
        ("fetch", "origin", "work/foo"): "",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "_is_ancestor", _fake_is_ancestor)
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    auto_push.run_push_with_retry(repo_root, "work/foo")

    assert call_count["n"] == 1
    assert ancestor_calls["n"] == 2
    log_path = tmp_path / ".git" / "push-failures.log"
    assert not log_path.exists()
    captured = capsys.readouterr()
    assert "race resolved" in captured.err


def test_run_push_with_retry_dead_ref_never_retries_never_logs_stderr_only(monkeypatch, tmp_path, capsys):
    # AC1-AC3: a `src refspec ... does not match any` rejection classifies
    # as dead-ref, is attempted exactly once (not retried), and reports on
    # stderr only -- push-failures.log must stay unwritten, mirroring
    # log_race_resolved()'s precedent for keeping non-actionable outcomes
    # out of the forensic failure log.
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv(auto_push._ENV_NO_SLEEP, "1")

    call_count = {"n": 0}

    def _fake_push_once(root, branch, windows_bash, ssh_remote):
        call_count["n"] += 1
        return False, "error: src refspec work/gone does not match any\n"

    monkeypatch.setattr(auto_push, "push_once", _fake_push_once)
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    auto_push.run_push_with_retry(repo_root, "work/gone")

    assert call_count["n"] == 1, "dead-ref must not be retried"
    log_path = tmp_path / ".git" / "push-failures.log"
    assert not log_path.exists()
    captured = capsys.readouterr()
    assert "dead-ref" in captured.err
    assert "work/gone" in captured.err


def test_run_push_with_retry_succeeds_on_second_attempt(monkeypatch, tmp_path):
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv(auto_push._ENV_NO_SLEEP, "1")

    call_count = {"n": 0}

    def _fake_push_once(root, branch, windows_bash, ssh_remote):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return False, "error: cannot lock ref 'refs/heads/work/foo'\n"
        return True, ""

    monkeypatch.setattr(auto_push, "push_once", _fake_push_once)
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    auto_push.run_push_with_retry(repo_root, "work/foo")

    assert call_count["n"] == 2
    log_path = tmp_path / ".git" / "push-failures.log"
    assert not log_path.exists()


# ---------------------------------------------------------------------------
# cockpit-contract release publish -- fires after a successful push, gated
# on a filesystem-only scoping guard (`_cockpit_publish_script`). See
# auto_push.py's "cockpit-contract release publish" section for the
# fleet-wide-firing defect this guard exists to prevent
# (cross-repo/archive/2026-07-25-claude-klabauter-em-cockpit-publish-use-a-github-action-not-a-claude-klabauter-directive.md,
# DoE-claude).
# ---------------------------------------------------------------------------

def test_run_push_with_retry_no_cockpit_script_never_attempts_publish_anti_regression(monkeypatch, tmp_path):
    # Anti-regression for the rejected fleet-wide-firing design: a repo that
    # does not track .github/scripts/publish_cockpit_contract.py (i.e. every
    # coordinator-installed repo except DoE-claude) must NEVER attempt to
    # invoke it, even on a fully successful push. No .github/ tree is created
    # in this tmp_path repo at all.
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv(auto_push._ENV_NO_SLEEP, "1")

    monkeypatch.setattr(auto_push, "push_once", lambda root, branch, windows_bash, ssh_remote: (True, ""))
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
        ("rev-parse", "work/foo"): "abc123",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    invoked = []
    monkeypatch.setattr(auto_push, "_invoke_cockpit_publish", lambda *a, **k: invoked.append(a))

    auto_push.run_push_with_retry(repo_root, "work/foo")

    assert invoked == []
    # The guard must short-circuit before the extra rev-parse this seam adds
    # for repos that DO carry the script -- resolve_candidate's ancestry
    # rev-parse never fires here.
    assert auto_push._cockpit_publish_script(repo_root) is None


def test_run_push_with_retry_cockpit_script_present_schema_not_touched_skips_publish(monkeypatch, tmp_path):
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    script_dir = tmp_path / ".github" / "scripts"
    script_dir.mkdir(parents=True)
    (script_dir / "publish_cockpit_contract.py").write_text("# stub\n")
    monkeypatch.setenv(auto_push._ENV_NO_SLEEP, "1")

    monkeypatch.setattr(auto_push, "push_once", lambda root, branch, windows_bash, ssh_remote: (True, ""))
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
        ("rev-parse", "work/foo"): "abc123",
        ("rev-parse", "refs/remotes/origin/work/foo"): "old111",
        ("diff", "--name-only", "old111..abc123", "--", auto_push._COCKPIT_SCHEMA_PATH): "",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    invoked = []
    monkeypatch.setattr(auto_push, "_invoke_cockpit_publish", lambda *a, **k: invoked.append(a))

    auto_push.run_push_with_retry(repo_root, "work/foo")

    assert invoked == []


def test_run_push_with_retry_cockpit_schema_touched_attempts_publish(monkeypatch, tmp_path):
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    script_dir = tmp_path / ".github" / "scripts"
    script_dir.mkdir(parents=True)
    script_path = script_dir / "publish_cockpit_contract.py"
    script_path.write_text("# stub\n")
    monkeypatch.setenv(auto_push._ENV_NO_SLEEP, "1")

    monkeypatch.setattr(auto_push, "push_once", lambda root, branch, windows_bash, ssh_remote: (True, ""))
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
        ("rev-parse", "work/foo"): "abc123",
        ("rev-parse", "refs/remotes/origin/work/foo"): "old111",
        ("diff", "--name-only", "old111..abc123", "--", auto_push._COCKPIT_SCHEMA_PATH):
            "coordinator/cockpit-contract/schema/cockpit-contract.schema.json",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    invoked = []
    monkeypatch.setattr(
        auto_push, "_invoke_cockpit_publish",
        lambda root, script: invoked.append((root, script)),
    )

    auto_push.run_push_with_retry(repo_root, "work/foo")

    assert invoked == [(repo_root, script_path)]


def test_run_push_with_retry_cockpit_schema_touched_multi_commit_push_uses_full_range(monkeypatch, tmp_path):
    # A schema-touching commit buried earlier in a multi-commit push must
    # still be caught -- the diff range is old_remote_sha..local_sha, not
    # merely the latest commit.
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    script_dir = tmp_path / ".github" / "scripts"
    script_dir.mkdir(parents=True)
    (script_dir / "publish_cockpit_contract.py").write_text("# stub\n")
    monkeypatch.setenv(auto_push._ENV_NO_SLEEP, "1")

    monkeypatch.setattr(auto_push, "push_once", lambda root, branch, windows_bash, ssh_remote: (True, ""))
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
        ("rev-parse", "work/foo"): "headsha",
        ("rev-parse", "refs/remotes/origin/work/foo"): "basesha",
        ("diff", "--name-only", "basesha..headsha", "--", auto_push._COCKPIT_SCHEMA_PATH):
            "coordinator/cockpit-contract/schema/cockpit-contract.schema.json",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    invoked = []
    monkeypatch.setattr(
        auto_push, "_invoke_cockpit_publish",
        lambda root, script: invoked.append((root, script)),
    )

    auto_push.run_push_with_retry(repo_root, "work/foo")

    assert len(invoked) == 1


def test_run_push_with_retry_first_push_no_remote_tracking_ref_uses_empty_tree_base(monkeypatch, tmp_path):
    # A brand-new branch has no refs/remotes/origin/<branch> yet -- the base
    # falls back to the empty-tree SHA rather than skipping the check.
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    script_dir = tmp_path / ".github" / "scripts"
    script_dir.mkdir(parents=True)
    (script_dir / "publish_cockpit_contract.py").write_text("# stub\n")
    monkeypatch.setenv(auto_push._ENV_NO_SLEEP, "1")

    monkeypatch.setattr(auto_push, "push_once", lambda root, branch, windows_bash, ssh_remote: (True, ""))
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
        ("rev-parse", "work/foo"): "abc123",
        ("rev-parse", "refs/remotes/origin/work/foo"): None,
        (
            "diff", "--name-only", f"{auto_push._EMPTY_TREE_SHA}..abc123",
            "--", auto_push._COCKPIT_SCHEMA_PATH,
        ): "coordinator/cockpit-contract/schema/cockpit-contract.schema.json",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    invoked = []
    monkeypatch.setattr(
        auto_push, "_invoke_cockpit_publish",
        lambda root, script: invoked.append((root, script)),
    )

    auto_push.run_push_with_retry(repo_root, "work/foo")

    assert len(invoked) == 1


def test_run_push_with_retry_push_failure_never_attempts_publish(monkeypatch, tmp_path):
    # A failed push (retries exhausted) must never attempt the publish, even
    # when the repo carries the script and the local tree touched the schema
    # dir -- nothing was actually pushed, so there is nothing new on origin
    # to publish from.
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    script_dir = tmp_path / ".github" / "scripts"
    script_dir.mkdir(parents=True)
    (script_dir / "publish_cockpit_contract.py").write_text("# stub\n")
    monkeypatch.setenv(auto_push._ENV_NO_SLEEP, "1")

    monkeypatch.setattr(
        auto_push, "push_once",
        lambda root, branch, windows_bash, ssh_remote: (
            False, "error: cannot lock ref 'refs/heads/work/foo'\n",
        ),
    )
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
        ("rev-parse", "work/foo"): "abc123",
        ("rev-parse", "refs/remotes/origin/work/foo"): "old111",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    invoked = []
    monkeypatch.setattr(auto_push, "_invoke_cockpit_publish", lambda *a, **k: invoked.append(a))

    auto_push.run_push_with_retry(repo_root, "work/foo")

    assert invoked == []


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


def test_run_push_with_retry_cockpit_publish_nonzero_exit_still_leaves_push_successful(monkeypatch, tmp_path, capsys):
    # The publish script declining/failing must never be mistaken for a push
    # failure -- no push-failures.log entry, and run_push_with_retry itself
    # must not raise.
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    script_dir = tmp_path / ".github" / "scripts"
    script_dir.mkdir(parents=True)
    (script_dir / "publish_cockpit_contract.py").write_text("# stub\n")
    monkeypatch.setenv(auto_push._ENV_NO_SLEEP, "1")

    monkeypatch.setattr(auto_push, "push_once", lambda root, branch, windows_bash, ssh_remote: (True, ""))
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
        ("rev-parse", "work/foo"): "abc123",
        ("rev-parse", "refs/remotes/origin/work/foo"): "old111",
        ("diff", "--name-only", "old111..abc123", "--", auto_push._COCKPIT_SCHEMA_PATH):
            "coordinator/cockpit-contract/schema/cockpit-contract.schema.json",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    class _FakeResult:
        returncode = 3

    monkeypatch.setattr(auto_push.subprocess, "run", lambda *a, **k: _FakeResult())
    monkeypatch.setattr(auto_push, "_resolve_python_exe", lambda: "python3")

    auto_push.run_push_with_retry(repo_root, "work/foo")  # must not raise

    captured = capsys.readouterr()
    assert "[coordinator]" in captured.err
    log_path = tmp_path / ".git" / "push-failures.log"
    assert not log_path.exists()


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


def test_route_label_always_reports_direct_push():
    # Even for Windows+SSH, route_label must report "direct push" -- there is
    # no fallback seam left to flip.
    assert auto_push.route_label(True, True) == "direct push"
    assert auto_push.route_label(False, False) == "direct push"


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




# ---------------------------------------------------------------------------
# Durable pending-push record (AC14/AC14a/AC15, C7/C9) -- hold, drain,
# coalesce, takeover, the loss path, and a real-subprocess wire-path test.
#
# Review: coordinator:code-reviewer (P3, 2026-08-30) -- `_shared_branch_
# live_count`/`_hold_window` are gravestoned (2026-08-30-who-pushes-and-
# when.md C8); the tests below drive `_write_pending_record`/
# `_read_pending_record`/`drain_pending_push` directly and do not
# monkeypatch either deleted predicate. This header previously described a
# monkeypatching approach that no longer applies to the tests beneath it.
# ---------------------------------------------------------------------------

def _dead_pid_for_record() -> int:
    """A `holder_pid` value `_holder_alive` will report as dead.

    Not a live PID at all (rather than racing a real spawned-and-reaped
    child's PID against OS recycling) -- deterministic across POSIX and
    Windows, and matches the plan body's own "simulate with a pending
    record carrying a dead pid" alternative to actually killing a holder
    mid-window.
    """
    return 999_999_999






# ---------------------------------------------------------------------------
# Review: coordinator:code-reviewer (P3, 2026-08-30) -- `_peer_commit_
# within_window` is gravestoned along with `_hold_window` (2026-08-30-who-
# pushes-and-when.md C8). The tests below exercise the loss/drain path via
# `_write_pending_record`/`_read_pending_record`/`drain_pending_push`
# directly and do not restore or call the deleted predicate; this header
# previously described a "restore the real function" approach that no
# longer applies to the tests beneath it.
# ---------------------------------------------------------------------------











def test_loss_path_dead_holder_mid_window_drains_and_reaches_remote(monkeypatch, tmp_path):
    # Simulates a holder that died mid-window (AC14's forensic-trace case):
    # a pending record is on disk, its hold_until is still far in the
    # future (the holder never got to wake and push), and its holder_pid is
    # dead. The record alone must make the interrupted hold detectable from
    # disk -- no process-table access beyond the recorded pid.
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    dead_pid = _dead_pid_for_record()
    future_hold_until = time.time() + 9999
    wrote = auto_push._write_pending_record(
        repo_root, "work/foo", "deadsha", future_hold_until, dead_pid
    )
    assert wrote

    # Detectable from disk alone, before any drain runs.
    on_disk = auto_push._read_pending_record(repo_root)
    assert on_disk == {
        "branch": "work/foo",
        "sha": "deadsha",
        "hold_until": future_hold_until,
        "holder_pid": dead_pid,
    }
    assert auto_push._holder_alive(dead_pid) is False
    assert auto_push._record_is_stale(on_disk, time.time()) is True

    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
        # Branch still resolves locally -- this scenario is a dead HOLDER
        # process, not a dead ref (AC4 must fall through to the unchanged
        # push path here, not the dead-ref handling).
        ("rev-parse", "--verify", "refs/heads/work/foo"): "deadsha",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    push_calls = []

    def _fake_push_once(root, branch, windows_bash, ssh_remote):
        push_calls.append(branch)
        return True, ""  # "reaches its remote"

    monkeypatch.setattr(auto_push, "push_once", _fake_push_once)

    auto_push.drain_pending_push(repo_root)

    assert push_calls == ["work/foo"], "drain must push the dead holder's branch"
    # Removed ONLY after the successful push -- never before, never on a
    # failed attempt.
    assert auto_push._read_pending_record(repo_root) is None


def test_loss_path_drain_leaves_record_in_place_on_failed_push(monkeypatch, tmp_path):
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    dead_pid = _dead_pid_for_record()
    auto_push._write_pending_record(
        repo_root, "work/foo", "deadsha", time.time() + 9999, dead_pid
    )
    monkeypatch.setenv(auto_push._ENV_NO_SLEEP, "1")
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
        ("rev-parse", "work/foo"): "deadsha",
        ("fetch", "origin", "work/foo"): "",
        # Branch still resolves locally -- dead HOLDER process, not a dead
        # ref; AC4 must fall through to the unchanged push path.
        ("rev-parse", "--verify", "refs/heads/work/foo"): "deadsha",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)
    monkeypatch.setattr(auto_push, "_is_ancestor", lambda root, sha, ref: False)
    monkeypatch.setattr(
        auto_push, "push_once",
        lambda root, branch, w, s: (False, "error: cannot lock ref\n"),
    )

    auto_push.drain_pending_push(repo_root)

    # Never removed on a failed/exhausted push -- a missed drain DELAYS,
    # never LOSES, the record stays for the next drain point to retry.
    record = auto_push._read_pending_record(repo_root)
    assert record is not None
    assert record["branch"] == "work/foo"


# ---------------------------------------------------------------------------
# Dead-ref pending records (AC4-AC7) -- a record whose `branch` no longer
# resolves locally (the branch-rename incident: `work/machine-a/2026-08-10`
# renamed to `work/machine-a/2026-08-10to11`, leaving the old name's record
# behind). Without AC4-AC7 this loops forever: `due` stays true on every
# later commit once `hold_until` has passed, so the pre-fix code kept
# re-calling `run_push_with_retry` for a branch that can never resolve,
# appending a `push-failures.log` row every time.
# ---------------------------------------------------------------------------

def test_drain_pending_push_dead_ref_sha_absent_drops_no_log(monkeypatch, tmp_path, capsys):
    # AC6: no pinned sha at all -- nothing to retry, drop with a stderr
    # note only.
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    auto_push._write_pending_record(repo_root, "work/gone", None, time.time() - 1, os.getpid())
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        # branch/gone does not resolve -- for-each-ref/branch --show-current
        # both come up empty, matching a branch that was renamed away.
    }.get(tuple(args)))

    push_calls = {"n": 0}
    monkeypatch.setattr(
        auto_push, "push_once",
        lambda root, branch, w, s: push_calls.__setitem__("n", push_calls["n"] + 1) or (True, ""),
    )

    auto_push.drain_pending_push(repo_root)

    assert push_calls["n"] == 0, "no sha pinned -- nothing to push"
    assert auto_push._read_pending_record(repo_root) is None
    log_path = tmp_path / ".git" / "push-failures.log"
    assert not log_path.exists()
    assert "dropping pending push" in capsys.readouterr().err


def test_drain_pending_push_dead_ref_already_on_origin_drops_no_log(monkeypatch, tmp_path, capsys):
    # AC6: the branch was renamed, but the rename's own push already
    # carried the pinned sha to origin under the CURRENT branch name --
    # the observed 2026-08-11 incident. Drop, stderr note only.
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    auto_push._write_pending_record(
        repo_root, "work/machine-a/2026-08-10", "abc123", time.time() - 1, os.getpid()
    )
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        # The old name never resolves; the resolved current branch is the
        # renamed one.
        ("branch", "--show-current"): "work/machine-a/2026-08-10to11",
        ("for-each-ref", "--format=%(refname:short)", "refs/heads/"): "work/machine-a/2026-08-10to11",
        ("fetch", "origin", "work/machine-a/2026-08-10to11"): "",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "_is_ancestor", lambda root, sha, ref: True)

    push_calls = {"n": 0}
    monkeypatch.setattr(
        auto_push, "push_once",
        lambda root, branch, w, s: push_calls.__setitem__("n", push_calls["n"] + 1) or (True, ""),
    )

    auto_push.drain_pending_push(repo_root)

    assert push_calls["n"] == 0, "commit is already on origin -- nothing to push"
    assert auto_push._read_pending_record(repo_root) is None
    log_path = tmp_path / ".git" / "push-failures.log"
    assert not log_path.exists()
    assert "dropping pending push" in capsys.readouterr().err


def test_drain_pending_push_dead_ref_reachable_from_current_branch_retargets_and_pushes(
    monkeypatch, tmp_path
):
    # AC5: the branch was renamed and the commits moved with it, but the
    # rename's push has NOT yet reached origin from this vantage point --
    # the record is re-targeted onto the current branch name and pushed.
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    auto_push._write_pending_record(
        repo_root, "work/machine-a/2026-08-10", "abc123", time.time() - 1, 4242
    )
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("branch", "--show-current"): "work/machine-a/2026-08-10to11",
        ("for-each-ref", "--format=%(refname:short)", "refs/heads/"): "work/machine-a/2026-08-10to11",
        ("fetch", "origin", "work/machine-a/2026-08-10to11"): None,  # fetch fails -> not superseded
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
        ("rev-parse", "work/machine-a/2026-08-10to11"): "abc123",
    }.get(tuple(args)))

    def _fake_is_ancestor(root, sha, ref):
        # Locally reachable from the CURRENT branch tip.
        return ref == "work/machine-a/2026-08-10to11" and sha == "abc123"

    monkeypatch.setattr(auto_push, "_is_ancestor", _fake_is_ancestor)
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    push_calls = []
    monkeypatch.setattr(
        auto_push, "push_once",
        lambda root, branch, w, s: (push_calls.append(branch), (True, ""))[1],
    )

    auto_push.drain_pending_push(repo_root)

    assert push_calls == ["work/machine-a/2026-08-10to11"], "retargeted push must use the CURRENT branch"
    # Successful push clears the (retargeted) record.
    assert auto_push._read_pending_record(repo_root) is None
    log_path = tmp_path / ".git" / "push-failures.log"
    assert not log_path.exists()


def test_drain_pending_push_dead_ref_orphaned_logs_once_and_drops(monkeypatch, tmp_path):
    # AC7: reachable from nowhere -- genuine loss risk, not a rename
    # artifact. ONE loud push-failures.log row naming the orphaned sha,
    # then drop; never looped.
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    auto_push._write_pending_record(repo_root, "work/gone", "orphansha", time.time() - 1, 4242)
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("branch", "--show-current"): "work/other",
        ("for-each-ref", "--format=%(refname:short)", "refs/heads/"): "work/other",
        ("fetch", "origin", "work/other"): "",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "_is_ancestor", lambda root, sha, ref: False)

    push_calls = {"n": 0}
    monkeypatch.setattr(
        auto_push, "push_once",
        lambda root, branch, w, s: push_calls.__setitem__("n", push_calls["n"] + 1) or (True, ""),
    )

    auto_push.drain_pending_push(repo_root)

    assert push_calls["n"] == 0, "unreachable sha must never be pushed"
    assert auto_push._read_pending_record(repo_root) is None
    log_path = tmp_path / ".git" / "push-failures.log"
    assert log_path.exists()
    content = log_path.read_text()
    assert "orphansha" in content
    assert "work/gone" in content


def test_drain_pending_push_dead_ref_sha_none_with_current_branch_retargets_and_pushes(
    monkeypatch, tmp_path, capsys
):
    # Review: coordinator:review-code, Finding 1, 2026-08-19 -- the compound
    # case AC3 exists to cover: `wsc_tail._deferred_publisher_backstop`
    # writes a record with `sha=None` (commit hadn't landed at write time),
    # and a branch rename (workday-start-step0) lands before the next drain.
    # Case 0 must retarget onto the current branch and push, exactly like
    # the sha-pinned case 2 -- NOT drop, which would silently lose the
    # backstopped obligation.
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    auto_push._write_pending_record(
        repo_root, "work/machine-a/2026-08-18", None, time.time() - 1, 4242
    )
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("branch", "--show-current"): "work/machine-a/2026-08-18to19",
        ("for-each-ref", "--format=%(refname:short)", "refs/heads/"): "work/machine-a/2026-08-18to19",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    push_calls = []
    monkeypatch.setattr(
        auto_push, "push_once",
        lambda root, branch, w, s: (push_calls.append(branch), (True, ""))[1],
    )

    auto_push.drain_pending_push(repo_root)

    assert push_calls == ["work/machine-a/2026-08-18to19"], (
        "an unknown-sha record must still retarget and push, not be dropped"
    )
    assert auto_push._read_pending_record(repo_root) is None
    assert "re-targeted" in capsys.readouterr().err


def test_drain_pending_push_dead_ref_sha_none_no_current_branch_drops(monkeypatch, tmp_path, capsys):
    # Complements the sha-absent test above: with no current branch to
    # retarget onto at all, case 0 still drops -- there is nowhere left the
    # payload could be.
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    auto_push._write_pending_record(repo_root, "work/gone", None, time.time() - 1, 4242)
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {}.get(tuple(args)))

    push_calls = {"n": 0}
    monkeypatch.setattr(
        auto_push, "push_once",
        lambda root, branch, w, s: push_calls.__setitem__("n", push_calls["n"] + 1) or (True, ""),
    )

    auto_push.drain_pending_push(repo_root)

    assert push_calls["n"] == 0
    assert auto_push._read_pending_record(repo_root) is None
    assert "dropping pending push" in capsys.readouterr().err










# ---------------------------------------------------------------------------
# Divergence gate (C4): a branch already diverged from its upstream defers
# to the pending record instead of pushing into a guaranteed non-fast-
# forward wall. Real git repos throughout (no `_run_git`/object-store
# monkeypatching) -- the whole point of `_branch_diverged_no_spawn` is a
# real spawn-free read of the real object store, so faking that store would
# test nothing.
# ---------------------------------------------------------------------------

def _real_git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"


def _init_divergence_repo(tmp_path: Path, branch: str) -> Path:
    """A real work repo on `branch`, with a real bare `origin` remote, one
    commit already pushed (so `refs/remotes/origin/<branch>` exists
    locally). Returns the work repo root."""
    bare = tmp_path / "bare.git"
    work = tmp_path / "work"
    _real_git(["init", "--bare", "-q", str(bare)], tmp_path)
    _real_git(["init", "-q", str(work)], tmp_path)
    _real_git(["config", "user.email", "auto-push-c4@example.com"], work)
    _real_git(["config", "user.name", "auto-push-c4"], work)
    _real_git(["config", "commit.gpgsign", "false"], work)
    _real_git(["checkout", "-q", "-b", branch], work)
    (work / "f.txt").write_text("one\n", encoding="utf-8")
    _real_git(["add", "f.txt"], work)
    _real_git(["commit", "-q", "-m", "c1"], work)
    _real_git(["remote", "add", "origin", str(bare)], work)
    _real_git(["push", "-q", "-u", "origin", branch], work)
    return work


















# ---------------------------------------------------------------------------
# Wire-path: the pending-push record's own respawn must survive being
# ACTUALLY EXECUTED, not merely argv-shape-asserted -- the same blind spot
# named in the 2026-08-01 import regression (see the "actually imports"
# tests above) applies equally to this record's code path, since
# `drain_pending_push` now runs unconditionally at the head of every
# `run_push_with_retry` call, including inside the respawned child. This
# test spawns a REAL child against a REAL local git remote (not a fake
# `.git` stub) and asserts the branch tip actually lands there -- a real
# push attempt, not just a clean exit.
# ---------------------------------------------------------------------------

def _init_real_repo_with_local_remote(tmp_path: Path) -> tuple[str, str, str]:
    """Build a real (non-bare) git repo with one commit, plus a real bare
    remote it can push to over the filesystem. Returns
    (repo_root, branch, bare_remote_path).
    """
    bare = tmp_path / "remote.git"
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "auto-push-test@example.com"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "auto-push-test"], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-b", "work/wire-path-test"], check=True, capture_output=True)
    (work / "file.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "file.txt"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "wire-path test commit"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "remote", "add", "origin", str(bare)],
        check=True, capture_output=True,
    )
    return str(work), "work/wire-path-test", str(bare)


# Review: overengineering-reviewer Finding 4 -- auto_push.main(),
# _release_claims_for_head, _push_would_be_a_noop, and _ref_sha are all
# gravestoned (no production caller since C7 removed the post-commit
# hook's invocation of this module); their driving tests retired with
# them.
