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
"""

from __future__ import annotations

import os
import re
import subprocess
from coordinator_core.win_portability import no_console_creationflags, same_path
import sys
from typing import List, Optional, Tuple

from coordinator_core._settings_home import settings_home
from coordinator_core.engine_root import coordinator_engine_root_env
from coordinator_core.git.repo_root import show_toplevel as _show_toplevel

_STATE_ROOT_OVERRIDE_ENV = "COORDINATOR_ROOT"
_CLAUDE_HOME_ENV = "CLAUDE_HOME"

_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_PROG = "list-review-trail-records.sh"


# ---------------------------------------------------------------------------
# State-root resolution (self-contained Rule-5 port — see module docstring)
# ---------------------------------------------------------------------------


def _claude_home() -> str:
    """Return the ~/.claude root, honouring CLAUDE_HOME env var for test isolation."""
    override = os.environ.get(_CLAUDE_HOME_ENV)
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".claude")


def _machine_local_impl() -> str:
    """Return the path to _machine_local.py, settings-home first, honouring
    MACHINE_LOCAL_IMPL for tests.

    Settings-home-first per DR-210 Amendment 2026-07-24 ("coordinator resolves
    nothing through ``~/.claude/bin``"); the retired compat mirror stays as a
    last-resort rung only. Negative-spec: does NOT stop consulting the mirror —
    a machine whose settings-home copy is absent must still resolve.
    """
    override = os.environ.get("MACHINE_LOCAL_IMPL")
    if override:
        return override
    settings_home_impl = os.path.join(str(settings_home()), "bin", "_machine_local.py")
    if os.path.exists(settings_home_impl):
        return settings_home_impl
    return os.path.join(_claude_home(), "bin", "_machine_local.py")


def _machine_local_get(key: str) -> Optional[str]:
    """Call ``machine-local get <key>`` and return the value, or None on failure."""
    impl = _machine_local_impl()
    if not os.path.exists(impl):
        return None
    try:
        result = subprocess.run(
            [sys.executable, impl, "get", key],
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
    except OSError:
        print(f"skip: _machine_local_get: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def _claude_klabauter_root() -> Optional[str]:
    """Resolve the claude-klabauter repo root: COORDINATOR_ENGINE_ROOT env (via the
    accessor), else machine-local, else None."""
    override = (coordinator_engine_root_env(__name__) or "").strip()
    if override:
        return override
    return _machine_local_get("repos.claude_klabauter")


def _same_path(a: str, b: str) -> bool:
    """Thin alias onto ``coordinator_core.win_portability.same_path`` -- the
    consolidated primitive (state/sizings/2026-08-07-path-equality-
    consolidates-onto-one-prim.yaml). Promoted from realpath-only to
    samefile-then-fallback semantics: broader (junction-aware) equality is
    correct here since this call site only checks "is git_root the meta-repo
    home", where a junction-aliased home must compare equal."""
    return same_path(a, b)


def _git_root() -> Optional[str]:
    """Resolve the current working directory's git repo root, or None if not in one."""
    return _show_toplevel()


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
        3. Rule 5 default: git root of cwd; meta-repo cwd routes to
           ``the engine root/state``, sibling-repo cwd uses ``<git-root>/state``.

    Returns None on any unresolvable case (not a git repo, meta-repo but
    claude-klabauter root unresolvable) — callers emit ONE generic error message
    regardless of which sub-case fired (oracle parity, see module
    negative-spec).
    """
    override = explicit_override if explicit_override is not None else os.environ.get(
        _STATE_ROOT_OVERRIDE_ENV, ""
    )
    if override:
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

    git_root = _git_root()
    if not git_root:
        return None

    if _same_path(git_root, _claude_home()):
        claude_klabauter_root = _claude_klabauter_root()
        if claude_klabauter_root is None:
            return None
        return os.path.join(claude_klabauter_root, "state")

    return os.path.join(git_root, "state")


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


def list_paths(date_prefix: str = "", state_root_override: Optional[str] = None) -> List[str]:
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
    if date_prefix and not _DATE_PREFIX_RE.match(date_prefix):
        raise ReviewTrailListError(
            f"{_PROG}: --date-prefix must be YYYY-MM-DD, got: {date_prefix}"
        )

    state_root = _resolve_state_root(state_root_override)
    if not state_root:
        raise ReviewTrailListError(
            f"{_PROG}: cwd is not a git repo and COORDINATOR_ROOT is not set — "
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
        raise ReviewTrailListError(f"{_PROG}: directory scan failed: {exc}") from exc

    if date_prefix:
        records = [r for r in records if r[0].startswith(date_prefix)]

    records.sort(key=lambda r: r[0])
    return [os.path.normpath(fullpath) for _basename, fullpath in records]
