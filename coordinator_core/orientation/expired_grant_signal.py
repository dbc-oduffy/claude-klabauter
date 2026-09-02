"""
coordinator_core.orientation.expired_grant_signal — surfaces a queue grant past
its own expiry, at GRANT-COUNT cost, never corpus-size cost.

Purpose: `docs/plans/2026-08-27-a-queue-deferral-is-a-grant-the-pm-issues.md`
closes the write side (a `status: deferred` record cannot land without a PM
grant, per C2-C4) but a granted deferral can still go stale silently: nothing
brings an expired grant back to the PM once its `deferred_until` has passed.
`queue.age_ping` (`state/kill-ledger.md` K-063) tried to solve the READ side of
this same problem and was killed for it — ~425-465ms of process time scanning
all 1,534 records to find the 6 that were deferred, a cost that scaled with
CORPUS SIZE. This module is that op's replacement, built to the opposite cost
shape: "what is overdue" is meant to be a read of the GRANT SET (currently one
record), never a walk of `state/{improvement-queue,debt-backlog,bug-backlog}/`.

HOW THE GRANT SET IS KNOWN, which is the whole engineering problem. There is no
grant-issuing surface in this repo to maintain an index at grant time -- the PM
grants, an author records, and a record can also arrive by `git pull` or be
edited by a sibling repo's tooling. So this module maintains its own index
against an MTIME WATERMARK: a `scandir` of the three queue directories is free
(0.00 ms process time over 1,637 records, measured 2026-08-29), and only records
whose mtime is newer than the last sweep are opened. Steady state opens ZERO
files. Bootstrap opens everything once, at ~94 ms, and never again.

That shape is what separates this from `queue.age_ping`, which was killed at
~425-465 ms for parsing all 1,534 records on every dispatch. Per-run cost here
scales with the number of CHANGED records, not with corpus size; the only
corpus-proportional term is a stat sweep that does not register on a
process-time clock.

THE FIRST VERSION OF THIS MODULE SHIPPED AS A READER WITH NO WRITER. It consumed
a conventional index path that no surface anywhere produced, so the section could
never render under any circumstance -- indistinguishable from a healthy box, and
exactly the failure `budget_breach_signal`'s own docstring names ("an instrument
nobody calls is indistinguishable from an instrument that was never built"),
reproduced in the module written by copying it. The producer is now attached to
the consumer here, and `test_expired_grant_signal.py` fails if they are ever
separated again.

Posture follows `budget_breach_signal`, NOT `hook_cancellation_signal`: render
NOTHING when no grant is past expiry (including "no index exists yet", which
is indistinguishable from "no grant is overdue" by design — the same
fail-open-to-silence contract every `emit_*` helper in this package already
keeps). An expired grant is a defect to act on, not an accepted residual, so a
standing line would train the eye to skip it.

Cold, orientation-regen-only, matching every other `emit_*` helper in
`regenerate_cache.py` — never `PreToolUse` or a dispatch hot path.

Fail-open throughout: a missing index, an unreadable index, a malformed entry,
an import failure, or any exception at all resolves to "" and the section is
omitted — matching every omit-when-empty section in `regenerate_cache.py`.

Negative-spec:
  - Does NOT open records that have not changed since the last sweep. The
    watermark is the mechanism; removing it turns this back into K-063.
  - Does NOT YAML-parse records to classify them. The corpus holds at least one
    record `yaml.safe_load` refuses outright, and a classifier that parses
    everything crashes on a defect unrelated to deferral.
  - Does NOT treat the index as a source of truth. It is derived state under
    gitignored `state/cache/`, rebuildable from the corpus at any time; a
    corrupt or absent index costs one bootstrap, never a wrong answer.
  - Does NOT compare an event-trigger `deferred_until` (a record whose expiry
    names a condition rather than a date, per C3's unresolved case) against
    today — an unparseable date is skipped, never treated as overdue and
    never treated as an error that raises.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import List


#: Generator-provenance declaration: `_write_index_atomically`'s only write is
#: `state/cache/queue-grants-index.json` (via `_grant_index_path`) — derived
#: state under gitignored `state/cache/` (module docstring's "Does NOT treat
#: the index as a source of truth"), never a tracked repo artifact.
GENERATES = []

_QUEUE_DIRS = ("improvement-queue", "debt-backlog", "bug-backlog")

#: Matches the `status:` FIELD set to `deferred`, anchored at column 0 so it can only
#: match a top-level key. Deliberately not a YAML load: the corpus contains at least
#: one record whose title begins with a backtick and which `yaml.safe_load` refuses
#: outright ("found character '`' that cannot start any token"), so a classifier that
#: parses every record crashes on a defect unrelated to deferral.
#:
#: ANCHORING IS NOT COSMETIC. A bare `"status: deferred" in text` substring test reads
#: the phrase wherever it appears, including inside a record's own prose — and records
#: ABOUT queue deferral quote it constantly. Measured on this corpus 2026-08-29: the
#: substring form reported 6 grants where 1 exists, wrongly indexing five records whose
#: status is `open` and whose bodies merely discuss deferral — one of them the very
#: record filed against this module. Each would have become a due-list candidate, and
#: the signal meant to catch a stale grant would have spent its credibility on five
#: records that were never deferred.
_DEFERRED_STATUS_RE = re.compile(
    r"^status:[ 	]*[\"']?deferred[\"']?[ 	]*$", re.MULTILINE
)


#: How long a CONDITION-form grant may sit before it surfaces anyway.
#:
#: A grant whose `deferred_until` names a condition ("revisit when a THIRD consumer
#: appears") cannot be compared against today — nothing here watches for a third
#: consumer. Left alone it never resurfaces, which is precisely the drift this
#: module exists to end, so a condition-form grant is not exempt from coming back;
#: it is on a timer instead of a date.
#:
#: This is a BACKSTOP, not an expiry, and the distinction is the whole point: it
#: does not overrule the grantor's condition, does not mark the park expired, and
#: does not invent a date the PM never gave. It says only "this has sat 90 days
#: and nobody is plausibly still watching for that condition — look again." The
#: alternative considered and rejected was refusing condition-form grants
#: outright, which penalises the one record on disk that was scrupulous about not
#: fabricating a date.
_CONDITION_GRANT_BACKSTOP_DAYS = 90


def _is_deferred(text: str) -> bool:
    """True when the record's top-level `status` field is `deferred`."""
    return _DEFERRED_STATUS_RE.search(text) is not None


def _grant_index_path(repo_root: Path) -> Path:
    """Where the grant set is cached. `state/cache/` is gitignored — this is
    derived state, rebuildable from the corpus at any time, never a source of
    truth and never something a reader must have."""
    return repo_root / "state" / "cache" / "queue-grants-index.json"


def _read_index(index_path: Path) -> tuple[float, dict]:
    """(watermark_mtime, {rel_path: deferred_until}). (0.0, {}) on anything
    unreadable, malformed, or absent — which triggers a full rebuild rather than
    an error, so a corrupted cache costs one bootstrap and never a wrong answer."""
    try:
        blob = json.loads(index_path.read_text(encoding="utf-8"))
        watermark = float(blob.get("watermark_mtime") or 0.0)
        grants = blob.get("grants")
        if not isinstance(grants, dict):
            return 0.0, {}
        return watermark, {str(k): v for k, v in grants.items()}
    except Exception:  # noqa: BLE001 — fail-open to a rebuild
        return 0.0, {}


def _deferred_until_of(path: Path) -> "str | None":
    """The record's `deferred_until` if it is currently deferred, else None.

    Reads the file once and scans lines rather than parsing YAML, for the reason
    in `_DEFERRED_STATUS_RE`. Returns "" for a deferred record whose expiry is absent
    or unparseable-shaped — it stays IN the index (it is a grant) but will never
    read as due, which is the correct treatment of C3's unresolved event-trigger
    case: present, tracked, never silently reported as overdue.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not _is_deferred(text):
        return None
    for line in text.splitlines():
        if line.startswith("deferred_until:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return ""


def refresh_grant_index(repo_root: Path) -> dict:
    """Bring the grant index up to date and return {rel_path: deferred_until}.

    COST SHAPE, which is this module's whole reason for existing (K-063 killed
    `queue.age_ping` for getting it wrong at ~425-465ms). Measured on this corpus
    at 1,637 records, 2026-08-29:

      - `os.scandir` + `stat` of all three queue directories: **0.00 ms** process
        time. Directory metadata is free; it is opening and parsing that is not.
      - Opening and substring-testing all 1,637: **109 ms** — paid ONCE, on the
        bootstrap run when no index exists.
      - Steady state: **zero files opened**. Only records whose mtime is newer
        than the stored watermark are read, and in a session where nobody touched
        a queue record that set is empty.

    So per-run cost scales with the number of CHANGED records, not with corpus
    size. The one corpus-proportional term is a stat sweep that does not register
    on a process-time clock, four orders of magnitude under the brightline. That
    is a different shape from the op that was killed, and the honest statement of
    the difference is: K-063 parsed 1,534 records on every dispatch; this opens
    zero on a normal one.

    WHY THE READER MAINTAINS THIS AND NOT THE WRITE GUARD, which was the obvious
    first answer and is the wrong one: a write-side index only ever learns about
    writes that pass through this harness. Records arriving by `git pull`, edited
    by a sibling repo's tooling, or changed by any process that is not a guarded
    Write would be invisible to it, and the index would drift silently — the
    failure mode being an expired grant that never surfaces, which is precisely
    what this signal exists to prevent. An mtime watermark sees every change to
    the bytes on disk regardless of who made it. It also keeps the PreToolUse
    guard free of state mutation, which it should be.

    Never raises: any failure returns the best grant set it has, and a failure to
    write the cache is not a failure to answer.
    """
    index_path = _grant_index_path(repo_root)
    watermark, grants = _read_index(index_path)
    high_water = watermark
    changed = 0

    for queue_dir in _QUEUE_DIRS:
        directory = repo_root / "state" / queue_dir
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if not entry.name.endswith(".yaml"):
                        continue
                    try:
                        mtime = entry.stat().st_mtime
                    except OSError:
                        continue
                    if mtime > high_water:
                        high_water = mtime
                    if mtime <= watermark:
                        continue  # unchanged since last refresh — do not open
                    changed += 1
                    rel = f"state/{queue_dir}/{entry.name}"
                    expiry = _deferred_until_of(Path(entry.path))
                    if expiry is None:
                        grants.pop(rel, None)  # no longer deferred (or gone)
                    else:
                        grants[rel] = expiry
        except OSError:
            continue

    # A record deleted outright never shows up in the scan, so drop index entries
    # whose file is gone. Bounded by GRANT count, not corpus size.
    for rel in list(grants):
        if not (repo_root / rel).is_file():
            grants.pop(rel, None)

    if changed or high_water > watermark or not index_path.is_file():
        _write_index_atomically(index_path, high_water, grants)
    return grants


def _write_index_atomically(index_path: Path, watermark: float, grants: dict) -> None:
    """Write via temp + `os.replace`, because this box runs dozens of concurrent
    sessions and every one of them regenerates its own orientation cache. A torn
    read of a half-written index would fail open to a rebuild rather than a wrong
    answer, but there is no reason to make that happen. A failed write is
    swallowed: the index is a cache, and being unable to save it must never stop
    the caller getting an answer."""
    try:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"watermark_mtime": watermark, "grants": grants}, indent=2, sort_keys=True
        )
        tmp = index_path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, index_path)
    except Exception:  # noqa: BLE001 — a cache that cannot be saved is still a cache
        try:
            tmp.unlink(missing_ok=True)  # type: ignore[possibly-undefined]
        except Exception:  # noqa: BLE001
            pass


def _parse_iso_date(value: object) -> "date | None":
    """Best-effort ISO-date parse. Returns None for anything not a clean
    calendar date -- including C3's unresolved event-trigger case ("no fixed
    calendar date -- the PM ruling names an event trigger, not a date") -- so
    such a grant is silently skipped rather than misread as always-overdue."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def _record_age_days(body: str, record: Path) -> "int | None":
    """Days since the record was created, for the condition-grant backstop.

    Anchored on the record's own `created:` field, NOT on file mtime. Mtime looks
    like the obvious clock and is the wrong one: any edit resets it, so a park that
    someone touches — to fix a typo, to add a field a re-vendor started requiring —
    silently restarts its 90 days and can be kept invisible forever by ordinary
    maintenance. That is a backstop that quietly stops backstopping, which is worse
    than none because it still reads as coverage. Measured instance: adding the
    newly-required `case_against` to the one condition-form grant on disk reset its
    mtime clock to zero on the same day this was written.

    Falls back to mtime only when `created:` is absent or unparseable, and answers
    None if neither resolves — the caller then omits the record rather than
    guessing at an age.
    """
    for line in body.splitlines():
        if line.startswith("created:"):
            created = _parse_iso_date(line.split(":", 1)[1].strip().strip("\"'"))
            if created is not None:
                return (date.today() - created).days
            break
    try:
        return (datetime.now() - datetime.fromtimestamp(record.stat().st_mtime)).days
    except OSError:
        return None


def emit_expired_grants(repo_root: Path) -> str:
    """Render the ``## Expired grants`` section's body lines, or ``""`` to
    omit the section entirely.

    Reads ONLY `_grant_index_path` -- never the backlog directories -- so cost
    scales with the size of the grant SET (today's index, if one exists),
    never with corpus size. A due entry is re-verified by opening only that
    one record (the "lazy per-record re-verify" the plan's spike measured at
    0.16ms), so a hand-edited record that no longer says ``status: deferred``
    self-heals out of the due-list without a corpus walk.

    Returns "" (omit) when: the index does not exist (true today -- see
    module docstring), is unreadable or malformed, no entry's
    ``deferred_until`` parses to a past-or-today date, or the re-verified
    record no longer carries ``status: deferred``.

    Never raises.
    """
    try:
        grants = refresh_grant_index(repo_root)
    except Exception:  # noqa: BLE001 — fail-open, same as every emit_* sibling
        return ""

    today = datetime.now().date()
    due_lines: List[str] = []
    for rel_path, raw_expiry in sorted(grants.items()):
        try:
            expiry = _parse_iso_date(raw_expiry)
            if expiry is None:
                # Condition-form grant: no date to compare, so fall back to the
                # backstop measured from when the record was last touched.
                record = repo_root / rel_path
                body = record.read_text(encoding="utf-8", errors="replace")
                if not _is_deferred(body):
                    continue
                age_days = _record_age_days(body, record)
                if age_days is None or age_days < _CONDITION_GRANT_BACKSTOP_DAYS:
                    continue
                due_lines.append(
                    f"- ⚠ `{rel_path}` — parked {age_days} days on a condition, "
                    f"not a date: {str(raw_expiry)[:80]}"
                )
                continue
            if expiry > today:
                continue
            # Lazy per-record re-verify: open ONLY the records that read as due,
            # so a hand-edited record that no longer says `status: deferred`
            # self-heals out of the due-list without a corpus walk. Bounded by
            # the number of DUE grants, which is normally zero.
            text = (repo_root / rel_path).read_text(encoding="utf-8", errors="replace")
            if not _is_deferred(text):
                continue
            due_lines.append(f"- ⚠ `{rel_path}` — grant expired {expiry.isoformat()}")
        except Exception:  # noqa: BLE001
            continue

    if not due_lines:
        return ""
    return "\n".join(due_lines)
