"""Read a peer session's receiver-state verdict, as written by claude-klabauter's sensor.

PURPOSE. This is the DoE-side reader for `receiver-state.json` (roadmap `gem-01`,
baton `gem-11` AC5, executable spec `archive/handoffs/2026-08/2026-08-14_120000_roadmap-gem-03.md`).
It answers exactly one question for exactly one peer: **what did claude-klabauter's own
verdict ladder last say about that session?** It is Arm A of the carrier choice
made in `gem-11` -- see `docs/decisions/DR-group-em-read-pass-carrier.md` for
the full ladder vocabulary, the arm-choice measurement, and why Arm A won.

THE CARRIER -- `.git/coordinator-sessions/<session_id>/receiver-state.json`,
written by claude-klabauter's `coordinator_core.session.receiver_state` module
(`write_receiver_state`). See the DR above for the vocabulary and the
measurement backing this choice.

Distinct surface from `coordinator_core.session.receiver_state.read_receiver_state`
(`coordinator_core/group_em/read_pass.py`'s own reader leg): that module's port
deliberately chose NOT to import this one (its own docstring: "the reader leg
no longer imports `coordinator.lib.receiver_state_reader` ... it calls
Claude-klabauter's own `read_receiver_state(sid, cwd)` instead"), because the two return
shapes differ (this module's explicit `"UNAVAILABLE"` verdict vs. that one's
`None`). This module lands as its own published surface for DoE-side callers
(group-em-enter.py, receiver-state-sensor.py, the group-em skills) that still
import it by this name; it is not folded into `session.receiver_state`.

NEGATIVE SPEC -- what this module deliberately does NOT do:

- **It re-implements no verdict.** Every field this module returns is a
  projection of `verdict`, `reason`, `stamped_at`, `unmodelled_type`,
  `unmodelled_subtype` -- fields the ladder already wrote. Nothing here
  re-reads a transcript, re-derives PAUSED/PRODUCING/UNKNOWN, or classifies a
  session. If a branch here appears to decide *why* a session is paused, that
  is a bug in this module, not a feature -- the ladder owns that call.
- **It never reads `cpu_cursor`.** Optional, tiebreak-only on the write side,
  and gated off (`_CPU_LEG_ENABLED = False`) in the source at the ref above.
  No code path in this module accesses it.
- **It never reaches into transcript content.** Only the five documented
  top-level fields (`schema_version`, `session_id`, `verdict`, `reason`,
  `stamped_at`) plus the two optional diagnostic fields
  (`unmodelled_type`/`unmodelled_subtype`) are ever read.
- **No timing, idle-duration, or session-age predicate decides a state.** The
  one time comparison in this module is the staleness window below, and it
  can only ever *withdraw* a verdict to `UNAVAILABLE` -- never manufacture
  PAUSED/PRODUCING/UNKNOWN from elapsed time.
- **No enumeration, classification, presentation, or send.** One session in,
  one verdict out. Those are `gem-13` and `gem-14`.

THE STALENESS RULE -- explicit, and stated as an assumption, not a
measurement. The artifact is written only at a Stop/SubagentStop turn
boundary, so a read is always of a past boundary, never live state. The
8-record sample available at authoring time spans thirteen minutes with no
discharge/re-open pairing to measure an age distribution from (unlike the
losing arm's 132-obligation corpus), so no percentile can be honestly quoted
here. Per the gem-03 spec's option (b), this module instead states an
explicit wall-clock cutoff as a documented assumption:
`STALE_AFTER_SECONDS = 3600` (one hour) -- long enough that a session which
last wrote a verdict within the window is plausibly still the same run, short
enough that a record from a session that has since exited or been abandoned
is not reported as if it were current. This is NOT the `90s`
`_TOOL_UNANSWERED_GRACE_SECONDS` constant from the source ladder (that grace
window governs a different question -- how long a tool call may sit
unanswered before the ladder itself downgrades PRODUCING to PAUSED -- and the
gem-03 spec explicitly forbids adopting it here without relabeling; this
module does not adopt it at all). A record older than the window is withdrawn
to `UNAVAILABLE` with `reason="stale"`, never re-labelled as a fresher
verdict.

`UNAVAILABLE` is a first-class, fourth verdict value -- distinct from the
ladder's own `PAUSED`/`PRODUCING`/`UNKNOWN` -- so a caller can always tell
"the carrier said nothing" (`UNAVAILABLE`, with a `reason` naming why) from
"the carrier said UNKNOWN" (a real ladder verdict). No file, an unreadable
file, malformed JSON, a missing/wrong-typed required field, an unrecognised
`schema_version`, and a stale-beyond-window record are all `UNAVAILABLE`
with a distinguishing `reason` -- never an exception, never folded into a
ladder-shaped state.

Path resolution: "published" class (§ Path resolution,
docs/plans/2026-09-18-doe-holds-no-scripts.md) with no seam to fix on
arrival -- `repo_root` is a REQUIRED caller-supplied argument on every
public function here, never derived from this module's own `__file__` or
any ambient probe, so this module's read of the DoE-claude@b644d5a9 lesson
that wave the rest of this chunk fixes does not apply to it: nothing here
ever resolved a path from its own location in the first place.

Spec backlink: `archive/handoffs/2026-08/2026-08-14_120000_roadmap-gem-03.md`,
`state/handoffs/2026-08-30-2026-08-29_190000_roadmap-gem-11.md` AC5.
Arrived from DoE-claude coordinator/lib/receiver_state_reader.py
(docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk W3-C6).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

#: ISO-8601 variant (fractional seconds, a numeric offset instead of `Z`),
#: every record silently collapses to REASON_MALFORMED/UNAVAILABLE with no
#: § THE STALENESS RULE for why no measured percentile is quoted.
STALE_AFTER_SECONDS = 3600

_SIBLING_FILENAME = "receiver-state.json"
_SESSIONS_DIRNAME = os.path.join(".git", "coordinator-sessions")
_SAFE_SID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_LADDER_VERDICTS = frozenset({"PAUSED", "PRODUCING", "UNKNOWN"})

VERDICT_UNAVAILABLE = "UNAVAILABLE"

REASON_NO_FILE = "no-file"
REASON_UNREADABLE = "unreadable"
REASON_MALFORMED = "malformed"
REASON_BAD_SCHEMA_VERSION = "unrecognised-schema-version"
REASON_STALE = "stale"

_SUPPORTED_SCHEMA_VERSION = 1


def receiver_state_path(repo_root: str, session_id: str) -> Optional[str]:
    # A bare "." or ".." matches _SAFE_SID_RE (no separator to catch) but is
    if not session_id or session_id in (".", "..") or not _SAFE_SID_RE.match(session_id):
        return None
    return os.path.join(repo_root, _SESSIONS_DIRNAME, session_id, _SIBLING_FILENAME)


def _parse_stamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _unavailable(session_id: str, reason: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "verdict": VERDICT_UNAVAILABLE,
        "reason": reason,
        "unmodelled_type": None,
        "unmodelled_subtype": None,
        "stamped_at": None,
    }


def read_receiver_state(
    repo_root: str,
    session_id: str,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """One peer in, one verdict out. Never raises on a missing or malformed carrier.

    `repo_root` is REQUIRED and never resolved here -- the caller holds it.

    Returns a dict with `session_id`, `verdict` (one of `PAUSED`, `PRODUCING`,
    `UNKNOWN` -- the ladder's own bare tags -- or `UNAVAILABLE`), `reason`
    (the ladder's own reason string when `verdict` is a ladder tag, or one of
    the `REASON_*` constants when `verdict == UNAVAILABLE`), `unmodelled_type`
    / `unmodelled_subtype` (populated only when the ladder recorded them, i.e.
    `verdict == "UNKNOWN"` with an unmodelled line; None otherwise), and
    `stamped_at` (the record's own stamp, ISO-8601, or None when unavailable
    for a reason that precedes having a stamp at all).

    Re-reads the file on every call -- callers must read immediately before
    acting, never from a snapshot taken earlier in the same turn.
    """
    path = receiver_state_path(repo_root, session_id)
    if path is None or not os.path.isfile(path):
        return _unavailable(session_id, REASON_NO_FILE)

    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return _unavailable(session_id, REASON_UNREADABLE)

    try:
        record = json.loads(raw)
    except ValueError:
        return _unavailable(session_id, REASON_MALFORMED)
    if not isinstance(record, dict):
        return _unavailable(session_id, REASON_MALFORMED)

    schema_version = record.get("schema_version")
    if schema_version != _SUPPORTED_SCHEMA_VERSION:
        return _unavailable(session_id, REASON_BAD_SCHEMA_VERSION)

    verdict = record.get("verdict")
    reason = record.get("reason")
    stamped_at_raw = record.get("stamped_at")
    if (
        not isinstance(verdict, str)
        or verdict not in _LADDER_VERDICTS
        or not isinstance(reason, str)
        or not isinstance(stamped_at_raw, str)
    ):
        return _unavailable(session_id, REASON_MALFORMED)

    stamp = _parse_stamp(stamped_at_raw)
    if stamp is None:
        return _unavailable(session_id, REASON_MALFORMED)

    now = now or datetime.now(timezone.utc)
    age_seconds = int((now - stamp).total_seconds())
    if age_seconds > STALE_AFTER_SECONDS:
        stale = _unavailable(session_id, REASON_STALE)
        stale["stamped_at"] = stamped_at_raw
        return stale

    if verdict == "UNKNOWN":
        unmodelled_type = record.get("unmodelled_type")
        unmodelled_subtype = record.get("unmodelled_subtype")
    else:
        unmodelled_type = None
        unmodelled_subtype = None

    return {
        "session_id": session_id,
        "verdict": verdict,
        "reason": reason,
        "unmodelled_type": unmodelled_type if isinstance(unmodelled_type, str) else None,
        "unmodelled_subtype": unmodelled_subtype if isinstance(unmodelled_subtype, str) else None,
        "stamped_at": stamped_at_raw,
    }
