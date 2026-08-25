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
(``docs/plans/<slug>.review.md``, ``.patrik-review.md``, ``.sonnet-review.md``,
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
Review: code-reviewer Finding 1): authors write the FORWARD edge ``supersedes:`` (scalar path
or YAML list of paths) on the superseding plan; nothing authors the backward edge directly,
and asking authors to double-write both directions is redundant and drift-prone. The backward
edge is derived cross-record as a SECOND PASS at the end of this module's own ``collect()`` —
NOT a post-collect ``envelope.py`` enricher. Unlike ``_stamp_initiative_goals`` (a genuine
CROSS-SECTION join — initiatives and goals are different section modules, each with only its
own ``collect(ctx)`` call and no visibility into the other's output), ``supersedes``/
``superseded_by`` is an INTRA-section self-join (plans × plans): ``collect()`` already has the
entire plan record set in scope via one ``_query_plan_records(ctx)`` call, so a second in-
function pass over the already-built ``records`` list reproduces the identical behavior with
zero coupling to ``envelope.py``. ``_apply_superseded_by`` stages the raw ``supersedes`` value
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

# Precedence tiering for resolving `reviewer` when a plan has MORE THAN ONE reviewed sidecar
# (measured: 1 of 27 reviewed plans today — 2026-07-08-backlog-opened-closed-emission.md has
# both a patrik-review and a sonnet-review).
#
# PRIMARY signal (Review: code-reviewer Finding 3, 2026-07-21): each sidecar's own `kind:`
# frontmatter field. `kind: sonnet-review` marks an LLM co-reviewer's self/model pass; any
# OTHER kind (`staff-eng-review`, `eng-director-review`, or an as-yet-unseen kind) marks a
# named human/staff review, which is the more authoritative verdict — robust to any future
# named staff reviewer (camelia, fru, sid, pali, ...) with zero roster maintenance, unlike a
# hardcoded persona-name list. An UNRECOGNIZED non-`sonnet-review` kind still ranks at the
# staff tier (never silently falls to "plain" or below `sonnet-review` — Finding 3's explicit
# robustness ask) because the absence-of-evidence ("this isn't a known staff kind") is weaker
# than the presence-of-evidence ("this isn't the model kind").
_REVIEWER_SIDECAR_MODEL_KIND = "sonnet-review"
_REVIEWER_SIDECAR_KIND_STAFF_TIER = 0

# FALLBACK signal — filename-substring matching, used ONLY when a sidecar carries no `kind:`
# frontmatter field at all (legacy/freeform review markdown with no frontmatter block —
# measured on disk 2026-07-21: e.g. `*.sonnet-review.md`/`*.review.md` files that are bare
# prose with no `---` fence). Named-marker ORDER (patrik, eng-director, zoli) is undocumented
# precedent carried over unchanged from the pre-`kind` implementation (Review: code-reviewer
# Finding 4 residual — no real-world named-vs-named collision has been measured to justify
# reordering; a plain ".review.md" sidecar with no named-reviewer marker sits between the
# named tier and the model-marker tier). Matched by substring against the sidecar's suffix
# segment (case-insensitive); an unrecognized future marker falls back to the "plain" tier.
_REVIEWER_SIDECAR_PRIORITY: tuple[str, ...] = ("patrik", "eng-director", "zoli")
_REVIEWER_SIDECAR_PLAIN_TIER = 100
_REVIEWER_SIDECAR_MODEL_MARKERS: tuple[str, ...] = ("sonnet",)
_REVIEWER_SIDECAR_MODEL_TIER = 200

# The frozen 9-value PlanStatus enum (bash:1616 / 1672). Order-insensitive membership set.
_PLAN_STATUS_ENUM = frozenset({
    "draft",
    "reviewed",
    "approved",
    "executing",
    "landed",
    "implemented",
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
    """Record passes when title/created/author/status are strings AND status ∈ enum (bash:1611)."""
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
    """Resolve ``PlanSummary.reviewer`` by joining same-stem review sidecars (dead-join fix).

    Globs ``<plan-dir>/<stem>.*.md`` next to the plan file, keeps only candidates whose
    suffix segment contains "review" (case-insensitive — excludes sibling sidecar families
    like ``.plan-coverage-check.md`` / ``.prior-art-check.md``), reads each candidate's own
    ``reviewer:`` (and ``kind:``) frontmatter fields via ``frontmatter.primitives``, and
    returns the highest-priority non-empty value per ``_sidecar_priority`` (ties broken by
    filename ascending, for full determinism). Returns ``None`` (D9 present-as-null) when the
    plan has no reviewed sidecar — the common case — never a malformed/quarantine condition.
    """
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
            # Unreadable review-file candidate -- skip it, try the next candidate.
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
    """Build (records, malformed) for the ``plans`` envelope key.

    records — valid PlanSummary dicts; malformed — quarantine dicts. Emit-derived fields
    (last_meaningful_activity / workstream_type / shipped_sha / deliverable_status) are null.
    """
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
                    # Direct authorship (0 today) always wins; else join the review sidecar.
                    "reviewer": fm.get("reviewer") or _resolve_reviewer(ctx, path),
                    "branch": fm.get("branch"),
                    # Direct authorship (0 today) always wins; else `_apply_superseded_by`
                    # (second pass below, before this function returns) fills the reverse
                    # `supersedes` edge.
                    "superseded_by": fm.get("superseded_by"),
                    # Staging-only: raw forward `supersedes` edge (scalar path or list of
                    # paths), popped unconditionally by `_apply_superseded_by` — this schema
                    # is strict/`additionalProperties: false` (see module docstring).
                    "_supersedes_raw": fm.get("supersedes"),
                    # source maps roadmap_id for roadmap-sourced plans (bash:1632).
                    "source": fm.get("source") if fm.get("source") is not None else fm.get("roadmap_id"),
                    # Deliverable spine identity + authored facets (C4a/C4b).
                    "deliverable_id": fm.get("deliverable_id"),
                    "plan_id": fm.get("plan_id"),
                    "initiative": fm.get("initiative"),
                    "caption": fm.get("caption"),
                    "status_reason": fm.get("status_reason"),
                    "owner": fm.get("owner"),
                    # Emit-DERIVED — stamped later (C3/enrich), null in collect().
                    "last_meaningful_activity": None,
                    "workstream_type": None,
                    "shipped_sha": None,
                    "deliverable_status": None,
                    # Derives from frontmatter alone, so it resolves here rather than in the
                    # enrich pass the two fields above wait for.
                    "review_verified": plan_review_verified(fm),
                    "provenance": ctx.provenance("local_fs", path=path, derivation="parsed"),
                }
            )
        else:
            malformed.append(
                {
                    "path": path,
                    "reason": _MALFORMED_REASON,
                    # jq ``$fm | keys`` returns sorted keys (bash:1677).
                    "frontmatter_keys": sorted(fm.keys()),
                }
            )

    _apply_superseded_by(records)

    return records, malformed


def _apply_superseded_by(records: list[dict]) -> None:
    """Second pass: derive ``superseded_by`` as the reverse edge of authored ``supersedes``
    (dead-join fix, 2026-07-21; relocated from ``envelope.py`` same day — Review: code-reviewer
    Finding 1 — intra-section self-join, not a cross-section enrichment).

    Purpose: nothing authors the backward edge directly (asking authors to double-write both
    directions is redundant and drift-prone), so it is derived here: if plan A declares
    ``supersedes: B`` (or ``supersedes: [B, C, ...]``), then B's (and C's, ...)
    ``superseded_by`` becomes A's ``path``. Mutates ``records`` in place; must run AFTER the
    caller's first pass has built every record (this function needs the full plan set in
    scope, which ``collect()`` already has via one ``_query_plan_records(ctx)`` call).

    ``superseded_by`` holds a plan ``path`` (not a ``plan_id``) — confirmed against
    ``PlanSummary.superseded_by``'s docstring ("Path to the superseding plan") and the
    existing (0-occurrence) authored values, which are already ``docs/plans/...md`` paths.

    Authored-wins precedence: a plan that directly authors ``superseded_by`` in its own
    frontmatter (future-proofing; 0 today) is NEVER overwritten by the derived edge.

    Self-supersession guard (Review: code-reviewer Finding 5): a plan whose ``supersedes``
    names its OWN path is not treated as superseding itself — such a self-target is dropped
    while building the forward index, so it can never stamp a record's ``superseded_by`` with
    its own ``path``.

    Multiple-supersession precedence (unexercised in current data — 0 collisions measured
    2026-07-21, but the rule is written so a future collision resolves deterministically):
    when more than one plan declares ``supersedes`` against the SAME target path, the
    most-recently-``created`` superseding plan wins (last-supersession-wins — the newest
    plan is presumed the most current supersession); ties are broken by ``path`` ascending.

    The staging ``_supersedes_raw`` key is POPPED from every record unconditionally (whether
    or not it produced a reverse edge) — the PlanSummary schema is ``.strict()``
    (``additionalProperties: false``); a leaked staging key would reject the whole envelope
    at the consumer's Zod parse. No staging key is ever visible in ``collect()``'s return value.

    Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P10
    """
    # Pop the staging key from every record first, building the forward index as we go.
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
            # Authored-wins — never overwrite a directly-authored value.
            continue
        superseding = forward.get(record["path"])
        if not superseding:
            continue
        # last-supersession-wins: newest `created` wins; ties broken by path ascending.
        superseding_sorted = sorted(
            superseding,
            key=lambda p: (created_by_path.get(p) or "", p),
        )
        record["superseded_by"] = superseding_sorted[-1]
