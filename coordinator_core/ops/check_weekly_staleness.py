"""
coordinator_core.ops.check_weekly_staleness — workday/workweek cadence
staleness predicate.

Purpose: `/workday-complete` Step 10 reads this to decide whether to nudge the
PM toward `/workweek-complete`. Reads `state/week-changelog/HEADER.md` to
extract the prior-week reset commit SHA and the "Week starting" date, then
computes two independent staleness dimensions against that reset point:

    - commit distance: `git rev-list --count <reset-sha>..HEAD`
    - calendar distance: today - "Week starting" date, in days

Verdicts (printed as the sole stdout line):
    STALE    — both thresholds crossed (>= 5 days AND >= 15 commits)
    MILD     — exactly one threshold crossed
    FRESH    — neither threshold crossed
    UNKNOWN  — HEADER.md absent/unparseable, cwd outside a git repo, the
               recorded reset SHA doesn't resolve to a commit in this repo,
               or the "Week starting" date is missing/a placeholder/
               unparseable (treated as never-run)

Exit code: always 0 (informational — callers decide whether to surface the
signal). Parity-critical: never sys.exit(1) or raise on a resolution
failure, mirroring the bash oracle's `set -euo pipefail` short-circuits that
always route to an explicit `echo UNKNOWN; exit 0`.

``main(argv)`` accepts an optional explicit ``--root <path>`` (or ``--root=<path>``)
argument that, when present, is used verbatim as the state root — bypassing
``_resolve_state_root()``'s cwd-based ``git rev-parse`` entirely. This is the AC-5
no-implicit-cwd bridge for in-process callers (``routine_signals.py``) that must not
depend on process cwd; absent ``--root``, behaviour is unchanged (cwd-based
resolution, as documented below).

Port of: check-weekly-staleness.sh (example-doctrine-repo b5a4192c, 2026-07-20)
Spec backlink: archive/specs/2026-05-04-workweek-cadence-split.md § Trigger Doctrine
               docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
               state/improvement-queue/2026-07-21-routine-signals-ac5-gate-vs-dr079-inprocess-design-conflict.yaml

Negative-spec:
    - Does NOT modify any file, does NOT trigger /workweek-complete, does NOT
      read daily changelog files — HEADER.md only.
    - Does NOT resolve state root via the coordinator-state-root.sh bash
      seam directly — self-contains the DR-047/stop-the-rot Rule-5
      default-branch resolution (bare `coordinator_state_root()`, no
      --central/--subject/--artifact): meta-repo cwd (git root ==
      CLAUDE_HOME) routes to CLAUDE_KLABAUTER_ROOT/state; any other (sibling-repo)
      cwd uses <git-root>/state directly. Mirrors the sibling pattern in
      ops/check_arch_audit_staleness.py::_resolve_state_root (same shape,
      independently duplicated per that module's own documented rationale —
      no shared helper exists yet).
    - Does NOT case-interpolate $MACHINE or branch names — reads only
      commit SHAs and dates from HEADER.md (see the original bash header's
      the Staff Engineer R2 F-R2-2 case-sensitivity audit note).
    - Reproduces a pre-existing oracle behaviour faithfully: an unresolvable
      `git rev-list --count` (e.g. the reset SHA/HEAD relationship errors)
      falls back to a commit distance of 0, exactly as the bash oracle's
      `|| echo 0` fallback does — this is NOT escalated to UNKNOWN.
"""

from __future__ import annotations

import os
import re
import subprocess
from coordinator_core.win_portability import no_console_creationflags, same_path
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import settings_home

DAY_THRESHOLD = 5
COMMIT_THRESHOLD = 15

_PRIOR_WEEK_RELEASED_RE = re.compile(r"^\*\*Prior week released:\*\*")
_SHA_EXTRACT_RE = re.compile(r"commit ([a-f0-9]{5,40})")
_WEEK_STARTING_RE = re.compile(r"^\*\*Week starting:\*\*")
_DATE_EXTRACT_RE = re.compile(r"\*\* *(\d{4}-\d{2}-\d{2})")

_CLAUDE_KLABAUTER_ROOT_ENV = "CLAUDE_KLABAUTER_ROOT"
_CLAUDE_HOME_ENV = "CLAUDE_HOME"
_STATE_ROOT_OVERRIDE_ENV = "CWS_TEST_STATE_ROOT"


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


def _git_root(cwd: Optional[str] = None) -> Optional[str]:
    """Resolve a git repo root, or None if not in one.

    ``cwd``, if given, pins the git invocation's working directory explicitly
    instead of relying on process-global cwd — the AC-5 no-implicit-cwd bridge
    for in-process callers (``routine_signals._resolve_coordinator_state_root``)
    that must resolve a root OTHER than the current process cwd without
    mutating process-global state. Existing bare callers (``_git_root()``) are
    unaffected: ``cwd=None`` is a legal ``subprocess.run`` keyword meaning
    "inherit the caller's cwd", identical to the previous implicit behaviour.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=cwd,
            **no_console_creationflags(),
        )
    except OSError:
        print(f"skip: _git_root: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return root or None


def _parse_root_arg(argv) -> Optional[str]:
    """Extract an explicit ``--root <path>`` value from *argv*, or None if absent.

    When present, the caller (``routine_signals.py``) has already resolved the
    correct state root itself via an explicit-cwd ``_git_root(cwd=...)`` call and
    hands it here directly — ``main()`` uses it verbatim, skipping
    ``_resolve_state_root()``'s cwd-based ``git rev-parse`` entirely (AC-5:
    no-implicit-cwd). This keeps the in-process callable contract
    (``main(argv) -> int``) DR-079 chose, rather than reintroducing a
    subprocess-per-call boundary.

    This function performs zero validation of the extracted value — no strip,
    no blank-check; it is returned exactly as it appeared in argv. ``main()``
    is what treats a blank/whitespace-only value as equivalent to absent.
    """
    if not argv:
        return None
    for i, arg in enumerate(argv):
        if arg == "--root" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--root="):
            return arg.split("=", 1)[1]
    return None


def _resolve_state_root() -> Optional[str]:
    """Resolve the coordinator state root — mirrors `coordinator_state_root`
    (Rule 5: bare call, no flags) from coordinator-state-root.sh (example-doctrine-repo 6fb5fb37, 2026-07-22).

    Test seam: CWS_TEST_STATE_ROOT, if set, is returned verbatim (bypasses
    git/meta-repo resolution entirely) — used by the co-located pytest.
    """
    override = os.environ.get(_STATE_ROOT_OVERRIDE_ENV)
    if override:
        return override

    git_root = _git_root()
    if not git_root:
        return None

    if _same_path(git_root, _claude_home()):
        claude_klabauter_root = _claude_klabauter_root()
        if claude_klabauter_root is None:
            return None
        # pathlib join (not os.path.join) — os.path.join left a mixed
        # separator form ('/claude-klabauter/root\state') when `claude_klabauter_root` came back
        # forward-slash-rooted from a resolver but the join used os.sep;
        # Path(...) / "state" renders consistently under the platform's own
        # separator end to end (C5 root-cause: os.sep-in-wire-id class).
        return str(Path(claude_klabauter_root) / "state")

    return os.path.join(git_root, "state")


def _extract_reset_sha(header_text: str) -> str:
    """Extract the reset commit SHA from the first `**Prior week released:**` line."""
    for raw_line in header_text.splitlines():
        if _PRIOR_WEEK_RELEASED_RE.match(raw_line):
            match = _SHA_EXTRACT_RE.search(raw_line)
            return match.group(1) if match else ""
    return ""


def _extract_week_start(header_text: str) -> str:
    """Extract the raw week-starting field value from the first `**Week starting:**` line."""
    for raw_line in header_text.splitlines():
        if _WEEK_STARTING_RE.match(raw_line):
            return raw_line
    return ""


def _sha_exists(sha: str, cwd: Optional[str] = None) -> bool:
    """Verify the SHA resolves to a commit object in the repo at *cwd*.

    ``cwd``, if given, pins the git invocation to that repo explicitly instead
    of relying on process-global cwd — closes the gap where the resolved
    ``state_root`` steered *which HEADER.md* is read but the git-derived facts
    (SHA existence, commit distance) still ran against ambient cwd (AC-5
    no-implicit-cwd, cross-file finding from the routine_signals.py slice
    review). ``cwd=None`` preserves the previous implicit-cwd behaviour for
    callers that don't pass an explicit root.
    """
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
            text=True,
            cwd=cwd,
            **no_console_creationflags(),
        )
    except OSError:
        print(f"skip: _sha_exists: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return result.returncode == 0


def _commit_distance(sha: str, cwd: Optional[str] = None) -> int:
    """git rev-list --count <sha>..HEAD, falling back to 0 on any failure
    (mirrors the bash oracle's `|| echo 0` fallback — NOT escalated to UNKNOWN).

    ``cwd``, if given, pins the git invocation to that repo explicitly — see
    ``_sha_exists`` docstring for the AC-5 rationale.
    """
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{sha}..HEAD"],
            capture_output=True,
            text=True,
            cwd=cwd,
            **no_console_creationflags(),
        )
    except OSError:
        print(f"skip: _commit_distance: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return 0
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        print(f"skip: _commit_distance: return int(result.stdout.strip()) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return 0


def _compute_staleness(header_text: str, today: Optional[date] = None, root: Optional[str] = None) -> str:
    """Pure predicate over HEADER.md's text content. Returns STALE/MILD/FRESH/UNKNOWN.

    ``root``, if given, pins the SHA-existence and commit-distance git calls to
    that repo explicitly (AC-5 no-implicit-cwd) rather than ambient process
    cwd — this is the repo the resolved ``state_root`` belongs to, which is
    not necessarily the same repo as process cwd. ``root=None`` preserves the
    previous implicit-cwd behaviour (callers that never resolved an explicit
    root, e.g. direct CLI invocation with cwd already inside the target repo).
    """
    reset_sha = _extract_reset_sha(header_text)
    week_start_line = _extract_week_start(header_text)
    week_start_match = _DATE_EXTRACT_RE.search(week_start_line)
    week_start = week_start_match.group(1) if week_start_match else ""

    if not reset_sha or not week_start or "not yet set" in week_start_line:
        return "UNKNOWN"

    if not _sha_exists(reset_sha, cwd=root):
        return "UNKNOWN"

    commit_distance = _commit_distance(reset_sha, cwd=root)

    if today is None:
        today = date.today()
    try:
        week_start_date = datetime.strptime(week_start, "%Y-%m-%d").date()
    except ValueError:
        print(f"skip: _compute_staleness: week_start_date = datetime.strptime(week_start, \"%Y-%m-%d\").date() failed: {sys.exc_info()[1]}", file=sys.stderr)
        return "UNKNOWN"

    day_distance = (today - week_start_date).days
    if day_distance < 0:
        day_distance = 0

    days_crossed = day_distance >= DAY_THRESHOLD
    commits_crossed = commit_distance >= COMMIT_THRESHOLD

    if days_crossed and commits_crossed:
        return "STALE"
    if days_crossed or commits_crossed:
        return "MILD"
    return "FRESH"


def main(argv) -> int:
    state_root = _parse_root_arg(argv)
    if not state_root:
        # A blank/whitespace-only --root value (e.g. "--root ""), like an absent
        # --root, must NOT fall through to Path("") — that resolves relative to
        # ambient process cwd, silently reopening the implicit-cwd dependency
        # this --root bridge exists to eliminate. Route it through the same
        # cwd-based resolution an absent --root gets.
        state_root = _resolve_state_root()
    if not state_root:
        print("UNKNOWN")
        return 0

    header_path = Path(state_root) / "week-changelog" / "HEADER.md"
    if not header_path.is_file():
        print("UNKNOWN")
        return 0

    try:
        header_text = header_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        print("UNKNOWN")
        return 0

    verdict = _compute_staleness(header_text, root=state_root)
    print(verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
