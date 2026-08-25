"""Section porter — HealthStatusSummary (envelope key: ``health``).

Emits one HealthStatusSummary record per ``state/health/*.md`` frontmatter row that carries
all required fields AND whose two orthogonal enum axes are in range:
    status — HealthStatusLifecycle (active | archived)   [lifecycle axis]
    health — HealthPosture (HEALTHY | WATCH | ACTION | CRITICAL)  [posture axis]
``created`` is truncated to date-only (schema IsoDate rejects a full datetime). Rows missing a
required field, or with status/health outside their enum, quarantine to
``malformed_records.health``. Composite natural key: (repo, coordinator_root_path, path).

Source: reads the central-state ``state/health`` tree via
``coordinator_core.ops.ceremony.records_query.query_records(record_type="health-status", ...)``
— the query-records root is the central-state git root (``central_state_root.parent``) —
central state is makima-resident post stop-the-rot, so health rows live there, not under the
meta-repo. Emit-DERIVED fields do not apply to this entity; collect() returns final records.

Port of: emit-cockpit-snapshot.sh (DoE 07eedcfb, 2026-07-19) — § SECTION 8.10,
  HealthStatusSummary. Byte/semantic parity port.
Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P14

Node-subprocess retirement: this section originally shelled out to
``node "$COORDINATOR_ROOT/bin/query-records.js" --type health-status --root <scan_root>
--limit 0 --format json``, cwd=``scan_root``. It now calls the native
``coordinator_core.ops.ceremony.records_query.query_records`` seam in-process — no ``node``
binary, no subprocess spawn, no JSON round-trip. ``worktree_root=scan_root`` reproduces the
bash oracle's ``--root``/cwd pairing exactly (query-records.js's ``root`` param drives its own
``walkGlob`` resolution the same way for every type, including ``health-status`` — see
query-records.js:1241-1243, 1259-1261). ``query_records``'s own ``_collect_files`` already
folds directory-scan failures into a caught ``_RecordsCollectError`` (stderr note + ``[]``),
so the try/except below exists only to preserve this call site's original fail-open posture
(``|| echo "[]"``) against any exception ``query_records`` itself does not already
swallow — same degraded-to-``[]`` contract as the retired subprocess path, not a new
failure mode.
"""

from __future__ import annotations

from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.ops.emit.context import EmitContext

from ._shared import normalize_frontmatter

# HealthStatusLifecycle (status axis) and HealthPosture (health axis) enum members — the two
# orthogonal validation gates the bash applies.
_STATUS_ENUM = frozenset({"active", "archived"})
_HEALTH_ENUM = frozenset({"HEALTHY", "WATCH", "ACTION", "CRITICAL"})

_MALFORMED_REASON = (
    "missing required field (title/created/status/health) or status/health outside enum"
)


def _query_health_records(ctx: EmitContext) -> list[dict]:
    """Native records-query seam over ``health-status``, scoped to the central-state git root.

    Parity: retired subprocess spawned ``node "$COORDINATOR_ROOT/bin/query-records.js" --type
    health-status --root <scan_root> --limit 0 --format json 2>/dev/null || echo "[]"``,
    cwd=``scan_root``. ``scan_root`` (``central_state_root.parent``,
    the same root the retired spawn's ``--root``/cwd pair resolved to) is now passed straight
    through as ``worktree_root`` — the seam's own file-collection walks it exactly as
    ``query-records.js``'s ``root`` param did (query-records.js:1241-1243). Any failure → ``[]``
    so the section (and the whole emit) continues (bash ``|| echo "[]"`` posture, preserved here).
    """
    scan_root = ctx.central_state_root.parent
    try:
        return query_records("health-status", scan_root, limit=0)
    except (OSError, ValueError):
        return []


def _is_valid(fm: dict) -> bool:
    """Required-fields-present + dual-enum gate (bash select at lines 1961-1968)."""
    title = fm.get("title")
    created = fm.get("created")
    status = fm.get("status")
    health = fm.get("health")
    return (
        isinstance(title, str)
        and isinstance(created, str)
        and isinstance(status, str)
        and status in _STATUS_ENUM
        and isinstance(health, str)
        and health in _HEALTH_ENUM
    )


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    """Build (records, malformed) for HealthStatusSummary from query-records output."""
    raw = _query_health_records(ctx)

    records: list[dict] = []
    malformed: list[dict] = []

    for rec in raw:
        if not isinstance(rec, dict):
            continue
        path = rec.get("path")
        fm = normalize_frontmatter(rec)

        if _is_valid(fm):
            records.append(
                {
                    "repo": ctx.repo_name,
                    "coordinator_root_path": ".",
                    "path": path,
                    "title": fm["title"],
                    "created": fm["created"][0:10],
                    "status": fm["status"],
                    "health": fm["health"],
                    "owner": fm.get("owner"),
                    "summary": fm.get("summary"),
                    "provenance": ctx.provenance("local_fs", path=path, derivation="parsed"),
                }
            )
        else:
            malformed.append(
                {
                    "path": path,
                    "reason": _MALFORMED_REASON,
                    "frontmatter_keys": sorted(fm.keys()),
                }
            )

    return records, malformed
