"""Section porter — TrackerSummary (envelope key: ``trackers``).

Emits one TrackerSummary per ``docs/project-tracker.md``-shaped record surfaced by
``query-records.js --type tracker``. Composite natural key: (repo, coordinator_root_path,
path). ``created`` is truncated to date-only (schema IsoDate rejects a full datetime);
``status`` is validated against the 2-value TrackerStatus enum (active|archived). Rows
missing a required field or carrying an off-enum status quarantine to
``malformed_records.trackers``.

Port of: emit-cockpit-snapshot.sh (coordinator-claude 07eedcfb, 2026-07-19) § SECTION 8.9 —
  TrackerSummary. Byte/semantic parity port.
Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P13

Node-subprocess retirement: this section originally shelled out to ``node
"$COORDINATOR_ROOT/bin/query-records.js" --type tracker --limit 0 --format json``
(oracle bin/query-records.js — ``tracker`` resolved via ``_buildTypeToGlob``,
query-records.js:211-272). It now calls
``coordinator_core.ops.ceremony.records_query.query_records`` in-process directly —
no ``node`` binary, no subprocess spawn, no ``timeout=30``. Root/cwd resolution is
preserved exactly: the seam's ``worktree_root`` is ``ctx.subprocess_root`` when set
(frozen-fixture test isolation), else ``ctx.repo_root`` — the same directory the
retired spawn's ``cwd`` and ``--root`` override resolved to. Fail-open posture is
unchanged: any seam failure (unknown type, unsupported ``where``/``since`` grammar,
or any other exception) yields ``[]``, exactly as the spawn's non-zero-exit /
timeout / unparseable-JSON paths did. See ``_query_tracker_records`` below.
"""

from __future__ import annotations

from coordinator_core.ops.ceremony.records_query import query_records as _ceremony_query_records
from coordinator_core.ops.emit.context import EmitContext

from ._shared import normalize_frontmatter

_TRACKER_STATUS_ENUM = frozenset({"active", "archived"})


def _query_tracker_records(ctx: EmitContext) -> list[dict]:
    """Call the ceremony records-query seam for ``type=tracker`` and return the record list.

    Root resolution mirrors the retired subprocess's ``cwd``/``--root`` pair: ``ctx.subprocess_root``
    (frozen-fixture test isolation) takes precedence over ``ctx.repo_root`` — the same
    directory the spawn's cwd (and, when set, its ``--root`` override) resolved to.

    Fail-open: this call raises only on programmer error (an unknown ``record_type``,
    which never applies — ``"tracker"`` is a fixed literal) or a ``SystemExit`` from an
    unsupported ``where``/``since`` grammar, neither of which this call site ever passes.
    The broad except below is defensive parity with the retired spawn's ``[]``-on-any-
    failure posture (bash-oracle-derived: ``… 2>/dev/null || echo "[]"``) rather than an
    expectation that these paths are reachable today.
    """
    root = ctx.subprocess_root if ctx.subprocess_root is not None else ctx.repo_root
    try:
        parsed = _ceremony_query_records("tracker", root, limit=0)
    except (ValueError, SystemExit, OSError):
        return []
    return parsed if isinstance(parsed, list) else []


def _jq_alternative(value):
    """Mirror jq // operator — return value unless null/false."""
    return value if value not in (None, False) else None


def _is_valid(fm: dict) -> bool:
    """Required fields present + status within the 2-value TrackerStatus enum (bash:1887-1892)."""
    return (
        isinstance(fm.get("title"), str)
        and isinstance(fm.get("created"), str)
        and isinstance(fm.get("status"), str)
        and fm.get("status") in _TRACKER_STATUS_ENUM
    )


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    """Build (records, malformed) for TrackerSummary rows."""
    raw = _query_tracker_records(ctx)

    records: list[dict] = []
    malformed: list[dict] = []

    for rec in raw:
        if not isinstance(rec, dict):
            continue
        fm = normalize_frontmatter(rec)
        path = rec.get("path")

        if _is_valid(fm):
            records.append(
                {
                    "repo": ctx.repo_name,
                    "coordinator_root_path": ".",
                    "path": path,
                    "title": fm["title"],
                    "created": fm["created"][0:10],
                    "status": fm["status"],
                    "owner": _jq_alternative(fm.get("owner")),
                    "items": _jq_alternative(fm.get("items")),
                    "provenance": ctx.provenance("local_fs", path=path, derivation="parsed"),
                }
            )
        else:
            malformed.append(
                {
                    "path": path,
                    "reason": (
                        "missing required field (title/created/status) or status "
                        "outside TrackerStatus enum"
                    ),
                    "frontmatter_keys": sorted(fm.keys()),
                }
            )

    return records, malformed
