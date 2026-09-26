"""Section porter — Backlogs (envelope key: ``backlogs``).

Emits BacklogItemSummary records for the three backlog families (bug / debt / improvement),
each sourced from ``coordinator_core.ops.ceremony.records_query.query_records``. Records
missing any required string field (id/created/status/title) are quarantined into the
malformed bucket rather than aborting the emit; bug records whose ``severity`` is present but
outside the ``P0..P3`` enum are also quarantined (non-fatal, B-F1).

Emit-DERIVED fields are not applicable to this entity — the shape is fully parsed from
frontmatter, so nothing is left for C3/enrich to stamp here.

Port of: emit-cockpit-snapshot.sh (DoE 07eedcfb, 2026-07-19) — § SECTION 2,
  BacklogItemSummary. Byte/semantic parity port.
Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P02

Node-subprocess retirement: this section originally shelled out to
``node COORDINATOR_ROOT/bin/query-records.js --type <t> --limit 0 --format json``
(``bin/query-records.js`` — see ``_buildTypeToGlob`` at :211-272 and the ``.yaml``
whole-file frontmatter path at :1400-1410 for the bug/debt/improvement semantics this
section relies on). It now calls
``coordinator_core.ops.ceremony.records_query.query_records`` in-process — no ``node``
binary, no subprocess spawn. This is a mechanical delegate-target swap only: the
``--root``/``cwd`` resolution (``ctx.subprocess_root or ctx.repo_root``), the
fail-open-to-``[]`` failure posture, and every downstream field-validation/quarantine
rule below are unchanged.
"""

from __future__ import annotations

from typing import Any

from coordinator_core.ops.ceremony.records_query import query_records as _ceremony_query_records
from coordinator_core.ops.emit.context import EmitContext

from ._shared import normalize_frontmatter

_BACKLOG_TYPES = ("bug", "debt", "improvement")

_VALID_SEVERITY = frozenset({"P0", "P1", "P2", "P3"})


def _query_records(ctx: EmitContext, type_tag: str) -> list[dict]:
    worktree_root = ctx.subprocess_root if ctx.subprocess_root is not None else ctx.repo_root
    try:
        parsed = _ceremony_query_records(type_tag, worktree_root, limit=0)
    except (ValueError, SystemExit, OSError):
        return []
    return parsed if isinstance(parsed, list) else []


def _has_required_fields(fm: dict) -> bool:
    return all(isinstance(fm.get(k), str) for k in ("id", "created", "status", "title"))


def _severity_valid(type_tag: str, fm: dict) -> bool:
    if type_tag != "bug":
        return True
    severity = fm.get("severity")
    return severity is None or severity in _VALID_SEVERITY


def _build_record(ctx: EmitContext, type_tag: str, rec: dict) -> dict:
    fm = normalize_frontmatter(rec)
    path = rec.get("path")
    return {
        "type": type_tag,
        "id": fm.get("id"),
        "created": fm.get("created"),
        "status": fm.get("status"),
        "title": fm.get("title"),
        "repo": ctx.repo_name,
        "from_repo": fm.get("from_repo") or "claude-central-em",
        "coordinator_root_path": ".",
        "queue_scope": fm.get("queue_scope") or "project",
        "severity": (fm.get("severity") if type_tag == "bug" else None),
        "risk": (fm.get("risk") if type_tag == "debt" else None),
        "provenance": ctx.provenance("local_fs", path=path, derivation="parsed"),
    }


def _build_malformed(type_tag: str, rec: dict) -> dict | None:
    fm = normalize_frontmatter(rec)
    path = rec.get("path")
    if not _has_required_fields(fm):
        return {
            "path": path,
            "type": type_tag,
            "reason": "missing required field (id/created/status/title)",
        }
    severity = fm.get("severity")
    if type_tag == "bug" and severity is not None and severity not in _VALID_SEVERITY:
        return {
            "path": path,
            "type": type_tag,
            "reason": (
                f"invalid severity enum value (got {severity}; expected P0|P1|P2|P3)"
            ),
        }
    return None


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    records: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []

    for type_tag in _BACKLOG_TYPES:
        raw = _query_records(ctx, type_tag)
        for rec in raw:
            if not isinstance(rec, dict):
                continue
            fm = normalize_frontmatter(rec)
            if _has_required_fields(fm) and _severity_valid(type_tag, fm):
                records.append(_build_record(ctx, type_tag, rec))
            else:
                quarantine = _build_malformed(type_tag, rec)
                if quarantine is not None:
                    malformed.append(quarantine)

    return records, malformed
