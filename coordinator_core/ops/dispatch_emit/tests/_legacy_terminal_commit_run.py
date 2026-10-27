"""Drive the retained `dispatch.terminal_commit` over the budget driver's plan-run fixture.

The driver's own `_run_full_path` calls `dispatch.land_run`; the legacy op's pins call this instead.
`prepare` points the lessons outbox, the session id and the baton claim at the fixture and writes the
no-op close script this module launches the op's close job at; it needs a pytest `monkeypatch` so the
driver's process-wide patches are undone with the test.
"""

from __future__ import annotations

import copy
from pathlib import Path

from coordinator_core.benchmarks import terminal_commit_full_path as driver


def prepare(repo: Path, monkeypatch) -> None:
    from coordinator_core.ops.dispatch_emit import land_run
    from coordinator_core.session import claims

    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", "")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", driver.SESSION_ID)
    monkeypatch.setattr(land_run, "_launch_close_job", land_run._launch_close_job)
    monkeypatch.setattr(claims, "list_claims_by_session_checked", claims.list_claims_by_session_checked)
    driver._prepare_close(repo)


def run(repo: Path, script: str) -> dict:
    """The op's reply for the fixture's one-row plan run; its close job launches at the no-op script."""
    from coordinator_core.ops.dispatch_emit import terminal_commit

    saved = terminal_commit.CLOSE_JOB_SCRIPT
    terminal_commit.CLOSE_JOB_SCRIPT = str(repo.parent / "close_job_noop.py")
    try:
        return terminal_commit._handler(
            {
                "script_path": script,
                "incomplete_chunks": [],
                "session_id": driver.SESSION_ID,
                "inline_review": copy.deepcopy(driver.INLINE_REVIEW),
            },
            repo_root=repo / ".git",
        )
    finally:
        terminal_commit.CLOSE_JOB_SCRIPT = saved
