"""
coordinator_core.ops.queue_age_ping — JSON-RPC "queue.age_ping" operation.

Purpose: COMPUTE-ONLY primitive that finds ``status: deferred`` entries across any of
the three queue families (``improvement-queue``, ``debt-backlog``, ``bug-backlog``)
whose age exceeds a caller-supplied threshold, so a parked entry re-enters triage
instead of sitting in ``deferred`` forever. Mirrors the age-predicate SHAPE of
``coordinator_core.ops.handoff_gate_aging`` (14d default threshold, computed-not-stored
age) but returns structured records for a caller to render, not human-readable lines,
and generalizes over queue families instead of being handoff-specific.

Spec backlink: pln-queue-triage-terminus-ops-clus-043c40 § C4

No queue schema gains an age/last-recheck field — age is always computed from
``created`` at call time, exactly as ``handoff_gate_aging`` computes handoff age from
``created`` rather than storing it.

Acknowledgment mechanism (load-bearing)
----------------------------------------
The op accepts a caller-supplied ``exclude_paths`` set — paths already dispositioned
this triage cycle — and omits them from the report. Ack/triage state lives entirely on
the CALLER side (e.g. the ceremony script tracking what it already surfaced this run);
this op stores nothing between calls. Without ``exclude_paths`` the op would re-report
the same re-deferred entry on every call forever, which is exactly the
"deferred becomes a synonym for closed" failure this op exists to catch — moved one
layer down. The parameter is a plain ``list[str]`` of repo-relative paths today so it
is trivially upgradeable later to a schema-backed ``last_triaged``/``last_recheck``
field (a caller could derive the same exclude set from that field) without changing
this op's wire contract.

Response envelope (RATIFIED by claude-central-em 2026-07-23)
--------------------------------------------------------------
``{"status": "ok", "entries": [...]}`` where each entry dict carries:
  - ``path``        (str)            — repo-relative record path.
  - ``family``      (str)            — one of the three queue-family directory names.
  - ``created``     (str)            — the raw ``created`` frontmatter scalar.
  - ``age_days``    (int)            — computed ``today - created`` in days.
  - ``status``      (str)            — always ``"deferred"`` (the input predicate).
  - ``title``       (str | None)     — the entry's ``title`` frontmatter scalar, so the
                                        caller can render a human list without opening
                                        each file. ``None`` on a legacy record missing it.
  - ``severity``    (str, optional)  — present iff the underlying record carries a
                                        ``severity`` scalar (bug-backlog: schema-declared;
                                        debt-backlog: optional). Never synthesized.
  - ``why_blocked`` (str, optional)  — present iff the record carries the scalar.
                                        Surfaced when present, never required — even
                                        though debt-backlog schema-requires it on
                                        ``deferred`` entries, a legacy record can still
                                        violate that and this op must not crash on it.

Empty result is ``{"status": "ok", "entries": []}`` — never ``null``.

Ordering: entries are sorted by ``age_days`` descending (oldest-parked first, the
entries most overdue for re-triage), then by ``path`` ascending as a stable tiebreak.

Negative-spec
-------------
  - Does NOT glob, walk directories, or parse YAML/frontmatter itself — all record
    loading routes through ``coordinator_core.ops.queue_family.load_family_records``,
    which itself delegates to ``query_records``.
  - Does NOT add an age/last-recheck field to any queue schema — age is computed here,
    every call, from ``created``.
  - Does NOT add 'priority', 'estimate', or 'tags' to the response envelope — the
    consumer explicitly declined all three.
  - Does NOT synthesize a ``severity`` default for a record that lacks the field.
  - Does NOT branch per-family (``if family == "debt-backlog": ...``) — families are
    iterated identically; the only per-family difference (which fields are declared)
    is irrelevant here since every field read is a plain optional-scalar lookup.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.queue_family import FAMILY_TO_RECORD_TYPE, load_family_records

AGING_THRESHOLD_DAYS = 14
"""Default staleness threshold in days — mirrors handoff_gate_aging's precedent."""


def _parse_date(value: object) -> Optional[date]:
    """Parse a YYYY-MM-DD-ish scalar into a date, or None if unparseable/absent."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _entry_for(family: str, record: dict, today: date, threshold_days: int) -> Optional[dict]:
    """Build one response entry for ``record``, or None if it doesn't qualify.

    Qualifies iff: ``status`` is ``"deferred"``, ``created`` is a parseable date, and
    ``today - created`` in days is ``>= threshold_days``.
    """
    fm = record.get("frontmatter") or {}
    if fm.get("status") != "deferred":
        return None

    created_raw = fm.get("created")
    created_date = _parse_date(created_raw)
    if created_date is None:
        return None

    age_days = (today - created_date).days
    if age_days < threshold_days:
        return None

    entry: dict = {
        "path": record.get("path"),
        "family": family,
        "created": created_raw,
        "age_days": age_days,
        "status": "deferred",
        "title": fm.get("title"),
    }
    severity = fm.get("severity")
    if severity is not None:
        entry["severity"] = severity
    why_blocked = fm.get("why_blocked")
    if why_blocked is not None:
        entry["why_blocked"] = why_blocked
    return entry


def age_ping(
    repo_root: Path,
    *,
    families: Optional[list[str]] = None,
    threshold_days: int = AGING_THRESHOLD_DAYS,
    exclude_paths: Optional[list[str]] = None,
    today: Optional[date] = None,
) -> list[dict]:
    """Scan ``families`` (default: all three) for aged ``deferred`` entries.

    ``exclude_paths`` — repo-relative paths the caller has already dispositioned this
    cycle; omitted from the result unconditionally (ack state lives on the caller side,
    never stored here). Matched via plain string membership against each entry's
    ``path`` field, so each entry MUST be exactly the ``path`` string this op
    previously returned (POSIX-separator, repo-relative, as ``query_records``/``rel_id``
    produce it) — an OS-reconstructed path (e.g. backslash-joined on Windows) will
    silently fail to match and the entry will resurface on the next call, looking
    like the acknowledgment mechanism isn't working. ``today`` is injectable for
    deterministic tests.
    """
    if today is None:
        today = date.today()
    target_families = families if families is not None else sorted(FAMILY_TO_RECORD_TYPE)
    exclude_set = set(exclude_paths or ())

    entries: list[dict] = []
    for family in target_families:
        for record in load_family_records(family, repo_root):
            path = record.get("path")
            if path in exclude_set:
                continue
            entry = _entry_for(family, record, today, threshold_days)
            if entry is not None:
                entries.append(entry)

    entries.sort(key=lambda e: (-e["age_days"], e["path"]))
    return entries


@register_op("queue.age_ping")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    if repo_root is None:
        return {"exit_code": 1, "error": "queue.age_ping: repo_root is required"}

    families = params.get("families")
    threshold_days = params.get("threshold_days", AGING_THRESHOLD_DAYS)
    exclude_paths = params.get("exclude_paths")

    try:
        entries = age_ping(
            repo_root,
            families=families,
            threshold_days=threshold_days,
            exclude_paths=exclude_paths,
        )
    except Exception as exc:  # noqa: BLE001 — fail-loud with an envelope, not a crash
        return {"exit_code": 1, "error": f"queue.age_ping: {exc}"}

    return {"status": "ok", "entries": entries}
