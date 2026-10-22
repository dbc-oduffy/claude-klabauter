"""Field-fill rule for fleet.migrate_prose_queue: RawRow -> CandidateEntry per family."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from coordinator_core.ops.fleet._prose_queue_contract import (
    BUCKET_PARSE_ERROR,
    BUCKET_SKIPPED,
    BUCKET_TO_MIGRATE,
    BUCKET_UNIVERSAL_LEFTOVER,
    CLOSURE_TOKEN_RE,
    FAMILIES,
    MIGRATED_INCOMPLETE_TAG,
    NOT_RECORDED,
    STATUS_CLOSED_RE,
    CandidateEntry,
    RawRow,
)

FLAG_AMBIGUOUS = "ambiguous"
FLAG_CLOSED = "closed-in-source"
FLAG_PARSE_ERROR_PREFIX = "parse-error:"

UNIVERSAL_TAG = "[universal]"

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DSR_ID_RE = re.compile(r"DSR-(\d{4}-\d{2}-\d{2})-\d+")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
_PROPOSED_TARGET_RE = re.compile(r"^proposed target:\s*", re.IGNORECASE)

# Header name (casefolded) -> field role. Columns not listed append to body.
_HEADER_ROLES: Dict[str, str] = {
    "id": "id",
    "system": "surface",
    "severity": "severity",
    "description": "description",
    "summary": "description",
    "source": "source",
    "status": "status",
    "found": "created",
    "added": "created",
    "why blocked": "why_blocked",
}

_CLOSED_SYNONYMS = frozenset({"closed", "resolved", "fixed", "done"})
_STATUS_PASSTHROUGH = frozenset({"open", "deferred"})
_SEVERITIES = frozenset({"P0", "P1", "P2", "P3"})

# Evaluated in order; first match wins.
_CHANGE_KIND_RULES: Tuple[Tuple[str, str], ...] = (
    (".sh", "script-edit"),
    ("SKILL.md", "skill-edit"),
    ("docs/wiki/", "wiki-append"),
)
_CHANGE_KIND_DEFAULT = "script-edit"


def infer_change_kind(surface: str) -> Tuple[str, bool]:
    """Return (change_kind, ambiguous); ambiguous is True when the default was applied."""
    for needle, kind in _CHANGE_KIND_RULES:
        if needle in surface:
            return kind, False
    return _CHANGE_KIND_DEFAULT, True


def first_sentence(text: str) -> str:
    """First sentence of text (whitespace collapsed); the whole text when no terminator exists."""
    collapsed = " ".join(text.split())
    return _SENTENCE_END_RE.split(collapsed, maxsplit=1)[0].strip()


def _is_closure(value: str) -> bool:
    v = value.strip()
    return bool(CLOSURE_TOKEN_RE.match(v) or STATUS_CLOSED_RE.search(v))


def _map_status(value: str) -> Optional[str]:
    """Synonym-table mapping; returns a status enum value, 'closed', or None when unmapped."""
    token = value.strip().strip("*").strip().casefold()
    if token in _CLOSED_SYNONYMS:
        return "closed"
    if token == "wontfix":
        return "wontfix"
    if token in _STATUS_PASSTHROUGH:
        return token
    return None


def _split_cells(row: RawRow) -> Tuple[Dict[str, str], List[str]]:
    """Return (role -> value, extra 'Header: cell' body lines) from a row's cells."""
    roles: Dict[str, str] = {}
    extras: List[str] = []
    for key, raw in row.cells.items():
        value = (raw or "").strip()
        role = _HEADER_ROLES.get(key.strip().casefold())
        if role is None:
            if value and not key.strip().isdigit():
                extras.append(f"{key.strip()}: {value}")
            continue
        if value or role not in roles:
            roles[role] = value
    return roles, extras


def _positional_pipe(row: RawRow) -> Optional[Dict[str, str]]:
    """Dated pipe-list row (positional cells 0-4: created | from_repo | surface | body | target)."""
    cells = row.cells
    if "0" not in cells or "3" not in cells:
        return None
    if not _DATE_RE.fullmatch(cells["0"].strip()):
        return None
    out = {
        "created": cells["0"].strip(),
        "from_repo": cells.get("1", "").strip(),
        "surface": cells.get("2", "").strip(),
        "description": cells["3"].strip(),
    }
    target = "|".join(v for k, v in sorted(cells.items()) if k.isdigit() and int(k) >= 4).strip()
    out["proposed_action"] = _PROPOSED_TARGET_RE.sub("", target).strip()
    return out


def _segment_body(segment: str) -> str:
    """Segment with list bullet, table pipes and a leading bold marker removed."""
    text = segment.strip()
    if text.startswith("- "):
        text = text[2:].strip()
    return text.strip("|").strip()


def _parse_error(row: RawRow, reason: str) -> CandidateEntry:
    return CandidateEntry(
        fields={},
        flags=[FLAG_PARSE_ERROR_PREFIX + reason],
        bucket_hint=BUCKET_PARSE_ERROR,
        source_row=row,
    )


def fill_fields(family: str, row: RawRow) -> CandidateEntry:
    """Map one RawRow into schema fields for family, choosing its bucket_hint and self-flags."""
    if family not in FAMILIES:
        raise ValueError(f"unknown family: {family!r}")

    if UNIVERSAL_TAG in row.segment:
        return CandidateEntry(
            fields={},
            flags=["universal-leftover"],
            bucket_hint=BUCKET_UNIVERSAL_LEFTOVER,
            source_row=row,
        )

    pipe = _positional_pipe(row)
    roles, extras = ({}, []) if pipe is not None else _split_cells(row)
    flags: List[str] = []

    legacy_id = roles.get("id", "")
    if not legacy_id:
        m = _DSR_ID_RE.match(row.segment.lstrip("-| *\t"))
        if m:
            legacy_id = m.group(0)
    if not legacy_id and "0" in row.cells and _DSR_ID_RE.fullmatch(row.cells["0"].strip()):
        legacy_id = row.cells["0"].strip()

    # Description / body.
    if pipe is not None:
        description = pipe["description"]
    else:
        description = roles.get("description", "")
        if "description" not in roles:
            text = _segment_body(row.segment)
            if legacy_id:
                text = re.sub(r"^\**" + re.escape(legacy_id) + r"\**\s*\|?\s*", "", text, count=1)
            description = text.strip()
    body = description
    if extras:
        body = (body + "\n" + "\n".join(extras)).strip()

    # created: date cell, else date embedded in the ID, never today.
    created = ""
    if pipe is not None:
        created = pipe["created"]
    else:
        m = _DATE_RE.search(roles.get("created", ""))
        if m:
            created = m.group(0)
        else:
            dm = _DSR_ID_RE.search(legacy_id)
            if dm:
                created = dm.group(1)
            else:
                dm = _DATE_RE.search(legacy_id)
                if dm:
                    created = dm.group(0)
    if not created:
        return _parse_error(row, "missing-created")
    title = first_sentence(description)
    if not title:
        return _parse_error(row, "missing-title")
    if not body:
        return _parse_error(row, "missing-body")

    # Closure-token rule precedes the synonym table.
    status_cell = roles.get("status", "")
    if pipe is None and "status" in roles:
        closure_candidates = [status_cell]
    elif pipe is None and legacy_id.startswith("DSR-"):
        closure_candidates = [v for v in row.cells.values()] if row.cells else []
        if not row.cells:
            closure_candidates = [row.segment.split(legacy_id, 1)[-1].lstrip("* |-:")]
    else:
        closure_candidates = []
    if any(_is_closure(c) for c in closure_candidates if c):
        return CandidateEntry(
            fields={"created": created, "title": title, "body": body},
            flags=[FLAG_CLOSED],
            bucket_hint=BUCKET_SKIPPED,
            source_row=row,
        )

    if "status" in roles and status_cell:
        status = _map_status(status_cell)
        if status is None:
            return _parse_error(row, "unmapped-status")
    else:
        status = "open"
    if status in ("closed", "wontfix"):
        return CandidateEntry(
            fields={"created": created, "title": title, "body": body},
            flags=[FLAG_CLOSED],
            bucket_hint=BUCKET_SKIPPED,
            source_row=row,
        )

    fields: Dict[str, object] = {"created": created, "title": title, "body": body, "status": status}

    # severity: bug and debt carry it; improvement has no field, so it is kept in the body.
    severity = roles.get("severity", "")
    if severity:
        sev = severity.strip().strip("*").strip().upper()
        if sev not in _SEVERITIES:
            return _parse_error(row, "unmapped-severity")
        if family == "improvement-queue":
            fields["body"] = f"{fields['body']}\nSeverity: {sev}"
        else:
            fields["severity"] = sev
    elif family == "bug-backlog":
        return _parse_error(row, "missing-severity")

    incomplete = False

    def constant(name: str) -> None:
        nonlocal incomplete
        fields[name] = NOT_RECORDED
        incomplete = True

    surface = pipe["surface"] if pipe is not None else roles.get("surface", "")
    source = roles.get("source", "")

    if family == "improvement-queue":
        if surface:
            fields["surface"] = surface
        else:
            constant("surface")
        proposed = pipe["proposed_action"] if pipe is not None else ""
        if proposed:
            fields["proposed_action"] = proposed
        else:
            constant("proposed_action")
        kind, ambiguous = infer_change_kind(surface)
        fields["change_kind"] = kind
        if ambiguous:
            flags.append(FLAG_AMBIGUOUS)
        fields["queue_scope"] = "project"
        if source:
            fields["body"] = f"{fields['body']}\nSource: {source}"
    elif family == "bug-backlog":
        if surface:
            fields["surface"] = surface
        else:
            constant("surface")
        if source:
            fields["body"] = f"{fields['body']}\nSource: {source}"
    else:
        if surface:
            fields["surface"] = surface
        if source:
            fields["source"] = source
        else:
            constant("source")
        constant("risk")
        constant("proposed_action")

    evidence: List[str] = []
    if legacy_id:
        evidence.append(f"legacy-id: {legacy_id}")
    from_repo = pipe["from_repo"] if pipe is not None else ""
    if from_repo and from_repo.casefold() != "self":
        evidence.append(f"from_repo: {from_repo}")
    if evidence:
        fields["evidence"] = "; ".join(evidence)

    why_blocked = roles.get("why_blocked", "")
    if why_blocked:
        fields["why_blocked"] = why_blocked

    if incomplete:
        fields["tags"] = [MIGRATED_INCOMPLETE_TAG]
        flags.append(MIGRATED_INCOMPLETE_TAG)

    return CandidateEntry(fields=fields, flags=flags, bucket_hint=BUCKET_TO_MIGRATE, source_row=row)
