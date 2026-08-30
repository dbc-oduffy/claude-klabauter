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


def test_main_branch_gate_skip_prints_to_stderr(monkeypatch, tmp_path, capsys):
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv(auto_push._ENV_SYNC, "1")
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("branch", "--show-current"): "feature/foo",
        ("for-each-ref", "--format=%(refname:short)", "refs/heads/"): "feature/foo",
        ("rev-parse", "--show-toplevel"): repo_root,
    }.get(tuple(args), repo_root if args and args[0] == "rev-parse" else None))

    rc = auto_push.main(["--repo-root", repo_root])
    assert rc == 0
    captured = capsys.readouterr()
    assert "skipping feature/foo" in captured.err


def test_main_unrecognized_branch_skip_prints_to_stderr(monkeypatch, tmp_path, capsys):
    repo_root = str(tmp_path)
    monkeypatch.setenv(auto_push._ENV_SYNC, "1")
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("branch", "--show-current"): "main",
        ("for-each-ref", "--format=%(refname:short)", "refs/heads/"): "main",
    }.get(tuple(args)))

    rc = auto_push.main(["--repo-root", repo_root])
    assert rc == 0
    captured = capsys.readouterr()
    assert "skipping main" in captured.err


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

def test_detach_and_run_windows_respawn_sets_sync_seam_and_no_infinite_loop(monkeypatch, tmp_path):
    # On a platform without os.fork() (Windows), _detach_and_run must respawn
    # itself via subprocess.Popen with COORDINATOR_AUTO_PUSH_SYNC=1 in the
    # child's env -- this is the property that stops the respawned child from
    # re-detaching infinitely (the child sees _ENV_SYNC set and runs
    # synchronously instead of forking/respawning again).
    monkeypatch.delattr(auto_push.os, "fork", raising=False)

    captured = {}

    def _fake_popen(argv, env=None, creationflags=0, close_fds=True, stdin=None, stdout=None, stderr=None):
        captured["argv"] = argv
        captured["env"] = env
        captured["creationflags"] = creationflags
        captured["stdin"] = stdin
        captured["stdout"] = stdout
        captured["stderr"] = stderr

        class _FakeProc:
            pid = 12345

        return _FakeProc()

    monkeypatch.setattr(auto_push.subprocess, "Popen", _fake_popen)

    repo_root = str(tmp_path)
    auto_push._detach_and_run(repo_root, "work/foo")

    assert captured["env"][auto_push._ENV_SYNC] == "1"

    # The respawn MUST target the resolved absolute path of auto_push.py, never
    # `-m coordinator_core.hooks.auto_push`. The hook fires in every fleet repo,
    # where cwd is the committing repo and `coordinator_core` is not importable,
    # so a `-m` respawn raises ModuleNotFoundError in a DETACHED child -- an
    # invisible failure: commit succeeds, push silently never happens, nothing
    # logged. Same exec-by-abspath contract the sh shim honors.
    # Regression guard for the 2026-07-20 Windows spike finding.
    assert "-m" not in captured["argv"], "respawn must not use -m (breaks outside claude-klabauter)"
    assert os.path.isabs(captured["argv"][1])
    assert os.path.basename(captured["argv"][1]) == "auto_push.py"
    assert os.path.exists(captured["argv"][1])

    # Regression guard for the 2026-08-01 silent ModuleNotFoundError: the
    # module's top-level `from coordinator_core.git.git_dir import
    # resolve_git_common_dir` cannot resolve from an abspath-script respawn
    # unless this repo's root is on the child's PYTHONPATH.
    pythonpath = captured["env"]["PYTHONPATH"].split(os.pathsep)
    assert auto_push._claude_klabauter_package_root() in pythonpath

    # Detached child must never inherit the hook's stdin: an inherited-but-invalid
    # handle under CREATE_NO_WINDOW hangs _execute_child on nt.
    assert captured["stdin"] == auto_push.subprocess.DEVNULL
    # AC3: the Windows respawn leg must also disown stdout -- an
    # un-redirected pipe here keeps a capture_output=True parent blocked on
    # pipe-EOF until this detached child exits.
    assert captured["stdout"] == auto_push.subprocess.DEVNULL
    # stderr is redirected to a durable FILE (not DEVNULL, not a PIPE) so a
    # child dying at import is observable instead of silently swallowed --
    # see `_open_respawn_stderr_log`'s docstring. A file carries none of the
    # pipe-hold hazard AC3 guards against (never read back by this process).
    assert captured["stderr"] not in (auto_push.subprocess.DEVNULL, None)
    assert captured["stderr"] is not auto_push.subprocess.PIPE
    captured["stderr"].close()
    assert captured["argv"][2:4] == ["--repo-root", repo_root]


def test_main_with_sync_env_does_not_detach(monkeypatch, tmp_path):
    # The respawn-loop guard from the child's perspective: when
    # COORDINATOR_AUTO_PUSH_SYNC is set (as the respawned child's env has
    # it), main() must compute async_mode=False and run synchronously
    # instead of calling _detach_and_run again -- otherwise a respawned
    # Windows child would re-detach forever.
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv(auto_push._ENV_SYNC, "1")

    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("branch", "--show-current"): "work/foo",
        ("for-each-ref", "--format=%(refname:short)", "refs/heads/"): "work/foo",
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
    }.get(tuple(args)))

    detach_called = {"n": 0}
    monkeypatch.setattr(auto_push, "_detach_and_run", lambda *a, **k: detach_called.__setitem__("n", detach_called["n"] + 1))
    monkeypatch.setattr(
        auto_push, "push_once",
        lambda repo_root, branch, windows_bash, ssh_remote: (True, ""),
    )

    rc = auto_push.main(["--repo-root", repo_root])

    assert rc == 0
    assert detach_called["n"] == 0


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


# ---------------------------------------------------------------------------
# exit-0-always
# ---------------------------------------------------------------------------

def test_main_exits_0_on_push_failure(monkeypatch, tmp_path):
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv(auto_push._ENV_NO_SLEEP, "1")
    monkeypatch.setenv(auto_push._ENV_SYNC, "1")

    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("branch", "--show-current"): "work/foo",
        ("for-each-ref", "--format=%(refname:short)", "refs/heads/"): "work/foo",
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
    }.get(tuple(args)))
    monkeypatch.setattr(
        auto_push, "push_once",
        lambda repo_root, branch, windows_bash, ssh_remote: (
            False, "fatal: Authentication failed for 'https://github.com/org/repo.git'\n"
        ),
    )

    rc = auto_push.main(["--repo-root", repo_root])
    assert rc == 0
    log_path = tmp_path / ".git" / "push-failures.log"
    assert log_path.exists()
    content = log_path.read_text()
    assert "work/foo" in content
    assert "auth" in content
    # Separator-norm regression: the forensic path is a TEXT artifact, so it must
    # be forward-slashed like the pre-port bash oracle emitted -- str(Path) on nt
    # would drift the log's own rows mid-file (DoE observed the split 2026-07-20).
    assert "\\" not in content, f"backslash leaked into push-failures.log: {content!r}"


def test_main_exits_0_on_internal_exception(monkeypatch, tmp_path):
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv(auto_push._ENV_SYNC, "1")

    def _boom(root, args):
        if tuple(args) == ("rev-parse", "--show-toplevel"):
            return repo_root
        raise RuntimeError("simulated internal failure")

    monkeypatch.setattr(auto_push, "_run_git", _boom)

    rc = auto_push.main(["--repo-root", repo_root])
    assert rc == 0


def test_main_internal_exception_logs_class_message_and_traceback(monkeypatch, tmp_path):
    """AC13: pins the internal-error log-line format written by main()'s
    catch-all -- the exception class name and message must appear in
    .git/push-failures.log, and the forensic sidecar it points at must
    contain a real traceback (not the pre-fix hardcoded empty string)."""
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv(auto_push._ENV_SYNC, "1")

    def _boom(root, args):
        if tuple(args) == ("rev-parse", "--show-toplevel"):
            return repo_root
        raise RuntimeError("simulated internal failure")

    monkeypatch.setattr(auto_push, "_run_git", _boom)

    rc = auto_push.main(["--repo-root", repo_root])
    assert rc == 0

    log_path = tmp_path / ".git" / "push-failures.log"
    assert log_path.exists()
    content = log_path.read_text()
    assert "RuntimeError: simulated internal failure" in content

    # Extract the forensic sidecar path this line points at and confirm it
    # carries a real traceback, not the old hardcoded "".
    marker = "stderr="
    idx = content.index(marker) + len(marker)
    forensic_rel = content[idx:].strip()
    forensic_path = Path(forensic_rel)
    assert forensic_path.exists(), f"forensic sidecar not found at {forensic_path!r}"
    forensic_text = forensic_path.read_text()
    assert "Traceback (most recent call last):" in forensic_text
    assert "RuntimeError: simulated internal failure" in forensic_text
    assert "\\" not in content, f"backslash leaked into push-failures.log: {content!r}"


def test_main_internal_exception_logs_resolved_module_provenance(monkeypatch, tmp_path):
    """The internal-error row must name WHICH copy of auto_push.py ran.

    The hook resolves this module from CLAUDE_KLABAUTER_ROOT at fire time, so an
    internal-error row is ambiguous without the resolved path -- the
    2026-07-22 `NameError: _disown_stdio` incident logged an error that was
    impossible at HEAD, and the row carried no way to tell which file had
    actually executed. Pins the module path, interpreter, and Python version
    into the row, forward-slashed so the log's separator norm holds on nt.
    """
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv(auto_push._ENV_SYNC, "1")

    def _boom(root, args):
        if tuple(args) == ("rev-parse", "--show-toplevel"):
            return repo_root
        raise RuntimeError("simulated internal failure")

    monkeypatch.setattr(auto_push, "_run_git", _boom)

    rc = auto_push.main(["--repo-root", repo_root])
    assert rc == 0

    content = (tmp_path / ".git" / "push-failures.log").read_text()
    expected_module = Path(auto_push.__file__).resolve().as_posix()
    assert f"module={expected_module}" in content
    assert f"interp={Path(sys.executable).as_posix()}" in content
    assert (
        f"python={sys.version_info.major}.{sys.version_info.minor}."
        f"{sys.version_info.micro}"
    ) in content
    assert "\\" not in content, f"backslash leaked into push-failures.log: {content!r}"


def test_module_provenance_survives_a_long_exception_message(monkeypatch, tmp_path):
    """The 200-char cap applies to the exception text, never to the provenance.

    A verbose exception must not push the module path out of the row -- that
    field is the whole point of the row for a wrong-copy-ran failure.
    """
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv(auto_push._ENV_SYNC, "1")

    def _boom(root, args):
        if tuple(args) == ("rev-parse", "--show-toplevel"):
            return repo_root
        raise RuntimeError("x" * 500)

    monkeypatch.setattr(auto_push, "_run_git", _boom)

    assert auto_push.main(["--repo-root", repo_root]) == 0

    content = (tmp_path / ".git" / "push-failures.log").read_text()
    assert f"module={Path(auto_push.__file__).resolve().as_posix()}" in content


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


def test_main_branch_gate_skip_never_reaches_run_push_with_retry_so_publish_not_attempted(monkeypatch, tmp_path, capsys):
    # A skipped push (branch_gate() rejects the branch) never even calls
    # run_push_with_retry, so the publish seam cannot fire -- assert this by
    # making run_push_with_retry itself raise if reached at all.
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv(auto_push._ENV_SYNC, "1")
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("branch", "--show-current"): "feature/foo",
        ("for-each-ref", "--format=%(refname:short)", "refs/heads/"): "feature/foo",
        ("rev-parse", "--show-toplevel"): repo_root,
    }.get(tuple(args), repo_root if args and args[0] == "rev-parse" else None))

    def _boom(*a, **k):
        raise AssertionError("run_push_with_retry must not run on a skipped branch")

    monkeypatch.setattr(auto_push, "run_push_with_retry", _boom)

    rc = auto_push.main(["--repo-root", repo_root])

    assert rc == 0
    captured = capsys.readouterr()
    assert "skipping feature/foo" in captured.err


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

def test_detach_and_run_fork_child_disowns_stdio_before_push(monkeypatch, tmp_path):
    # The fork child must redirect its stdio to devnull BEFORE running the
    # push -- if the redirect happened after (or not at all), the child would
    # still hold the parent's stdout/stderr pipe write end open for the
    # entire push+retry lifetime.
    calls = []
    # os.fork() never exists on Windows -- raising=False lets this attach the
    # attribute rather than requiring it pre-exist, so the no-fork (Windows)
    # leg is exercised for real instead of the test dying in setup.
    monkeypatch.setattr(auto_push.os, "fork", lambda: 0, raising=False)
    monkeypatch.setattr(auto_push.os, "setsid", lambda: calls.append("setsid"), raising=False)
    monkeypatch.setattr(auto_push, "_disown_stdio", lambda: calls.append("disown"))
    monkeypatch.setattr(
        auto_push, "run_push_with_retry",
        lambda repo_root, branch: calls.append("push"),
    )

    def _fake_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(auto_push.os, "_exit", _fake_exit)

    with pytest.raises(SystemExit):
        auto_push._detach_and_run(str(tmp_path), "work/foo")

    assert calls == ["setsid", "disown", "push"]


def test_detach_and_run_fork_child_logs_disown_stdio_failure_before_exit(monkeypatch, tmp_path):
    """The fork-child guard around `_disown_stdio()` must write a forensic
    row to .git/push-failures.log before `os._exit(1)` -- previously it was
    completely silent, no log row, no stderr, no evidence it ever fired
    (cross-repo/inbox/2026-07-23-claude-central-em-enum-parity-consumed-and-fork-child-silence-reply.md).
    The row must name `_disown_stdio` and carry the same module-provenance
    fields as main()'s top-level internal-error row, and run_push_with_retry
    must never be reached -- the guard's whole point is to bail out before
    the push runs, since the child's stdio was never actually redirected.
    """
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()

    # os.fork()/os.setsid() never exist on Windows -- raising=False attaches
    # them rather than requiring pre-existence, so this exercises the same
    # fork-child guard logic on every platform.
    monkeypatch.setattr(auto_push.os, "fork", lambda: 0, raising=False)
    monkeypatch.setattr(auto_push.os, "setsid", lambda: None, raising=False)

    def _boom_disown():
        raise RuntimeError("simulated disown failure")

    monkeypatch.setattr(auto_push, "_disown_stdio", _boom_disown)

    push_called = {"n": False}
    monkeypatch.setattr(
        auto_push, "run_push_with_retry",
        lambda repo_root, branch: push_called.__setitem__("n", True),
    )

    def _fake_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(auto_push.os, "_exit", _fake_exit)

    with pytest.raises(SystemExit) as exc_info:
        auto_push._detach_and_run(repo_root, "some-branch")

    assert exc_info.value.code == 1
    assert push_called["n"] is False

    log_path = tmp_path / ".git" / "push-failures.log"
    assert log_path.exists()
    content = log_path.read_text()
    assert "_disown_stdio" in content
    assert "RuntimeError: simulated disown failure" in content
    expected_module = Path(auto_push.__file__).resolve().as_posix()
    assert f"module={expected_module}" in content
    assert f"interp={Path(sys.executable).as_posix()}" in content
    assert (
        f"python={sys.version_info.major}.{sys.version_info.minor}."
        f"{sys.version_info.micro}"
    ) in content
    assert "\\" not in content, f"backslash leaked into push-failures.log: {content!r}"


def test_disown_stdio_redirects_process_stdio_to_devnull(tmp_path):
    # Real subprocess (not the pytest process itself) proves _disown_stdio()
    # actually severs the OS-level fd 1/2, not just a Python-level object --
    # a write AFTER the call must land in devnull, invisible to a
    # capture_output=True reader of this subprocess.
    module_root = str(Path(auto_push.__file__).resolve().parents[2])
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {module_root!r})
        from coordinator_core.hooks import auto_push
        auto_push._disown_stdio()
        sys.stdout.write("SHOULD_NOT_REACH_PARENT_PIPE")
        sys.stdout.flush()
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_fork_leg_parent_returns_before_slow_push_child_finishes(tmp_path):
    # AC3's core regression: a monkeypatched slow push child + a
    # capture_output=True parent ("git commit"-shaped invocation) must return
    # immediately, never blocking on the detached child's stdio pipe.
    if not hasattr(os, "fork"):
        pytest.skip("POSIX fork leg only")

    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    module_root = str(Path(auto_push.__file__).resolve().parents[2])
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {module_root!r})
        from coordinator_core.hooks import auto_push

        def _slow_push(repo_root, branch):
            time.sleep(2)

        auto_push.run_push_with_retry = _slow_push
        auto_push._detach_and_run({repo_root!r}, "work/foo")
        sys.exit(0)
    """)
    start = time.time()
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10,
    )
    elapsed = time.time() - start

    assert result.returncode == 0
    assert elapsed < 1.5, (
        f"capture_output=True parent took {elapsed:.2f}s -- fork child is "
        "still holding the stdout/stderr pipe open (pipe-hold regression)"
    )


def test_spawn_detached_push_never_uses_fork(monkeypatch, tmp_path):
    def _boom():
        raise AssertionError("spawn_detached_push must never call os.fork()")

    # os.fork() never exists on Windows -- raising=False attaches the guard
    # attribute rather than requiring it pre-exist.
    monkeypatch.setattr(auto_push.os, "fork", _boom, raising=False)
    monkeypatch.setattr(
        auto_push.subprocess, "Popen",
        lambda *a, **k: type("_FakeProc", (), {"pid": 1})(),
    )

    auto_push.spawn_detached_push(str(tmp_path), "work/foo")


# ---------------------------------------------------------------------------
# 2026-08-01/03 regression -- respawned child must actually IMPORT, not just
# have the right-shaped argv. The pre-fix bug (a6daf112e's top-level
# `from coordinator_core.git.git_dir import resolve_git_common_dir`) was
# invisible to every test above because they all monkeypatch
# `subprocess.Popen` and never let the child actually run -- an argv-shape
# assertion is exactly the blind spot that let a silently-dying respawn ship
# for two days. These tests spawn the REAL child process.
# ---------------------------------------------------------------------------

def _wait_for_stable_file_content(path: Path, quiet_period: float = 0.5, deadline_s: float = 10.0) -> str:
    """Poll `path` until its content stops changing for `quiet_period` seconds.

    The detached grandchild this test spawns writes to `path` asynchronously
    and at a variable pace (near-instant on an import crash, slightly longer
    once it reaches a real `git push` attempt) -- breaking out of a poll loop
    the FIRST time the file merely EXISTS (or contains the parent-written
    header) races the child's own writes and can read a truncated snapshot,
    silently passing a test that should have caught a crash. Waiting for the
    content to go quiet is what actually observes "the child is done writing."
    """
    deadline = time.time() + deadline_s
    last = None
    last_change = time.time()
    while time.time() < deadline:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current != last:
            last = current
            last_change = time.time()
        elif time.time() - last_change >= quiet_period:
            return current
        time.sleep(0.05)
    return last or ""

def test_respawn_env_injects_claude_klabauter_package_root_on_pythonpath():
    env = auto_push._respawn_env()
    entries = env["PYTHONPATH"].split(os.pathsep)
    assert auto_push._claude_klabauter_package_root() in entries
    assert env[auto_push._ENV_SYNC] == "1"


def test_respawn_env_prepends_without_discarding_existing_pythonpath(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/some/other/path")
    env = auto_push._respawn_env()
    entries = env["PYTHONPATH"].split(os.pathsep)
    assert auto_push._claude_klabauter_package_root() in entries
    assert "/some/other/path" in entries


def test_spawn_detached_push_child_actually_imports_when_spawned_outside_claude_klabauter(tmp_path):
    """Actually SPAWN the respawned child (real subprocess.Popen, real
    interpreter) from a repo that is NOT this checkout, and assert it
    survives its own top-level `coordinator_core` import instead of dying
    with ModuleNotFoundError -- the exact failure that was silently
    swallowed pre-fix (child's stderr was DEVNULL'd).
    """
    fake_repo = tmp_path / "not-claude-klabauter"
    fake_repo.mkdir()
    (fake_repo / ".git").mkdir()

    env = dict(os.environ)
    env[auto_push._ENV_NO_SLEEP] = "1"
    # Simulate a fleet repo whose own PYTHONPATH would NOT resolve
    # coordinator_core -- the respawn helper must inject its own entry, not
    # rely on the ambient environment already having it.
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.path.insert(0, %r); "
                "from coordinator_core.hooks import auto_push; "
                "auto_push.spawn_detached_push(%r, 'work/does-not-exist')"
            )
            % (str(Path(auto_push.__file__).resolve().parents[2]), str(fake_repo)),
        ],
        cwd=str(fake_repo),
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    assert result.returncode == 0, (
        f"parent process failed to spawn the child: {result.stderr}"
    )

    stderr_log = fake_repo / ".git" / "auto-push-respawn-stderr.log"
    stderr_text = _wait_for_stable_file_content(stderr_log)

    assert "--- respawn" in stderr_text, (
        f"respawn stderr log was never written: {stderr_text!r}"
    )
    assert "ModuleNotFoundError" not in stderr_text, (
        f"respawned child died at import (this is the 2026-08-01 regression): {stderr_text}"
    )
    assert "Traceback" not in stderr_text, stderr_text


def test_detach_and_run_windows_leg_child_actually_imports_when_spawned_outside_claude_klabauter(tmp_path):
    """Same real-spawn regression guard as
    test_spawn_detached_push_child_actually_imports_when_spawned_outside_claude_klabauter,
    for `_detach_and_run`'s Windows (no-fork) respawn leg specifically --
    forces the no-fork branch inside a fresh interpreter (can't monkeypatch
    os.fork away in-process without corrupting the current pytest worker).
    """
    fake_repo = tmp_path / "not-claude-klabauter-2"
    fake_repo.mkdir()
    (fake_repo / ".git").mkdir()

    env = dict(os.environ)
    env[auto_push._ENV_NO_SLEEP] = "1"
    env.pop("PYTHONPATH", None)

    claude_klabauter_root = str(Path(auto_push.__file__).resolve().parents[2])
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {claude_klabauter_root!r}); "
                "from coordinator_core.hooks import auto_push; "
                # force the Windows (no-fork) leg -- os.fork already doesn't
                # exist on real Windows, so guard the delete for POSIX only
                "hasattr(auto_push.os, 'fork') and delattr(auto_push.os, 'fork'); "
                f"auto_push._detach_and_run({str(fake_repo)!r}, 'work/does-not-exist')"
            ),
        ],
        cwd=str(fake_repo),
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    assert result.returncode == 0, (
        f"parent process failed to spawn the child: {result.stderr}"
    )

    stderr_log = fake_repo / ".git" / "auto-push-respawn-stderr.log"
    stderr_text = _wait_for_stable_file_content(stderr_log)

    assert "--- respawn" in stderr_text, (
        f"respawn stderr log was never written: {stderr_text!r}"
    )
    assert "ModuleNotFoundError" not in stderr_text, (
        f"respawned child died at import (this is the 2026-08-01 regression): {stderr_text}"
    )
    assert "Traceback" not in stderr_text, stderr_text


def test_spawn_detached_push_respawn_popen_shape(monkeypatch, tmp_path):
    captured = {}

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)

        class _FakeProc:
            pid = 999

        return _FakeProc()

    monkeypatch.setattr(auto_push.subprocess, "Popen", _fake_popen)

    repo_root = str(tmp_path)
    auto_push.spawn_detached_push(repo_root, "work/foo")

    # Respawn by resolved absolute path, never `-m` -- same contract as the
    # hook's own Windows respawn leg (see _detach_and_run's comment).
    assert "-m" not in captured["argv"]
    assert os.path.isabs(captured["argv"][1])
    assert os.path.basename(captured["argv"][1]) == "auto_push.py"
    assert os.path.exists(captured["argv"][1])
    assert captured["argv"][2:4] == ["--repo-root", repo_root]

    assert captured["env"][auto_push._ENV_SYNC] == "1"
    # Regression guard for the 2026-08-01 silent ModuleNotFoundError -- see
    # the matching assertion in
    # test_detach_and_run_windows_respawn_sets_sync_seam_and_no_infinite_loop.
    pythonpath = captured["env"]["PYTHONPATH"].split(os.pathsep)
    assert auto_push._claude_klabauter_package_root() in pythonpath

    assert captured["stdin"] == auto_push.subprocess.DEVNULL
    assert captured["stdout"] == auto_push.subprocess.DEVNULL
    # stderr goes to a durable file, not DEVNULL -- see
    # `_open_respawn_stderr_log`'s docstring and the matching assertion in
    # test_detach_and_run_windows_respawn_sets_sync_seam_and_no_infinite_loop.
    assert captured["stderr"] not in (auto_push.subprocess.DEVNULL, None)
    assert captured["stderr"] is not auto_push.subprocess.PIPE
    captured["stderr"].close()

    if hasattr(os, "fork"):
        assert captured.get("start_new_session") is True
    else:
        assert captured.get("creationflags", 0) != 0


def test_spawn_detached_push_forwards_branch_argument(monkeypatch, tmp_path):
    # Finding 1 regression: `branch` must be forwarded to the respawned
    # child's argv, not silently discarded on the primary (interpreter-found)
    # path.
    captured = {}

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return type("_FakeProc", (), {"pid": 999})()

    monkeypatch.setattr(auto_push.subprocess, "Popen", _fake_popen)

    auto_push.spawn_detached_push(str(tmp_path), "release/not-work-branch")

    argv = captured["argv"]
    assert "--branch" in argv
    assert argv[argv.index("--branch") + 1] == "release/not-work-branch"


def test_main_with_explicit_branch_flag_bypasses_resolve_and_gate(monkeypatch, tmp_path):
    # Finding 1/2 regression: main()'s --branch flag (as passed by
    # spawn_detached_push's respawn) must push the given branch
    # unconditionally -- never re-deriving via resolve_branch() nor
    # re-applying branch_gate()'s work/*-only doctrine. A non-work/* branch
    # must still result in a push attempt, not a silent gate-skip.
    def _boom_resolve(repo_root):
        raise AssertionError("main() must not call resolve_branch() when --branch is given")

    def _boom_gate(branch):
        raise AssertionError("main() must not call branch_gate() when --branch is given")

    monkeypatch.setattr(auto_push, "resolve_branch", _boom_resolve)
    monkeypatch.setattr(auto_push, "branch_gate", _boom_gate)

    calls = []
    monkeypatch.setattr(
        auto_push, "run_push_with_retry",
        lambda repo_root, branch: calls.append(branch),
    )
    monkeypatch.setenv(auto_push._ENV_SYNC, "1")

    rc = auto_push.main(
        ["--repo-root", str(tmp_path), "--branch", "release/not-work-branch"]
    )

    assert rc == 0
    assert calls == ["release/not-work-branch"]


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


# The real `_peer_commit_within_window`, captured before the autouse fixture
# below patches the module attribute -- tests that exercise the predicate
# itself restore this via `monkeypatch.setattr`.
_REAL_PEER_COMMIT_WITHIN_WINDOW = auto_push._peer_commit_within_window


@pytest.fixture(autouse=True)
def _default_retraction_not_lost(request, monkeypatch):
    """Default `_peer_commit_within_window` to False (no peer commit in the
    window) unless a test overrides it or opts out.

    SCOPE WARNING: a module-level autouse fixture applies to EVERY test in this
    file regardless of where it is defined -- "below" is about reading order,
    not effect. A test of the real predicate added ABOVE this line would still
    be stubbed and would pass vacuously, so opting out is a marker, not a
    matter of placement: mark such a test `@pytest.mark.real_peer_predicate`
    (the predicate tests further down do exactly that).

    The many pre-existing `_shared_branch_live_count`-mocked hold tests
    (coalescing, staleness takeover, loss-path, wire-path) exercise record
    coalescing/staleness/draining, not the retraction predicate -- this
    fixture pins the pre-predicate "hold fires" behavior as their default so
    they keep asserting exactly what they did before, instead of every site
    needing its own unrelated `_run_git` "log" + session_core mock. Tests
    that DO exercise the predicate restore the real function via
    `_REAL_PEER_COMMIT_WITHIN_WINDOW`.
    """
    if request.node.get_closest_marker("real_peer_predicate"):
        return
    monkeypatch.setattr(auto_push, "_peer_commit_within_window", lambda *a, **k: False)


# ---------------------------------------------------------------------------
# Durable pending-push record (AC14/AC14a/AC15, C7/C9) -- hold, drain,
# coalesce, takeover, the loss path, and a real-subprocess wire-path test.
#
# `_shared_branch_live_count` (the "predicate") is monkeypatched directly
# throughout rather than faked via `session_liveness.live_session_ids` +
# on-disk session dirs -- it is the documented seam `_hold_window` calls,
# and C10 (session/tests/test_liveness.py) owns pinning what feeds it.
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


def test_hold_window_solo_predicate_pushes_immediately_no_sleep_no_pending_record(monkeypatch, tmp_path):
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(auto_push, "_shared_branch_live_count", lambda root, branch: 1)
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
        ("rev-parse", "work/foo"): "sha1",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    push_calls = {"n": 0}

    def _fake_push_once(root, branch, windows_bash, ssh_remote):
        push_calls["n"] += 1
        return True, ""

    monkeypatch.setattr(auto_push, "push_once", _fake_push_once)

    def _boom_sleep(_seconds):
        raise AssertionError("solo predicate must never sleep")

    monkeypatch.setattr(auto_push.time, "sleep", _boom_sleep)

    auto_push.run_push_with_retry(repo_root, "work/foo")

    assert push_calls["n"] == 1
    assert auto_push._read_pending_record(repo_root) is None
    assert not (tmp_path / ".git" / auto_push._PENDING_RECORD_NAME).exists()


def test_hold_window_shared_predicate_writes_record_before_sleep_not_via_backoff(monkeypatch, tmp_path):
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(auto_push, "_shared_branch_live_count", lambda root, branch: 2)
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
        ("rev-parse", "work/foo"): "abc123",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    push_calls = {"n": 0}

    def _fake_push_once(root, branch, windows_bash, ssh_remote):
        push_calls["n"] += 1
        return True, ""

    monkeypatch.setattr(auto_push, "push_once", _fake_push_once)

    seen_record_at_sleep = {}
    backoff_calls = {"n": 0}

    def _spy_backoff(err_class, attempt):
        backoff_calls["n"] += 1
        return 0.0

    def _spy_sleep(seconds):
        # The record must already be on disk BEFORE this call -- the sleep
        # is the hold's own wait, not a backoff sleep (which never carries
        # the full hold window as its argument).
        seen_record_at_sleep["record"] = auto_push._read_pending_record(repo_root)
        seen_record_at_sleep["seconds"] = seconds

    monkeypatch.setattr(auto_push, "_backoff_seconds", _spy_backoff)
    monkeypatch.setattr(auto_push.time, "sleep", _spy_sleep)

    auto_push.run_push_with_retry(repo_root, "work/foo")

    assert backoff_calls["n"] == 0, "hold sleep must not route through _backoff_seconds"
    record = seen_record_at_sleep.get("record")
    assert record is not None, "pending record was not written before the sleep began"
    assert record["branch"] == "work/foo"
    assert record["sha"] == "abc123"
    assert isinstance(record["hold_until"], (int, float))
    assert seen_record_at_sleep["seconds"] == auto_push._HOLD_WINDOW_SECONDS
    # After a successful push the record is cleared -- removed only once the
    # push actually landed.
    assert auto_push._read_pending_record(repo_root) is None
    assert push_calls["n"] == 1


# ---------------------------------------------------------------------------
# `_peer_commit_within_window` -- the narrowing predicate. These restore the
# real function (the fixture above defaults it to False for every other
# test in this section) and drive it through `_run_git`'s "log" branch.
# ---------------------------------------------------------------------------

@pytest.mark.real_peer_predicate
def test_busy_shared_branch_with_recent_foreign_commit_skips_hold_pushes_immediately(monkeypatch, tmp_path):
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(auto_push, "_peer_commit_within_window", _REAL_PEER_COMMIT_WITHIN_WINDOW)
    monkeypatch.setattr(auto_push, "_shared_branch_live_count", lambda root, branch: 2)
    monkeypatch.setattr(auto_push.session_core, "resolve_session_id", lambda cwd=None: "own-sid")

    def _fake_run_git(root, args):
        if args[0] == "log":
            # A peer session's commit landed inside the window.
            return "deadbeef\x1fother-sid"
        return {
            ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
            ("rev-parse", "work/foo"): "abc123",
        }.get(tuple(args))

    monkeypatch.setattr(auto_push, "_run_git", _fake_run_git)
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    push_calls = {"n": 0}

    def _fake_push_once(root, branch, windows_bash, ssh_remote):
        push_calls["n"] += 1
        return True, ""

    monkeypatch.setattr(auto_push, "push_once", _fake_push_once)

    def _boom_sleep(_seconds):
        raise AssertionError("retraction already lost -- must not sleep")

    monkeypatch.setattr(auto_push.time, "sleep", _boom_sleep)

    auto_push.run_push_with_retry(repo_root, "work/foo")

    assert push_calls["n"] == 1
    assert auto_push._read_pending_record(repo_root) is None


@pytest.mark.real_peer_predicate
def test_shared_branch_no_recent_foreign_commit_hold_still_fires(monkeypatch, tmp_path):
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(auto_push, "_peer_commit_within_window", _REAL_PEER_COMMIT_WITHIN_WINDOW)
    monkeypatch.setattr(auto_push, "_shared_branch_live_count", lambda root, branch: 2)
    monkeypatch.setattr(auto_push.session_core, "resolve_session_id", lambda cwd=None: "own-sid")

    def _fake_run_git(root, args):
        if args[0] == "log":
            # Only this same session's own commits are in the window.
            return "abc123\x1fown-sid"
        return {
            ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
            ("rev-parse", "work/foo"): "abc123",
        }.get(tuple(args))

    monkeypatch.setattr(auto_push, "_run_git", _fake_run_git)
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    push_calls = {"n": 0}

    def _fake_push_once(root, branch, windows_bash, ssh_remote):
        push_calls["n"] += 1
        return True, ""

    monkeypatch.setattr(auto_push, "push_once", _fake_push_once)

    sleep_calls = []
    monkeypatch.setattr(auto_push.time, "sleep", lambda s: sleep_calls.append(s))

    auto_push.run_push_with_retry(repo_root, "work/foo")

    assert sleep_calls == [auto_push._HOLD_WINDOW_SECONDS], "hold must still fire when no foreign commit is recent"
    assert push_calls["n"] == 1


@pytest.mark.real_peer_predicate
def test_retraction_predicate_evaluation_failure_pushes_rather_than_holds(monkeypatch, tmp_path):
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(auto_push, "_peer_commit_within_window", _REAL_PEER_COMMIT_WITHIN_WINDOW)
    monkeypatch.setattr(auto_push, "_shared_branch_live_count", lambda root, branch: 2)
    # Own session IS resolvable, but the bounded git log call itself fails
    # (mirrors a git spawn/timeout failure -- `_run_git` returns None).
    monkeypatch.setattr(auto_push.session_core, "resolve_session_id", lambda cwd=None: "own-sid")

    def _fake_run_git(root, args):
        if args[0] == "log":
            return None
        return {
            ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
            ("rev-parse", "work/foo"): "abc123",
        }.get(tuple(args))

    monkeypatch.setattr(auto_push, "_run_git", _fake_run_git)
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    push_calls = {"n": 0}

    def _fake_push_once(root, branch, windows_bash, ssh_remote):
        push_calls["n"] += 1
        return True, ""

    monkeypatch.setattr(auto_push, "push_once", _fake_push_once)

    def _boom_sleep(_seconds):
        raise AssertionError("unresolvable predicate must fail toward pushing, not holding")

    monkeypatch.setattr(auto_push.time, "sleep", _boom_sleep)

    auto_push.run_push_with_retry(repo_root, "work/foo")

    assert push_calls["n"] == 1
    assert auto_push._read_pending_record(repo_root) is None


@pytest.mark.real_peer_predicate
def test_retraction_predicate_unresolvable_own_session_id_pushes_rather_than_holds(monkeypatch, tmp_path):
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(auto_push, "_peer_commit_within_window", _REAL_PEER_COMMIT_WITHIN_WINDOW)
    monkeypatch.setattr(auto_push, "_shared_branch_live_count", lambda root, branch: 2)
    # Own session id unresolvable (e.g. no env var set) -- the predicate
    # must not even need the git log call to fail toward pushing.
    monkeypatch.setattr(auto_push.session_core, "resolve_session_id", lambda cwd=None: "")
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
        ("rev-parse", "work/foo"): "abc123",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    push_calls = {"n": 0}

    def _fake_push_once(root, branch, windows_bash, ssh_remote):
        push_calls["n"] += 1
        return True, ""

    monkeypatch.setattr(auto_push, "push_once", _fake_push_once)

    def _boom_sleep(_seconds):
        raise AssertionError("unresolvable own session id must fail toward pushing, not holding")

    monkeypatch.setattr(auto_push.time, "sleep", _boom_sleep)

    auto_push.run_push_with_retry(repo_root, "work/foo")

    assert push_calls["n"] == 1
    assert auto_push._read_pending_record(repo_root) is None


def test_no_sleep_env_skips_backoff_but_not_the_hold_decision(monkeypatch, tmp_path):
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv(auto_push._ENV_NO_SLEEP, "1")
    monkeypatch.setattr(auto_push, "_shared_branch_live_count", lambda root, branch: 2)
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("remote", "get-url", "origin"): "https://github.com/org/repo.git",
        ("rev-parse", "work/foo"): "shaXYZ",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)

    def _boom_sleep(_seconds):
        raise AssertionError("COORDINATOR_AUTO_PUSH_NO_SLEEP must skip the sleep call")

    monkeypatch.setattr(auto_push.time, "sleep", _boom_sleep)

    push_calls = {"n": 0}
    written_record_seen = {"value": None}

    def _fake_push_once(root, branch, windows_bash, ssh_remote):
        push_calls["n"] += 1
        # The hold DECISION still ran (a record was written) even though the
        # sleep call itself was skipped -- captured here, right before the
        # push, since a successful push clears the record afterward.
        written_record_seen["value"] = auto_push._read_pending_record(repo_root)
        return True, ""

    monkeypatch.setattr(auto_push, "push_once", _fake_push_once)

    auto_push.run_push_with_retry(repo_root, "work/foo")

    assert push_calls["n"] == 1
    record = written_record_seen["value"]
    assert record is not None, "NO_SLEEP must not disable the hold decision itself"
    assert record["branch"] == "work/foo"
    assert record["sha"] == "shaXYZ"


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


def test_run_push_with_retry_skips_redundant_hold_when_drain_already_published_this_branch(
    monkeypatch, tmp_path
):
    # Review: coordinator:review-code, Finding 2, 2026-08-19 --
    # `wsc_tail._deferred_publisher_backstop` writes an already-due pending
    # record for THIS branch immediately before step 5e spawns this exact
    # call (`_skip_hold=False`). The head-of-function `drain_pending_push`
    # reads that record back and pushes it via a nested `_skip_hold=True`
    # call, clearing it. Without the fix, `_hold_window` then finds no
    # record, mistakes this outer call for a fresh holder on a shared
    # branch, and sleeps out a redundant `_HOLD_WINDOW_SECONDS`. The fix
    # must return immediately instead -- no second push, no sleep.
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    auto_push._write_pending_record(
        repo_root, "work/foo", None, time.time() - 1, 4242
    )
    monkeypatch.setenv(auto_push._ENV_NO_SLEEP, "1")
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("rev-parse", "work/foo"): "sha1",
        ("rev-parse", "--verify", "refs/heads/work/foo"): "sha1",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)
    # Shared branch -- the case where the pre-fix bug actually bites: a
    # solo branch's `_hold_window` returns True immediately regardless.
    monkeypatch.setattr(auto_push, "_shared_branch_live_count", lambda root, branch: 2)

    push_calls = []
    monkeypatch.setattr(
        auto_push, "push_once",
        lambda root, branch, w, s: (push_calls.append(branch), (True, ""))[1],
    )
    sleep_calls = {"n": 0}
    monkeypatch.setattr(
        auto_push.time, "sleep",
        lambda s: sleep_calls.__setitem__("n", sleep_calls["n"] + 1),
    )

    auto_push.run_push_with_retry(repo_root, "work/foo")

    # Exactly ONE push -- the nested drain push -- not a second one from a
    # phantom fresh hold, and no coalescing sleep.
    assert push_calls == ["work/foo"]
    assert sleep_calls["n"] == 0, "must not re-enter _hold_window as a fresh holder"
    assert auto_push._read_pending_record(repo_root) is None


def test_run_push_with_retry_still_holds_when_drain_left_a_live_record_for_another_branch(
    monkeypatch, tmp_path
):
    # Guard against an over-broad fix: a due/stale record belonging to a
    # DIFFERENT branch (opportunistically drained for free) must not
    # suppress this call's own hold/push decision for its own branch.
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    auto_push._write_pending_record(
        repo_root, "work/other", "othersha", time.time() - 1, 4242
    )
    monkeypatch.setenv(auto_push._ENV_NO_SLEEP, "1")
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("rev-parse", "work/other"): "othersha",
        ("rev-parse", "--verify", "refs/heads/work/other"): "othersha",
        ("rev-parse", "work/foo"): "sha1",
    }.get(tuple(args)))
    monkeypatch.setattr(auto_push, "is_windows_bash", lambda: False)
    monkeypatch.setattr(auto_push, "_shared_branch_live_count", lambda root, branch: 1)

    push_calls = []
    monkeypatch.setattr(
        auto_push, "push_once",
        lambda root, branch, w, s: (push_calls.append(branch), (True, ""))[1],
    )

    auto_push.run_push_with_retry(repo_root, "work/foo")

    # The other branch's leftover was drained for free, AND this call's own
    # branch was still pushed -- unaffected by the fix.
    assert push_calls == ["work/other", "work/foo"]


def test_hold_window_coalesces_two_commits_into_one_pusher(monkeypatch, tmp_path):
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(auto_push, "_shared_branch_live_count", lambda root, branch: 2)
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("rev-parse", "work/foo"): "commit1sha",
    }.get(tuple(args)))

    sleep_calls = {"n": 0}
    monkeypatch.setattr(auto_push.time, "sleep", lambda s: sleep_calls.__setitem__("n", sleep_calls["n"] + 1))

    # First commit's hold-child becomes the holder.
    assert auto_push._hold_window(repo_root, "work/foo") is True
    first_record = auto_push._read_pending_record(repo_root)
    assert first_record is not None
    assert sleep_calls["n"] == 1

    # A second commit lands inside the same window, before the incumbent
    # has drained/cleared its record -- it must NOT start a second sleeper,
    # it must delegate to the incumbent (which will publish the branch tip
    # at wake time, a superset covering both commits).
    assert auto_push._hold_window(repo_root, "work/foo") is False
    assert sleep_calls["n"] == 1, "coalescing failed -- a second pusher slept"
    # The record is untouched by the declined second call -- still the
    # first holder's.
    assert auto_push._read_pending_record(repo_root) == first_record


def test_hold_window_takes_over_stale_record_with_dead_holder_no_duplicate(monkeypatch, tmp_path):
    repo_root = str(tmp_path)
    (tmp_path / ".git").mkdir()
    dead_pid = _dead_pid_for_record()
    auto_push._write_pending_record(
        repo_root, "work/foo", "stalesha", time.time() + 9999, dead_pid
    )
    monkeypatch.setattr(auto_push, "_shared_branch_live_count", lambda root, branch: 2)
    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: {
        ("rev-parse", "work/foo"): "freshsha",
    }.get(tuple(args)))

    sleep_calls = {"n": 0}
    monkeypatch.setattr(auto_push.time, "sleep", lambda s: sleep_calls.__setitem__("n", sleep_calls["n"] + 1))

    result = auto_push._hold_window(repo_root, "work/foo")

    assert result is True, "a stale record (dead holder) must be taken over, not trusted"
    assert sleep_calls["n"] == 1, "the takeover call must become the new holder and hold its own window"

    record = auto_push._read_pending_record(repo_root)
    assert record["holder_pid"] == os.getpid()
    assert record["sha"] == "freshsha"
    # Exactly one record on disk -- the takeover REPLACED the stale one, it
    # did not duplicate it alongside.
    pending_files = list((tmp_path / ".git").glob(f"{auto_push._PENDING_RECORD_NAME}*"))
    assert pending_files == [tmp_path / ".git" / auto_push._PENDING_RECORD_NAME]


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


def test_branch_diverged_no_spawn_ahead_only_is_false(tmp_path):
    work = _init_divergence_repo(tmp_path, "work/c4-ahead")
    (work / "f.txt").write_text("two\n", encoding="utf-8")
    _real_git(["commit", "-q", "-am", "c2 (local only)"], work)

    assert auto_push._branch_diverged_no_spawn(str(work), "work/c4-ahead") is False


def test_branch_diverged_no_spawn_ahead_and_behind_is_true(tmp_path):
    bare = tmp_path / "bare.git"
    work = _init_divergence_repo(tmp_path, "work/c4-diverged")

    # A peer clones the bare remote and pushes a commit `work`'s tracking ref
    # doesn't know about yet.
    peer = tmp_path / "peer"
    _real_git(["clone", "-q", str(bare), str(peer)], tmp_path)
    _real_git(["config", "user.email", "peer@example.com"], peer)
    _real_git(["config", "user.name", "peer"], peer)
    _real_git(["config", "commit.gpgsign", "false"], peer)
    _real_git(["checkout", "-q", "work/c4-diverged"], peer)
    (peer / "f.txt").write_text("peer\n", encoding="utf-8")
    _real_git(["commit", "-q", "-am", "peer commit"], peer)
    _real_git(["push", "-q", "origin", "work/c4-diverged"], peer)

    # `work` learns about the peer's tip (updates its remote-tracking ref)
    # but does NOT merge it -- then commits its own divergent history on top
    # of the OLD base.
    _real_git(["fetch", "-q", "origin"], work)
    (work / "f.txt").write_text("mine\n", encoding="utf-8")
    _real_git(["commit", "-q", "-am", "our own divergent commit"], work)

    assert auto_push._branch_diverged_no_spawn(str(work), "work/c4-diverged") is True


def test_branch_diverged_no_spawn_no_remote_tracking_ref_is_false(tmp_path):
    work = tmp_path / "work"
    _real_git(["init", "-q", str(work)], tmp_path)
    _real_git(["config", "user.email", "auto-push-c4@example.com"], work)
    _real_git(["config", "user.name", "auto-push-c4"], work)
    _real_git(["config", "commit.gpgsign", "false"], work)
    _real_git(["checkout", "-q", "-b", "work/c4-new"], work)
    (work / "f.txt").write_text("one\n", encoding="utf-8")
    _real_git(["add", "f.txt"], work)
    _real_git(["commit", "-q", "-m", "c1"], work)

    assert auto_push._branch_diverged_no_spawn(str(work), "work/c4-new") is False


@pytest.mark.real_peer_predicate
def test_hold_window_diverged_branch_defers_no_push_no_sleep(monkeypatch, tmp_path):
    """AC12/AC13/AC14: on a diverged branch, `_hold_window` writes/updates
    the pending record and returns False -- no sleep, no push, no resident
    child -- and the divergence gate must be reached (and win) even though
    `_shared_branch_live_count` is shared and `_peer_commit_within_window`
    would otherwise bypass the hold."""
    branch = "work/c4-diverged-hold"
    bare = tmp_path / "bare.git"
    work = _init_divergence_repo(tmp_path, branch)
    peer = tmp_path / "peer"
    _real_git(["clone", "-q", str(bare), str(peer)], tmp_path)
    _real_git(["config", "user.email", "peer@example.com"], peer)
    _real_git(["config", "user.name", "peer"], peer)
    _real_git(["config", "commit.gpgsign", "false"], peer)
    _real_git(["checkout", "-q", branch], peer)
    (peer / "f.txt").write_text("peer\n", encoding="utf-8")
    _real_git(["commit", "-q", "-am", "peer commit"], peer)
    _real_git(["push", "-q", "origin", branch], peer)
    _real_git(["fetch", "-q", "origin"], work)
    (work / "f.txt").write_text("mine\n", encoding="utf-8")
    _real_git(["commit", "-q", "-am", "our own divergent commit"], work)

    repo_root = str(work)
    monkeypatch.setattr(auto_push, "_shared_branch_live_count", lambda root, b: 2)

    sleep_calls = {"n": 0}
    monkeypatch.setattr(auto_push.time, "sleep", lambda s: sleep_calls.__setitem__("n", sleep_calls["n"] + 1))

    result = auto_push._hold_window(repo_root, branch)

    assert result is False, "diverged branch must defer, not push"
    assert sleep_calls["n"] == 0, "diverged path must never sleep"
    record = auto_push._read_pending_record(repo_root)
    assert record is not None, "diverged commit must end with a live pending record naming it"
    assert record["branch"] == branch


def test_hold_window_diverged_solo_branch_defers_instead_of_pushing_into_the_wall(monkeypatch, tmp_path):
    """The divergence gate must be reachable on a SOLO branch.

    Regression, 2026-08-27: the gate sat below `_shared_branch_live_count`'s
    `<=1 -> return True` short-circuit, so a lone session on a diverged
    branch never reached it and re-issued a guaranteed-non-fast-forward push
    on every commit. Observed cross-repo as 22 identical NFF failures over an
    hour on DoE-claude `work/machine-a/2026-08-22`. A push whose rejection is
    already determined carries no crash insurance, so deferring it trades
    nothing away.
    """
    branch = "work/c4-diverged-solo"
    bare = tmp_path / "bare.git"
    work = _init_divergence_repo(tmp_path, branch)
    peer = tmp_path / "peer"
    _real_git(["clone", "-q", str(bare), str(peer)], tmp_path)
    _real_git(["config", "user.email", "peer@example.com"], peer)
    _real_git(["config", "user.name", "peer"], peer)
    _real_git(["config", "commit.gpgsign", "false"], peer)
    _real_git(["checkout", "-q", branch], peer)
    (peer / "f.txt").write_text("peer\n", encoding="utf-8")
    _real_git(["commit", "-q", "-am", "peer commit"], peer)
    _real_git(["push", "-q", "origin", branch], peer)
    _real_git(["fetch", "-q", "origin"], work)
    (work / "f.txt").write_text("mine\n", encoding="utf-8")
    _real_git(["commit", "-q", "-am", "our own divergent commit"], work)

    repo_root = str(work)
    # Solo -- and unresolvable -- are the two shapes that short-circuited.
    for live_count in (1, None):
        auto_push._remove_pending_record(repo_root)
        monkeypatch.setattr(auto_push, "_shared_branch_live_count", lambda root, b, _c=live_count: _c)
        monkeypatch.setattr(auto_push.time, "sleep", lambda s: pytest.fail("diverged path must never sleep"))

        assert auto_push._hold_window(repo_root, branch) is False, (
            f"solo diverged branch (live_count={live_count!r}) must defer, not push"
        )
        record = auto_push._read_pending_record(repo_root)
        assert record is not None and record["branch"] == branch


def test_hold_window_solo_undiverged_branch_still_pushes_immediately(monkeypatch, tmp_path):
    """The hoist must not hold back the ordinary solo case: ahead-only (never
    diverged) still pushes with no record and no sleep."""
    branch = "work/c4-solo-ahead"
    work = _init_divergence_repo(tmp_path, branch)
    _real_git(["fetch", "-q", "origin"], work)
    (work / "f.txt").write_text("mine\n", encoding="utf-8")
    _real_git(["commit", "-q", "-am", "ahead-only commit"], work)

    repo_root = str(work)
    monkeypatch.setattr(auto_push, "_shared_branch_live_count", lambda root, b: 1)
    monkeypatch.setattr(auto_push.time, "sleep", lambda s: pytest.fail("solo predicate must never sleep"))

    assert auto_push._hold_window(repo_root, branch) is True
    assert auto_push._read_pending_record(repo_root) is None


@pytest.mark.real_peer_predicate
def test_hold_window_diverged_branch_n_commits_yield_one_deferred_record(monkeypatch, tmp_path):
    """N commits on a diverged branch yield one deferred publish (one
    updated record), not N attempted pushes."""
    branch = "work/c4-diverged-n"
    bare = tmp_path / "bare.git"
    work = _init_divergence_repo(tmp_path, branch)
    peer = tmp_path / "peer"
    _real_git(["clone", "-q", str(bare), str(peer)], tmp_path)
    _real_git(["config", "user.email", "peer@example.com"], peer)
    _real_git(["config", "user.name", "peer"], peer)
    _real_git(["config", "commit.gpgsign", "false"], peer)
    _real_git(["checkout", "-q", branch], peer)
    (peer / "f.txt").write_text("peer\n", encoding="utf-8")
    _real_git(["commit", "-q", "-am", "peer commit"], peer)
    _real_git(["push", "-q", "origin", branch], peer)
    _real_git(["fetch", "-q", "origin"], work)

    repo_root = str(work)
    monkeypatch.setattr(auto_push, "_shared_branch_live_count", lambda root, b: 2)
    push_calls = {"n": 0}
    monkeypatch.setattr(auto_push, "push_once", lambda *a, **k: push_calls.__setitem__("n", push_calls["n"] + 1) or (True, ""))
    monkeypatch.setattr(auto_push.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("must never sleep")))

    for i in range(3):
        (work / "f.txt").write_text(f"mine {i}\n", encoding="utf-8")
        _real_git(["commit", "-q", "-am", f"our divergent commit {i}"], work)
        assert auto_push._hold_window(repo_root, branch) is False

    assert push_calls["n"] == 0, "no push must be issued while diverged"
    record = auto_push._read_pending_record(repo_root)
    assert record is not None


@pytest.mark.real_peer_predicate
def test_hold_window_diverged_record_survives_session_death(monkeypatch, tmp_path):
    """The record `_hold_window` writes on the diverged path must still be
    takeover-able by `drain_pending_push`/`boot_sweep` if this session dies
    -- i.e. it carries the same shape (branch/sha/hold_until/holder_pid) a
    dead-holder takeover already reads (see
    `test_hold_window_takes_over_stale_record_with_dead_holder_no_duplicate`)."""
    branch = "work/c4-diverged-crash"
    bare = tmp_path / "bare.git"
    work = _init_divergence_repo(tmp_path, branch)
    peer = tmp_path / "peer"
    _real_git(["clone", "-q", str(bare), str(peer)], tmp_path)
    _real_git(["config", "user.email", "peer@example.com"], peer)
    _real_git(["config", "user.name", "peer"], peer)
    _real_git(["config", "commit.gpgsign", "false"], peer)
    _real_git(["checkout", "-q", branch], peer)
    (peer / "f.txt").write_text("peer\n", encoding="utf-8")
    _real_git(["commit", "-q", "-am", "peer commit"], peer)
    _real_git(["push", "-q", "origin", branch], peer)
    _real_git(["fetch", "-q", "origin"], work)
    (work / "f.txt").write_text("mine\n", encoding="utf-8")
    _real_git(["commit", "-q", "-am", "our own divergent commit"], work)

    repo_root = str(work)
    monkeypatch.setattr(auto_push, "_shared_branch_live_count", lambda root, b: 2)
    monkeypatch.setattr(auto_push.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("must never sleep")))

    assert auto_push._hold_window(repo_root, branch) is False

    record = auto_push._read_pending_record(repo_root)
    assert record is not None
    assert record["branch"] == branch
    assert isinstance(record["sha"], str) and record["sha"]
    assert isinstance(record["hold_until"], (int, float))
    assert record["holder_pid"] == os.getpid()
    # A later takeover call (simulating this session's death) must find a
    # non-stale-yet record here -- staleness is `_record_is_stale`'s own
    # concern (dead pid or hold_until + grace elapsed), already covered by
    # the pre-existing takeover test; this assertion pins that the record
    # this path writes is not ALREADY stale the instant it lands.
    assert auto_push._record_is_stale(record, time.time()) is False


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


def test_wire_path_respawn_actually_pushes_to_a_real_remote(tmp_path):
    repo_root, branch, bare = _init_real_repo_with_local_remote(tmp_path)
    local_sha = subprocess.run(
        ["git", "-C", repo_root, "rev-parse", branch],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    claude_klabauter_root = str(Path(auto_push.__file__).resolve().parents[2])

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {claude_klabauter_root!r}); "
                "from coordinator_core.hooks import auto_push; "
                f"auto_push.spawn_detached_push({repo_root!r}, {branch!r})"
            ),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )

    assert result.returncode == 0, (
        f"parent process failed to spawn the wire-path child: {result.stderr}"
    )

    def _remote_tip() -> str:
        out = subprocess.run(
            ["git", "-C", bare, "rev-parse", branch],
            capture_output=True, text=True,
        )
        return out.stdout.strip() if out.returncode == 0 else ""

    # Poll interval intentionally coarser than the deadline resolution: this
    # loop's own `git rev-parse` calls are additional real spawns on top of
    # the fixture's, and a 0.1s interval against a 15s deadline bounds the
    # failure-path worst case at ~150 extra spawns for one test. 0.5s bounds
    # the same worst case at 30 while leaving the happy path (resolves in a
    # fraction of a second) unaffected.
    deadline = time.time() + 15
    remote_sha = ""
    while time.time() < deadline:
        remote_sha = _remote_tip()
        if remote_sha:
            break
        time.sleep(0.5)

    assert remote_sha == local_sha, (
        "the respawned child's real push attempt never reached the "
        f"remote (remote tip {remote_sha!r} != local {local_sha!r})"
    )


# ---------------------------------------------------------------------------
# Durable oracle: interpreter starts across all three arms of the plan's
# quoted (composed-from-control-flow, not previously OBSERVED)
# three-interpreter-start figure for the deferred ceremony path.
#
# state/dispatch-briefs/2026-08-19-windows-commit-hook-starts-python-once/C1.md
# Spec backlink: docs/plans/2026-08-19-windows-commit-hook-starts-python-once.md
#
# THE ORACLE: count `--- respawn ` headers in
# `<git-common-dir>/auto-push-respawn-stderr.log`. `_open_respawn_stderr_log`
# appends one in the PARENT, before every `Popen` -- this counts STARTS, not
# concurrent existence, so it is race-free, psutil-free, and window-
# independent (immune to the "fast child exits inside a poll interval reads
# as absent" false negative a sampled process census would produce -- see
# this module's own header comment / the C1 brief's NEGATIVE SPEC).
#
#     interpreter starts = 1 (the hook process git execs) + respawn-header count
#
# Arms 1/2 are proxies (the suppression env var toggled directly on a
# throwaway repo carrying a REAL generated post-commit hook shim mirror, per
# the brief's Closed-brief wording -- "produced by toggling the suppression
# env var directly" refers to how the *publisher* env is set, not to the hook
# shim, which is real in every arm here). Arm 3 -- a REAL `ceremony.wsc_tail`
# invocation at its real default -- died with that op (PM kill ruling
# 2026-08-23) and is not replaced: no surviving caller drives this leg.
# ---------------------------------------------------------------------------

#: `coordinator-auto-push.py`'s own DoE-side trampoline over
#: `coordinator_core.hooks.auto_push.main()` -- see that file's module
#: docstring: "git -> post-commit hook shim (sh) -> exec ... coordinator-
#: auto-push -> in-process import + call of auto_push.main()". Used here
#: (rather than execing `auto_push.py` directly) so the installed hook mirror
#: below reproduces the REAL invocation chain, not a shortcut around it.
_COORDINATOR_AUTO_PUSH_TRAMPOLINE = (
    Path(auto_push.__file__).resolve().parents[2] / "coordinator" / "bin" / "coordinator-auto-push.py"
)


def _respawn_header_count(common_dir: Path) -> int:
    """The oracle read: settle the respawn-stderr log (detached children
    write asynchronously -- see `_wait_for_stable_file_content`'s docstring
    for why a first-existence read races the child's own writes), then count
    `--- respawn ` headers. Absent log -- no respawn ever happened -- reads
    as 0, not an error."""
    log_path = common_dir / "auto-push-respawn-stderr.log"
    if not log_path.exists():
        return 0
    text = _wait_for_stable_file_content(log_path)
    return text.count("--- respawn ")


def _install_mirror_post_commit_hook(common_dir: Path) -> None:
    """Install a post-commit hook shim mirroring the REAL generated one
    (`coordinator/bin/lib/git_hook_install.py::_shim_body`): `#!/bin/sh` ->
    exec python -> `coordinator-auto-push.py` trampoline -> `auto_push.main()`.

    Simplified to ONE deterministic rung (no fallback probe chain / no
    `python_probe_sh` WindowsApps-stub skip) -- this test controls its own
    interpreter and environment outright, and is pinning interpreter-START
    COUNT, not the shim's own resolution ladder (that is
    `coordinator/tests/test_git_hook_install.py`'s remit, not this chunk's).
    `common_dir` (not repo_root) so this installs correctly for both a plain
    clone (`common_dir == repo_root/.git`) and any non-plain topology.
    """
    hooks_dir = common_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "post-commit"
    python_exe = sys.executable.replace("\\", "/")
    script = _COORDINATOR_AUTO_PUSH_TRAMPOLINE.as_posix()
    hook_path.write_text(
        "#!/bin/sh\n"
        f'exec "{python_exe}" "{script}"\n',
        encoding="utf-8",
    )
    hook_path.chmod(0o755)


def _init_hook_mirror_repo(tmp_path: Path) -> Path:
    """A throwaway repo, checked out to a `work/*` branch (`branch_gate` must
    allow the push or the hook never reaches `_detach_and_run` at all -- the
    default `main` branch_gate-skips before any detach/respawn happens), with
    the real generated post-commit hook shim installed for real. Returns
    repo_root."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo_root), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "oracle@claude-klabauter.test"], cwd=str(repo_root), check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Oracle Test"], cwd=str(repo_root), check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=str(repo_root), check=True,
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "work/test/oracle"], cwd=str(repo_root), check=True,
    )
    _install_mirror_post_commit_hook((repo_root / ".git").resolve())
    return repo_root


def _commit_env(claude_klabauter_root: str, *, suppress: bool) -> dict:
    """Env for the `git commit` subprocess that fires the real hook. Rung 1
    of `cc_invoke._resolve_claude_klabauter_root()` (env var already set -> fast-path
    return) is used to point the trampoline at THIS checkout without touching
    the real settings-home pointer file or machine-local registry."""
    env = dict(os.environ)
    env.pop(auto_push._ENV_SYNC, None)
    env.pop(auto_push._ENV_SUPPRESS_FOR_SYNC_PUSH, None)
    env["COORDINATOR_ENGINE_ROOT"] = claude_klabauter_root
    env[auto_push._ENV_NO_SLEEP] = "1"
    if suppress:
        env[auto_push._ENV_SUPPRESS_FOR_SYNC_PUSH] = "1"
    return env


def test_oracle_arm1_suppressed_sync_publisher_one_interpreter_start(tmp_path):
    """Arm 1: a synchronous publisher's default --
    `_sole_publisher_env` (git_native.py) sets
    `COORDINATOR_AUTO_PUSH_SUPPRESS_FOR_SYNC_PUSH=1` because the caller
    publishes synchronously, in the SAME invocation. `auto_push.main()`
    stands down before resolving a branch or detaching at all (see the
    `_ENV_SUPPRESS_FOR_SYNC_PUSH` check, first thing inside its try:), so the
    ONLY interpreter start on this arm is the hook process itself:
    `interpreter starts = 1 + respawn-header count = 1 + 0 = 1`.
    """
    repo_root = _init_hook_mirror_repo(tmp_path)
    claude_klabauter_root = str(Path(auto_push.__file__).resolve().parents[2])
    env = _commit_env(claude_klabauter_root, suppress=True)

    result = subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "arm1: suppressed"],
        cwd=str(repo_root), env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr

    common_dir = (repo_root / ".git").resolve()
    header_count = _respawn_header_count(common_dir)
    assert header_count == 0, "suppressed arm must never detach/respawn"


def test_oracle_arm2_unsuppressed_deferred_default_two_on_nt_one_on_posix(tmp_path):
    """Arm 2: a deferred publisher's default (suppression OFF) -- the hook
    runs its own async self-detach unconditionally
    (`async_mode = not bool(os.environ.get(_ENV_SYNC))`, unset here -> True).
    Platform-gated per `_detach_and_run`: nt has no `os.fork()`, so the
    Windows leg writes ONE respawn header and Popens a fresh interpreter (2
    starts total); POSIX forks IN-PROCESS -- no new interpreter, no respawn
    header, 1 start total. This is AC8's "POSIX still forks -- no new
    interpreter on the fork leg" assertion, exercised for real (not
    monkeypatched) via `hasattr(os, "fork")`.
    """
    repo_root = _init_hook_mirror_repo(tmp_path)
    claude_klabauter_root = str(Path(auto_push.__file__).resolve().parents[2])
    env = _commit_env(claude_klabauter_root, suppress=False)

    result = subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "arm2: unsuppressed"],
        cwd=str(repo_root), env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr

    common_dir = (repo_root / ".git").resolve()
    header_count = _respawn_header_count(common_dir)
    interpreter_starts = 1 + header_count

    if hasattr(os, "fork"):
        assert header_count == 0, "POSIX must fork in-process, never respawn a new interpreter"
        assert interpreter_starts == 1
    else:
        assert header_count == 1
        assert interpreter_starts == 2


def test_release_claims_for_head_releases_the_landed_commits_paths(monkeypatch):
    seen = {}

    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: "a.py\nb.py\n")

    from coordinator_core.session import core as session_core
    from coordinator_core.session import scope as session_scope

    monkeypatch.setattr(session_core, "resolve_session_id", lambda cwd=None: "sid-1")
    monkeypatch.setattr(
        session_scope,
        "release_committed_claims",
        lambda sid, paths, cwd=None: seen.update(sid=sid, paths=paths, cwd=cwd),
    )

    auto_push._release_claims_for_head("/repo")

    assert seen == {"sid": "sid-1", "paths": ["a.py", "b.py"], "cwd": "/repo"}


def test_release_claims_for_head_never_raises(monkeypatch):
    # Fail-open is the whole contract of this path: a stale claim is a far
    # smaller harm than a blocked commit.
    def _boom(*a, **k):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(auto_push, "_run_git", _boom)

    auto_push._release_claims_for_head("/repo")  # must not raise


def test_release_claims_for_head_is_a_no_op_when_the_commit_named_no_paths(monkeypatch):
    called = []

    monkeypatch.setattr(auto_push, "_run_git", lambda root, args: "")

    from coordinator_core.session import scope as session_scope

    monkeypatch.setattr(
        session_scope, "release_committed_claims", lambda *a, **k: called.append(a)
    )

    auto_push._release_claims_for_head("/repo")

    assert called == []


def test_main_releases_claims_on_a_plain_post_commit_invocation(monkeypatch, tmp_path):
    released = []

    monkeypatch.delenv(auto_push._ENV_SUPPRESS_FOR_SYNC_PUSH, raising=False)
    monkeypatch.setattr(auto_push, "_resolve_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(auto_push, "resolve_branch", lambda root: "work/x")
    monkeypatch.setattr(auto_push, "branch_gate", lambda branch: (False, None))
    monkeypatch.setattr(
        auto_push, "_release_claims_for_head", lambda root: released.append(root)
    )

    assert auto_push.main([]) == 0
    # Released even though the branch gate declined the push -- the ledger is
    # about what was COMMITTED, not about what was pushed.
    assert released == [str(tmp_path)]


def test_main_does_not_release_on_the_detached_respawn(monkeypatch, tmp_path):
    # spawn_detached_push respawns main() with an explicit --branch; releasing
    # there would repeat the work and add a spawn in the detached child.
    released = []

    monkeypatch.delenv(auto_push._ENV_SUPPRESS_FOR_SYNC_PUSH, raising=False)
    monkeypatch.setattr(auto_push, "_resolve_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(auto_push, "run_push_with_retry", lambda root, branch: None)
    monkeypatch.setattr(
        auto_push, "_release_claims_for_head", lambda root: released.append(root)
    )

    assert auto_push.main(["--branch", "work/x", "--no-async"]) == 0
    assert released == []


def test_main_does_not_release_when_the_coordinator_path_suppressed_it(monkeypatch):
    # The coordinator commit path releases its own claims and suppresses this
    # hook -- releasing here too would be redundant work on the hot path.
    released = []

    monkeypatch.setenv(auto_push._ENV_SUPPRESS_FOR_SYNC_PUSH, "1")
    monkeypatch.setattr(
        auto_push, "_release_claims_for_head", lambda root: released.append(root)
    )

    assert auto_push.main([]) == 0
    assert released == []


# ---------------------------------------------------------------------------
# _push_would_be_a_noop -- the spawn-free gate ahead of the interpreter respawn
# ---------------------------------------------------------------------------

_SHA_A = "a" * 40
_SHA_B = "b" * 40


def _write_ref(common_dir, ref, sha):
    p = common_dir / ref
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(sha, encoding="utf-8")


@pytest.fixture
def _common_dir(monkeypatch, tmp_path):
    from coordinator_core.git import git_state

    monkeypatch.setattr(git_state, "resolve_git_common_dir", lambda root: tmp_path)
    return tmp_path


def test_noop_gate_true_when_local_and_remote_refs_match(_common_dir):
    _write_ref(_common_dir, "refs/heads/wip", _SHA_A)
    _write_ref(_common_dir, "refs/remotes/origin/wip", _SHA_A)
    assert auto_push._push_would_be_a_noop(str(_common_dir), "wip") is True


def test_noop_gate_false_when_local_is_ahead(_common_dir):
    _write_ref(_common_dir, "refs/heads/wip", _SHA_A)
    _write_ref(_common_dir, "refs/remotes/origin/wip", _SHA_B)
    assert auto_push._push_would_be_a_noop(str(_common_dir), "wip") is False


def test_noop_gate_fails_open_when_never_pushed(_common_dir):
    _write_ref(_common_dir, "refs/heads/wip", _SHA_A)
    assert auto_push._push_would_be_a_noop(str(_common_dir), "wip") is False


def test_noop_gate_fails_open_on_unborn_branch(_common_dir):
    assert auto_push._push_would_be_a_noop(str(_common_dir), "wip") is False


def test_noop_gate_fails_open_when_common_dir_read_raises(monkeypatch, tmp_path):
    from coordinator_core.git import git_state

    def _boom(root):
        raise OSError("no common dir")

    monkeypatch.setattr(git_state, "resolve_git_common_dir", _boom)
    assert auto_push._push_would_be_a_noop(str(tmp_path), "wip") is False


def test_noop_gate_reads_packed_refs_when_loose_refs_absent(_common_dir):
    lines = [
        "# pack-refs with: peeled fully-peeled sorted",
        _SHA_A + " refs/heads/wip",
        _SHA_A + " refs/remotes/origin/wip",
    ]
    (_common_dir / "packed-refs").write_text(chr(10).join(lines), encoding="utf-8")
    assert auto_push._push_would_be_a_noop(str(_common_dir), "wip") is True


def test_noop_gate_skips_comment_and_peeled_lines_in_packed_refs(_common_dir):
    lines = [
        "# pack-refs with: peeled fully-peeled sorted",
        _SHA_B + " refs/tags/v1",
        "^" + _SHA_A,
        _SHA_A + " refs/heads/wip",
        _SHA_B + " refs/remotes/origin/wip",
    ]
    (_common_dir / "packed-refs").write_text(chr(10).join(lines), encoding="utf-8")
    assert auto_push._push_would_be_a_noop(str(_common_dir), "wip") is False


def test_noop_gate_prefers_the_loose_ref_over_packed_refs(_common_dir):
    # A stale packed-refs entry must not decide the gate when a loose ref exists.
    _write_ref(_common_dir, "refs/heads/wip", _SHA_B)
    _write_ref(_common_dir, "refs/remotes/origin/wip", _SHA_A)
    lines = [_SHA_A + " refs/heads/wip", _SHA_A + " refs/remotes/origin/wip"]
    (_common_dir / "packed-refs").write_text(chr(10).join(lines), encoding="utf-8")
    assert auto_push._push_would_be_a_noop(str(_common_dir), "wip") is False


def _stub_main_preamble(monkeypatch, tmp_path):
    """Same idiom as the respawn-loop guard test above: a real repo_root
    on disk plus a stubbed `_run_git`, so `main()` reaches the async leg
    without touching git."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    monkeypatch.setattr(auto_push, "resolve_branch", lambda root: "work/foo")
    monkeypatch.setattr(auto_push, "branch_gate", lambda b: (True, None))
    monkeypatch.setattr(auto_push, "_release_claims_for_head", lambda root: None)
    return str(tmp_path)


def test_main_skips_the_interpreter_respawn_when_push_would_be_a_noop(
    monkeypatch, tmp_path
):
    detach_calls = []
    monkeypatch.setattr(
        auto_push, "_detach_and_run", lambda *a, **k: detach_calls.append(a)
    )
    repo_root = _stub_main_preamble(monkeypatch, tmp_path)
    monkeypatch.setattr(auto_push, "_push_would_be_a_noop", lambda root, b: True)

    assert auto_push.main(["--repo-root", repo_root]) == 0
    assert detach_calls == []


def test_main_still_respawns_when_the_gate_declines(monkeypatch, tmp_path):
    detach_calls = []
    monkeypatch.setattr(
        auto_push, "_detach_and_run", lambda *a, **k: detach_calls.append(a)
    )
    repo_root = _stub_main_preamble(monkeypatch, tmp_path)
    monkeypatch.setattr(auto_push, "_push_would_be_a_noop", lambda root, b: False)

    assert auto_push.main(["--repo-root", repo_root]) == 0
    assert len(detach_calls) == 1


def test_sync_leg_is_not_gated(monkeypatch, tmp_path):
    # --no-async pays no interpreter start, so the gate must not touch it.
    pushed = []
    monkeypatch.setattr(
        auto_push, "run_push_with_retry", lambda root, b, **k: pushed.append(b)
    )
    repo_root = _stub_main_preamble(monkeypatch, tmp_path)
    monkeypatch.setattr(auto_push, "_push_would_be_a_noop", lambda root, b: True)

    assert auto_push.main(["--no-async", "--repo-root", repo_root]) == 0
    assert pushed == ["work/foo"]
