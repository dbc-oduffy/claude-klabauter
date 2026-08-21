"""
coordinator_core.engine_root_census -- durable sink for engine-root fallback reads.

Purpose: C14's hard precondition item 4 -- *"no site has read the fallback in
N days"* -- asks for evidence of ABSENCE. C10's hook (AC24 clause 1,
discharged at 5a2ff0198) prints to stderr and nothing persists it, so the
only durable capture was session transcripts, where an emission lands only
if its stderr happened to flow into a recorded tool result. That surface
systematically UNDERCOUNTS: safe for the non-zero verdict measured on
2026-08-20 (26 genuine reads, N = 0 days), worthless for the zero reading
C14 actually needs, because a future zero would be indistinguishable from
"no emission was captured". This module is the durable half -- every
fallback read the accessor observes is appended here, so a later census
reads "has any site read the fallback, and when" off DISK EVIDENCE.

Spec backlink: docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md,
               chunk C14 item 4 / AC24 clause 2.
Bug row closed: state/bug-backlog/2026-08-20-ac24-s-evidence-clause-is-unsatisfiable-96c02228e7b3.yaml
               (fix 1 of the two that row declined to pick between: give the
               hook a durable append-only sink so absence becomes observable).

THE SERIES-AGE GUARD IS THE WHOLE POINT, and it is what `series_present`
alone does not buy. `coordinator_core.ops.shim_usage_census` already learned
half of this lesson (its docstring: `invoked=False, series_present=False`
means "we have not been watching yet", not "we watched and saw nothing").
Absence-over-a-WINDOW needs one rung more: a sink created an hour ago reads
zero over a 7-day window and would licence closing the dual-read window on
its first day of existence. So `evidences_absence` is False unless the
series has itself been observing for at least the full window. A census
that cannot distinguish "quiet" from "new" is the same defect this module
was built to remove, one layer down.

Where the series lives: `<settings-home>/telemetry/engine-root-fallback-census.jsonl`.
Settings-home, NOT a repo `state/` dir, and the choice is load-bearing: the
accessor fires in any process on this box -- the live tree, the published
mirror, a sibling repo, a detached agent -- and a repo-anchored sink would
scatter the series across roots, so a reader anchored on claude-klabauter would see a
partial series and read the gap as quiet. `_settings_home.settings_home()`
resolves fleet-wide, machine-local, with zero external calls and a Windows
rung that a bare `$HOME` read does not have. The sink is machine-local
telemetry and is NOT committed (see `.gitignore`), matching
`state/shim-usage-census.jsonl`'s precedent.

Cheap by construction -- the accessor sits on the `scoped-git-commit` hot
path, which is where all 26 of the measured reads came from:
    - The write is already deduped once per site per process by
      `engine_root._ENGINE_ROOT_FALLBACK_EMITTED`, so a process pays at
      most a handful of appends for its whole lifetime, not one per read.
    - No `coordinator_core.ops` import ANYWHERE in this module, and this
      module deliberately does not live under that package. Importing
      `coordinator_core.ops.<anything>` triggers `_eager_import_all` over
      ~206 op modules; paying that on an engine-root read would tax the
      exact path this plan exists to unload. Imports here are stdlib plus
      `coordinator_core._settings_home`, which is bootstrap-safe and makes
      zero external calls.
    - No subprocess spawn, no lock. `open(path, "a")`'s append-mode write
      is what POSIX and NTFS both guarantee is atomic for a SINGLE
      `write()` sized well under the atomic-append boundary (~100 bytes
      here, far under the 4KiB PIPE_BUF-class guarantee both platforms
      honour for local disk), so concurrent writers each get a complete
      line without a lock. Same pattern as
      `coordinator_core.telemetry.cost_census._append_row` and
      `state/raw-cmdline-transport-ledger.jsonl`; not invented here.

Failure discipline (negative-spec): `record_fallback_read()` NEVER raises,
under any failure -- unwritable sink, unresolvable settings home, disk
full, encoding error, anything. It is called from an observability hook on
the commit hot path; a census that can throw there turns every ceremony on
a 50-70-concurrent-session box into an outage, and not recording is always
the better failure. The read side never raises either: a missing or corrupt
series degrades to "never observed", which `evidences_absence` then refuses
to treat as evidence.

THE SERIES CAN ONLY FAIL SAFE, and that is why there is no suppression
mechanism here. A spurious row — an ad-hoc validation run exercising the
accessor by hand, say — makes `reads_in_window` non-zero and
`evidences_absence` False. It can therefore only ever DELAY C14, never
licence it. Building a kill-switch to keep such reads out would add cost on
the commit hot path to prevent an error that already points the safe way,
and would replace an artifact-enforced property with an operator-remembers
one. The test suite is separately incapable of reaching the live sink:
`conftest._quarantine_real_home` is autouse and redirects the home the
settings-home resolver reads, so a suite run writes into its own tmp dir.
Verified 2026-08-20 by running the accessor suite and confirming the live
series stayed absent.

C24 -- THE WATCH-START SENTINEL. Code review 2026-08-20 caught the defect
one layer down: `observed_days` was measured from `series_first_ts`, the
first RECORDED READ, so a box that never had a stale pin never created the
series and `series_present` stayed False forever -- `evidences_absence`
could never become True on a box that converged cleanly, which is the same
failure mode this module's own docstring warned about ("a future zero would
be indistinguishable from no emission was captured"), recurring in the
guard built to remove it. `ensure_watch_start()` fixes this with a SIBLING
FILE (`watch_start_path()`), not a row in the jsonl series: a sentinel row
would need every reader of the series to recognize and skip it, and getting
that wrong either counts it as a read (inverting the fix) or drops it into
`unparsable_rows`/`undatable_rows` (tripping the corruption guard below for
a reason that isn't corruption). A separate file makes both failure modes
structurally unreachable. The clock starts at whichever happens first --
`record_fallback_read` (using that read's own timestamp, so a box that DID
see a stale pin keeps its `observed_days` measured from the earliest read)
or `census()` itself (using its own call time, so a box that never sees a
stale pin still starts a clock the first time anyone asks for a verdict).
Creation is `O_CREAT | O_EXCL` -- lock-free, race-safe: the loser of a
concurrent create just reads the winner's timestamp back.

Negative-spec:
    - Never imports anything under `coordinator_core.ops`.
    - Never spawns a subprocess and never takes a lock.
    - Never truncates or rewrites the series -- strictly append-only.
    - Never raises out of `record_fallback_read()` for any reason.
    - `evidences_absence` is NEVER True on a watch younger than the
      window, however quiet that watch has been.
    - The watch-start sentinel is NEVER counted in `total_reads`,
      `reads_in_window`, or `sites`, and NEVER lands in `unparsable_rows`
      or `undatable_rows` -- it lives in a separate file from the series,
      so the series-parsing loop never sees it at all.
    - Does not decide N. C14's exit picks the window; this module reports
      against whatever window it is handed and echoes it back in the
      report so a reader cannot lose track of which N was asked.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core._settings_home import settings_home

#: Seconds per day, named so the window arithmetic below reads as intended.
_SECONDS_PER_DAY = 86400.0

#: Default window in days when a caller does not name one. Not a ruling on
#: C14's N -- C14's exit names its own, and every report echoes the value it
#: actually used.
DEFAULT_WINDOW_DAYS = 7


def series_path(sink_root: Optional[Path] = None) -> Path:
    """Resolve the series file. `sink_root` overrides the settings home
    (tests, and a caller deliberately scoping to another root)."""
    root = Path(sink_root) if sink_root is not None else settings_home()
    return root / "telemetry" / "engine-root-fallback-census.jsonl"


def watch_start_path(sink_root: Optional[Path] = None) -> Path:
    """Resolve the watch-start sentinel file.

    A SIBLING FILE, deliberately not a row inside the jsonl series (C24).
    The series-parsing loop in `census()` has no `kind` discriminator and
    was never meant to need one; a sentinel that lived as a row would
    require every future reader to remember to skip it, and getting that
    wrong either counts the sentinel as a read (inverts the fix -- the sink
    would then never evidence absence) or drops it into
    `unparsable_rows`/`undatable_rows` (trips the 2026-08-20 corruption
    guard and blocks the verdict for a reason that isn't corruption). A
    separate file makes both failure modes structurally unreachable instead
    of relying on every reader remembering a rule.
    """
    root = Path(sink_root) if sink_root is not None else settings_home()
    return root / "telemetry" / "engine-root-fallback-census.watch-start.json"


def ensure_watch_start(
    sink_root: Optional[Path] = None, now: Optional[float] = None
) -> Optional[float]:
    """Idempotently start the observation clock; return its timestamp.

    THE BUG THIS FIXES (C24): `observed_days` was measured from
    `series_first_ts` -- the timestamp of the first RECORDED READ. A box
    that converged cleanly never calls `record_fallback_read`, so the
    series file never exists, `series_present` stays False forever, and
    `evidences_absence` can never become True. That is the module
    docstring's own stated failure mode ("a future zero would be
    indistinguishable from no emission was captured") recurring one layer
    down, in the successor state of the guard written to prevent it.

    The fix is a clock that starts the first time ANYTHING asks, not the
    first time something goes wrong. Two call sites reach this, and
    whichever runs first wins:
      - `record_fallback_read`, using the timestamp of that read -- so a
        box that DID see a stale pin keeps the same `observed_days` this
        module always reported (measured from the earliest read), not a
        clock that resets to "now" every time a report is pulled.
      - `census()`, using its own report time -- so a box that never sees
        a stale pin still starts a clock the first time anyone asks for a
        verdict, which is what makes the verdict reachable at all on a box
        that converged cleanly and is the whole point of this function.

    Atomic, lock-free creation: `os.O_CREAT | os.O_EXCL` either wins (this
    call is first) or loses (another process already created it -- this
    machine runs 50-70 concurrent sessions and several can race this at
    once) -- the loser reads the winner's timestamp back. No
    read-modify-write, no lock; same append-only-file philosophy as the
    series itself, just applied to a create instead of an append.

    Never raises: an unwritable sink degrades to `None` ("not observing"),
    which keeps `evidences_absence` False -- the safe direction, matching
    every other failure mode in this module.
    """
    try:
        path = watch_start_path(sink_root)
        os.makedirs(path.parent, exist_ok=True)
        ts = now if now is not None else time.time()
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, json.dumps({"ts": ts}).encode("utf-8"))
            finally:
                os.close(fd)
            return ts
        except FileExistsError:
            pass
        with open(path, "r", encoding="utf-8") as fh:
            existing = json.loads(fh.read())
        existing_ts = existing.get("ts")
        return existing_ts if isinstance(existing_ts, (int, float)) else None
    except Exception:
        return None


def record_fallback_read(
    site: str,
    *,
    root_value: Optional[str] = None,
    sink_root: Optional[Path] = None,
    now: Optional[float] = None,
) -> None:
    """Best-effort, append-only record that `site` read the `CLAUDE_KLABAUTER_ROOT`
    fallback.

    Call site: `coordinator_core.engine_root._maybe_emit_engine_root_fallback`,
    which has already deduped by site for this process. Never raises; see
    the module docstring's failure-discipline section.

    `root_value` is the path that answered, recorded because the 2026-08-20
    measurement's real finding was not the count but the CAUSE -- every read
    traced to an operator hand-pinning the old name per our own remediation
    text. A recurrence should be self-diagnosing rather than costing another
    session's tracing. Optional, so no existing caller shape breaks.
    """
    try:
        ts = now if now is not None else time.time()
        # Best-effort: start the watch clock at this read's own timestamp, so
        # a box that DOES see a stale pin keeps `observed_days` measured from
        # the earliest read rather than reset to whenever a report happens to
        # be pulled. If another process already started the clock (a census
        # call, or an earlier read), this is a no-op -- see
        # `ensure_watch_start`'s docstring for the race.
        ensure_watch_start(sink_root, now=ts)
        entry: Dict[str, object] = {"site": site, "ts": ts}
        if root_value:
            entry["root"] = root_value
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        path = series_path(sink_root)
        os.makedirs(path.parent, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
    except Exception:
        # Negative-spec: an observability write must never break the commit
        # hot path it observes. Every failure mode degrades to "not
        # recorded" rather than propagating.
        pass


def census(
    *,
    window_days: float = DEFAULT_WINDOW_DAYS,
    sink_root: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict:
    """Read-only-over-the-series report answering C14 item 4 against disk
    evidence. (Not read-only overall -- see `watch_start_ts` below.)

    Returns a dict carrying, at minimum:

      series_present     -- has the watch started (C24: sentinel present),
                            NOT "does the jsonl data file exist". A box that
                            never records a read never creates the jsonl,
                            but calling `census()` at all starts the watch
                            clock, so this can still become True.
      watch_start_ts      -- when the clock started (C24 sentinel), None
                            only if the sink could not be written at all.
      series_first_ts    -- oldest RECORDED READ, None if there has never
                            been one. Distinct from `watch_start_ts`: a
                            quiet series has a watch start but no reads.
      series_last_ts     -- newest observation, None if never
      observed_days      -- how long the watch has been running. Measured
                            from `watch_start_ts` (C24), NOT from
                            `series_first_ts` -- the latter was the module's
                            original defect: a box that never recorded a
                            read never started a clock, so `evidences_absence`
                            could never become True on a box that converged
                            cleanly. See `ensure_watch_start`'s docstring.
      total_reads        -- rows in the series (the sentinel is never one)
      reads_in_window    -- rows with ts inside the last `window_days`
      sites              -- per-site {count, first_ts, last_ts}
      days_since_last    -- quiet stretch, None if never observed
      window_days        -- echoed back, so the N asked cannot be lost
      evidences_absence  -- the ONLY field C14's exit should read

    `evidences_absence` is True iff the watch has started, has been running
    for at least the full window, and saw zero reads inside it. All three
    conjuncts are load-bearing; see the module docstring's series-age note
    for why the middle one exists.

    Never raises: a missing, unreadable, or corrupt series degrades to
    "never observed" -- which yields `evidences_absence: False`, not a
    false clear. A watch-start write that itself fails (unwritable sink)
    degrades the same way, via `ensure_watch_start`'s own guard.
    """
    report: dict = {
        "series_present": False,
        "watch_start_ts": None,
        "series_first_ts": None,
        "series_last_ts": None,
        "observed_days": 0.0,
        "total_reads": 0,
        "reads_in_window": 0,
        "sites": {},
        "days_since_last": None,
        "window_days": window_days,
        "unparsable_rows": 0,
        "undatable_rows": 0,
        "evidences_absence": False,
    }

    at = now if now is not None else time.time()

    # C24: start (or read back) the watch clock unconditionally -- this is
    # what makes the verdict reachable on a box that never records a single
    # read. `ensure_watch_start` never raises; a failure here degrades to
    # `watch_start_ts is None`, which leaves `series_present` False, the
    # safe direction.
    watch_start_ts = ensure_watch_start(sink_root, now=at)
    if watch_start_ts is None:
        return report
    report["series_present"] = True
    report["watch_start_ts"] = watch_start_ts
    report["observed_days"] = max(0.0, (at - watch_start_ts) / _SECONDS_PER_DAY)

    window_start = at - (window_days * _SECONDS_PER_DAY)
    sites: Dict[str, dict] = {}

    # The jsonl data file itself is optional -- a quiet box has a watch
    # start but no reads, and never creates it. `evidences_absence` still
    # needs computing in that case (a quiet box is exactly the shape it
    # exists to detect), so a missing/unreadable file falls through to the
    # finalize step below rather than returning early. `series_path` reaches
    # `_settings_home.settings_home()`, which can raise on a box with no
    # resolvable settings home; that must not propagate either.
    try:
        path = series_path(sink_root)
        has_series = path.is_file()
    except Exception:
        has_series = False

    if has_series:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        report["unparsable_rows"] += 1
                        continue
                    if not isinstance(entry, dict):
                        report["unparsable_rows"] += 1
                        continue
                    site = entry.get("site")
                    ts = entry.get("ts")
                    if not isinstance(site, str) or not isinstance(ts, (int, float)):
                        # A row we cannot place in time is counted, never merely
                        # skipped. The earlier reasoning here -- "skipping can
                        # never manufacture absence" -- was WRONG, and the test
                        # pinned the wrong behaviour with it. Skipping lowers
                        # `reads_in_window` WITHOUT lowering `observed_days` when
                        # the surviving rows are older, so one torn line (a
                        # concurrent append, an encoding error, a disk-full
                        # truncation) that was the only in-window read flips
                        # `evidences_absence` from False to True. An undatable row
                        # is treated as in-window until proven otherwise, which is
                        # the fail-safe direction for a detector whose whole job is
                        # evidencing absence.
                        report["undatable_rows"] += 1
                        continue
                    report["total_reads"] += 1
                    if ts >= window_start:
                        report["reads_in_window"] += 1
                    if (
                        report["series_first_ts"] is None
                        or ts < report["series_first_ts"]
                    ):
                        report["series_first_ts"] = ts
                    if (
                        report["series_last_ts"] is None
                        or ts > report["series_last_ts"]
                    ):
                        report["series_last_ts"] = ts
                    rec = sites.setdefault(
                        site, {"count": 0, "first_ts": ts, "last_ts": ts}
                    )
                    rec["count"] += 1
                    rec["first_ts"] = min(rec["first_ts"], ts)
                    rec["last_ts"] = max(rec["last_ts"], ts)
        except OSError:
            pass

    report["sites"] = sites
    if report["series_last_ts"] is not None:
        report["days_since_last"] = max(
            0.0, (at - report["series_last_ts"]) / _SECONDS_PER_DAY
        )

    report["evidences_absence"] = (
        report["series_present"]
        and report["observed_days"] >= window_days
        and report["reads_in_window"] == 0
        # A series carrying rows we could not read is not evidence of
        # anything. Without this conjunct a single torn line is enough to
        # turn "we cannot tell" into a clear.
        and report["unparsable_rows"] == 0
        and report["undatable_rows"] == 0
    )
    return report


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint -- `python -m coordinator_core.engine_root_census
    [--window-days N]`. Exit 0 always; the verdict is in the JSON, because
    an exit code cannot carry the difference between "quiet" and "not
    watching long enough" and this surface exists precisely to keep those
    two apart."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="engine_root_census",
        description=(
            "Report engine-root fallback reads off durable disk evidence "
            "(C14 item 4 / AC24 clause 2)."
        ),
    )
    parser.add_argument(
        "--window-days",
        type=float,
        default=DEFAULT_WINDOW_DAYS,
        help=(
            "Window for the absence verdict, in days "
            f"(default {DEFAULT_WINDOW_DAYS}). C14's exit names its own N."
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    print(json.dumps(census(window_days=args.window_days), indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
