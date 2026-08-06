"""
coordinator_core.ops.workday_complete_backfill_scan — skipped-workday backfill detector.

Port of: workday-complete-backfill-scan.sh (example-doctrine-repo b5a4192c, 2026-07-20)
Spec backlink: docs/plans/2026-07-07-workday-complete-local-day-and-targeted-wrap.md § C3
Spec backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
Spec backlink: docs/plans/2026-07-19-de-machine-backfill-scan-per-day.md § C1

Purpose: `/workday-complete` Step 3.5 and `/workday-start` Step 1.85 both call the
trampoline over this module to detect past days with commits but no daily-summary
record. Emits TSV rows per past day (within `--lookback`) that need backfill
attention.

Daily changelogs are a per-DAY narrative, not per-device (2026-07-19 PM ruling):
this scanner has exactly one row shape, keyed by day only — there is no machine
column and no per-machine fan-out.

  `<YYYY-MM-DD>\\t<commit_count>\\t<baseline_sha>\\t<tip_sha>`

baseline_sha is the parent of that day's OLDEST commit (across the full-day
union span, see below); tip_sha is NEWEST. Rows are emitted OLDEST-FIRST so the
caller backfills chronologically. Empty stdout = no missed days (the common,
healthy case).

Bounded by `--lookback` (default 14 days) so a long-idle repo never triggers an
unbounded historical backfill.

Exit codes (main()):
    0 — success. Includes the healthy empty-output case AND every git-lookup
        failure inside the per-day scan body (those degrade to "skip this row"
        per the faithfully-reproduced bash-oracle `|| true` swallow-failure
        posture, never a hard failure of the whole scan).
    1 — CLI usage error (unknown flag, unexpected positional argument,
        non-positive-integer `--lookback`) OR the process cwd is not a git
        repository and `COORDINATOR_ROOT` is not set. Both are pre-flight
        validation failures, distinct from any in-scan degrade.
This module has no claude-klabauter-transport dependency of its own (it IS the claude-klabauter
module) — the transport-failure code (import/CLAUDE_KLABAUTER_ROOT resolution) lives at
the example-doctrine-repo trampoline layer, which treats that failure as best-effort/
advisory (exit 0, loud stderr) since this scanner only ever feeds a nudge or
an auto-backfill fan-out, never a hard ceremony gate. See the trampoline's own
comment block for its chosen code.

Negative-spec (do NOT "fix" mid-port — faithfully reproduced oracle quirks):
    - `_resolve_state_root_seam` (Rule-5 default-branch resolution, mirroring
      `ops/check_weekly_staleness.py::_resolve_state_root`) ALWAYS re-derives
      from the actual process cwd's git root, never from a `COORDINATOR_ROOT`
      test-only override, UNLESS `COORDINATOR_ROOT` is set — in which case the
      whole seam is bypassed and `<COORDINATOR_ROOT>/state` is used directly.
      This mirrors the bash oracle's documented pre-existing quirk that
      `coordinator_state_root` never reads the script's own `$ROOT` variable.
    - The bare-bash-4-version guard at the top of the `.sh` oracle has no
      Python analog (irrelevant once the logic runs as a plain Python module)
      and is intentionally NOT reproduced here.
    - Does NOT write any file — read-only scan (stdout/stderr only).
    - `_window_args` passes bare (offset-less) `YYYY-MM-DDTHH:MM:SS` strings to
      `git log --after/--before`; git's date parser interprets a timezone-less
      date-time in the INVOKING PROCESS's local system timezone, not UTC. This
      faithfully mirrors the bash oracle it ports, which passes the same bare
      dates to git identically — changing it to a UTC-explicit computation
      would DIVERGE from the oracle. ANTI-SCOPE (see
      docs/plans/2026-07-19-de-machine-backfill-scan-per-day.md Anti-scope):
      do NOT "fix" this seam. (The test suite pins its own process TZ to UTC
      so fixture commits and the window agree deterministically in CI/local
      runs — that's a test-determinism fix, not a change to this seam.)
    - Per-day-coverage residual (2026-07-19 PM ruling — daily changelogs are a
      per-day narrative, not per-machine): under the per-day coverage
      predicate, a day carrying a changelog block AND a daily-summary record
      is "covered," so a concurrent peer whose own unmerged branch never
      wraps that day is NOT re-flagged as a gap, even though that peer's own
      work is itself unrecorded. This is a deliberate consequence of retiring
      per-machine attribution wholesale, not a bug: the prior TM5/TM6
      false-negative
      (see cross-repo/inbox/2026-07-13-claude-central-em-backfill-scan-engine-migration.md
      § Regression oracle) was itself an artifact of per-machine EXCLUSIVITY
      ATTRIBUTION — retiring that attribution wholesale retires the tradeoff
      it existed for. The full-day union span (below) only covers
      fully-skipped days; merged peer work is reached only when the peer's
      branch merged into the union refs (work/*/* ∪ main ∪ HEAD) before the
      wrapping machine ran. The genuine "authored nothing, only pulled a
      peer's work" reconcile-only case is a merge-only day, already excluded
      by `--no-merges`. See also: the 2026-07-19 per-day-changelog memos
      (cross-repo/inbox/2026-07-19-claude-central-em-changelog-de-machine-gap-predicate.md,
      cross-repo/inbox/2026-07-19-claude-central-em-changelog-append-day-de-machine-filename.md),
      the 2026-07-01 multi-machine-coverage blind-spot-1 plan
      (docs/plans/2026-07-01-workday-complete-multi-machine-coverage.md), and
      the 2026-06-30 machine-a incident it documents (a machine's merged work
      dangled uncovered because coverage was checked per-machine).
    - AND-not-OR coverage (2026-08-06 fix, superseding the 2026-07-19 DEC-1
      OR): `_day_covered` requires BOTH a week-changelog block (live under
      `state/week-changelog/` or swept into `archive/week-changelogs/<week>/`,
      see `_has_changelog_block`) AND
      `archive/daily-summaries/<day>*.md` to exist — see `_day_covered`'s own
      docstring for the two live incidents (dangling-link blindness;
      summary-written-first no-op) the OR caused and why the `<day>*.md` glob
      tolerance (still present, still correct) is not the same thing as
      unioning across the two directories. Do NOT re-introduce the OR as a
      "safe superset" — that reasoning is exactly what this fix retires.
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
from datetime import date, timedelta
from typing import List, Optional, Tuple

from coordinator_core._settings_home import settings_home
from coordinator_core.daily_day import local_day as _coordinator_local_day

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_GIT_TIMEOUT = 20

_CLAUDE_KLABAUTER_ROOT_ENV = "CLAUDE_KLABAUTER_ROOT"
_CLAUDE_HOME_ENV = "CLAUDE_HOME"

# ---------------------------------------------------------------------------
# git subprocess helpers
# ---------------------------------------------------------------------------


def _run_git(args: List[str], cwd: Optional[str] = None) -> Optional["subprocess.CompletedProcess[str]"]:
    """Run a git subcommand with a bounded timeout and a closed stdin.

    Every call in this module loops over externally-authored repo state (refs,
    commit history spanning an arbitrary lookback window) — per the porter
    addendum's P1 unbounded-hang rule, EVERY subprocess.run here carries both
    `timeout=` and `stdin=subprocess.DEVNULL`.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_stdout(args: List[str], cwd: Optional[str] = None) -> str:
    """Run git and return stripped stdout, or "" on any failure (mirrors `... || true`)."""
    result = _run_git(args, cwd=cwd)
    if result is None or result.returncode != 0:
        return ""
    return result.stdout.strip()


def _git_lines(args: List[str], cwd: Optional[str] = None) -> List[str]:
    """Run git and return non-empty stdout lines, or [] on any failure."""
    result = _run_git(args, cwd=cwd)
    if result is None or result.returncode != 0:
        return []
    return [ln for ln in result.stdout.splitlines() if ln]


# ---------------------------------------------------------------------------
# state-root seam (self-contained Rule-5 mirror — see negative-spec above)
# ---------------------------------------------------------------------------


def _claude_home() -> str:
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
    impl = _machine_local_impl()
    if not os.path.exists(impl):
        return None
    try:
        result = subprocess.run(
            [sys.executable, impl, "get", key],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def _claude_klabauter_root() -> Optional[str]:
    override = os.environ.get(_CLAUDE_KLABAUTER_ROOT_ENV, "").strip()
    if override:
        return override
    return _machine_local_get("repos.claude_klabauter")


def _same_path(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except OSError:
        return False


def _git_root_of_cwd() -> Optional[str]:
    out = _git_stdout(["rev-parse", "--show-toplevel"])
    return out or None


def _resolve_state_root_seam() -> Optional[str]:
    """Mirror `coordinator_state_root` Rule 5 (bare call, no --central/--subject/
    --artifact) — meta-repo cwd routes to CLAUDE_KLABAUTER_ROOT/state; sibling-repo cwd uses
    <git-root>/state. Always resolves from the actual process cwd, never from any
    COORDINATOR_ROOT override (see module negative-spec)."""
    git_root = _git_root_of_cwd()
    if not git_root:
        return None
    if _same_path(git_root, _claude_home()):
        claude-klabauter = _claude_klabauter_root()
        if claude-klabauter is None:
            return None
        return os.path.join(claude-klabauter, "state")
    return os.path.join(git_root, "state")


# ---------------------------------------------------------------------------
# date arithmetic
# ---------------------------------------------------------------------------


def _date_minus(d: str, n: int) -> str:
    """Return YYYY-MM-DD n days before d, or "" if d is unparseable."""
    try:
        return (date.fromisoformat(d) - timedelta(days=n)).isoformat()
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# per-day coverage predicate
# ---------------------------------------------------------------------------


def _dir_has_day_file(directory: str, day: str) -> bool:
    """True iff a file matching `<day>*.md` exists in `directory`.

    The `<day>*.md` glob is the ONLY tolerance this predicate carries — it
    absorbs the DEC-1 (2026-07-19) filename-shape transition (post-de-machining
    `<day>.md` vs legacy per-machine `<day>-<machine>.md`), both of which name
    the SAME artifact type in the SAME directory. It is not a stand-in for
    checking a different directory.
    """
    return bool(glob.glob(os.path.join(directory, f"{day}*.md")))


def _has_changelog_block(root: str, state_root: Optional[str], day: str) -> bool:
    """True iff a week-changelog block exists for `day`, live OR archived.

    A changelog block does not stay in `state/week-changelog/` — the weekly
    ceremony sweeps the closed week into `archive/week-changelogs/<week-start>/`
    and leaves only the CURRENT week live. Both locations hold the SAME artifact
    type; the union here is a live-vs-archived LOCATION tolerance, exactly like
    `_dir_has_day_file`'s filename-shape tolerance, and is categorically NOT the
    cross-artifact-type OR that `_day_covered` retires. Consulting only the live
    directory would flag every day older than the current week as an
    unrecoverable gap forever (measured 2026-08-06: 11 of 11 days in a 14-day
    lookback, every one of them fully recorded).

    Archived blocks resolve under `root`, so they are consulted even when
    `state_root` is unresolvable — an existing archived block is positive
    evidence of coverage, and suppressing it would manufacture a gap rather than
    fail safe.
    """
    if state_root and _dir_has_day_file(os.path.join(state_root, "week-changelog"), day):
        return True
    archive_root = os.path.join(root, "archive", "week-changelogs")
    return bool(glob.glob(os.path.join(archive_root, "*", f"{day}*.md")))


def _day_covered(root: str, state_root: Optional[str], day: str) -> bool:
    """AND coverage predicate (2026-08-06 fix, replacing the 2026-07-19 DEC-1 OR):
    a day is "covered" only when BOTH a changelog block (live in
    `state/week-changelog/` or swept into `archive/week-changelogs/<week>/`, see
    `_has_changelog_block`) AND a daily-summary record
    (`archive/daily-summaries/`) exist for it, each tolerant of the `<day>*.md`
    filename-shape transition (see `_dir_has_day_file`).

    Why the prior OR was wrong: `state/week-changelog/` and
    `archive/daily-summaries/` are two DIFFERENT artifact types (a per-day
    narrative block vs. a per-day analyst summary), not two filename shapes of
    the same artifact — DEC-1's tolerance was meant for the latter (see
    `_dir_has_day_file`) and got over-applied to the former. Unioning across
    them let either artifact alone mask the other's absence: (1) a
    week-changelog block whose own `Links:` line named a
    `archive/daily-summaries/<day>-<machine>.md` file that was never written
    (dangling-link blindness — the OR saw the changelog block and called the
    day covered without the linked summary ever existing); (2) writing the
    daily summary before Step 6 Phase B ran flipped the archive/ side of the OR
    true, so `workday-complete-close backfill-dispatch-rows` silently no-opped
    and the changelog block never landed, making Step 6 -> Step 6b ordering
    load-bearing for a reason nobody could see. Both are the same root bug: OR
    across artifact types papers over exactly the gap this scanner exists to
    catch. See the 2026-08-06 incident writeup this fix responds to.

    Legitimately summary-less days (Step 7's `skip_no_new_work` disposition —
    see `coordinator_core/workday_complete/brief.py` `jp_step4b_analyst_dispatch`)
    are NOT a false-positive risk under AND: that disposition requires zero new
    commits for the day, and `_scan_day` below only calls this predicate before
    `_scan_full_day_union` — a day with zero commits in the full-day union ref
    span never gets a row appended regardless of what this predicate returns
    (see `_scan_full_day_union`'s early `if not entries: return`). The AND
    tightening only bites on days that DID have commits, which is exactly the
    class Step 7 does not skip.

    `state_root` unresolvable (`None`, see `_resolve_state_root_seam`): treated
    as "the LIVE changelog directory cannot be consulted" rather than "changelog
    side skipped" — a day with real commits, no archived block, and an
    unresolvable state_root is reported as a gap, never silently swallowed.
    Under-reporting a gap is the failure mode this fix exists to close;
    over-reporting one when the seam itself is broken is the safe direction.
    """
    summary_dir = os.path.join(root, "archive", "daily-summaries")
    return _has_changelog_block(root, state_root, day) and _dir_has_day_file(summary_dir, day)


# ---------------------------------------------------------------------------
# full-day union commit-window collection (AC3/DEC-3)
# ---------------------------------------------------------------------------


def _window_args(prev_day: str, day: str) -> List[str]:
    """Return the `--after`/`--before` args bounding `(prev_day 23:59:59, day
    23:59:59]` for `day`. Bare offset-less date-times — see module negative-spec
    for the accepted host-local-TZ oracle-fidelity quirk this implies."""
    return ["--after", f"{prev_day}T23:59:59", "--before", f"{day}T23:59:59"]


def _collect_union_refs(root: str) -> List[str]:
    """Enumerate the full-day union ref set (AC3/DEC-3): local `work/*/*` heads +
    `origin/work/*/*` remotes + `main` (local + origin) via a single
    `for-each-ref` sweep, PLUS the literal checked-out `HEAD` ref. HEAD is
    included IN THE UNION (not a separate fallback path) so the no-work-refs /
    checked-out-non-work-branch case preserves reach WITHOUT `git log --all`
    (ANTI-SCOPE forbids `--all`)."""
    refs = _git_lines(
        [
            "-C",
            root,
            "for-each-ref",
            "--format=%(refname)",
            "refs/heads/work/*/*",
            "refs/remotes/origin/work/*/*",
            "refs/heads/main",
            "refs/remotes/origin/main",
        ]
    )
    refs.append("HEAD")
    return refs


def _tip_and_oldest(entries: List[Tuple[int, str]]) -> Tuple[Optional[str], Optional[str]]:
    """Mirror the bash sort|awk dedup-and-pick idiom: tip = sha of the
    highest-ct entry, oldest = sha of the lowest-ct entry (ties keep
    original/insertion order, matching a stable sort)."""
    if not entries:
        return None, None
    tip = max(entries, key=lambda e: e[0])[1]
    oldest = min(entries, key=lambda e: e[0])[1]
    return tip, oldest


def _rev_parse_parent(root: str, sha: str) -> str:
    """Parent of sha, or sha itself on failure (root-commit fallback)."""
    out = _git_stdout(["-C", root, "rev-parse", f"{sha}^"])
    return out or sha


def _scan_full_day_union(root: str, day: str, prev_day: str, out: List[str]) -> None:
    """Full-day union span (AC3/DEC-3): union of `git log --no-merges` across
    work/*/* ∪ main ∪ HEAD (local + origin), deduped by SHA. base = parent of
    the oldest commit, tip = newest, count = unique non-merge commit count."""
    refs = _collect_union_refs(root)
    if not refs:
        return
    lines = _git_lines(
        [
            "-C",
            root,
            "log",
            "--no-merges",
            "--format=%ct %H",
            *_window_args(prev_day, day),
            *refs,
        ]
    )
    if not lines:
        return

    seen: set = set()
    entries: List[Tuple[int, str]] = []
    for ln in lines:
        parts = ln.split(" ", 1)
        if len(parts) != 2:
            continue
        ts_str, sha = parts
        if sha in seen:
            continue
        seen.add(sha)
        try:
            ts = int(ts_str)
        except ValueError:
            # Malformed timestamp field in this log line -- drop just this entry,
            # not the whole day's union.
            continue
        entries.append((ts, sha))

    if not entries:
        return

    tip, oldest = _tip_and_oldest(entries)
    if tip is None or oldest is None:
        return
    base = _rev_parse_parent(root, oldest)
    out.append(f"{day}\t{len(entries)}\t{base}\t{tip}")


# ---------------------------------------------------------------------------
# per-day scan
# ---------------------------------------------------------------------------


def _scan_day(
    root: str,
    state_root: Optional[str],
    day: str,
    prev_day: str,
    out: List[str],
) -> None:
    """Emit exactly one row for `day` if it is uncovered (per `_day_covered`),
    else emit nothing. No per-machine fan-out (2026-07-19 per-day PM ruling)."""
    if _day_covered(root, state_root, day):
        return
    _scan_full_day_union(root, day, prev_day, out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: List[str]) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """Returns (lookback, today_override, error_message). error_message set means
    usage error (exit 1)."""
    lookback = 14
    today_override = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--lookback":
            if i + 1 >= len(argv):
                return None, None, "--lookback needs a value"
            raw = argv[i + 1]
            # Bash oracle: [[ "$LOOKBACK" =~ ^[0-9]+$ ]] — unsigned decimal digits
            # only; "-5"/"+5"/"3.5" are all rejected. Regex-gate BEFORE int() so a
            # leading '+' (which int() would silently accept) is faithfully rejected.
            if not re.match(r"^[0-9]+$", raw):
                return None, None, f"--lookback must be a positive integer (got '{raw}')"
            lookback = int(raw)
            i += 2
        elif arg == "--today":
            if i + 1 >= len(argv):
                return None, None, "--today needs a value"
            raw = argv[i + 1]
            # --- Tier 2 (behaviour change -- PM sign-off required) ---
            # A malformed --today silently degraded the scan into a no-op: every
            # `_date_minus` call on an unparseable base date returns "" (see
            # `_date_minus` docstring), which makes the main loop's `if not day:
            # continue` skip EVERY iteration -- the tool exits 0 having scanned
            # zero days, reporting "no backfill gaps found" when it never looked.
            # Regex-gate at parse time (mirrors the --lookback idiom above) so
            # malformed input is a usage error, not a silent empty scan.
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
                return None, None, f"--today must be YYYY-MM-DD (got '{raw}')"
            try:
                date.fromisoformat(raw)
            except ValueError:
                return None, None, f"--today must be a valid YYYY-MM-DD date (got '{raw}')"
            today_override = raw
            # --- end Tier 2 ---
            i += 2
        elif arg.startswith("-"):
            return None, None, f"Unknown option: {arg}"
        else:
            return None, None, f"Unexpected argument: {arg}"
    if lookback < 1:
        return None, None, f"--lookback must be a positive integer (got '{lookback}')"
    return lookback, today_override, None


def main(argv: List[str]) -> int:
    lookback, today_override, err = _parse_args(argv)
    if err is not None:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    assert lookback is not None

    coordinator_root = os.environ.get("COORDINATOR_ROOT", "").strip()
    warn_suppress = os.environ.get("COORDINATOR_ROOT_WARN_SUPPRESS", "") == "1"

    cwd_top = _git_root_of_cwd()
    if cwd_top:
        try:
            cwd_top = os.path.realpath(cwd_top)
        except OSError:
            # Same fallback as cr_real below: keep the unresolved path rather than fail.
            pass

    if coordinator_root and not warn_suppress and cwd_top:
        try:
            cr_real = os.path.realpath(coordinator_root)
        except OSError:
            cr_real = coordinator_root
        if os.path.normcase(cr_real) != os.path.normcase(cwd_top):
            print(
                f"WARNING: COORDINATOR_ROOT='{coordinator_root}' differs from the cwd "
                f"git toplevel '{cwd_top}'. COORDINATOR_ROOT is a TEST-ONLY override; "
                "continuing with it as the repo root for this run. If this is a live "
                "ceremony on a consumer project, unset COORDINATOR_ROOT and re-run "
                "(COORDINATOR_ROOT_WARN_SUPPRESS=1 silences this in tests).",
                file=sys.stderr,
            )

    root = coordinator_root or cwd_top
    if not root:
        print("ERROR: cwd is not a git repo and COORDINATOR_ROOT is not set", file=sys.stderr)
        return 1

    if coordinator_root:
        state_root: Optional[str] = os.path.join(root, "state")
    else:
        state_root = _resolve_state_root_seam()

    today = today_override or _coordinator_local_day()

    out: List[str] = []

    i = lookback
    while i >= 1:
        day = _date_minus(today, i)
        i -= 1
        if not day:
            continue
        prev_day = _date_minus(day, 1)
        if not prev_day:
            continue
        _scan_day(root, state_root, day, prev_day, out)

    for line in out:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
