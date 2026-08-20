"""
coordinator_core.ops.check_arch_audit_staleness — rotational architecture-audit
staleness predicate.

Purpose: /workweek-complete Step 7.6 reads this to decide whether to auto-fold
a targeted-on-diff architecture audit. Reads the `Last targeted audit` clock
from the health ledger and reports STALE / FRESH / UNKNOWN.

Verdicts (printed as the sole stdout line):
    STALE    — > 10 days since the last targeted audit (auto-fold the audit)
    STALE    — the field is present but unset (placeholder/"none") — never
               targeted-audited but the ledger exists → overdue
    FRESH    — <= 10 days since the last targeted audit
    UNKNOWN  — health-ledger.md absent, the `Last targeted audit` line absent,
               or its date present-but-unparseable (caller decides; do NOT
               auto-fold on UNKNOWN)

Exit code: always 0 (informational — callers decide whether to surface the
signal). Parity-critical: never sys.exit(1) or raise on a resolution failure,
mirroring the bash oracle's `set -euo pipefail` short-circuits that always
route to an explicit `echo ...; exit 0`.

IMPORTANT — clock separation: this module reads `Last targeted audit`, NOT
`Last full audit`. The two clocks are distinct by design (a folded
targeted-on-diff audit updates ONLY `Last targeted audit`; only a genuine
PM-invoked /architecture-survey updates `Last full audit`). Reading the wrong
field would either fire indefinitely or mask the real survey gap.

``main(argv)`` accepts an optional explicit ``--root <path>`` (or ``--root=<path>``)
argument that, when present, is used verbatim as the state root — bypassing
``_resolve_state_root()``'s cwd-based ``git rev-parse`` entirely. This is the AC-5
no-implicit-cwd bridge for in-process callers (``routine_signals.py``) that must not
depend on process cwd; absent ``--root``, behaviour is unchanged (cwd-based
resolution, as documented below).

Port of: check-arch-audit-staleness.sh (DoE b5a4192c, 2026-07-20)
Spec backlink: docs/plans/2026-05-23-weekly-gate-restructure-and-arch-survey-audit-rename.md § Strand 3b
               docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
               state/improvement-queue/2026-07-21-routine-signals-ac5-gate-vs-dr079-inprocess-design-conflict.yaml

Negative-spec:
    - Does NOT modify any file, does NOT trigger /architecture-audit or
      /workweek-complete, does NOT read the atlas — health-ledger.md only.
    - Does NOT resolve state root via the shared coordinator_state_root seam
      module (none exists yet in claude-klabauter — queued:
      state/improvement-queue/2026-07-06-claude-klabauter-root-shared-helper-extraction.yaml).
      Self-contains the DR-047/stop-the-rot Rule-5 default-branch resolution
      (bare `coordinator_state_root()`, no --central/--subject/--artifact):
      meta-repo cwd (git root == CLAUDE_HOME) routes to the engine root/state;
      any other (sibling-repo) cwd uses <git-root>/state directly. Mirrors
      the pattern already used ad hoc in ops/queue_append.py._output_path.
    - Reproduces a pre-existing oracle quirk faithfully: a present-but-
      unparseable-as-YYYY-MM-DD field value (e.g. the literal placeholder
      "(none)", or free text) is treated as STALE, not UNKNOWN — only a
      value that MATCHES the YYYY-MM-DD shape but fails calendar validation
      (e.g. "2026-13-45") falls through to UNKNOWN. This mirrors the bash
      oracle's two-stage regex-then-`date`-parse structure exactly; it is
      not "fixed" here even though the STALE/UNKNOWN split for malformed
      input looks inconsistent at first read.
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
from coordinator_core.git.repo_root import show_toplevel

STALENESS_THRESHOLD_DAYS = 10

_LAST_TARGETED_AUDIT_RE = re.compile(r"^\*\*Last targeted audit:\*\*")
_DATE_EXTRACT_RE = re.compile(r"\*\* *(\d{4}-\d{2}-\d{2})")

_CLAUDE_KLABAUTER_ROOT_ENV = "CLAUDE_KLABAUTER_ROOT"
_CLAUDE_HOME_ENV = "CLAUDE_HOME"
_STATE_ROOT_OVERRIDE_ENV = "CAAS_TEST_STATE_ROOT"


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
    correct here since this call site only checks "is repo_root the meta-repo
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
    return show_toplevel(cwd)


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
    (Rule 5: bare call, no flags) from coordinator/lib/coordinator-state-root.sh.

    Test seam: CAAS_TEST_STATE_ROOT, if set, is returned verbatim (bypasses
    git/meta-repo resolution entirely) — used by the co-located pytest and by
    this module's own parity-test fixtures.
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


def _compute_staleness(ledger_path: Path, today: Optional[date] = None) -> str:
    """Pure predicate over a health-ledger.md path. Returns STALE/FRESH/UNKNOWN."""
    if not ledger_path.is_file():
        return "UNKNOWN"

    line = None
    try:
        with ledger_path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                if _LAST_TARGETED_AUDIT_RE.match(raw_line):
                    line = raw_line.rstrip("\n")
                    break
    except OSError:
        print(f"skip: _compute_staleness: with ledger_path.open(\"r\", encoding=\"utf-8\", errors=\"replace\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return "UNKNOWN"

    if line is None:
        return "UNKNOWN"

    date_match = _DATE_EXTRACT_RE.search(line)
    if not date_match:
        # Field present but no parseable date (placeholder / "none" / free
        # text). The ledger exists but no targeted audit has ever been
        # recorded → overdue. (Negative-spec: deliberately STALE, not
        # UNKNOWN — see module docstring.)
        return "STALE"

    last_date_str = date_match.group(1)
    try:
        last_date = datetime.strptime(last_date_str, "%Y-%m-%d").date()
    except ValueError:
        # Shape matched YYYY-MM-DD but the calendar date itself is invalid
        # (e.g. 2026-13-45) — mirrors the bash oracle's `date -j`/`date -d`
        # parse failure branch.
        return "UNKNOWN"

    if today is None:
        today = date.today()

    day_distance = (today - last_date).days
    if day_distance < 0:
        day_distance = 0

    return "STALE" if day_distance > STALENESS_THRESHOLD_DAYS else "FRESH"


def main(argv) -> int:
    state_root = _parse_root_arg(argv)
    if not state_root:
        # A blank/whitespace-only --root value (e.g. "--root ""), like an absent
        # --root, must NOT fall through to Path("") — that resolves relative to
        # ambient process cwd, silently reopening the implicit-cwd dependency
        # this --root bridge exists to eliminate. Route it through the same
        # cwd-based resolution an absent --root gets. Applied identically to
        # check_weekly_staleness.py to preserve parity.
        state_root = _resolve_state_root()
    if not state_root:
        print("UNKNOWN")
        return 0

    ledger_path = Path(state_root) / "health-ledger.md"
    verdict = _compute_staleness(ledger_path)
    print(verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
