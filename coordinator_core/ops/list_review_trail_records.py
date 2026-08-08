"""
coordinator_core.ops.list_review_trail_records — union reader for live and
archived review-trail records.

Purpose: canonical reader for ``state/review-trail/**/*.json`` AND
``archive/review-trail/**/*.json``, sorted by basename (ascending =
chronological, since basenames are ``YYYY-MM-DD-HHMMSS-{sid}.json`` prefixed).
Absent directories are silently skipped (fresh-install case).

CLI usage:
    list-review-trail-records.sh [--date-prefix YYYY-MM-DD] [--print0]

Options:
    --date-prefix YYYY-MM-DD   Only emit records whose basename starts with
                               this prefix.
    --print0                   Separate records with NUL instead of newlines
                               (safe for downstream ``xargs -0`` / ``read -d ''``
                               pipelines).

Exit codes:
    0  — success (including empty result — absent dirs are not errors)
    1  — tool failure (state-root unresolvable, bad --date-prefix, unknown
         option, or a directory-scan OSError)

Sort mechanism: sort by basename, NOT by full path.
    ``archive/review-trail/week-2026-05-25/2026-05-19-foo.json`` has an
    earlier basename than ``archive/review-trail/week-2026-05-18/2026-05-20-
    bar.json``, but its full-path sort-order would be reversed across the
    week-subdir boundary. Basename is extracted as the primary sort key,
    mirroring the oracle's ``awk -F'/'`` extraction.

State-root resolution: self-contains the DR-047/stop-the-rot Rule-5
default-branch resolution (bare ``coordinator_state_root()``, no
``--central``/``--subject``/``--artifact``): meta-repo cwd (git root ==
CLAUDE_HOME) routes to CLAUDE_KLABAUTER_ROOT/state; any other (sibling-repo) cwd uses
``<git-root>/state`` directly. Mirrors the sibling pattern in
``ops/check_weekly_staleness.py::_resolve_state_root`` /
``ops/check_arch_audit_staleness.py::_resolve_state_root`` (same shape,
independently duplicated per those modules' own documented rationale — no
shared helper exists yet).

``COORDINATOR_ROOT`` env var is an explicit override (test/caller use):
interpreted as a state root verbatim when it ends in ``/state``, or as a
repo root (``+"/state"`` appended) otherwise — mirrors the oracle's exact
branching, backward-compat with existing callers.

Port of: list-review-trail-records.sh (example-doctrine-repo b5a4192c, 2026-07-20)
Spec backlink: docs/plans/2026-05-28-archive-aware-review-oracle-and-audit-skill.md § Chunk C0
               docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Negative-spec:
    - Does NOT write any file — pure reader, stdout-only.
    - Does NOT sort by full path — basename only (see rationale above).
    - Does NOT treat an absent live or archive directory as an error —
      silently skipped (fresh-install case), reproduced faithfully from the
      oracle's ``[[ -d "${dir}" ]] || return 0`` guard.
    - Does NOT escalate --date-prefix "no records match" to an error — that
      is grep's exit-1-no-match case in the oracle, a valid empty result.
    - On ANY state-root resolution failure (not-a-git-repo, meta-repo-but-
      claude-klabauter-unresolvable, or any other coordinator_state_root failure mode)
      emits the SAME generic stderr message and exit 1 — the oracle's
      ``coordinator_state_root 2>/dev/null || true`` swallows the specific
      failure reason before the wrapping script's own generic check fires;
      reproduced faithfully rather than "improved" with a more specific
      message.
"""

from __future__ import annotations

import os
import re
import subprocess
from coordinator_core.win_portability import no_console_creationflags, same_path
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core._settings_home import settings_home

_STATE_ROOT_OVERRIDE_ENV = "COORDINATOR_ROOT"
_CLAUDE_KLABAUTER_ROOT_ENV = "CLAUDE_KLABAUTER_ROOT"
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
    """Resolve the claude-klabauter repo root: CLAUDE_KLABAUTER_ROOT env, else machine-local, else None."""
    override = os.environ.get(_CLAUDE_KLABAUTER_ROOT_ENV, "").strip()
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
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
    except OSError:
        print(f"skip: _git_root: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return root or None


def _resolve_state_root() -> Optional[str]:
    """Resolve the coordinator state root.

    Precedence:
        1. ``COORDINATOR_ROOT`` env var — verbatim if it ends in ``/state``,
           else ``+"/state"`` appended (oracle's exact override branching).
        2. Rule 5 default: git root of cwd; meta-repo cwd routes to
           ``CLAUDE_KLABAUTER_ROOT/state``, sibling-repo cwd uses ``<git-root>/state``.

    Returns None on any unresolvable case (not a git repo, meta-repo but
    claude-klabauter root unresolvable) — callers emit ONE generic error message
    regardless of which sub-case fired (oracle parity, see module
    negative-spec).
    """
    override = os.environ.get(_STATE_ROOT_OVERRIDE_ENV, "")
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


def list_paths(date_prefix: str = "") -> List[str]:
    """Programmatic (in-process) API: sorted live+archive review-trail file paths.

    Mirrors ``main()``'s non-``--print0`` success path — added so in-process callers
    (``coordinator_core.ops.emit.sections.review_trail`` / ``rollups``) invoke this
    module directly instead of spawning a subprocess against the CLI, retiring the
    ``bash <script>`` bridge those callers used pre-migration. Honours the same
    ``COORDINATOR_ROOT`` env-var override ``_resolve_state_root`` already reads (test
    isolation / frozen-fixture callers set this env var exactly as before).

    Raises ``ReviewTrailListError`` (message = the CLI's stderr text) on any failure
    the CLI would have exited 1 for — in-process callers get an exception instead of
    an exit code + stderr line.
    """
    if date_prefix and not _DATE_PREFIX_RE.match(date_prefix):
        raise ReviewTrailListError(
            f"{_PROG}: --date-prefix must be YYYY-MM-DD, got: {date_prefix}"
        )

    state_root = _resolve_state_root()
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: List[str]) -> int:
    date_prefix = ""
    print0 = False

    args = list(argv)
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--date-prefix":
            date_prefix = args[i + 1] if i + 1 < len(args) else ""
            i += 2
            continue
        if arg == "--print0":
            print0 = True
            i += 1
            continue
        if arg == "--":
            i += 1
            break
        if arg.startswith("-"):
            print(f"{_PROG}: unknown option: {arg}", file=sys.stderr)
            return 1
        break

    state_root = _resolve_state_root()
    if not state_root:
        print(
            f"{_PROG}: cwd is not a git repo and COORDINATOR_ROOT is not set — "
            "cannot resolve state/review-trail/",
            file=sys.stderr,
        )
        return 1

    live_dir = os.path.join(state_root, "review-trail")
    # Same basename-not-suffix check as _resolve_state_root() above — see
    # that function's comment for why a literal "/state" suffix check
    # misses a native-backslash state_root on Windows.
    if os.path.basename(state_root.rstrip("/\\")) == "state":
        archive_base = os.path.dirname(state_root.rstrip("/\\"))
    else:
        archive_base = state_root
    archive_dir = os.path.join(archive_base, "archive", "review-trail")

    try:
        records = _collect(live_dir) + _collect(archive_dir)
    except OSError as exc:
        print(f"{_PROG}: directory scan failed: {exc}", file=sys.stderr)
        return 1

    if date_prefix:
        if not _DATE_PREFIX_RE.match(date_prefix):
            print(
                f"{_PROG}: --date-prefix must be YYYY-MM-DD, got: {date_prefix}",
                file=sys.stderr,
            )
            return 1
        records = [r for r in records if r[0].startswith(date_prefix)]

    records.sort(key=lambda r: r[0])

    if not records:
        return 0

    # normpath() at the print boundary: state_root may itself carry a mixed
    # separator form (a computed "<override>/state" append literal-joins
    # with "/" onto a native-backslash override on Windows, while a
    # caller-supplied override that already ends in "state" is returned
    # verbatim) — two COORDINATOR_ROOT spellings for the same directory
    # must still print the identical string. Normalizing once, here, at
    # the point the path leaves the process is the single wire boundary
    # for this module (C5 root-cause: os.sep-in-wire-id class).
    if print0:
        for _basename, fullpath in records:
            sys.stdout.write(os.path.normpath(fullpath))
            sys.stdout.write("\0")
    else:
        for _basename, fullpath in records:
            sys.stdout.write(os.path.normpath(fullpath))
            sys.stdout.write("\n")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
