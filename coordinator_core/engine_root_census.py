"""
coordinator_core.engine_root_census -- durable stale-pin regression detector
for the retired CLAUDE_KLABAUTER_ROOT fallback.

What this is: a durable, append-only observation sink. A row here means some
process on this box still had `CLAUDE_KLABAUTER_ROOT` exported when
`coordinator_core.engine_root` checked it; the row names the reading site
and the pinned value so a recurrence is self-diagnosing rather than costing
another session's tracing.

What this is NOT, and must never be re-grown into: evidence of ABSENCE. The
hook that feeds this sink (`engine_root._maybe_emit_engine_root_retired`)
fires when `CLAUDE_KLABAUTER_ROOT` is merely SET IN THE ENVIRONMENT, never when it
actually answers a lookup -- and post-C14 the retired name never answers
anything. So a zero reading here measures operator shell hygiene across a
50-70-session box, never code correctness. A verifier who exports
`CLAUDE_KLABAUTER_ROOT` by hand to drive a ceremony trips the counter by verifying;
that is exactly how the original absence-verdict design polluted its own
evidence on 2026-08-21, and is why this module reports observations only,
never a verdict.

C14 item 4 was discharged at `02ef8ae9de77` on C23's three-leg ratchet
instead: zero unexcluded executable read sites, verified by falsification
against planted tuple/list/dict shapes -- a property of the code, proved
once, and strictly stronger than anything a rolling sample of operator
environments could ever show. This module's `evidences_absence` field is
gone; treating a future all-zero census as licence to close anything would
recreate the exact trap the removal exists to forbid.

Spec backlink: docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md,
               chunk C14 item 4 / AC24 clause 2 -- SUPERSEDED by C23's
               three-leg ratchet at `02ef8ae9de77`, not pending.
Bug row closed: state/bug-backlog/2026-08-20-ac24-s-evidence-clause-is-unsatisfiable-96c02228e7b3.yaml
               -- on NEITHER of the two fixes that row offered. Narrowing the
               sink to reads that ANSWERED is vacuous once the retired name
               answers nothing, and retiring the sink outright would delete a
               working detector to remove a field. The row's dichotomy was
               the artifact of reading the sink as a gate; separating the
               observation from the verdict dissolves it.

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
      module deliberately does not live under that package. Post-lazy-only
      (2026-08-22, the import-path-costs-nothing sprint), a bare
      `import coordinator_core.ops` no longer forces `_eager_import_all` over
      ~206 op modules — that rationale is retired, not merely stale — but the
      rule it justified still holds: this accessor has zero need for any op,
      and even a single targeted per-op import still pays a real compile/import
      cost this hot-path read has no reason to spend. Imports here stay stdlib
      plus `coordinator_core._settings_home`, which is bootstrap-safe and makes
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
series degrades to "never observed", which is a fine answer for an
observation-only report -- there is no verdict left for a false negative to
threaten.

THE SERIES CAN ONLY FAIL SAFE, and that is why there is no suppression
mechanism here. A spurious row -- an ad-hoc validation run exercising the
accessor by hand, say -- makes `reads_in_window` non-zero, which is
correct: something did read the fallback, and the row names it. Building a
kill-switch to keep such reads out would add cost on the commit hot path to
strip a signal that is always accurate on its own terms, and would replace
an artifact-enforced property with an operator-remembers one. The test
suite is separately incapable of reaching the live sink:
`conftest._quarantine_real_home` is autouse and redirects the home the
settings-home resolver reads, so a suite run writes into its own tmp dir.
Verified 2026-08-20 by running the accessor suite and confirming the live
series stayed absent.

Negative-spec:
    - Never imports anything under `coordinator_core.ops`.
    - Never spawns a subprocess and never takes a lock.
    - Never truncates or rewrites the series -- strictly append-only.
    - Never raises out of `record_fallback_read()` for any reason.
    - Never reports a verdict field (e.g. an `evidences_absence`-shaped
      key). C14 item 4 is discharged elsewhere (see above); a verdict
      field here would invite closing that item a second time by waiting
      for a field that measures operator shell hygiene, not code.
    - Does not decide the window. Callers pick `window_days`; this module
      reports against whatever it is handed and echoes it back in the
      report so a reader cannot lose track of which N was used.
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
    """Read-only report of fallback-read observations, off disk evidence.

    Returns a dict carrying, at minimum:

      series_present     -- does the jsonl series file exist and readable.
      series_first_ts    -- oldest recorded read, None if there has never
                            been one.
      series_last_ts     -- newest observation, None if never
      total_reads        -- rows in the series
      reads_in_window    -- rows with ts inside the last `window_days`
      sites              -- per-site {count, first_ts, last_ts}
      days_since_last    -- quiet stretch, None if never observed
      window_days        -- echoed back, so the N asked cannot be lost

    This report carries no verdict field. A non-zero `reads_in_window` is
    an actionable stale-pin regression signal (see the module docstring);
    a zero reading is not evidence of anything beyond "no stale pin was
    observed in this window on this box" and must never be treated as a
    close condition for anything.

    Never raises: a missing, unreadable, or corrupt series degrades to
    "never observed" rather than propagating.
    """
    report: dict = {
        "series_present": False,
        "series_first_ts": None,
        "series_last_ts": None,
        "total_reads": 0,
        "reads_in_window": 0,
        "sites": {},
        "days_since_last": None,
        "window_days": window_days,
        "unparsable_rows": 0,
        "undatable_rows": 0,
    }

    at = now if now is not None else time.time()
    window_start = at - (window_days * _SECONDS_PER_DAY)
    sites: Dict[str, dict] = {}

    # `series_path` reaches `_settings_home.settings_home()`, which can
    # raise on a box with no resolvable settings home; that must not
    # propagate.
    try:
        path = series_path(sink_root)
        has_series = path.is_file()
    except Exception:
        has_series = False

    report["series_present"] = has_series

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
                        # A row we cannot place in time is counted, never
                        # merely skipped: an undatable row lands in
                        # `undatable_rows`, not `total_reads`/
                        # `reads_in_window` -- it is preserved, not folded
                        # into the main counters. A reader asking "has any
                        # site read the fallback" must check
                        # `unparsable_rows` and `undatable_rows` alongside
                        # `reads_in_window`, because a torn line (a
                        # concurrent append, an encoding error, a disk-full
                        # truncation) can carry a live stale-pin signal that
                        # neither main counter reflects.
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

    return report


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint -- `python -m coordinator_core.engine_root_census
    [--window-days N]`. Exit 0 always; this surface reports observations,
    never a verdict."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="engine_root_census",
        description=(
            "Report engine-root fallback reads (stale CLAUDE_KLABAUTER_ROOT pins) "
            "off durable disk evidence."
        ),
    )
    parser.add_argument(
        "--window-days",
        type=float,
        default=DEFAULT_WINDOW_DAYS,
        help=(
            "Recency window for `reads_in_window`, in days "
            f"(default {DEFAULT_WINDOW_DAYS})."
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    print(json.dumps(census(window_days=args.window_days), indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
