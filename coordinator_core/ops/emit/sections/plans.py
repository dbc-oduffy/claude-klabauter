"""Section porter — PlanSummary (envelope key: ``plans``).

Emits one PlanSummary per ``docs/plans/*.md`` frontmatter record: identity + authored
facets from frontmatter, verbatim author (no git-blame inference — C-F6), status validated
against the frozen 8-value PlanStatus enum. Rows missing a required field (title / created /
author / status) or carrying a status outside the enum quarantine to ``malformed_records.plans``
(handoffs section is the template — quarantine, do not silent-drop).

Emit-DERIVED fields are LEFT null in collect() and stamped later (C3/enrich):
last_meaningful_activity (git-log LMA), workstream_type, shipped_sha, deliverable_status.

``reviewer`` join (dead-join fix, 2026-07-21): plan frontmatter never authors ``reviewer:``
directly (0 occurrences measured) — reviewer attribution lives in sibling review-sidecar files
(``docs/plans/<slug>.review.md``, ``.the Staff Engineer-review.md``, ``.sonnet-review.md``,
``.eng-director-review.md``, ``.review-<name>.md``, and similar variants; NOT
``.plan-coverage-check.md`` / ``.prior-art-check.md``, which are a different sidecar family with
no "review" marker and no reviewer attribution). ``_resolve_reviewer`` globs same-stem sidecars
whose suffix segment contains "review" (case-insensitive) and reads their own ``reviewer:``
frontmatter field via the shared ``frontmatter.primitives`` text primitives (not query-records.js
— sidecars are not ``kind: plan`` records so query-records never surfaces them for this
section). See ``_sidecar_priority`` for the precedence rule when a plan has more than
one reviewed sidecar — tiered primarily on each sidecar's own ``kind:`` frontmatter field
(Review: code-reviewer Finding 3, 2026-07-21: a structural staff-vs-model signal already
present on disk, e.g. ``kind: staff-eng-review`` vs ``kind: sonnet-review``, robust to any
future named staff reviewer without a hardcoded persona-name roster), falling back to
filename-substring matching only for sidecars carrying no ``kind:`` field at all (legacy/
freeform review markdown with no frontmatter block). A directly-authored ``fm.get("reviewer")``
on the plan's own frontmatter (future-proofing; 0 today) always wins over any sidecar join.

``superseded_by`` derivation (dead-join fix, 2026-07-21; relocated into ``collect()`` same day —
code-reviewer Finding 1): authors write the FORWARD edge ``supersedes:`` (scalar path
or YAML list of paths) on the superseding plan; nothing authors the backward edge directly,
and asking authors to double-write both directions is redundant and drift-prone. The backward
edge is derived cross-record as a SECOND PASS at the end of this module's own ``collect()`` —
NOT a post-collect ``resolvers.py`` enricher. Unlike ``_stamp_initiative_goals`` (a genuine
CROSS-SECTION join — initiatives and goals are different section modules, each with only its
own ``collect(ctx)`` call and no visibility into the other's output), ``supersedes``/
``superseded_by`` is an INTRA-section self-join (plans × plans): ``collect()`` already has the
entire plan record set in scope via one ``_query_plan_records(ctx)`` call, so a second in-
function pass over the already-built ``records`` list reproduces the identical behavior with
zero coupling to ``resolvers.py``. ``_apply_superseded_by`` stages the raw ``supersedes`` value
under a private ``_supersedes_raw`` key during the first pass and pops it from every record
unconditionally before ``collect()`` returns — the staging key never leaks into the returned
records (strict/``additionalProperties: false`` schema).

Node-subprocess retirement (2026-07-22): ``_query_plan_records`` originally shelled out to
``node COORDINATOR_ROOT/bin/query-records.js --type plan --limit 0 --format json``.
It now calls ``coordinator_core.ops.ceremony.records_query.query_records`` in-
process — no ``node`` binary, no subprocess spawn. The canonical-name/sidecar filtering
query-records.js applies for ``--type plan`` (bin/query-records.js:1360-1389) is unchanged —
the seam reuses ``coordinator_core.ops.records_query._apply_plan_filename_filter``, the same
port that JSON-RPC ``records.query`` uses, so this section never re-derives that filter
locally. See ``_query_plan_records`` for the root/cwd and fail-open parity notes.

Port of: emit-cockpit-snapshot.sh (DoE 07eedcfb, 2026-07-19) — § SECTION 8.6,
  PlanSummary. Byte/semantic parity port.
Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P10
  (upstream: docs/plans/2026-06-23-cockpit-contract-ext-wave2-emit-and-queue-migration.md § C4b).
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.ops.ceremony.records_query import query_records as _ceremony_query_records
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.deliverable_status import plan_review_verified

from ._shared import normalize_frontmatter

# hardcoded persona-name list. An UNRECOGNIZED non-`sonnet-review` kind still ranks at the
_REVIEWER_SIDECAR_MODEL_KIND = "sonnet-review"
_REVIEWER_SIDECAR_KIND_STAFF_TIER = 0

# FALLBACK signal — filename-substring matching, used ONLY when a sidecar carries no `kind:`
_REVIEWER_SIDECAR_PRIORITY: tuple[str, ...] = ("patrik", "eng-director", "zoli")
_REVIEWER_SIDECAR_PLAIN_TIER = 100
_REVIEWER_SIDECAR_MODEL_MARKERS: tuple[str, ...] = ("sonnet",)
_REVIEWER_SIDECAR_MODEL_TIER = 200

# terminal/archivable per lifecycle_constants.PLAN_ARCHIVABLE_STATUS). Order-insensitive
_PLAN_STATUS_ENUM = frozenset({
    "draft",
    "reviewed",
    "approved",
    "blocked",
    "executing",
    "landed",
    "implemented",
    "closed_partial",
    "deferred",
    "abandoned",
    "superseded",
})

_MALFORMED_REASON = (
    "missing required field (title/created/author/status) or status outside PlanStatus enum"
)


def _query_plan_records(ctx: EmitContext) -> list[dict]:
    """Return the ``--type plan`` record list (``[]`` on failure) via the native records seam.

    Node-subprocess retirement: this originally shelled out to ``node
    "$COORDINATOR_ROOT/bin/query-records.js" --type plan --limit 0 --format json
    2>/dev/null || echo "[]"`` (bash:1603). It now calls
    ``coordinator_core.ops.ceremony.records_query.query_records`` in-process — no ``node``
    binary, no subprocess spawn. That seam reuses ``coordinator_core.ops.records_query``'s
    ``_apply_plan_filename_filter`` (port of query-records.js's ``--type plan`` sidecar-
    exclusion block, bin/query-records.js:1360-1389) to keep only canonical
    ``YYYY-MM-DD-<slug>.md`` plan filenames and drop sidecars, so the canonical-name/sidecar
    filtering the JS applies stays byte-for-byte in the seam, not reimplemented here. Each
    record is ``{path, frontmatter}`` — the same shape the retired subprocess path produced.

    Root/cwd parity: the retired spawn resolved its records-root from ``query-records.js``'s
    own process cwd via ``git rev-parse --show-toplevel`` (query-records.js:533) unless
    ``--root`` was passed, and was invoked with ``cwd=str(ctx.repo_root)``. The native seam
    takes an explicit ``worktree_root`` instead of resolving cwd itself, so this call passes
    ``ctx.subprocess_root`` when set (mirrors the retired ``--root`` override) else
    ``ctx.repo_root`` (mirrors the retired ``cwd``/no-``--root`` default) — the same root
    the spawn would have resolved to either way.

    Fail-open is preserved exactly: any exception from the native call degrades to ``[]``,
    matching the retired spawn's ``OSError``/``ValueError``/JSON-parse-failure/non-zero-exit
    catch-all.
    """
    worktree_root = ctx.subprocess_root if ctx.subprocess_root is not None else ctx.repo_root
    try:
        parsed = _ceremony_query_records("plan", Path(worktree_root), limit=0)
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _is_str(value) -> bool:
    return isinstance(value, str)


def _valid(fm: dict) -> bool:
    return (
        _is_str(fm.get("title"))
        and _is_str(fm.get("created"))
        and _is_str(fm.get("author"))
        and _is_str(fm.get("status"))
        and fm.get("status") in _PLAN_STATUS_ENUM
    )


def _sidecar_priority(suffix: str, kind: "str | None") -> int:
    """Return the precedence tier for a review-sidecar (lower wins).

    PRIMARY: tier on the sidecar's own ``kind:`` frontmatter field when present — any kind
    other than ``sonnet-review`` (including an unrecognized future kind) ranks at the staff
    tier, ``sonnet-review`` ranks at the model tier. FALLBACK: filename-substring matching
    against ``suffix``, used only when ``kind`` is absent (``None``/empty).

    See the module-level ``_REVIEWER_SIDECAR_*`` constants for the full rationale.
    """
    if kind:
        if kind == _REVIEWER_SIDECAR_MODEL_KIND:
            return _REVIEWER_SIDECAR_MODEL_TIER
        return _REVIEWER_SIDECAR_KIND_STAFF_TIER

    lowered = suffix.lower()
    for rank, marker in enumerate(_REVIEWER_SIDECAR_PRIORITY):
        if marker in lowered:
            return rank
    if any(marker in lowered for marker in _REVIEWER_SIDECAR_MODEL_MARKERS):
        return _REVIEWER_SIDECAR_MODEL_TIER
    return _REVIEWER_SIDECAR_PLAIN_TIER


def _resolve_reviewer(ctx: EmitContext, path: "str | None") -> "str | None":
    if not path:
        return None
    plan_path = Path(path)
    plan_dir = Path(ctx.repo_root) / plan_path.parent
    base = plan_path.stem
    try:
        candidates = sorted(plan_dir.glob(f"{base}.*.md"))
    except OSError:
        return None

    resolved: list[tuple[int, str, str]] = []
    for candidate in candidates:
        suffix = candidate.name[len(base) + 1 : -len(".md")]
        if "review" not in suffix.lower():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        split = split_frontmatter(text)
        if split is None:
            continue
        value = read_fm_field(split.fm_text, "reviewer")
        if not value:
            continue
        kind = read_fm_field(split.fm_text, "kind")
        resolved.append((_sidecar_priority(suffix, kind), candidate.name, value))

    if not resolved:
        return None
    resolved.sort(key=lambda item: (item[0], item[1]))
    return resolved[0][2]


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    raw = _query_plan_records(ctx)

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
                    # IsoDate (YYYY-MM-DD): truncate any IsoDateTime to date-only (bash:1624).
                    "created": fm["created"][0:10],
                    "author": fm["author"],
                    "status": fm["status"],
                    "scope_mode": fm.get("scope_mode"),
                    "reviewer": fm.get("reviewer") or _resolve_reviewer(ctx, path),
                    "branch": fm.get("branch"),
                    "superseded_by": fm.get("superseded_by"),
                    "_supersedes_raw": fm.get("supersedes"),
                    "source": fm.get("source") if fm.get("source") is not None else fm.get("roadmap_id"),
                    "deliverable_id": fm.get("deliverable_id"),
                    "plan_id": fm.get("plan_id"),
                    "initiative": fm.get("initiative"),
                    "caption": fm.get("caption"),
                    "status_reason": fm.get("status_reason"),
                    "owner": fm.get("owner"),
                    "last_meaningful_activity": None,
                    "workstream_type": None,
                    "shipped_sha": None,
                    "deliverable_status": None,
                    "review_verified": plan_review_verified(fm),
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

    _apply_superseded_by(records)

    return records, malformed


def _apply_superseded_by(records: list[dict]) -> None:
    forward: dict[str, list[str]] = {}
    for record in records:
        raw = record.pop("_supersedes_raw", None)
        if not raw:
            continue
        source_path = record["path"]
        targets = raw if isinstance(raw, list) else [raw]
        for target in targets:
            if isinstance(target, str) and target and target != source_path:
                forward.setdefault(target, []).append(source_path)

    if not forward:
        return

    created_by_path = {record["path"]: record.get("created") for record in records}

    for record in records:
        if record.get("superseded_by") is not None:
            continue
        superseding = forward.get(record["path"])
        if not superseding:
            continue
        superseding_sorted = sorted(
            superseding,
            key=lambda p: (created_by_path.get(p) or "", p),
        )
        record["superseded_by"] = superseding_sorted[-1]
