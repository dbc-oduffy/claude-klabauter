"""
coordinator_core.session.fleet_delegation — the fleet-delegation grant:
writer, reader, the never-delegable floor, and mandatory absolute expiry.

Purpose: docs/plans/2026-08-28-the-ask-the-pm-step-gets-an-artifact-to-
check.md, chunk C2. Models directly on
``coordinator_core.session.grant`` (the Tier-U grant) — same atomic
``tempfile.mkstemp`` + ``os.replace`` discipline, same writer/reader/check
triple — but this record lives at ``<settings_home()>/fleet-delegation.json``
(resolved via ``coordinator_core._settings_home.settings_home()``, never a
hand-rolled path) and answers a different question: not "may THIS session
run the full suite" but "does a human's grant currently designate ONE OTHER
live session to answer for the human on a bounded set of decision classes".

Record shape (``schema_version`` 1):
    designated    {"pid": int, "create_time": float} — the (pid, create_time)
                  identity pair for the designated session's stable process.
                  Names collide and rotate; pid alone is recycled, so the
                  pair is the identity (see the plan's "What ships" table).
    classes       list[str] — the delegated decision classes, ENUMERATED,
                  never a wildcard. Any member in ``NEVER_DELEGABLE`` is
                  rejected at write time.
    granted_at    ISO-8601 UTC timestamp, caller-supplied.
    expires_at    ISO-8601 UTC timestamp, REQUIRED — absent is a rejection.
    granted_by    must be the literal string "human" — the only accepted
                  value in v1.
    authorship    {"verdict": "human", "reason": <str>} — SELF-ATTESTED: the
                  writer's own recorded verdict from
                  ``coordinator_core.session.grant_authorship``, not
                  something the reader re-verifies. Its only corroboration
                  is that the write-surface guards (C3/C4) stop an
                  un-sanctioned writer from reaching this field at all.
    note          the human's verbatim utterance, stored exactly as given
                  (mirrors the ``execution_authorized_note`` convention).

Public functions:
    write_fleet_delegation   — writer. Validates every HARD CONSTRAINT below;
                                atomic create-or-overwrite; returns
                                ``(ok, reason)`` so a rejection can be
                                reported without a second read.
    read_fleet_delegation    — raw reader. Returns the record exactly as
                                persisted, or None if absent/unreadable/
                                malformed. NO liveness check, NO validation —
                                use ``check_fleet_delegation`` to decide
                                whether to ACT on it.
    check_fleet_delegation   — THE authorization predicate for one decision
                                class: returns ``(granted, record_or_None)``.

NEVER_DELEGABLE is declared HERE and nowhere else — the CLI
(``coordinator/bin/coordinator-delegation.py``, out of this chunk's scope)
imports it rather than restating it, per the plan's "(2) Never-delegable
classes are rejected at write time, and the check sits in the writer" —
not in the CLI's own argument parser, so an agent that imports this module
directly gets the same floor as one that shells out to the CLI.

Write-time validation (rejects with the reason, and writes NOTHING):
  - ``authorship`` verdict is not HUMAN (an AGENT or UNRESOLVED verdict from
    ``grant_authorship.authorship_verdict`` both refuse — see
    ``AuthorshipVerdict.refuses``).
  - ``expires_at`` is absent.
  - ``granted_at`` is not within +/- 5 minutes of wall clock AT WRITE TIME —
    a FUTURE ``granted_at`` is itself a rejection, never merely a
    lease-length input (Review: staff-eng (the Staff Engineer), finding 4).
  - ``expires_at`` is more than 12h after WALL CLOCK AT WRITE TIME — never
    measured against the caller-supplied ``granted_at``. Anchoring the
    ceiling to the clock (not to caller data) is what keeps a
    ``granted_at = now + 100h`` / ``expires_at = granted_at + 1h`` pair from
    slipping through: the reader only ever evaluates ``now >= expires_at``,
    so an ungated ``granted_at`` could buy 101 live hours from a single
    dishonest or clock-skewed writer.
  - ``granted_by`` is not the literal string ``"human"``.
  - ``classes`` is empty.
  - any requested class is a member of ``NEVER_DELEGABLE``.
  - any requested class is not a member of ``DELEGABLE`` (the positive
    allow-list, checked AFTER the ``NEVER_DELEGABLE`` check so a
    never-delegable class keeps its own message rather than falling
    through to the allow-list's "unknown class" one).

Read-time semantics (``check_fleet_delegation``): this is the INSPECTION
API, the layer behind ``coordinator-delegation show``, which genuinely needs
to explain WHY a grant was refused. ``granted`` is False, and the RECORD IS
STILL RETURNED (never coerced to None), when the grant is malformed, an
unknown ``schema_version``, expired (``now >= expires_at``), carrying a
non-HUMAN ``authorship`` verdict, naming a class not in the record's own
``classes``, or naming a designated ``(pid, create_time)`` pair that is not
LIVE — only a TRUE ABSENCE (no file at all, or an unreadable/malformed-JSON
file that never parsed into a record) returns ``(False, None)``. No warning
branch, no grace window on the boolean — but no identity-with-absence
promise at THIS layer either: that promise belongs to
``coordinator_core.ops.delegation_check`` (the agent-facing consumer
surface), which collapses every one of these denial reasons — expired
included — to the SAME reply an absent grant would produce, because a
session asking that surface must not be able to distinguish "never granted"
from "expired" any more than a "not granted" denial should disclose who
currently holds the relay. See that module's own test suite
(``coordinator_core/ops/tests/test_delegation_check.py``) for the identity
assertion; THIS module's test suite
(``coordinator_core/session/tests/test_fleet_delegation.py``) instead
asserts that the record survives an expired denial, which is what the
inspection API is for.

Liveness-probe failure is explicitly FAIL-CLOSED (Review: staff-eng
(the Staff Engineer), finding 8): any exception the ``psutil`` probe raises
(``AccessDenied``, ``NoSuchProcess``, ``OSError``, or any other) resolves to
ABSENT, the SAME as "not live" — never caught-and-ignored (that would let a
denial vanish into a silent False), never re-raised (that would crash a
caller merely asking a yes/no question). The distinguishing reason string
names the probe failure specifically (``"designated-process-unreadable:
<ExceptionType>"``), distinct from the plain "grantee exited"
(``"designated-process-not-found"``) reason, so a diagnosing operator can
tell the two apart even though both collapse to the same ABSENT boolean.

Spec backlink: docs/plans/2026-08-28-the-ask-the-pm-step-gets-an-artifact-to-check.md § chunk C2
Precedent (writer/reader/check shape, atomicity discipline): coordinator_core/session/grant.py
Precedent (authorship verdict consumed, never re-derived): coordinator_core/session/grant_authorship.py

Negative-spec:
  - Does NOT accept an env-keyed override on any leg (no
    ``COORDINATOR_FLEET_DELEGATION_*`` toggle) — see the plan's Anti-scope:
    "Do not add an env-keyed override to any leg."
  - Does NOT call this mechanism a boundary anywhere in code or docstring —
    it is a layer; see the plan's "The ceiling sentence is mandatory".
  - Does NOT soften an expired grant to a warned-but-honoured state — expiry
    reads byte-identical to absence, full stop.
  - Does NOT restate ``NEVER_DELEGABLE`` anywhere else in this codebase —
    every other consumer imports this module's frozenset.
  - Does NOT restate ``DELEGABLE`` anywhere else in this codebase either —
    same import-not-restate rule, same reason.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from coordinator_core._settings_home import settings_home
from coordinator_core.session.core import _psutil
from coordinator_core.session.grant_authorship import Verdict, authorship_verdict

#: The only legal value of the ``granted_by`` field in schema v1 (the plan's
#: "What ships" table: "the only accepted value in v1").
_VALID_GRANTED_BY = "human"

#: The never-delegable decision-class floor (the plan's "(2) Never-delegable
#: classes"). Declared HERE and nowhere else in this codebase — every other
#: consumer (the CLI included) imports this frozenset rather than restating
#: it. Membership drawn verbatim from the plan's own enumeration of the
#: observed run: anything irreversible, anything outward-facing to a party
#: outside the fleet, and anything touching scope/deliverable/product
#: direction.
NEVER_DELEGABLE = frozenset(
    {
        "irreversible-action",
        "outward-facing-action",
        "scope-change",
        "deliverable-change",
        "product-direction",
    }
)

#: The positive delegable-class allow-list — the ONLY classes a grant may
#: name. Declared HERE and nowhere else in this codebase, mirroring
#: ``NEVER_DELEGABLE``'s own convention: every other consumer imports this
#: frozenset rather than restating it. Enforced at write time, AFTER the
#: ``NEVER_DELEGABLE`` check (order matters — see ``write_fleet_delegation``)
#: so a never-delegable class keeps its own existing rejection message
#: instead of falling through to the allow-list's "unknown class" message.
#:
#: This list is the coordinator-claude plane's (DoE's) to change — it
#: arrives here as a ratified list, not a proposal this module negotiates.
#: Membership, exactly two:
#:   - "execute-approved-plan" — the gate where the PM has already ratified
#:     the plan and only timing remains.
#:   - "expensive-test-tier" — a pure cost and machine-load call.
#:
#: DELIBERATELY EXCLUDED, and why the list stays short rather than growing
#: by oversight:
#:   - "merging-to-main" and cross-repo commit assent — excluded outright,
#:     these are never delegable via this mechanism.
#:   - every keyword-gated skill — for those, the literal keyword IS the
#:     authorization; a grant covering one would become a second route
#:     around that keyword, defeating the reason the keyword gate exists.
DELEGABLE = frozenset(
    {
        "execute-approved-plan",
        "expensive-test-tier",
    }
)

#: Fixed filename under settings_home() this module reads/writes — the
#: single location-naming constant every public function routes through.
_GRANT_FILENAME = "fleet-delegation.json"

#: Generator-provenance declaration: write_fleet_delegation()'s only write is
#: `_grant_file()` = `settings_home() / _GRANT_FILENAME` — the operator's
#: coordinator settings home (COORDINATOR_SETTINGS_HOME / CLAUDE_HOME /
#: `~/.coordinator-claude-settings`), never a path inside this repo's tracked
#: tree. No tracked artifact exists for `GENERATES` to name.
GENERATES = []

#: Tolerance for the ``granted_at`` freshness check (Review: staff-eng
#: (the Staff Engineer), finding 4) — +/- 5 minutes of wall clock at write time. A
#: FUTURE ``granted_at`` outside this window is itself a rejection.
_GRANTED_AT_TOLERANCE = timedelta(minutes=5)

#: The absolute lease ceiling, measured against wall clock AT WRITE TIME —
#: never against the caller-supplied ``granted_at`` (see module docstring).
_MAX_LEASE = timedelta(hours=12)

#: Distinct reason strings for the two liveness-probe failure shapes a
#: diagnosing operator needs to tell apart — both resolve to the same
#: ABSENT boolean (see module docstring's "Liveness-probe failure").
_REASON_NOT_FOUND = "designated-process-not-found"
_REASON_UNREADABLE_FMT = "designated-process-unreadable:{exc}"


def _grant_file() -> Path:
    """Resolve ``<settings_home()>/fleet-delegation.json`` — the single
    location-naming seam every public function in this module routes
    through, mirroring ``grant.py``'s own ``_grant_file`` shape.
    """
    return settings_home() / _GRANT_FILENAME


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp string into an aware ``datetime``, or
    ``None`` on anything that is not a parseable string (absent, wrong type,
    malformed). Accepts a trailing ``Z`` (``core.now_iso()``'s own output
    shape) by translating it to ``+00:00`` before delegating to
    ``datetime.fromisoformat`` — the stdlib parser does not accept a bare
    ``Z`` suffix.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def write_fleet_delegation(
    *,
    designated_pid: int,
    designated_create_time: float,
    classes: List[str],
    granted_at: str,
    expires_at: Optional[str],
    granted_by: str,
    note: str,
    authorship_start_pid: Optional[int] = None,
) -> Tuple[bool, Optional[str]]:
    """Create-or-overwrite the fleet-delegation grant.

    Every HARD CONSTRAINT (see module docstring's "Write-time validation")
    is checked BEFORE anything is written; a rejection returns
    ``(False, <reason>)`` with the file untouched — the atomic ``mkstemp`` +
    ``os.replace`` discipline means a rejected write leaves no partial file,
    same as ``grant.py``'s own writer.

    ``authorship_start_pid`` is exposed purely for test injection (threaded
    straight through to ``grant_authorship.authorship_verdict``); production
    callers should not need to pass it — the default resolves the CALLING
    process's own ancestry.

    Returns ``(True, None)`` on success; ``(False, reason)`` on ANY
    rejection or infra failure (settings-home dir uncreatable, write/replace
    failure) — never raises for a caller-supplied validation failure.
    """
    verdict = authorship_verdict(authorship_start_pid)
    if verdict.verdict is not Verdict.HUMAN:
        return False, f"authorship-refused:{verdict.verdict.value}:{verdict.reason}"

    if granted_by != _VALID_GRANTED_BY:
        return False, f"granted_by must be {_VALID_GRANTED_BY!r}, got {granted_by!r}"

    if not expires_at:
        return False, "expires_at is required"

    granted_at_dt = _parse_iso(granted_at)
    if granted_at_dt is None:
        return False, f"granted_at is not a parseable ISO-8601 timestamp: {granted_at!r}"

    expires_at_dt = _parse_iso(expires_at)
    if expires_at_dt is None:
        return False, f"expires_at is not a parseable ISO-8601 timestamp: {expires_at!r}"

    now = datetime.now(timezone.utc)

    # A future granted_at is itself a rejection, not a lease-length input
    # (Review: staff-eng (the Staff Engineer), finding 4) — checked via the SAME +/-5min
    # tolerance window as a stale one, so this is one comparison, not a
    # separate "granted_at in the future" branch.
    if abs((granted_at_dt - now)) > _GRANTED_AT_TOLERANCE:
        return False, (
            "granted_at is not within 5 minutes of wall clock at write time "
            f"(granted_at={granted_at!r}, now={now.isoformat()})"
        )

    # The 12h ceiling anchors to the clock at write time, never to the
    # caller-supplied granted_at (module docstring; Review: staff-eng
    # (the Staff Engineer), finding 4).
    if expires_at_dt > now + _MAX_LEASE:
        return False, (
            "expires_at is more than 12h after wall clock at write time "
            f"(expires_at={expires_at!r}, now={now.isoformat()})"
        )

    requested_classes = list(classes or [])
    if not requested_classes:
        return False, "classes must be a non-empty list"

    never_delegable_hit = NEVER_DELEGABLE.intersection(requested_classes)
    if never_delegable_hit:
        return False, f"class(es) not delegable: {sorted(never_delegable_hit)}"

    unknown_classes = [c for c in requested_classes if c not in DELEGABLE]
    if unknown_classes:
        return False, (
            f"class(es) not in the delegable allow-list: {sorted(unknown_classes)} "
            f"(accepted: {sorted(DELEGABLE)})"
        )

    record = {
        "schema_version": 1,
        "designated": {"pid": designated_pid, "create_time": designated_create_time},
        "classes": requested_classes,
        "granted_at": granted_at,
        "expires_at": expires_at,
        "granted_by": granted_by,
        "authorship": {"verdict": verdict.verdict.value, "reason": verdict.reason},
        "note": note,
    }

    grant_file = _grant_file()
    sdir = grant_file.parent
    try:
        os.makedirs(sdir, exist_ok=True)
    except OSError:
        return False, "settings-home directory uncreatable"

    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f"{_GRANT_FILENAME}.", dir=str(sdir))
    except OSError:
        return False, "temp file creation failed"
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(record, fh)
            fh.write("\n")
        os.replace(tmp_name, grant_file)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            # Best-effort tmp-file cleanup on the error path; the caller
            # already gets a False return regardless.
            pass
        return False, "write/replace failed"
    return True, None


def read_fleet_delegation() -> Optional[dict]:
    """Raw reader — returns the grant record exactly as persisted on disk,
    or None if absent/unreadable/malformed.

    NO liveness check, NO expiry check, NO validation — this is the raw
    artifact. ``check_fleet_delegation`` is the authorization-grade
    predicate; use that to decide whether to ROUTE to the designated
    session.
    """
    grant_file = _grant_file()
    if not grant_file.is_file():
        return None
    try:
        raw = grant_file.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    return record


def _designated_live(designated: Any) -> Tuple[bool, str]:
    """Probe the designated ``(pid, create_time)`` pair via ``psutil``,
    fail-closed on ANY probe exception (Review: staff-eng (the Staff Engineer), finding
    8 — see module docstring's "Liveness-probe failure").

    Returns ``(live, reason)``. ``live`` is True only when the process at
    ``pid`` exists AND its ``create_time()`` matches the stored value —
    a recycled PID (different create_time) reads not-live, same shape as
    ``core.stable_pid_alive``'s birth-instant compare, but this module does
    its own direct probe rather than routing through that function: this is
    not a SESSION liveness decision (``core.stable_pid_alive`` is reserved
    for that single call site per ``liveness.py``'s single-liveness-key
    invariant), it is a one-off probe of an arbitrary designated process.
    """
    if not isinstance(designated, dict):
        return False, "designated-malformed"
    pid = designated.get("pid")
    stored_create_time = designated.get("create_time")
    if not isinstance(pid, int) or not isinstance(stored_create_time, (int, float)):
        return False, "designated-malformed"

    ps = _psutil()
    if ps is None:
        return False, "walk-miss:psutil-absent"

    try:
        proc = ps.Process(pid)
        current_create_time = proc.create_time()
    except ps.NoSuchProcess:
        return False, _REASON_NOT_FOUND
    except Exception as exc:  # AccessDenied, OSError, or any other probe failure
        return False, _REASON_UNREADABLE_FMT.format(exc=type(exc).__name__)

    if current_create_time != stored_create_time:
        return False, _REASON_NOT_FOUND
    return True, "designated-process-live"


def check_fleet_delegation(decision_class: str) -> Tuple[bool, Optional[dict]]:
    """THE authorization predicate: does a live, unexpired grant cover
    ``decision_class``?

    Returns ``(granted, record)``. ``record`` is the raw parsed dict
    whenever one was found and parsed as an object, REGARDLESS of whether
    ``granted`` is True — mirroring ``check_tier_u_grant``'s shape so a
    denial or audit line can still quote the grant's fields.

    ABSENT (``granted=False``) — byte-identical in RETURN SHAPE to the
    no-file case — on: no file, malformed JSON, unknown ``schema_version``,
    expiry (``now >= expires_at``), a non-HUMAN ``authorship`` verdict,
    ``decision_class`` not present in the record's own ``classes``, or the
    designated ``(pid, create_time)`` pair not LIVE (including any
    liveness-probe exception — see ``_designated_live``). No warning
    branch, no grace window.
    """
    record = read_fleet_delegation()
    if record is None:
        return False, None

    if record.get("schema_version") != 1:
        return False, record

    authorship = record.get("authorship")
    if not isinstance(authorship, dict) or authorship.get("verdict") != Verdict.HUMAN.value:
        return False, record

    if record.get("granted_by") != _VALID_GRANTED_BY:
        return False, record

    classes = record.get("classes")
    if not isinstance(classes, list) or decision_class not in classes:
        return False, record

    expires_at_dt = _parse_iso(record.get("expires_at"))
    if expires_at_dt is None:
        return False, record
    now = datetime.now(timezone.utc)
    if now >= expires_at_dt:
        return False, record

    live, _reason = _designated_live(record.get("designated"))
    if not live:
        return False, record

    return True, record
