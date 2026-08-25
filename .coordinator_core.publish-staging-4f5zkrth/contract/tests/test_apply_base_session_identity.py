"""chunk C6 (docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-
cold-start.md): `apply_base.session_identity()` moved from an `os.environ`
process-wide mutation to a per-context `contextvars.ContextVar` scope, with
`os.environ` mirrored only at the one outermost boundary a subprocess spawn
needs it (`scoped_commit`'s own `run_git` calls). This file proves the two
assertions the C2 characterization test (`coordinator_core/warm/tests/
test_process_global_characterization.py::
test_session_identity_cross_contaminates_ambient_environ_under_interleave`)
established were MISSING at HEAD: two overlapping identities do not
cross-contaminate, and the ambient `os.environ` is unchanged once both
overlapping blocks have exited.

Spec backlink: this plan, chunk C6.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from coordinator_core.contract import apply_base


def test_overlapping_session_identities_do_not_cross_contaminate():
    """Two interleaved `session_identity()` blocks (the warm-dispatch
    shape: two threads, each holding its own identity for its own block's
    lifetime) each observe ONLY their own session id for the duration of
    their own block -- the opposite of the C2 characterization test's
    proven-wrong behaviour."""
    entered_a = threading.Event()
    observed_inside_a: dict[str, Optional[str]] = {}
    observed_inside_b: dict[str, Optional[str]] = {}
    let_b_finish = threading.Event()
    b_done = threading.Event()

    def _session_a():
        with apply_base.session_identity("session-A"):
            entered_a.set()
            b_done.wait(timeout=5)
            observed_inside_a["COORDINATOR_SESSION_ID"] = apply_base.current_session_env().get(
                "COORDINATOR_SESSION_ID"
            )
            let_b_finish.set()

    def _session_b():
        entered_a.wait(timeout=5)
        with apply_base.session_identity("session-B"):
            observed_inside_b["COORDINATOR_SESSION_ID"] = apply_base.current_session_env().get(
                "COORDINATOR_SESSION_ID"
            )
            b_done.set()
            let_b_finish.wait(timeout=5)

    thread_a = threading.Thread(target=_session_a)
    thread_b = threading.Thread(target=_session_b)
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)

    assert observed_inside_a["COORDINATOR_SESSION_ID"] == "session-A"
    assert observed_inside_b["COORDINATOR_SESSION_ID"] == "session-B"


def test_overlapping_session_identities_leave_ambient_environ_unchanged():
    """`os.environ` is never written by `session_identity()` itself -- only
    `_mirror_session_env_for_subprocess` does, and only for the duration of
    one subprocess call. Two overlapping blocks that never call
    `scoped_commit` must leave the ambient `os.environ` exactly as it was
    before either block was entered, both DURING the overlap and after both
    have exited."""
    original = {var: os.environ.get(var) for var in apply_base.SESSION_ENV_VARS}
    for var in apply_base.SESSION_ENV_VARS:
        os.environ.pop(var, None)

    entered_a = threading.Event()
    observed_environ_inside_a: dict[str, Optional[str]] = {}
    let_b_finish = threading.Event()
    b_done = threading.Event()

    def _session_a():
        with apply_base.session_identity("session-A"):
            entered_a.set()
            b_done.wait(timeout=5)
            observed_environ_inside_a["COORDINATOR_SESSION_ID"] = os.environ.get(
                "COORDINATOR_SESSION_ID"
            )
            let_b_finish.set()

    def _session_b():
        entered_a.wait(timeout=5)
        with apply_base.session_identity("session-B"):
            b_done.set()
            let_b_finish.wait(timeout=5)

    try:
        thread_a = threading.Thread(target=_session_a)
        thread_b = threading.Thread(target=_session_b)
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=5)
        thread_b.join(timeout=5)

        assert observed_environ_inside_a["COORDINATOR_SESSION_ID"] is None
        for var in apply_base.SESSION_ENV_VARS:
            assert os.environ.get(var) == original[var]
    finally:
        for var, value in original.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value


class _RecordingRunGit:
    def __init__(self) -> None:
        self.observed_env: list[dict[str, Optional[str]]] = []

    def __call__(self, args: list[str], cwd: Path):
        self.observed_env.append(
            {var: os.environ.get(var) for var in apply_base.SESSION_ENV_VARS}
        )
        if args[0] == "add":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ["diff", "--cached"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")  # "changed"
        if args[0] == "commit":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "rev-parse":
            return SimpleNamespace(returncode=0, stdout="deadbeef\n", stderr="")
        raise AssertionError(f"unexpected git invocation: {args}")


def test_scoped_commit_mirrors_session_id_to_environ_only_around_run_git(tmp_path):
    """The ONE outermost boundary: while `session_identity()` is active,
    `scoped_commit`'s own `run_git` calls observe the session id mirrored
    into `os.environ` -- and the mirror is undone immediately after each
    call, so `os.environ` is not left holding it in between."""
    original = {var: os.environ.get(var) for var in apply_base.SESSION_ENV_VARS}
    for var in apply_base.SESSION_ENV_VARS:
        os.environ.pop(var, None)

    artifact = tmp_path / "artifact.md"
    artifact.write_text("content\n", encoding="utf-8")
    run_git = _RecordingRunGit()

    try:
        with apply_base.session_identity("session-mirror"):
            sha = apply_base.scoped_commit(tmp_path, "artifact.md", "msg", run_git)
        assert sha == "deadbeef"
        assert run_git.observed_env
        for observed in run_git.observed_env:
            assert observed["COORDINATOR_SESSION_ID"] == "session-mirror"
            assert observed["CLAUDE_SESSION_ID"] == "session-mirror"

        # Outside the session_identity() block (and between each run_git
        # call, though this only asserts the post-block state), the mirror
        # must be undone -- never left resident in os.environ.
        for var in apply_base.SESSION_ENV_VARS:
            assert os.environ.get(var) == original[var]
    finally:
        for var, value in original.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value
