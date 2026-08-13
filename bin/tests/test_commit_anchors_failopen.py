"""
bin.tests.test_commit_anchors_failopen — Fail-open contract smoke tests for
bin/claude-klabauter-commit-anchors.

Ported from bin/claude-klabauter-commit-anchors.test.sh (bash) per
docs/plans/2026-07-21-claude-klabauter-pure-python-shop-retire-all-bash.md § C8. Preserves the
original's OBSERVABLE fail-open contract (print nothing, exit 0, on every error path) but
re-targets the exercised failure paths at the current DR-215 command-entrypoint transport:
the bash oracle's socket-bind/ECONNREFUSED cases and its `_candidate_sockets` probe tested
the retired UDS daemon transport, which no longer exists in the shim on disk (verified: the
shim has no `_candidate_sockets` symbol and does not touch `COORDINATOR_SVC_ROOT` or any
AF_UNIX socket — the oracle predates the C5b transport migration and, run as-is today, no
longer fails open at all: it prints real `Nature: ...` trailers from a live invoke call in
this repo, then a hard AttributeError on the socket-introspection case). This port instead
drives the shim's actual failure surface: no session ID, and an invoke-subprocess failure
(module import failure) with a resolvable repo — the two ways the current fail-open
contract can actually be hit.

These are isolation tests only (no live service required). Runtime integration (shim
talking to a real commit.anchors op) is the EM's job at merge.

Spec backlink: pln-claude-klabauter-commit-anchor-stamper-q-29b891 § C3-shim
Negative-spec: does NOT start a coordinator_core service — isolation-only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_SHIM = _REPO_ROOT / "bin" / "claude-klabauter-commit-anchors"


@pytest.fixture(scope="module", autouse=True)
def _require_shim() -> None:
    if not _SHIM.exists():
        pytest.skip("bin/claude-klabauter-commit-anchors not on disk")


def _run(
    env_overrides: dict[str, str],
    unset: tuple[str, ...] = (),
    extra_args: list[str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    """Run the shim with a controlled environment; never raises on non-zero exit."""
    env = os.environ.copy()
    for key in unset:
        env.pop(key, None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(_SHIM), *(extra_args or [])],
        env=env,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _assert_fail_open(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode} (stderr: {result.stderr!r})"
    )
    assert result.stdout == "", f"expected empty stdout, got {result.stdout!r}"


def _init_tmp_repo(tmp_path: Path) -> Path:
    """A throwaway git repo, deliberately NOT the project root — `coordinator_core` is
    unimportable from here, so any invoke subprocess run with this as cwd fails to import
    the module and the shim must fail open rather than propagate the error."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q"],
        cwd=repo,
        check=True,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return repo


def test_no_session_id(tmp_path: Path) -> None:
    """Case 1: no session ID anywhere (env unset, no sentinel file) -> exit 0, no output."""
    repo = _init_tmp_repo(tmp_path)
    result = _run(
        env_overrides={"CLAUDE_KLABAUTER_COMMIT_NATURE": ""},
        unset=("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"),
        cwd=repo,
    )
    _assert_fail_open(result)


def test_invoke_subprocess_import_failure(tmp_path: Path) -> None:
    """Case 2: session ID resolves and the repo resolves, but the invoke subprocess can't
    import coordinator_core (cwd is a throwaway repo, not the project tree) -> the
    transport call returns None -> shim prints nothing and exits 0.

    This is the current-transport equivalent of the retired bash oracle's
    socket-missing / ECONNREFUSED cases: any way the `commit.anchors` call can fail,
    the shim must still fail open.
    """
    repo = _init_tmp_repo(tmp_path)
    result = _run(
        env_overrides={"CLAUDE_SESSION_ID": "test-session-smoke"},
        cwd=repo,
    )
    _assert_fail_open(result)


def test_nature_flag_no_service(tmp_path: Path) -> None:
    """Case 3: --nature flag accepted, still fails open when the invoke call fails."""
    repo = _init_tmp_repo(tmp_path)
    result = _run(
        env_overrides={"CLAUDE_SESSION_ID": "test-session-smoke"},
        extra_args=["--nature", "infra"],
        cwd=repo,
    )
    _assert_fail_open(result)


def test_nature_env_no_service(tmp_path: Path) -> None:
    """Case 4: CLAUDE_KLABAUTER_COMMIT_NATURE env accepted, still fails open when the invoke call fails."""
    repo = _init_tmp_repo(tmp_path)
    result = _run(
        env_overrides={
            "CLAUDE_SESSION_ID": "test-session-smoke",
            "CLAUDE_KLABAUTER_COMMIT_NATURE": "fix",
        },
        cwd=repo,
    )
    _assert_fail_open(result)


def test_shim_is_executable() -> None:
    """Case 5: shim is executable (sanity gate).

    POSIX-only: this test always invokes the shim via `[sys.executable,
    str(_SHIM), ...]` (see `_run` above), so the exec bit is not load-bearing
    for the test itself -- it's an installer-hygiene check, and
    `os.access(X_OK)` degrades to a meaningless existence check on Windows
    anyway. Guarded rather than dropped so the POSIX assertion still fires."""
    if os.name != "nt":
        assert os.access(_SHIM, os.X_OK), f"{_SHIM} is not executable"


def test_not_a_git_repo_fails_open(tmp_path: Path) -> None:
    """Bonus current-transport case: cwd resolves to no git repo at all (git rev-parse
    fails) -> _git_show_toplevel returns None -> shim returns immediately, exit 0, no
    output. Not present in the bash oracle (which always ran from inside this repo) but
    exercises the shim's very first fail-open gate.
    """
    result = _run(
        env_overrides={"CLAUDE_SESSION_ID": "test-session-smoke"},
        cwd=tmp_path,
    )
    _assert_fail_open(result)
