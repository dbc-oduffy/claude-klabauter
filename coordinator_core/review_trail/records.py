"""
coordinator_core.review_trail.records — live+archive review-trail corpus reader.

Purpose: read-side access to ``state/review-trail/**/*.json`` and
``archive/review-trail/**/*.json`` — the 3,662-record corpus DR-372 rules
stays on disk after the writer is retired. Moved here, verbatim, from
``coordinator_core.ops.list_review_trail_records`` (C2b,
state/dispatch-briefs/2026-08-29-the-gravestoned-review-trail-surface-is-
deleted/C2b.md): that module's op handler and ``main(argv)`` CLI entry are
gravestoned per DR-374 and deleted by a follow-up chunk, but ``_collect``,
``list_paths``, and the state-root resolution they need are live readers of
live data — three module-scope importers (``ops/changelog_ops.py``,
``ops/emit/sections/review_trail.py``, ``ops/plan_suggest_completion_steps.py``)
depend on them at IMPORT time, so they needed a surviving home before the
doomed module could be deleted.

This package (``coordinator_core.review_trail``) already holds the
reviewed-set STORE read live by the close gate — this is read-side code over
the same corpus in the same domain, per this row's own citation of that
overlap. AMENDS ``coordinator_core.review_trail``'s own ``__init__.py``
docstring, which previously stated the reader "stays in `ops/`" — superseded
here, not by unexamined drift.

This module is now a narrow reader over the historical review-trail corpus,
not a general state-root resolver: an overengineering pass (2026-08-29)
deleted the CLI-only resolution rungs (``CLAUDE_HOME``/machine-local/meta-repo
detection) that had no live caller once ``main(argv)`` was gravestoned — every
surviving caller of ``list_paths`` always supplies ``state_root_override``
explicitly, so ``_resolve_state_root`` only needs the explicit-override and
``COORDINATOR_ROOT``-env rungs.

Spec backlink: state/dispatch-briefs/2026-08-29-the-gravestoned-review-trail-
surface-is-deleted/C2b.md
DR authority: docs/decisions/DR-374 (gravestone, not narrowing)

Negative-spec:
    - Does NOT write any file — pure reader, stdout-only (CLI stays behind in
      the doomed ``ops.list_review_trail_records`` module, deleted by C3).
    - Does NOT sort by full path — basename only, mirroring the oracle this
      was ported from (``ops.list_review_trail_records``'s own module
      docstring carries the full rationale, not re-derived here).
    - Does NOT treat an absent live or archive directory as an error —
      silently skipped (fresh-install case).
    - Does NOT resolve a state root via ``CLAUDE_HOME``/machine-local/meta-repo
      detection — that rung had no live caller (every surviving caller always
      passes ``state_root_override``) and was deleted with the CLI it existed
      for; only the explicit-override and ``COORDINATOR_ROOT``-env rungs remain.
    - Does NOT accept a ``date_prefix`` filter — no surviving caller passed
      one; deleted with the CLI flag it existed for.
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

_STATE_ROOT_OVERRIDE_ENV = "COORDINATOR_ROOT"


# ---------------------------------------------------------------------------
# State-root resolution (self-contained Rule-5 port — see module docstring)
# ---------------------------------------------------------------------------


def _resolve_state_root(explicit_override: Optional[str] = None) -> Optional[str]:
    """Resolve the coordinator state root.

    Precedence:
        1. *explicit_override* param, if given — same branching as the env
           override below, but supplied by an in-process caller directly
           rather than staged into ``os.environ``. Warm-server callers MUST
           use this: an env write is process-global and visible to every
           concurrently-served request (state/bug-backlog/2026-08-18-a-warm-
           server-stamps-every-op-it-serves-eeb801fc6bee.yaml, ROOT half).
        2. ``COORDINATOR_ROOT`` env var — verbatim if it ends in ``/state``,
           else ``+"/state"`` appended (oracle's exact override branching).
           Correct only for a cold, single-request process (the CLI path).

    Returns None when neither rung resolves — every surviving caller always
    supplies ``state_root_override``, so this is the CLI-only "no override at
    all" case (callers emit ONE generic error message, oracle parity, see
    module negative-spec).
    """
    override = explicit_override if explicit_override is not None else os.environ.get(
        _STATE_ROOT_OVERRIDE_ENV, ""
    )
    if not override:
        return None

    # Detect "already ends in a `state` path segment" by basename, not
    # by a literal ``.endswith("/state")`` string suffix check —
    # os.path.basename() (ntpath on Windows) splits on either
    # separator, so this matches a COORDINATOR_ROOT that was supplied
    # in the platform's own native form (e.g. "...\\state" on Windows).
    # The old suffix-only check silently missed that on Windows and
    # doubled the "/state" append (C5 root-cause: os.sep-in-wire-id
    # class — an os.sep-bearing value was compared against a
    # forward-slash-only literal).
    if os.path.basename(override.rstrip("/\\")) == "state":
        return override
    return override.rstrip("/") + "/state"


# ---------------------------------------------------------------------------
# Record collection
# ---------------------------------------------------------------------------


def _collect(dir_path: str) -> List[Tuple[str, str]]:
    """Collect (basename, fullpath) pairs for every ``*.json`` under dir_path.

    Absent-dir-safe: returns [] when dir_path does not exist (mirrors the
    oracle's ``[[ -d "${dir}" ]] || return 0`` guard) — a fresh-install case,
    not an error.

    Follows symlinks (mirrors ``find -L``). Raises OSError on a genuine scan
    failure (permission error, etc.) — caller maps this to exit 1, mirroring
    the oracle's explicit ``|| return $?`` propagation from ``find``/``awk``.
    """
    if not os.path.isdir(dir_path):
        return []
    out: List[Tuple[str, str]] = []
    for root, _dirs, files in os.walk(dir_path, followlinks=True):
        for name in files:
            if name.endswith(".json"):
                full = os.path.join(root, name)
                out.append((name, full))
    return out


class ReviewTrailListError(RuntimeError):
    """Raised by ``list_paths`` on any oracle-parity failure (mirrors the CLI's exit 1)."""


def list_paths(state_root_override: Optional[str] = None) -> List[str]:
    """Programmatic (in-process) API: sorted live+archive review-trail file paths.

    Mirrors the retiring CLI's non-``--print0`` success path — in-process
    callers (``coordinator_core.ops.changelog_ops`` /
    ``coordinator_core.ops.emit.sections.review_trail`` /
    ``coordinator_core.ops.plan_suggest_completion_steps``) invoke this
    module directly instead of spawning a subprocess.

    ``state_root_override``, when given, takes ``_resolve_state_root``'s explicit-
    override precedence rung directly — the caller-scoped alternative to staging
    ``COORDINATOR_ROOT`` into ``os.environ`` (which a warm-served caller must not do:
    an env write is process-global and visible to every other request the same
    resident server is concurrently serving). ``None`` (the default) falls through to
    the ``COORDINATOR_ROOT`` env var exactly as before — unchanged for the CLI/cold
    path and for any existing caller not passing this param.

    Raises ``ReviewTrailListError`` (message = the CLI's stderr text) on any failure
    the CLI would have exited 1 for — in-process callers get an exception instead of
    an exit code + stderr line.
    """
    state_root = _resolve_state_root(state_root_override)
    if not state_root:
        raise ReviewTrailListError(
            # Review: overengineering-reviewer — _PROG named a shell script this
            # workstream deleted (list-review-trail-records.sh); the sole caller
            # catches ReviewTrailListError and never reads the message.
            "cwd is not a git repo and COORDINATOR_ROOT is not set — "
            "cannot resolve state/review-trail/"
        )

    live_dir = os.path.join(state_root, "review-trail")
    if os.path.basename(state_root.rstrip("/\\")) == "state":
        archive_base = os.path.dirname(state_root.rstrip("/\\"))
    else:
        archive_base = state_root
    archive_dir = os.path.join(archive_base, "archive", "review-trail")

    try:
        records = _collect(live_dir) + _collect(archive_dir)
    except OSError as exc:
        raise ReviewTrailListError(f"directory scan failed: {exc}") from exc

    records.sort(key=lambda r: r[0])
    return [os.path.normpath(fullpath) for _basename, fullpath in records]
