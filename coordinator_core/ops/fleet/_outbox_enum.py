"""
coordinator_core.ops.fleet._outbox_enum — the light leaf that answers "which outbox drafts exist".

Purpose: hold only what `memo.list_outbox` needs to enumerate a repo's memo
drafts and wrap them in the fleet envelope, so the op's import closure does not
pull the archival/commit machinery in `_common` or the draft op in `memo_draft`.
`_common` and `memo_draft` re-export every name here, so no other importer changes.

Negative-spec:
  - Imports stdlib plus `session.machinery_paths` and `lifecycle` only; never
    anything from `coordinator_core.ops`, `ipc`, `dag`, `git_native` or `session.core`.
  - Does NOT register any op.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import sys
from pathlib import Path

from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.session import machinery_paths as _machinery_paths

_LOG = logging.getLogger(__name__)
_LOG.addHandler(logging.NullHandler())

__all__ = [
    "build_dry_run_result",
    "build_setup_error_result",
    "legacy_outbox_dir",
    "main_worktree_root",
    "merged_outbox_drafts",
    "outbox_dir",
    "quiet_setup_errors",
]

_QUIET = contextvars.ContextVar("fleet_setup_error_quiet", default=False)


@contextlib.contextmanager
def quiet_setup_errors():
    """Within this context `_setup_error` returns its envelope and emits no diagnostic.

    For a probe that asks a refusing classifier "would you refuse?" and is not itself refusing.
    Context-local, so it neither leaks to other threads nor touches `sys.stderr`.
    """
    token = _QUIET.set(True)
    try:
        yield
    finally:
        _QUIET.reset(token)


def outbox_dir(caller_worktree: Path) -> Path:
    """The CANONICAL (write) outbox dir for `caller_worktree` --
    `.coordinator-local/memo-outbox/`. Every NEW write in this op family
    targets this directory only; see `legacy_outbox_dir` for the retired
    read-fallback root."""
    return Path(_machinery_paths.memo_outbox_dir(str(caller_worktree)))


def legacy_outbox_dir(caller_worktree: Path) -> Path:
    return Path(_machinery_paths.legacy_memo_outbox_dir(str(caller_worktree)))


def merged_outbox_drafts(caller_worktree: Path) -> list[Path]:
    new_dir = outbox_dir(caller_worktree)
    legacy_dir = legacy_outbox_dir(caller_worktree)

    new_paths = sorted(new_dir.glob("*.md"), key=lambda p: p.name) if new_dir.is_dir() else []
    seen_topics = {p.stem for p in new_paths}
    legacy_paths = (
        sorted(
            (p for p in legacy_dir.glob("*.md") if p.stem not in seen_topics),
            key=lambda p: p.name,
        )
        if legacy_dir.is_dir()
        else []
    )
    return [*new_paths, *legacy_paths]


def _emit_warm_diagnostic(text: str) -> None:
    """Forward one diagnostic line to the warm per-request sink, if one is bound.

    Function-local import and fail-open: a cold spawn has no sink and must not
    import `coordinator_core.warm` at module scope; a failure here never turns a
    well-formed refusal into an exception.
    """
    try:
        from coordinator_core.warm.entry_seam import emit_diagnostic

        emit_diagnostic(text)
    except Exception:  # noqa: BLE001 — diagnostics never fail the op
        pass


def _setup_error(mode, dry_run, reason: str) -> dict:
    """Log reason to stderr and return the exit_code:1 standard-echoed envelope.

    The reason is NOT a top-level wire field (contract §2.1 is frozen); stderr is
    its only cold channel and is written explicitly so logging configuration
    cannot divert it. `_emit_warm_diagnostic` is the warm half of the same channel.
    Pinned by tests/test_setup_error_stderr_channel.py.
    """
    if not _QUIET.get():
        _LOG.error("fleet op setup error: %s", reason)
        print(f"fleet op setup error: {reason}", file=sys.stderr, flush=True)
        _emit_warm_diagnostic(f"fleet op setup error: {reason}")
    return {
        "exit_code": 1,
        "mode": mode,
        "dry_run": dry_run,
        "candidates": [],
        "acted": [],
        "skipped": [],
        "failed": [],
    }


def build_setup_error_result(mode, dry_run, reason: str) -> dict:
    """Logs reason daemon-side; returns the exit_code:1 standard-echoed envelope.
    The frozen wire envelope is NOT expanded — no reason/error top-level field.
    """
    return _setup_error(mode, dry_run, reason)


def build_dry_run_result(mode: str, candidates: list) -> dict:
    """Build the exit_code:0 dry_run:true response envelope (contract §2.1 :176-215).

    Mutates nothing — the preview phase is read-only.
    """
    return {
        "exit_code": 0,
        "mode": mode,
        "dry_run": True,
        "candidates": candidates,
        "acted": [],
        "skipped": [],
        "failed": [],
    }
