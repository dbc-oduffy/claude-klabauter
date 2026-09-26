"""Section porter — RoadmapSummary (envelope key: ``roadmaps``).

Emits one RoadmapSummary per ``state/roadmap/*/OVERVIEW.md`` frontmatter record returned
by ``bin/query-records.js --type roadmap``. Composite natural key: (repo,
coordinator_root_path, path). ``created`` is truncated to date-only (schema IsoDate rejects
a full datetime); ``status`` is validated against the 5-value RoadmapStatus enum. Rows
missing a required field (title/created/status) or carrying an off-enum status quarantine
to ``malformed_records.roadmaps``.

Deliverable-spine facets (purpose / deliverable_id / initiative / caption / status_reason)
are read present-as-null from frontmatter (D9); the emit-DERIVED fields
(last_meaningful_activity, workstream_type, shipped_sha, deliverable_status) are LEFT null
here and stamped by a later enrich/cross-join step — NOT computed in collect().

Assembler scalars (C2 — v2.6.0 emit switch):
  roll_up and critical_path are populated via ctx.assembler_dag(roadmap_id) for records
  where roadmap_id is non-null. Records with a null/absent roadmap_id get roll_up=null and
  critical_path=null — do NOT call the assembler with a null id (it requires a string).
  The assembler call is memoized at the EmitContext instance level (D2) — no recompute
  across sections within the same emission run.

Null roadmap_id guard (F4): if roadmap_id is None, both scalars are set to null without
  touching ctx.assembler_dag.

Assembler degraded-state signals (scan_incomplete / scan_errors — RoadmapSummary schema
  3.12.0, both `required`, non-nullable): read off the same ctx.assembler_dag(roadmap_id)
  payload as roll_up/critical_path, mirroring roadmap_serve.py's
  ``dag.get("scan_incomplete", False)`` / ``dag.get("scan_errors", [])`` shape. For the
  null-roadmap_id branch (no assembler call), both fields take their schema-typed empty
  values — scan_incomplete=False (schema type: boolean, no assembler ran so nothing is
  known to be incomplete) and scan_errors=[] (schema type: array of string) — matching
  roadmap_serve.py's own no-dag defaults, not `None` (the schema forbids null for either
  field).

Port of: emit-cockpit-snapshot.sh (DoE 07eedcfb, 2026-07-19) § SECTION 8.8 —
  RoadmapSummary. Byte/semantic parity port.
Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P12
Spec backlink: pln-emit-first-class-roadmap-dag-i-137a28 § C2

Node-subprocess retirement (2026-07-22): this section originally shelled out to
``node COORDINATOR_ROOT/bin/query-records.js --type roadmap --limit 0 --format json``
(cwd=``ctx.central_state_root.parent``, 30s timeout, fail-open to ``[]``). It now calls
``coordinator_core.ops.ceremony.records_query.query_records`` in-process — no ``node``
binary, no subprocess spawn. ``normalizeRoadmapStatus`` (query-records.js:1052-1080) is
applied by that seam's own ``_load_record`` path (``coordinator_core.ops.records_query``'s
``_normalize_roadmap_status``, invoked immediately before liveness injection, matching
oracle ordering) — this module does not re-normalize; ``_is_valid`` below still gates on
the SAME 5-value RoadmapStatus enum the normalizer targets, so a legacy off-enum status
(e.g. ``approved``) that the seam maps to ``active`` is accepted here exactly as it was
under the node oracle, not quarantined. See ``_query_roadmap_records`` for the calling
convention and the preserved fail-open/cwd-parity contract this repoint introduces.
"""

from __future__ import annotations

from coordinator_core.ops.ceremony.records_query import query_records as _ceremony_query_records
from coordinator_core.ops.emit.context import EmitContext

from ._shared import normalize_frontmatter

_ROADMAP_STATUS_ENUM = frozenset({"planning", "active", "blocked", "shipped", "archived"})


def _query_roadmap_records(ctx: EmitContext) -> list[dict]:
    """Enumerate ``roadmap``-type records via the native ceremony records-query seam.

    Native-seam successor to the node ``query-records.js --type roadmap --limit 0
    --format json`` spawn this porter used to shell out to (bash:1744 parity). Root
    parity is preserved exactly: ``worktree_root=ctx.central_state_root.parent`` is the
    SAME root the retired spawn's ``cwd=`` resolved to (the ``state/roadmap/*/OVERVIEW.md``
    source lives under ``coordinator_state_root --central``'s parent post-relocation), so
    the record ``path`` values returned are still repo-relative to the claude-klabauter working
    tree. Any failure degrades to an empty list — same fail-open contract as the retired
    bash ``|| echo "[]"`` — never aborts emit.
    """
    try:
        return _ceremony_query_records(
            "roadmap", ctx.central_state_root.parent, limit=0
        )
    except (ValueError, OSError):
        return []


def _no_overview_malformed(ctx: EmitContext, seen_paths: set[str]) -> list[dict]:
    """No-OVERVIEW bucket (Item 58): a roadmap directory missing ``OVERVIEW.md`` is
    invisible to ``_query_roadmap_records`` — its ``state/roadmap/**/OVERVIEW.md`` glob
    only matches directories that HAVE the file, so such a directory previously vanished
    silently instead of surfacing anywhere. Enumerate ``state/roadmap/*`` directly and
    quarantine each directory lacking ``OVERVIEW.md`` to ``malformed_records.roadmaps``,
    matching the shape ``collect`` already uses for an invalid record.

    ``seen_paths`` guards against double-counting a directory whose OVERVIEW.md the
    query seam already returned (valid or malformed by content) — this bucket is only
    for the directories the query seam never saw at all.
    """
    worktree_root = ctx.central_state_root.parent
    roadmap_root = worktree_root / "state" / "roadmap"
    if not roadmap_root.is_dir():
        return []

    malformed: list[dict] = []
    for entry in sorted(roadmap_root.iterdir()):
        if not entry.is_dir():
            continue
        overview = entry / "OVERVIEW.md"
        if overview.exists():
            continue
        path = f"state/roadmap/{entry.name}/OVERVIEW.md"
        if path in seen_paths:
            continue
        malformed.append({
            "path": path,
            "reason": "no OVERVIEW.md in roadmap directory",
            "frontmatter_keys": [],
        })
    return malformed


def _is_valid(fm: dict) -> bool:
    return (
        isinstance(fm.get("title"), str)
        and isinstance(fm.get("created"), str)
        and isinstance(fm.get("status"), str)
        and fm.get("status") in _ROADMAP_STATUS_ENUM
    )


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    raw = _query_roadmap_records(ctx)

    records: list[dict] = []
    malformed: list[dict] = []
    seen_paths: set[str] = set()

    for rec in raw:
        if not isinstance(rec, dict):
            continue
        fm = normalize_frontmatter(rec)
        path = rec.get("path")
        if isinstance(path, str):
            seen_paths.add(path)

        if not _is_valid(fm):
            malformed.append({
                "path": path,
                "reason": (
                    "missing required field (title/created/status) or status "
                    "outside RoadmapStatus enum"
                ),
                "frontmatter_keys": sorted(fm.keys()),
            })
            continue

        roadmap_id = fm.get("roadmap_id")

        if roadmap_id is not None:
            roadmap_id = str(roadmap_id)
            dag = ctx.assembler_dag(roadmap_id)
            roll_up = dag.get("roll_up")
            critical_path = dag.get("critical_path")
            scan_incomplete = dag.get("scan_incomplete", False)
            scan_errors = dag.get("scan_errors", [])
        else:
            roll_up = None
            critical_path = None
            scan_incomplete = False
            scan_errors = []

        records.append({
            "repo": ctx.repo_name,
            "coordinator_root_path": ".",
            "path": path,
            "title": fm["title"],
            "created": fm["created"][0:10],
            "status": fm["status"],
            "owner": fm.get("owner"),
            "horizon": fm.get("horizon"),
            "roadmap_id": roadmap_id,
            "stub_id": fm.get("stub_id"),
            "items": fm.get("items"),
            "blocks": fm.get("blocks"),
            "blocked_by": fm.get("blocked_by"),
            "purpose": fm.get("purpose"),
            "deliverable_id": fm.get("deliverable_id"),
            "initiative": fm.get("initiative"),
            "caption": fm.get("caption"),
            "status_reason": fm.get("status_reason"),
            "last_meaningful_activity": None,
            "workstream_type": None,
            "shipped_sha": None,
            "deliverable_status": None,
            "roll_up": roll_up,
            "critical_path": critical_path,
            "scan_incomplete": scan_incomplete,
            "scan_errors": scan_errors,
            "provenance": ctx.provenance("local_fs", path=path, derivation="parsed"),
        })

    malformed.extend(_no_overview_malformed(ctx, seen_paths))

    return records, malformed
