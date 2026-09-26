"""
coordinator_core.orientation.route_unreachable_signal — sanctioned-route
unreachability, aggregated across sessions.

Purpose: on 2026-09-20 the warm door degraded and three sessions independently
hand-wrote the artifact their CLI would have produced. Every one of them
reconciled first, produced a valid record, and kept working — the correct
behaviour under a degraded engine, and NOT the defect. The defect was that each
logged it as a local inconvenience and nothing logged that the gate had stopped
being reachable, so the pattern surfaced only because three sessions compared
notes by hand and a fourth noticed the comparison was worth making. Alone, any
one of them would have left no trace. `cc_invoke :: _record_route_unreachable`
now writes the events; this module is the half that reads them, because a
ledger with no reader repeats the failure it was built to fix.

Shape mirrors `warm_health_signal.emit_warm_engine_health` deliberately: one
rendered line, omit-when-quiet, fail-open to "" on any error, cold
orientation-regen path only. Two differences, both load-bearing:

  - PER-BOX, not repo-scoped or clone-scoped. Warm telemetry is keyed to the
    engine clone this interpreter runs from; this ledger is keyed to a
    per-box runtime base (see `LEDGER_RELPATH` below), because the question is
    "which routes went unreachable to how many sessions on this box", which is
    not answerable from one clone's view — and a repo-relative path was tried
    first and broke across the source/mirror clone split (same comment).
  - WINDOWED, not cumulative. A rate over all time would still be rendering a
    line about a bad afternoon weeks later, and an operator who learns to
    scroll past a section has lost it. Only the recent window counts.

Negative-spec: this reports, it never advises re-running anything. The events
it counts are delivered-but-unanswered MUTATIONS — the one refusal that says
nothing about whether the write landed. `cc_invoke.WarmDispatchIndeterminate`'s
own negative-spec forbids retrying them, and a line here that read as "these
failed, try again" would invert it. The line names what went unreachable and
points at the instrument; it does not diagnose, and it does not prescribe.

Negative-spec: this is NOT a signal about hand-written artifacts, and nothing
here counts or detects one. A hand-written record that validates is
indistinguishable from a CLI-produced one by design, that design is correct,
and the improvement-queue row this implements
(`state/improvement-queue/2026-09-20-the-gates-get-routed-around-exactly-when-
the-system-is-under-stress.yaml`) explicitly forbids resolving the finding by
blocking them. The gap was silence about the GATE, not the workaround.
"""

from __future__ import annotations

import datetime
import json
import os

#: compares FULL RESOLVED PATHS rather than the relpath that hid this.
LEDGER_RELPATH = ("coordinator", "sanctioned-route-unreachable.jsonl")

#: Test-isolation seam, shared by name with `warm.breadcrumb.RUNTIME_BASE_ENV`.
RUNTIME_BASE_ENV = "COORDINATOR_WARM_RUNTIME_BASE"

WINDOW_HOURS = 24

#: bound while only `WINDOW_HOURS` of it is ever renderable. Reading it whole
_TAIL_SCAN_BYTES = 256 * 1024

#: `abandoned_claim_signal`'s `_MAX_NAMED` posture.
_MAX_NAMED = 4


def _runtime_base() -> str:
    override = os.environ.get(RUNTIME_BASE_ENV, "").strip()
    if override:
        return override
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return local
    return os.path.join(os.path.expanduser("~"), ".cache")


def ledger_path() -> str:
    return os.path.join(_runtime_base(), *LEDGER_RELPATH)


def _parse_ts(value: object) -> "datetime.datetime | None":
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def read_recent_events(now: "datetime.datetime | None" = None) -> list[dict]:
    """Ledger rows inside `WINDOW_HOURS`, oldest first. Never raises.

    Only the last `_TAIL_SCAN_BYTES` are scanned — see that constant for why a
    whole-file read on this path gets more expensive every day nobody prunes.

    A malformed row is skipped, not fatal: this file is append-space shared by
    every session on the box (~50 concurrent per `docs/wiki/machine-load-norm.md`),
    written on an already-failing path, and one torn or future-shaped row must
    not blind the reader to every other row around it.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(hours=WINDOW_HOURS)
    events: list[dict] = []
    try:
        with open(ledger_path(), "rb") as raw:
            raw.seek(0, os.SEEK_END)
            size = raw.tell()
            start = max(0, size - _TAIL_SCAN_BYTES)
            raw.seek(start)
            blob = raw.read()
        text = blob.decode("utf-8", errors="replace")
        lines = text.split("\n")
        if start > 0 and lines:
            lines = lines[1:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            ts = _parse_ts(row.get("ts"))
            if ts is None or ts < cutoff:
                continue
            events.append(row)
    except OSError:
        return []
    return events


def emit_route_unreachable(now: "datetime.datetime | None" = None) -> str:
    try:
        events = read_recent_events(now=now)
    except Exception:  # noqa: BLE001 -- fail-open, see module docstring
        return ""
    if not events:
        return ""

    ops = sorted({str(e.get("op") or "?") for e in events})
    sessions = {str(e.get("session") or "") for e in events}
    sessions.discard("")
    entrypoints = sorted({str(e.get("entrypoint") or "?") for e in events})

    named = ", ".join(f"`{op}`" for op in ops[:_MAX_NAMED])
    if len(ops) > _MAX_NAMED:
        named += f", +{len(ops) - _MAX_NAMED} more"

    # "at least" because a session that never set CLAUDE_SESSION_ID contributes
    who = (
        f" across at least {len(sessions)} sessions"
        if len(sessions) > 1
        else ""
    )

    return (
        f"- ⚠ {len(events)} delivered-but-unanswered dispatch(es) in the last "
        f"{WINDOW_HOURS}h{who}: {named} "
        f"(via {', '.join(entrypoints[:_MAX_NAMED])}). The sanctioned CLI route was "
        "unreachable at those moments, so any artifact produced around them was "
        "likely hand-written. Do NOT re-run them -- a delivered mutation may have "
        "landed. Ledger: `" + os.path.join(*LEDGER_RELPATH) + "`."
    )
