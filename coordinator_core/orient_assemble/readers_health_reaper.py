"""
coordinator_core.orient_assemble.readers_health_reaper — C2d reader port:
health-probe subcommands (claude-klabauter-bin-sentinel / ceremony-hook) + the
day-cadence handoff-archival/reaper family; marker-freshness dedup (AC-7).

Purpose, per `docs/plans/2026-07-24-computed-skills-b2-ceremony-start.md`
§ Reader-in-process port scoping, chunk C2d:

1. Import `coordinator/bin/workday-start-health-probes.py`'s detector
   subcommands (`claude-klabauter-bin-sentinel`, `ceremony-hook`) AS-IS — both are
   read-only/report-only detectors, never mutators. (`observer-sidecar-scan`,
   the fused CLI's other subcommand, is NOT one of the three named in the
   plan's original port-scoping table and is out of scope here.)

   This chunk originally also imported a THIRD subcommand, `exec-bit-check`
   (`_read_exec_bit_check`, wired to the now-deleted `check-all-shebanged-
   exec-bits.py`). Retired 2026-07-28: that probe asserted "every shebanged
   file must be git mode 100755" -- the exact invariant the 2026-07-28 PM
   ruling retired (POSIX-only execution assumptions are now a portability
   defect class; Windows is the P0 primary platform). The same violation
   shape is now tracked with the opposite polarity by
   `coordinator_core.ops.check_posix_exec_assumptions`'s `mode_100755`
   blocking class (frozen-baseline, shrink-only ratchet) -- not a daily
   ceremony-surfaced ReaderResult.
   Added 2026-08-05 (outside the original C2d port list): a fourth
   subcommand, `working-repo-registration`, wired the same way as
   `claude-klabauter-bin-sentinel` — see `_read_working_repo_registration`'s own
   docstring for the DR-132 backlink.

2. The day-cadence handoff-archival/reaper family
   (`coordinator/bin/reap-orphaned-in-flight-handoffs.py`, 629 lines,
   fused): per the Approach's explicit zero-spawn budget decision, this
   reader accepts exactly ONE `subprocess.run` call to that CLI's
   `--dry-run` mode for the read-side report — extracting the dry-run
   detection logic in-process was assessed disproportionate cost for a
   once-a-day boot. This is the SOLE accepted subprocess in this reader
   family; see `_REAP_SUBPROCESS_EXCEPTION` below.
3. Marker-freshness dedup (AC-7): the three cadence-duplicated checks
   (session reads `state/.workday-start-marker`; day owns it and, via the
   `d-workday-marker-write` directive naming the real
   `write-workday-start-marker` CLI (`coordinator_core.ops.write_workday_start_marker`),
   is the surface that gets it written; week reads/writes
   `state/week-changelog/HEADER.md`'s date fields) collapse into ONE
   cadence-parameterized `_read_marker_freshness(cadence)` gate — cadence
   tunes which sentinel is consulted and at what severity, never a branch
   into three independently-implemented checks.

Spec backlink: DoE-claude:pln-computed-skills-b2-ceremony-st-e82420, chunk C2d

Negative-spec:
    - Does NOT call `observer-sidecar-scan` — not one of the three
      health-probe subcommands the plan's port-scoping table names for this
      chunk.
    - Does NOT extract `reap-orphaned-in-flight-handoffs.py`'s read-side
      logic in-process, and does NOT add a SECOND accepted subprocess call
      anywhere in this module — the reaper `--dry-run` call is the one and
      only documented exception to the assembler's zero-spawn budget.
    - Does NOT ever invoke the reaper's LIVE (non-`--dry-run`) mode, and
      does NOT invoke `_run_archive_stamp_cli` — mutation stays a
      `directives[]` entry naming the existing CLI, never performed here.
    - Does NOT re-implement three independent marker-freshness checks —
      `_read_marker_freshness(cadence)` is the single dedup target AC-7
      requires; a finding that re-splits it back into session/day/week
      helper functions is a regression of this chunk's AC.
    - Does NOT wire these results into `brief()` — `__init__.py`'s cadence
      dispatch is shared write-surface across C2a-C2d (the plan's own
      "same package — serial, write-overlap" note); this chunk lands
      alongside concurrently-dispatched sibling reader ports, so wiring
      `collect()` into `brief()` is left to a follow-up integration pass.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
)
from coordinator_core.orient_assemble.reader_result import ReaderResult

#: The two source CLIs' absolute paths — resolved relative to this file,
#: never a literal device path (portability discipline, AC-16). This file
#: lives at coordinator_core/orient_assemble/, so parents[2] is the claude-klabauter
#: repo root (mirrors readers_handoff_triage._SOURCE_PATH's same parents[2]).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_HEALTH_PROBES_PATH = _REPO_ROOT / "coordinator" / "bin" / "workday-start-health-probes.py"
_REAPER_PATH = _REPO_ROOT / "coordinator" / "bin" / "reap-orphaned-in-flight-handoffs.py"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SUBPROCESS_TIMEOUT = 30

#: Ceremony-name each cadence's own converted surface hooks — mirrors the
#: three command surfaces this baton converts (workday-start.md,
#: workweek-start.md, workstream-start/SKILL.md).
_CEREMONY_NAME_BY_CADENCE = {
    "day": "workday-start",
    "week": "workweek-start",
    "session": "workstream-start",
}


def _load_module(name: str, path: Path):
    """Load a hyphenated-filename source CLI as an importable module (same
    pattern as `readers_handoff_triage._load_source_module` /
    `readers_branch_reconcile._load_source_module`) — a normal `import`
    statement cannot address a `-`-containing filename."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load source module at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_health_probes = _load_module("workday_start_health_probes", _HEALTH_PROBES_PATH)
_cmd_claude_klabauter_bin_sentinel = _health_probes.cmd_claude_klabauter_bin_sentinel
_cmd_ceremony_hook = _health_probes.cmd_ceremony_hook
_cmd_working_repo_registration = _health_probes.cmd_working_repo_registration


def _read_claude_klabauter_bin_sentinel() -> ReaderResult:
    """Claude-Klabauter-bin sentinel check, imported as-is. Unlike exec-bit-check, this
    probe's failure messages are genuine Python-level `print(..., file=sys.stderr)`
    calls, so they ARE captured here rather than left to reach the operator
    only via a direct terminal write."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        exit_code = _cmd_claude_klabauter_bin_sentinel([])
    if exit_code == 0:
        return ReaderResult()
    detail = buf.getvalue().strip() or "claude-klabauter-bin sentinel check failed"
    return ReaderResult(
        directives=[
            {
                "id": "d-claude-klabauter-bin-sentinel",
                "cli": "workday-start-health-probes",
                "args": ["claude-klabauter-bin-sentinel"],
                "depends_on": None,
                "already_satisfied": False,
                "detail": detail,
            }
        ]
    )


def _read_working_repo_registration() -> ReaderResult:
    """Working-repo registration check, imported as-is (see
    `workday-start-health-probes.py`'s `working-repo-registration`
    subcommand docstring for the DR-132 backlink and "arms when klabauter
    registers" rationale). Modelled exactly on
    `_read_claude_klabauter_bin_sentinel` — same capture shape, same directive
    schema — EXCEPT the directive's `args` name the `--fix` apply form, not
    the bare detector: the bare form the DETECTOR calls below stays
    zero-spawn (this reader never spawns), but the emitted directive is the
    thing that actually repairs the key when an EM/apply-mechanism runs it,
    per the discharge test (a directive naming only the detector would hand
    the operator a command to retype, not a repair). Precedent for a
    directive naming a mutating-but-idempotent CLI already exists one
    function down (`_read_marker_freshness`'s `d-workday-marker-write`)."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        exit_code = _cmd_working_repo_registration([])
    if exit_code == 0:
        return ReaderResult()
    detail = buf.getvalue().strip() or "working-repo registration check failed"
    return ReaderResult(
        directives=[
            {
                "id": "d-working-repo-registration",
                "cli": "workday-start-health-probes",
                "args": ["working-repo-registration", "--fix"],
                "depends_on": None,
                "already_satisfied": False,
                "detail": detail,
            }
        ]
    )


def _read_ceremony_hook(cadence: str) -> ReaderResult:
    """Post-ceremony command hook, imported as-is. Always exits 0 (the
    wrapped `coordinator-ceremony-hook.py` contract: never blocks the
    calling ceremony) — captured stdout, if non-empty, is surfaced as an
    informational directive rather than silently discarded."""
    ceremony_name = _CEREMONY_NAME_BY_CADENCE.get(cadence, cadence)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _cmd_ceremony_hook([ceremony_name])
    text = buf.getvalue().strip()
    if not text:
        return ReaderResult()
    return ReaderResult(
        directives=[
            {
                "id": "d-ceremony-hook-output",
                "cli": "workday-start-health-probes",
                "args": ["ceremony-hook", ceremony_name],
                "depends_on": None,
                "already_satisfied": False,
                "detail": text,
            }
        ]
    )


#: ACCEPTED SUBPROCESS EXCEPTION (Approach § "Zero-spawn budget — explicit
#: decision"): the assembler is zero-spawn for every reader EXCEPT this one.
#: `reap-orphaned-in-flight-handoffs.py --dry-run` is a 629-line fused CLI;
#: extracting its read-side detection logic in-process was assessed
#: disproportionate cost for a once-a-day boot, so ONE subprocess call to
#: the existing `--dry-run` mode is accepted here instead. This is the SOLE
#: subprocess call in this reader family — do not add a second one anywhere
#: in this module.
_REAP_SUBPROCESS_EXCEPTION = True

_REAP_WOULD_RELEASE_RE = re.compile(
    r"^(\d+) orphaned in_flight handoffs would be released \(dry-run\)$", re.MULTILINE
)
_REAP_WOULD_RECLAIM_RE = re.compile(
    r"^(\d+) orphaned in_flight handoffs would be reclaimed as shipped \(dry-run\)$",
    re.MULTILINE,
)


def _read_reaper_dry_run() -> ReaderResult:
    """Day-cadence handoff-archival/reaper family — the accepted single
    subprocess call (see `_REAP_SUBPROCESS_EXCEPTION` above). Parses the
    dry-run summary counts (never per-file lines, to stay resilient to the
    source CLI's own prose wording) into a directive naming the live CLI as
    the remediation, when there is anything to reap/reclaim."""
    try:
        proc = subprocess.run(
            [sys.executable, str(_REAPER_PATH), "--dry-run"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            creationflags=_NO_WINDOW,
            cwd=str(_REPO_ROOT),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ReaderResult()

    stdout = proc.stdout or ""
    would_release_match = _REAP_WOULD_RELEASE_RE.search(stdout)
    would_reclaim_match = _REAP_WOULD_RECLAIM_RE.search(stdout)
    would_release = int(would_release_match.group(1)) if would_release_match else 0
    would_reclaim = int(would_reclaim_match.group(1)) if would_reclaim_match else 0

    if would_release == 0 and would_reclaim == 0:
        return ReaderResult()

    detail_parts = []
    if would_release:
        detail_parts.append(f"{would_release} orphaned in_flight handoff(s) would be released")
    if would_reclaim:
        detail_parts.append(f"{would_reclaim} orphaned in_flight handoff(s) would be reclaimed as shipped")

    return ReaderResult(
        directives=[
            {
                "id": "d-reaper-orphaned-handoffs",
                "cli": "reap-orphaned-in-flight-handoffs",
                "args": [],
                "depends_on": None,
                "already_satisfied": False,
                "detail": "; ".join(detail_parts),
            }
        ]
    )


def _read_marker_freshness(cadence: str) -> ReaderResult:
    """AC-7 dedup target: ONE cadence-parameterized marker-freshness gate
    replacing the three duplicated checks named in the Approach § "Marker
    sentinels" table. Cadence tunes which on-disk sentinel is consulted and
    at what severity — never a branch into three independently-implemented
    checks:

    - session: reads `state/.workday-start-marker`; stale/absent surfaces a
      judgment point naming the day-cadence ceremony as the thing that's due
      (never a directive — `/workday-start` is a ceremony/skill, not a bin
      CLI, and the directive schema's `cli` field is a required string; a
      ceremony name is not a legal value for it, so this is a judgment point,
      not a phantom-CLI directive).
    - day: owns `state/.workday-start-marker` and actually writes it — via
      `write-workday-start-marker`, a real bin CLI (port of
      `coordinator_core.ops.write_workday_start_marker`), not ceremony-side
      residue. Stale/absent surfaces a `d-workday-marker-write` directive
      naming that CLI; the write itself is mechanical (idempotent,
      today's-date-or-no-op), so this is a directive, never a judgment
      point — there is no "your call" here, unlike session/week below.
    - week: reads `state/week-changelog/HEADER.md` via the existing
      `check_weekly_staleness` ops module (imported as-is, not re-derived)
      and surfaces STALE/MILD as a judgment point — reset-vs-update-in-place
      is a genuine week-cadence human call, kept as ceremony-side residue
      per the Approach, never auto-resolved here.
    """
    from coordinator_core.daily_day import local_day
    from coordinator_core.ops.check_weekly_staleness import (
        _compute_staleness,
        _resolve_state_root,
    )

    state_root_str = _resolve_state_root()
    directives: list[dict[str, Any]] = []
    judgment_points: list[dict[str, Any]] = []

    if cadence in ("day", "session") and state_root_str:
        today = local_day()
        marker_path = Path(state_root_str) / ".workday-start-marker"
        marker_value = ""
        if marker_path.is_file():
            marker_value = marker_path.read_text(encoding="utf-8", errors="replace").strip()
        marker_fresh = marker_value == today

        if cadence == "day" and not marker_fresh:
            directives.append(
                {
                    "id": "d-workday-marker-write",
                    "cli": "write-workday-start-marker",
                    "args": [],
                    "depends_on": None,
                    "already_satisfied": False,
                    "detail": (
                        f"state/.workday-start-marker={marker_value or 'absent'!r} != "
                        f"today {today!r} — run write-workday-start-marker to refresh it"
                    ),
                }
            )
        elif cadence == "session" and not marker_fresh:
            judgment_points.append(
                build_judgment_point(
                    None,
                    id="j-session-day-review-due",
                    question=(
                        f"state/.workday-start-marker={marker_value or 'absent'!r} != "
                        f"today {today!r} — day-cadence review is due; run "
                        "/workday-start now or defer?"
                    ),
                    dispositions=[
                        build_disposition("run_workday_start_now"),
                        build_disposition("defer"),
                    ],
                    evidence=(
                        f"state/.workday-start-marker={marker_value or 'absent'!r} != "
                        f"today {today!r} | reason: /workday-start is a ceremony/skill, "
                        "not a bin CLI, so this is never a directive; when to run it "
                        "is a PM/EM judgment call, never auto-resolved"
                    ),
                    reason="recommendation-forbidden",
                )
            )

    if cadence == "week" and state_root_str:
        header_path = Path(state_root_str) / "week-changelog" / "HEADER.md"
        if header_path.is_file():
            header_text = header_path.read_text(encoding="utf-8", errors="replace")
            verdict = _compute_staleness(header_text, root=state_root_str)
            if verdict in ("STALE", "MILD"):
                judgment_points.append(
                    build_judgment_point(
                        None,
                        id="j-week-marker-freshness",
                        question=(
                            f"state/week-changelog/HEADER.md staleness={verdict} — "
                            "reset for a new week or update in place?"
                        ),
                        dispositions=[
                            build_disposition("reset_week"),
                            build_disposition("update_in_place"),
                        ],
                        evidence=(
                            f"check_weekly_staleness verdict={verdict} | "
                            "reason: reset-vs-update-in-place is a week-cadence "
                            "PM/EM judgment call, never auto-resolved"
                        ),
                        reason="recommendation-forbidden",
                    )
                )

    return ReaderResult(directives=directives, judgment_points=judgment_points)


def collect(cadence: str) -> ReaderResult:
    """Compute this reader family's directives/judgment_points for `cadence`.

    Health probes run for every cadence (their detail is not cadence-tuned).
    The reaper family's accepted subprocess call is day-cadence only — the
    Approach scopes it as "the day-cadence handoff-archival/reaper family",
    never fired at session/week cadence.
    """
    results = [
        _read_claude_klabauter_bin_sentinel(),
        _read_working_repo_registration(),
        _read_ceremony_hook(cadence),
        _read_marker_freshness(cadence),
    ]
    if cadence == "day":
        results.append(_read_reaper_dry_run())

    directives: list[dict[str, Any]] = []
    judgment_points: list[dict[str, Any]] = []
    for result in results:
        directives.extend(result.directives)
        judgment_points.extend(result.judgment_points)
    return ReaderResult(directives=directives, judgment_points=judgment_points)
