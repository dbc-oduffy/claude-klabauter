"""
coordinator_core.tests._fixtures — shared pytest fixture helpers.

Purpose: shared implementation body for the _isolated_svc_root autouse fixture
defined in coordinator_core/tests/conftest.py and
coordinator_core/invoke/tests/conftest.py. Both conftest files keep their own
@pytest.fixture(autouse=True) decorator (pytest requires the decorator to live in
the conftest), but delegate their bodies here to eliminate duplication.

Any change to the isolation strategy (prefix, base dir, teardown) needs to be made
only in this one place.

Review: code-reviewer (F7) — extracted from conftest.py / invoke/tests/conftest.py
to remove identical duplicate implementations.

Spec backlink: pln-coordinator-core-global-multip-9ddcf7 § C9
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

#: Shared by test_archive_stamp_claimant_identity.py and
#: test_archive_stamp_human_claimant.py — the two claimant-stamp test modules,
#: which independently authored byte-identical git scaffolding for the same
#: two claim paths (handoff, memo) before this extraction.
#: Review: overengineering-reviewer (Kira) — hoisted to end the duplication;
#: _seed_handoff/_seed_memo stay local to each module (differing signatures,
#: not worth reconciling for this).
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # popup-safe-env-suppressed
    )


def init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    run_git(repo, "init")
    run_git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    run_git(repo, "add", "README.md")
    run_git(repo, "commit", "-m", "init")


def isolated_svc_root_impl(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Shared implementation for the _isolated_svc_root autouse fixture.

    Sets COORDINATOR_SVC_ROOT to a short per-test tmpdir so every test that exercises
    global_sentinel_dir() or global_socket_path() (directly or via SingletonLock /
    ServiceContext) targets an isolated path rather than the machine-global
    /tmp/coordinator-svc-<uid>/... path. Prevents full-suite global-singleton contention.

    Path is intentionally short (prefix ccsvc- under /tmp) to stay within the macOS
    AF_UNIX sun_path limit of 103 usable bytes:
        /tmp/ccsvc-XXXXXXXX/coordinator.sock  →  ~34 chars, always < 103.

    Teardown removes the tmp dir (ignore_errors=True — subprocess may still be writing).

    Negative-spec:
      - Do NOT use pytest's tmp_path here — it produces deep paths under
        /tmp/pytest-<N>/test_<name>/<N>/ that may approach or exceed 103 bytes.
    """
    # Prefer /tmp on POSIX (short path — stays within the macOS AF_UNIX sun_path
    # 103-byte limit); fall back to the OS temp dir on Windows, where /tmp does
    # not exist and there is no sun_path constraint.
    _short_base = "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()
    tmpdir = tempfile.mkdtemp(prefix="ccsvc-", dir=_short_base)
    monkeypatch.setenv("COORDINATOR_SVC_ROOT", tmpdir)
    yield
    shutil.rmtree(tmpdir, ignore_errors=True)
