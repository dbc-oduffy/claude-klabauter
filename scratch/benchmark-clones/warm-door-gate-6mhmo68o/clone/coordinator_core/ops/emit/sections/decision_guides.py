"""Section porter — DecisionGuideSummary (envelope key: ``decision_guides``).

Emits one DecisionGuideSummary per ``state/decisions/*.md`` frontmatter record (queried via
``query-records.js --type decision-guide``): identity + authored facets from frontmatter, with
``created`` truncated to date-only (schema ``IsoDate`` rejects a full datetime) and ``status``
validated against the 2-value DecisionGuideLifecycle enum (``active`` | ``archived``).
decision-guide is SINGLE-AXIS (status only — no health/posture axis). Rows missing a required
field (title / created / status) or carrying a status outside the enum quarantine to
``malformed_records.decision_guides``.

Composite natural key: (repo, coordinator_root_path, path).

Port of: emit-cockpit-snapshot.sh (DoE 07eedcfb, 2026-07-19) — § SECTION 8.11,
  DecisionGuideSummary. Byte/semantic parity port.
Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P15
  (upstream: docs/plans/2026-06-27-cockpit-emission-decision-guide-4th-type.md § Cn).

Node-subprocess retirement: this section originally shelled out to
``node COORDINATOR_ROOT/bin/query-records.js --type decision-guide``. It now calls
``coordinator_core.ops.ceremony.records_query.query_records`` in-process directly — no
``node`` binary, no subprocess spawn. ``decision-guide`` is one of the 12 types that
module's ``_TYPE_TO_GLOB`` covers (ported from query-records.js's ``_buildTypeToGlob``,
bin/query-records.js:211-272); its record shape (``{path, frontmatter}``) and the
fail-open-to-``[]`` posture on any collection failure are unchanged from the retired
subprocess call. See ``_query_decision_guide_records`` below.
"""

from __future__ import annotations

from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.ops.emit.context import EmitContext

from ._shared import normalize_frontmatter

# The 2-value DecisionGuideLifecycle enum (bash:2043). Order-insensitive membership set.
_DECISION_GUIDE_STATUS_ENUM = frozenset({"active", "archived"})

_MALFORMED_REASON = (
    "missing required field (title/created/status) or status outside enum"
)


def _query_decision_guide_records(ctx: EmitContext) -> list[dict]:
    """Return the ``decision-guide`` record list via the in-process records seam.

    Parity (retired form): bash:2031 ``node "$COORDINATOR_ROOT/bin/query-records.js"
    --type decision-guide --limit 0 --format json 2>/dev/null || echo "[]"``. Each record
    is ``{path, frontmatter}``; the fail-open contract is unchanged — any collection
    failure (bad worktree root, unreadable directory) yields ``[]`` here exactly as a
    non-zero exit / spawn error did for the retired subprocess.

    Root resolution mirrors every other in-process section's ``subprocess_root``
    convention (frozen-fixture test isolation takes precedence over ``ctx.repo_root``) —
    same root the retired spawn's ``cwd``/``--root`` resolved to. Unlike the JSON-RPC
    ``records.query`` op (which wants the git common dir), ``ceremony.query_records``
    takes the worktree root directly — no ``.git`` suffix.
    """
    root = ctx.subprocess_root if ctx.subprocess_root is not None else ctx.repo_root
    try:
        return query_records("decision-guide", root, limit=0)
    except (OSError, ValueError, SystemExit):
        return []


def _or_null(value):
    """Mirror jq // operator — return value unless null/false."""
    if value is None or value is False:
        return None
    return value


def _valid(fm: dict) -> bool:
    """Record passes when title/created/status are strings AND status ∈ enum (bash:2039-2044)."""
    return (
        isinstance(fm.get("title"), str)
        and isinstance(fm.get("created"), str)
        and isinstance(fm.get("status"), str)
        and fm.get("status") in _DECISION_GUIDE_STATUS_ENUM
    )


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    """Build (records, malformed) for the ``decision_guides`` envelope key.

    records — valid DecisionGuideSummary dicts; malformed — quarantine dicts. decision-guide
    carries no emit-DERIVED fields (single-axis; no LMA / deliverable_status).
    """
    raw = _query_decision_guide_records(ctx)

    records: list[dict] = []
    malformed: list[dict] = []

    for rec in raw:
        if not isinstance(rec, dict):
            continue
        fm = normalize_frontmatter(rec)
        path = rec.get("path")

        if _valid(fm):
            records.append(
                {
                    "repo": ctx.repo_name,
                    "coordinator_root_path": ".",
                    "path": path,
                    "title": fm["title"],
                    # IsoDate (YYYY-MM-DD): truncate any IsoDateTime to date-only (bash:2050).
                    "created": fm["created"][0:10],
                    "status": fm["status"],
                    "owner": _or_null(fm.get("owner")),
                    "summary": _or_null(fm.get("summary")),
                    "id_range": _or_null(fm.get("id_range")),
                    "decision_count": _or_null(fm.get("decision_count")),
                    "provenance": ctx.provenance("local_fs", path=path, derivation="parsed"),
                }
            )
        else:
            malformed.append(
                {
                    "path": path,
                    "reason": _MALFORMED_REASON,
                    # jq ``$fm | keys`` returns sorted keys (bash:2082).
                    "frontmatter_keys": sorted(fm.keys()),
                }
            )

    return records, malformed
