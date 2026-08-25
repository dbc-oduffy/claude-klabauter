"""
coordinator_core.ops.check_makima_doctor_sentinel — read-only consumer of
makima's own doctor-probe health sentinel.

Purpose: surface makima's doctor-probe health verdict during fleet
`/workday-start`, mirroring how `check-plugin-drift.sh` nudges on drift.
This is a READ-CONSUMER only — the sentinel is makima-owned and written by
`bin/makima-doctor-probe.py` (on `--triage` and full runs); this module
never writes it.

Sentinel location: <engine-root>/state/doctor-last-run.json
Sentinel schema (makima-owned):
  { "verdict": "GREEN|AMBER|RED", "red_probes": ["<probe id>", ...],
    "hint": "<one-line remediation>", "ts": <epoch seconds>,
    "advisory_only": <bool, optional> }

Nudge-worthy states (mirrors the memo's ask):
  - absent  — doctor never run on this machine (fresh install / bootstrap gap)
  - stale   — sentinel older than COORDINATOR_MAKIMA_DOCTOR_STALE_SEC (default 7d)
  - RED/AMBER — last run found a broken/degraded probe; echo `hint`

Output: zero or one line of the form
  [health] makima-doctor: <message>
Exit 0 always (advisory, never gating) — matches check-plugin-drift.sh /
scan-addon-health.sh convention of "probe never fails the ceremony".

Port of: check-makima-doctor-sentinel.sh (DoE b5a4192c, 2026-07-20)
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
Spec backlink: cross-repo/inbox/2026-07-04-workday-start-makima-doctor-sentinel.md

Negative-spec:
  - Does NOT write the sentinel — `bin/makima-doctor-probe.py` owns that
    (invoked as `python bin/makima-doctor-probe.py --triage`); this module
    only reads what it wrote.
  - Does NOT require, validate, or enumerate the sentinel's full key set. This
    module reads exactly five fields (`verdict`, `hint`, `red_probes`, `ts`,
    `advisory_only`) via `.get()` with defaults, so it is unaffected by additive
    keys the writer gains over time — e.g. the `vendor_drift` public key added
    2026-07-26 (see `bin/makima-doctor-probe.py::_write_doctor_sentinel`). A
    sentinel from before a given key existed, and one from after, both parse
    identically here. `advisory_only` (added 2026-08-17) is read via
    `.get("advisory_only")` with the same absent-key tolerance: an older
    sentinel lacking it renders exactly as before this key existed (plain
    AMBER/RED, no ADVISORY branch) — see `_format_verdict`'s `advisory_only`
    parameter, default `False`. If this module ever needs to read
    `vendor_drift` (a future DoE-side ask), do so via `.get("vendor_drift")`
    with the same absent-key tolerance, never a strict key-membership
    assertion — a sentinel this module fails to parse degrades to the
    "sentinel unreadable" line, never a crash.
  - Does NOT re-derive the engine root via the shell resolution ladder — this
    module IS running from inside the resolved engine root (the DoE-side
    trampoline already resolved it to reach this import), so the engine root is
    simply this file's own repo root, three parents up
    (ops/ -> coordinator_core/ -> <engine-root>).
  - Does NOT shell out to a bootstrapped Python interpreter to parse the
    sentinel JSON (the original .sh had to locate a `python3`/`python`/`py`
    binary from bash to do this) — this module already runs under Python, so
    it uses the stdlib `json` module directly.
  - A sentinel-unreadable engine root (resolution itself failing) is not this
    module's concern — that failure mode is caught by the DoE-side trampoline
    before this module is ever imported, and degrades silently there (fail
    loud on stderr, exit 0), mirroring the original .sh's fully-silent
    `coordinator_makima_root 2>/dev/null || exit 0` skip.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

_DEFAULT_STALE_SEC = 604800  # 7 days


def _makima_root() -> Path:
    """This module's own repo root: ops/ -> coordinator_core/ -> <engine-root>."""
    return Path(__file__).resolve().parents[2]


def _stale_sec() -> int:
    raw = os.environ.get("COORDINATOR_MAKIMA_DOCTOR_STALE_SEC", "")
    try:
        return int(raw) if raw else _DEFAULT_STALE_SEC
    except ValueError:
        return _DEFAULT_STALE_SEC


def _format_verdict(
    verdict: str,
    hint: str,
    red_probes: List[str],
    stale: bool,
    age_days: Optional[int],
    advisory_only: bool = False,
) -> str:
    """Render the single advisory line for a parsed, well-formed sentinel.

    Mirrors the bash `case "$verdict" in ...` block line-for-line, including
    the RED/AMBER/GREEN|""/unknown-verdict branch shapes, with one addition:
    when `advisory_only` is True (the sentinel's DEGRADED/BROKEN overall was
    driven entirely by non-required probes — see
    `bin/makima-doctor-probe.py::_sentinel_advisory_only`), the RED/AMBER band
    is replaced with an ADVISORY line that says the install is not broken. The
    degradation itself is still surfaced — it must never vanish, only stop
    reading as a required-prerequisite failure. `advisory_only` defaults to
    False so a sentinel written before this key existed (or any sentinel
    lacking it) renders through the original RED/AMBER branches unchanged.
    """
    if verdict == "RED" and advisory_only:
        probe_clause = f" ({','.join(red_probes)})" if red_probes else ""
        hint_clause = f" — {hint}." if hint else ""
        return f"[health] makima-doctor: ADVISORY (non-gating){probe_clause}{hint_clause} Run python bin/makima-doctor-probe.py --triage for details."

    if verdict == "RED":
        probe_clause = f" ({','.join(red_probes)})" if red_probes else ""
        hint_clause = f" — {hint}." if hint else ""
        return f"[health] makima-doctor: RED{probe_clause}{hint_clause} Run python bin/makima-doctor-probe.py --triage for details."

    if verdict == "AMBER" and advisory_only:
        hint_clause = f" — {hint}." if hint else ""
        if stale and age_days is not None:
            return f"[health] makima-doctor: ADVISORY (non-gating, {age_days}d old){hint_clause} Run python bin/makima-doctor-probe.py --triage to re-probe."
        return f"[health] makima-doctor: ADVISORY (non-gating){hint_clause} Run python bin/makima-doctor-probe.py --triage to re-probe."

    if verdict == "AMBER":
        hint_clause = f" — {hint}." if hint else ""
        if stale and age_days is not None:
            return f"[health] makima-doctor: AMBER ({age_days}d old){hint_clause} Run python bin/makima-doctor-probe.py --triage to re-probe."
        return f"[health] makima-doctor: AMBER{hint_clause} Run python bin/makima-doctor-probe.py --triage to re-probe."

    if verdict in ("GREEN", ""):
        if stale:
            if age_days is None:
                return "[health] makima-doctor: sentinel ts unparseable. Run python bin/makima-doctor-probe.py --triage."
            return f"[health] makima-doctor: stale (last run {age_days}d ago). Run python bin/makima-doctor-probe.py --triage."
        return ""

    return f"[health] makima-doctor: unknown verdict '{verdict}'. Run python bin/makima-doctor-probe.py --triage."


def main(argv: List[str]) -> int:  # noqa: ARG001 — argv unused (no flags), kept for trampoline contract parity
    """Read the doctor sentinel and emit zero or one advisory line.

    Exit 0 always — advisory, never gating (matches check-plugin-drift.sh /
    scan-addon-health.sh convention).
    """
    makima_root = _makima_root()
    sentinel = makima_root / "state" / "doctor-last-run.json"

    if not sentinel.is_file():
        print(
            "[health] makima-doctor: sentinel absent (doctor never run on this machine) "
            "— run python bin/makima-doctor-probe.py --triage to bootstrap."
        )
        return 0

    try:
        raw = sentinel.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        print(
            f"[health] makima-doctor: sentinel unreadable at {sentinel} (malformed JSON?). "
            "Run python bin/makima-doctor-probe.py --triage."
        )
        return 0

    if not isinstance(data, dict):
        print(
            f"[health] makima-doctor: sentinel unreadable at {sentinel} (malformed JSON?). "
            "Run python bin/makima-doctor-probe.py --triage."
        )
        return 0

    verdict = str(data.get("verdict") or "")
    hint = str(data.get("hint") or "")
    red_probes_raw = data.get("red_probes") or []
    red_probes = [str(p) for p in red_probes_raw] if isinstance(red_probes_raw, list) else []
    advisory_only = data.get("advisory_only") is True
    ts = data.get("ts", "")

    stale = True
    age_days: Optional[int] = None
    ts_str = str(ts)
    if ts_str.isdigit():
        age_sec = int(time.time()) - int(ts_str)
        age_days = age_sec // 86400
        stale = age_sec > _stale_sec()

    line = _format_verdict(verdict, hint, red_probes, stale, age_days, advisory_only)
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
