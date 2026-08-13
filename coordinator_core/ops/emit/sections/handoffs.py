"""Section porter — Handoffs (envelope key: ``handoffs``).

Ports bash SECTION 1 + SECTION 1.5 — the HandoffSummary records. Merges live (``query-records.js --type handoff``) + archived (``--type
handoff-archived``), normalizes the frontmatter (kind/workstream/predecessor/scope injection,
superseded→claimed coercion, tolerant claimed_at datetime guard), quarantines records missing
any required non-nullable field (title/created/status/deployment_state), and enriches each
record's ``shipped_in`` ({sha, date}) via a single ``git log`` SHA→date resolution and its
``acceptance_criteria`` ({done, total}) via a body-checklist parse.

DR-084 P4 ingest tolerance RESTORED-AS-TRANSITIONAL 2026-07-23. The wire (contract, schema)
emits/validates NEW vocabulary only (``HandoffStatus = Literal["open", "claimed"]``;
``DeploymentState`` includes ``continued``/``closed``, no ``abandoned``) — that narrowing
(d652253c) is unchanged and this shim never widens it: coercion happens strictly
BEFORE the pydantic model, never after. What's restored is the P1/P3 old->new coerce shim at
ingest, because the "corpus migration already re-expressed every live/archived record" premise
that justified retiring it (commit 5372260e) held only for claude-klabauter's OWN corpus — this section
also runs against consumer repos' `state/handoffs/` + `archive/handoffs/` trees (example-retrieval-repo,
Example-cockpit-repo, etc.), which were verified un-migrated on 2026-07-23 (example-retrieval-repo: 100% old
vocabulary; example-cockpit-repo: majority old vocabulary, including ``deployment_state: abandoned``
records).

    MEASUREMENT SUPERSEDED IN PART, 2026-08-11 — do not cite the cockpit half above as current.
    example-cockpit-repo-em re-measured their own corpus through the ``handoff.columns`` CLI and
    compared every served value against the raw frontmatter it came from: 169 of 169 rows, all
    four columns (``status``/``deployment_state``/``predecessor``/``shipped_in``), **zero
    divergence and zero surviving old tokens**. Their corpus is uniformly NEW vocabulary today.
    The "majority old vocabulary" description of cockpit is ~3 weeks stale and was cited from
    this docstring, in good faith, to tell a sibling repo that this coercion was load-bearing
    for them specifically — a claim their measurement then falsified. That is the cost of a
    dated corpus census reading as a standing fact: annotate it here rather than let the next
    reader repeat it.

    **The shim STAYS, and the exit condition below is NOT met.** Two reasons, both live:
    example-retrieval-repo's leg has NOT been re-measured since 2026-07-23 and its last known state is
    100% old vocabulary — one repo migrating discharges one leg of an all-consumers condition,
    not the condition. And cockpit themselves argued for keeping it: coercion that is a no-op
    on a corpus today is exactly what stops being a no-op when an old archived record
    resurfaces or the vocabulary moves again, and they would rather that logic live in this
    producer than be forked into their ingest.

A record still authored in OLD vocabulary (``status: active``/``consumed``,
``deployment_state: abandoned``, ``consumed_at``/``consumed_by``) is coerced up to the new
tokens before it reaches the strict pydantic model; a record already authored in NEW vocabulary
passes through untouched. ``status: superseded`` remains recognized on its own, separate,
permanently-grandfathered axis (2026-06-26, the Staff Engineer-ratified DO-NOT-NARROW invariant on
``handoff-archived.schema.json``) — see ``_STATUS_RECOGNIZED`` below.

Exit condition (named, not open-ended): this tolerance retires again once every consumer repo's
on-disk handoff corpus is migrated to the new vocabulary (``active``→``open``,
``consumed``→``claimed``, ``consumed_at``→``claimed_at``, ``consumed_by``→``claimed_by``,
``abandoned``→``continued``/``closed``) AND a pre-flight consumer-corpus scan confirms zero
surviving old tokens — not on a timer, not on claude-klabauter's own corpus alone.
Spec backlink: docs/plans/2026-07-22-handoff-lifecycle-vocabulary-overhaul-scope.md § C7.

``deployment_state: abandoned`` splits into two new terminals that don't map 1:1
(``continued`` + required ``continued_into`` successor, vs ``closed`` + required
``closed_reason``) — see ``_coerce_legacy_abandoned`` below for the mapping this shim picks
and why.

Emit-DERIVED fields are LEFT null in collect() (spine contract): ``last_meaningful_activity``
(LMA), ``shipped_sha`` (bash §1.5 merge-verification), and ``deliverable_status`` (bash §8.16
cross-join). They are stamped later (C3/enrich); the parity harness normalizes them on both
sides.

``plan_id`` join (dead-join fix, 2026-07-21): handoff frontmatter authors ``origin_plan_id``
(the ``origin_*`` convention shared with ``origin_goal_id`` / ``origin_handoff`` /
``origin_session``), never the bare ``plan_id`` key this reader used before — that key is
authored 0 times, so the wire field was always null. ``origin_plan_id`` mints the same
``pln-*`` id shape as ``PlanSummary.plan_id`` / plan frontmatter ``plan_id`` — same join
target, only the key name read here was wrong. The wire field name stays ``plan_id``
(contract-frozen); the bare ``plan_id`` key is kept as a fallback read only.

Contract-model-load-bearing fix (2026-07-21): every emitted record now routes through
``coordinator_core.contract.cockpit_schema.HandoffSummary`` (``model_dump()``) instead of a
hand-rolled dict literal. Before this fix, ALL 67 handoff records on this repo's own
``state/cockpit-emission.json`` violated ``handoff-summary.schema.json`` — missing the entire
``origin_*`` ancestry family (``origin_session``, ``origin_handoff``, ``origin_plan_id``,
``origin_goal_id``), which has been in the schema's ``required`` array since at least contract
2.10.0 (still required at 2.17.0 and the current 2.20.0 pin) — this was NOT new-schema
breakage, it was a long-standing emission defect that the (dead, zero-production-caller)
vendored Zod validator never caught. The pydantic model already carried these fields
(``entities/summaries.py`` D9 ancestry-origin block); the dict literal here simply never read
them from frontmatter. Routing through the model makes it load-bearing: a future field added to
the model without a matching frontmatter read now fails LOUD (``pydantic.ValidationError``,
quarantined per-record) instead of silently omitting the key.

``origin_*`` sourcing — handoff frontmatter authors these keys directly (bare id/path
strings), UNLIKE the ``plan_id`` dead-join above:
  - ``origin_session``    — session UUID string, or null.
  - ``origin_handoff``    — parent handoff path string, or null.
  - ``origin_plan_id``    — same ``pln-*`` value as the ``plan_id`` field above (the C7
    ancestry-origin spec: ``session``/``handoff``/``plan_id`` are "EMITTED kinds" — ids
    already resolvable elsewhere in this contract — so a bare id suffices for cockpit;
    no separate authoring convention, same frontmatter read as the dead-join fix).
  - ``origin_goal_id``    — frontmatter authors a bare ``list[str]`` of goal ids (see
    ``handoff_author_fork.py``'s ``origin_goal_id`` cardinality), but ``goal`` is a FOREIGN
    kind (not itself an emitted HandoffSummary row) so the contract wants the full
    ``{id, kind, label}`` triple per element (``ForeignOriginTriple``) to avoid a
    stub-less dangle. No goal-title lookup exists in this section (would require a
    cross-section join into the ``goals`` porter, out of scope for this fix) — ``label``
    is set to the bare id itself as a documented, minimal placeholder; a follow-up may
    wire a real title lookup.

Port of: emit-cockpit-snapshot.sh (example-doctrine-repo 07eedcfb, 2026-07-19) — § SECTION 1 + § SECTION
  1.5. Byte/semantic parity port.
Spec backlink: docs/plans/2026-07-04-tc3-emission-stack-python-port-and-backlog-history.md § P01
Spec backlink (origin_* fix): docs/plans/2026-07-07-spinoff-provenance-ancestry.md § C7;
  contract requiredness verified against handoff-summary.schema.json at 2.10.0/2.17.0/2.20.0.

Node-subprocess retirement (2026-07-22): this section originally shelled out to ``node
bin/query-records.js --type <handoff|handoff-archived> --limit 0 --format json`` (cwd
``ctx.repo_root``, optional ``--root ctx.subprocess_root`` override). ``_query_records`` now
calls ``coordinator_core.ops.ceremony.records_query.query_records`` in-process — no ``node``
binary, no subprocess spawn. Root resolution is preserved verbatim: query-records.js's
``detectRoot()`` (bin/query-records.js:586-593) resolves ``--root`` when given, else ``git
rev-parse --show-toplevel`` from cwd, so the seam's ``worktree_root`` argument is
``ctx.subprocess_root`` when set, else ``ctx.repo_root`` (the same value the spawn's cwd
resolved to). The fail-open contract (bash :221-222 ``… 2>/dev/null || echo "[]"``) is
unchanged — any exception from the seam call degrades to ``[]``, same as a non-zero/unparseable
node exit. The seam's ``{"path", "frontmatter"}`` return shape matches the JSON records this
function used to parse, so ``collect()`` below needed no change.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ops.ceremony.records_query import query_records as _ceremony_query_records
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.priority_resolve import (
    PriorityResolveCache,
    load_priority_ledger,
    resolve_priority,
)

from coordinator_core.frontmatter.baton_class import (
    BatonClassSchemaError,
    baton_class as _baton_class,
)
from ._shared import normalize_frontmatter
from .handoff_columns import (
    PREDECESSOR_DEFAULT,
    _DEPLOYMENT_RECOGNIZED,
    _coerce_legacy_abandoned,
    _resolve_shipped_in_dates as _resolve_shipped_in_dates_batch,
)

# Required non-nullable fields; a record missing any (non-string) is quarantined (bash:245-249).
_REQUIRED_STRING_FIELDS = ("title", "created", "status", "deployment_state")

# DR-084 P4 transitional ingest tolerance (module docstring) — old->new legacy tolerance,
# restored 2026-07-23. Old-vocabulary ``status``/``deployment_state`` values are coerced UP to
# the NEW wire vocabulary here; new-vocabulary values pass through untouched (absent from these
# maps). ``_STATUS_RECOGNIZED``/``_DEPLOYMENT_RECOGNIZED`` are the union of old-and-new legal
# values for each axis — a value outside that union is neither, and is per-record quarantined
# into ``malformed`` (2026-08-08), not raised. These records arrive from a corpus this repo does
# not own — a sibling repo authors handoffs into a shared vocabulary — so a whole-emit hard-abort
# gives one foreign artifact's unrecognized value fleet-wide blast radius over an unrelated
# ceremony (every other workstream's cadence step wedged by one record it has no stake in).
# ``superseded`` is the already-retired handoff status (2026-06-26); it maps to ``claimed``
# under the new vocabulary for the same reason it mapped to ``consumed`` under the old one —
# a superseded handoff is no longer "in play" (see ``HandoffStatus`` docstring, contract), and
# this mapping is a SEPARATE, permanently-grandfathered axis, not part of the transitional
# old->new tolerance described above.
_STATUS_OLD_TO_NEW = {"active": "open", "consumed": "claimed", "superseded": "claimed"}
_STATUS_RECOGNIZED = {"active", "consumed", "superseded", "open", "claimed"}
# _DEPLOYMENT_RECOGNIZED moved to handoff_columns.py (C1) — imported above.

# Acceptance-criteria checklist line matchers (bash awk regexes :402-403). [[:space:]] == \s.
_AC_DONE_RE = re.compile(r"^[ \t\r\n\f\v]*- \[[xX]\]")
_AC_OPEN_RE = re.compile(r"^[ \t\r\n\f\v]*- \[ \]")

# Authored handoff_id shape (coordinator-doc-new's _mint_artifact_id(prefix="hnd", slug) —
# frontmatter/schemas/handoff.schema.json :230). A frontmatter value that doesn't match this
# shape is NOT trusted as an authored id — it falls through to derivation below, same as an
# absent value.
_HANDOFF_ID_RE = re.compile(r"^hnd-[a-z0-9-]+-[0-9a-f]{6}$")


def _jq_or(value: Any, default: Any) -> Any:
    """Mirror jq ``//`` — return ``default`` when ``value`` is null or false, else ``value``.

    jq's alternative operator treats only ``null`` and ``false`` as falsy (empty string / empty
    array stay), which is the exact semantics the bash normalization relies on (:235-242, 262-289).
    """
    if value is None or value is False:
        return default
    return value


def _query_records(ctx: EmitContext, record_type: str) -> list[dict]:
    """Enumerate ``record_type`` records via the native records-query seam; [] on any failure.

    Parity: bash :221-222 ``… 2>/dev/null || echo "[]"`` — see module docstring "Node-subprocess
    retirement" for the full repoint rationale. ``worktree_root`` mirrors the retired spawn's
    root resolution (``ctx.subprocess_root`` override, else ``ctx.repo_root``); the broad
    ``except Exception`` mirrors the bash oracle's unconditional stderr-swallow-and-fall-back-to
    ``[]`` — this call site has no use for a partial/error-flagged result, only the same
    degraded-empty-list behavior the spawn already had.
    """
    worktree_root = ctx.subprocess_root if ctx.subprocess_root is not None else ctx.repo_root
    try:
        result = _ceremony_query_records(record_type, worktree_root, limit=0)
    except Exception:
        return []
    return result if isinstance(result, list) else []


def _resolve_shipped_in_dates(ctx: EmitContext, raw_shas: list[str]) -> dict[str, str]:
    """Resolve distinct raw ``shipped_in`` SHAs to commit dates via ONE git log.

    Thin ``EmitContext``-shaped wrapper over ``handoff_columns._resolve_shipped_in_dates``
    (moved there at C1) — this section's callers already have a ``ctx``, so this keeps that
    call shape rather than repointing every call site to pass ``ctx.repo_root`` directly.
    """
    return _resolve_shipped_in_dates_batch(ctx.repo_root, raw_shas)


def _acceptance_criteria(root: Path, path: Optional[str]) -> Optional[dict]:
    """Parse a handoff body checklist into {done, total}, or None (bash awk :395-405).

    Counts ``- [x]``/``- [X]`` (done+total) and ``- [ ]`` (total); returns None when the file
    is absent/untracked or has no checklist items (total == 0). File-absent → None matches the
    bash behaviour (the awk only sees existing ``$ROOT/$path`` files, :388-390).
    """
    if not path:
        return None
    full = root / path
    try:
        if not full.is_file():
            return None
        text = full.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    done = 0
    total = 0
    for line in text.splitlines():
        if _AC_DONE_RE.match(line):
            done += 1
            total += 1
        elif _AC_OPEN_RE.match(line):
            total += 1
    if total > 0:
        return {"done": done, "total": total}
    return None


def _origin_goal_id_triples(fm: dict) -> Optional[list[dict]]:
    """Wrap frontmatter ``origin_goal_id`` (bare ``list[str]`` goal ids) into the contract's
    ``ForeignOriginTriple`` shape (``{id, kind, label}``).

    ``goal`` is a FOREIGN kind (not itself emitted as a HandoffSummary row elsewhere in this
    contract), so the schema wants a full triple per element rather than a bare id (see the
    module docstring's ``origin_*`` sourcing note). No goal-title lookup is available in this
    section (that would require joining into the ``goals`` porter's own JSONL read — out of
    scope for this fix), so ``label`` is set to the bare id itself as a documented placeholder.

    Returns ``None`` when the frontmatter key is absent, null, or an empty list (D9 present-
    as-null: absent input stays null, never an empty array).
    """
    raw = fm.get("origin_goal_id")
    if not isinstance(raw, list) or not raw:
        return None
    return [
        {"id": str(goal_id), "kind": "goal", "label": str(goal_id)}
        for goal_id in raw
        if goal_id
    ] or None


# _coerce_legacy_abandoned moved to handoff_columns.py (C1) — imported above.


_TIMESTAMP_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(_\d{6})?_")
_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_SLUG_MAX_LEN = 40


def _slugify_basename(basename: str) -> str:
    """Turn a handoff basename into the human-readable slug for a derived id.

    Strips the ``.md`` extension and a leading ISO date / timestamp prefix (e.g.
    ``2026-07-01_100000_``), lowercases, collapses any run of non-alphanumerics to a single
    hyphen, strips leading/trailing hyphens, and truncates to ``_SLUG_MAX_LEN`` chars. Returns
    ``""`` when the input has no alphanumeric content left to slugify — callers fall back to the
    literal ``derived`` in that case so the id is never malformed.
    """
    stem = basename[:-3] if basename.endswith(".md") else basename
    stem = _TIMESTAMP_PREFIX_RE.sub("", stem)
    slug = _SLUG_NON_ALNUM_RE.sub("-", stem.lower()).strip("-")
    return slug[:_SLUG_MAX_LEN].strip("-")


def _derive_handoff_id(repo: str, path: Optional[str]) -> tuple[str, str]:
    """Populate ``handoff_id``/``handoff_id_derivation`` for a record (C4, wire-level only).

    Returns ``(handoff_id, "derived")`` keyed on ``(repo, basename)`` — deliberately NOT
    ``provenance.path``. ``provenance.path`` changes the moment a handoff moves from
    ``state/handoffs/`` to ``archive/handoffs/<YYYY-MM>/`` (the ordinary end-of-life move every
    handoff eventually makes), which would silently re-point this key mid-life — a join key
    that mutates out from under its consumers on a routine housekeeping move is worse than no
    key. The basename survives that move untouched. ``repo`` qualifies the pair because the
    cockpit corpus aggregates records across repos (claude-klabauter, example-doctrine-repo, example-retrieval-repo,
    example-cockpit-repo, …) and a timestamp-prefixed basename (``2026-07-19-foo.md``) is exactly the
    shape that can collide between two repos emitting on the same day.

    The id is ``hnd-<slug>-<6hex>``, matching the authored id shape exactly. The slug is derived
    from the basename (``_slugify_basename``, falling back to the literal ``derived`` when
    slugification yields nothing usable) rather than being a constant ``"derived"`` literal:
    this value is used as a JOIN KEY by a downstream consumer, and a constant slug in front of a
    6-hex suffix put only ~24 bits of entropy in front of every derived record in a corpus that
    aggregates across repos and grows without bound — the birthday bound for a 50% collision
    probability at 24 bits alone is ~several thousand records, a real ceiling for a corpus that
    grows unbounded. Widening the human-readable slug portion widens that entropy without
    changing the join key's shape.
    Review: code-reviewer -- Finding 7 -- prior wording claimed "non-trivial ... at just a
    few hundred records", which overstated the actual birthday-bound math (a few hundred
    records at 24 bits alone is well under 1% collision probability); corrected.

    The 6-hex suffix is a SHA-1 hex digest over ``f"{repo}:{basename}"``, truncated to 6 chars —
    deterministic across repeated emit runs (same inputs always produce the same id),
    collision-resistant enough for a corpus this size, and requires no persisted counter or
    registry. This hash input, hash function, and digest length are UNCHANGED by the slug
    widening above: determinism across runs and across the archival move is the ratified
    property, and only the human-readable prefix in front of it grew.

    NO PRECEDENT IN THIS REPO FAMILY, stated explicitly rather than implied: this mechanism
    SYNTHESIZES a value the record never authored, which is a third thing, not a variant of
    either family already in the codebase.
      - ``origin_*`` fields (module docstring "origin_* sourcing") are D9 present-as-null:
        when the frontmatter doesn't author the key, the field stays null forever — never
        backfilled, never invented. That policy explicitly forbids what this function does.
      - Filename-as-identity (the ``path``/basename join used elsewhere in this corpus, e.g.
        ``provenance.path`` itself) has nothing to derive — the filename already IS the
        identity, used as-is.
      - This function instead manufactures a NEW value (a hash) from inputs that are themselves
        already-stable identity material, specifically because a real ``handoff_id`` is
        genuinely absent (pre-``handoff_id``-field records, per handoff.schema.json's own
        "OPTIONAL — pre-existing handoffs … are NOT backfilled" policy) and every downstream
        consumer of this wire field still needs a non-null join key. Callers MUST use
        ``handoff_id_derivation`` to distinguish an authored id from this synthesized one —
        they are not interchangeable provenance, only interchangeable as a join key.
    """
    basename = Path(path).name if path else ""
    digest = hashlib.sha1(f"{repo}:{basename}".encode("utf-8")).hexdigest()[:6]
    slug = _slugify_basename(basename) or "derived"
    return f"hnd-{slug}-{digest}", "derived"


def _resolve_handoff_id(repo: str, path: Optional[str], fm: dict) -> tuple[str, str]:
    """Resolve ``(handoff_id, handoff_id_derivation)`` for one record (C4).

    Authored wins when present and shaped like a real minted id (``_HANDOFF_ID_RE``); anything
    else (absent, blank, malformed) falls through to ``_derive_handoff_id``. Frontmatter is
    never mutated and never backfilled — this is wire-level only (module contract, see the
    chunk's own no-backfill instruction and ``_derive_handoff_id``'s docstring).
    """
    authored = fm.get("handoff_id")
    if isinstance(authored, str) and _HANDOFF_ID_RE.match(authored):
        return authored, "authored"
    return _derive_handoff_id(repo, path)


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    """Build the HandoffSummary records + malformed bucket (parity: bash SECTION 1 + 1.5)."""
    raw_records = _query_records(ctx, "handoff") + _query_records(ctx, "handoff-archived")

    records: list[dict] = []
    malformed: list[dict] = []

    # Review: code-reviewer -- Finding 1 (28a20f28) -- baton_class.py's `_load_mapping`
    # deliberately re-reads+re-parses the vendored schema on every call and its own
    # docstring tells a tight-loop caller to cache at its own boundary instead of
    # asking the module to cache silently. `kind` is drawn from a small, bounded
    # vocabulary (HandoffKind literal, ~9 values), so caching by `kind` here collapses
    # this loop's cost to at most one schema read per DISTINCT kind for the whole
    # `collect()` call, not one read+parse per handoff record.
    #
    # Review: code-reviewer -- Finding 2 (28a20f28) -- `baton_class()` can raise
    # `BatonClassSchemaError` (missing/corrupt/unparseable vendored schema). Every other
    # failure mode in this file degrades rather than aborting (`_query_records` broad-
    # excepts to `[]`; per-record contract violations are quarantined into `malformed`),
    # so a schema-read failure here degrades every record's `baton_class` to `None` for
    # this emit run instead of hard-crashing `collect()` — and is reported once as a
    # `malformed` diagnostic below (fail-open, but the degrade is observable, not silent).
    _baton_class_cache: dict[str, Optional[str]] = {}
    _baton_class_schema_error: Optional[str] = None

    def _cached_baton_class(kind: str) -> Optional[str]:
        nonlocal _baton_class_schema_error
        if kind in _baton_class_cache:
            return _baton_class_cache[kind]
        if _baton_class_schema_error is not None:
            return None
        try:
            value = _baton_class(kind)
        except BatonClassSchemaError as e:
            _baton_class_schema_error = str(e)
            return None
        _baton_class_cache[kind] = value
        return value

    for rec in raw_records:
        if not isinstance(rec, dict):
            continue
        fm = normalize_frontmatter(rec)
        path = rec.get("path")

        # Quarantine: any required field not a string (bash select :245-249 / :317-322).
        if not all(isinstance(fm.get(f), str) for f in _REQUIRED_STRING_FIELDS):
            malformed.append({
                "path": path,
                "reason": "missing required non-nullable field (title/created/status/deployment_state)",
                "frontmatter_keys": sorted(fm.keys()),
            })
            continue

        status = fm["status"]
        # DR-084 transitional coerce shim (module-level constants' docstring) — old->new
        # legacy tolerance at ingest. ``superseded`` folds into the old->new map too (both
        # retired tokens land on ``claimed``), so a single lookup handles all three
        # legacy/retired inputs.
        if status not in _STATUS_RECOGNIZED:
            malformed.append({
                "path": path,
                "reason": f"unrecognized status {status!r}",
                "frontmatter_keys": sorted(fm.keys()),
            })
            continue
        status = _STATUS_OLD_TO_NEW.get(status, status)

        deployment_state = fm["deployment_state"]
        # DR-084 transitional coerce shim (module-level constants' docstring).
        if deployment_state not in _DEPLOYMENT_RECOGNIZED:
            malformed.append({
                "path": path,
                "reason": f"unrecognized deployment_state {deployment_state!r}",
                "frontmatter_keys": sorted(fm.keys()),
            })
            continue
        if deployment_state == "abandoned":
            deployment_state, continued_into, closed_reason = _coerce_legacy_abandoned(fm)
        elif deployment_state == "continued":
            continued_into = _jq_or(fm.get("continued_into"), None)
            closed_reason = None
        elif deployment_state == "closed":
            continued_into = None
            closed_reason = _jq_or(fm.get("closed_reason"), None)
        else:
            continued_into = None
            closed_reason = None

        # DR-084 transitional coerce shim: legacy consumed_at/consumed_by are read as a
        # fallback when the new-named field is absent, so old-vocabulary records still
        # project onto the NEW wire field names below.
        claimed_at = fm.get("claimed_at")
        if claimed_at is None:
            claimed_at = fm.get("consumed_at")
        if not (isinstance(claimed_at, str) and "T" in claimed_at):
            claimed_at = None

        claimed_by = _jq_or(fm.get("claimed_by"), _jq_or(fm.get("consumed_by"), None))

        # _shipped_in_sha temp value: raw frontmatter SHA, coerced to string (bash:299).
        raw_shipped = _jq_or(fm.get("shipped_in"), None)
        shipped_sha_raw = None if raw_shipped is None else str(raw_shipped)

        handoff_id, handoff_id_derivation = _resolve_handoff_id(ctx.repo_name, path, fm)

        # Derived from the SAME normalised value that is emitted as `kind` below, not
        # from `fm.get("kind")` — reading frontmatter a second time here is how the
        # emitted pair would come to disagree on a record whose `kind:` is absent.
        emitted_kind = _jq_or(fm.get("kind"), "session-handoff")

        records.append({
            "repo": ctx.repo_name,
            "coordinator_root_path": ".",
            "title": fm["title"],
            "created": fm["created"],
            "status": status,
            "kind": emitted_kind,
            "baton_class": _cached_baton_class(emitted_kind),
            "deployment_state": deployment_state,
            "workstream": _jq_or(fm.get("workstream"), ""),
            "predecessor": _jq_or(fm.get("predecessor"), PREDECESSOR_DEFAULT),
            "additional_predecessors": _jq_or(fm.get("additional_predecessors"), None),
            "forked_from": _jq_or(fm.get("forked_from"), None),
            "disposed_successors": _jq_or(fm.get("disposed_successors"), None),
            "scope": _jq_or(fm.get("scope"), []),
            "claimed_by": claimed_by,
            "claimed_at": claimed_at,
            "continued_into": continued_into,
            "closed_reason": closed_reason,
            "picked_up_by": _jq_or(fm.get("picked_up_by"), None),
            # shipped_in / acceptance_criteria enriched below; keep temp SHA under a private key.
            "shipped_in": None,
            "acceptance_criteria": None,
            "deliverable_id": _jq_or(fm.get("deliverable_id"), None),
            # Dead-join fix (2026-07-21): handoff frontmatter authors `origin_plan_id`
            # (the `origin_*` convention shared with `origin_goal_id` / `origin_handoff` /
            # `origin_session`), never the bare `plan_id` key this field used to read — that
            # key is authored 0 times, so the wire field was always null. `origin_plan_id`
            # values are `pln-*` ids minted by `coordinator-doc-new --type plan`, identical in
            # shape to PlanSummary.plan_id and docs/plans/*.md frontmatter `plan_id` — same
            # join target, only the reader's key name was wrong. The wire field name itself
            # stays `plan_id` (contract-frozen); `plan_id` is kept as a fallback read for any
            # future/legacy record that authors the bare key directly.
            "plan_id": _jq_or(fm.get("origin_plan_id"), _jq_or(fm.get("plan_id"), None)),
            "initiative": _jq_or(fm.get("initiative"), None),
            "caption": _jq_or(fm.get("caption"), None),
            "status_reason": _jq_or(fm.get("status_reason"), None),
            "owner": _jq_or(fm.get("owner"), None),
            "last_meaningful_activity": None,  # emit-DERIVED (LMA); stamped later.
            "workstream_type": _jq_or(fm.get("category"), None),
            "shipped_sha": None,  # emit-DERIVED (§1.5 merge-verification); stamped later.
            "deliverable_status": None,  # emit-DERIVED (§8.16 cross-join); stamped later.
            "provenance": ctx.provenance("local_fs", path=path, derivation="parsed"),
            # Ancestry-origin family (D9 present-as-null; contract-required since 2.10.0 —
            # see module docstring "Contract-model-load-bearing fix"). Frontmatter authors
            # these directly, unlike the `plan_id` dead-join above.
            "origin_session": _jq_or(fm.get("origin_session"), None),
            "origin_handoff": _jq_or(fm.get("origin_handoff"), None),
            "origin_plan_id": _jq_or(fm.get("origin_plan_id"), None),
            "origin_goal_id": _origin_goal_id_triples(fm),
            # Wire-level handoff_id derivation (C4) — see _resolve_handoff_id/_derive_handoff_id.
            "handoff_id": handoff_id,
            "handoff_id_derivation": handoff_id_derivation,
            # Priority-ledger resolution (C6a) — suggested_priority is a straight frontmatter
            # passthrough set here; pm_priority/pm_priority_origin/pm_priority_source_id are
            # placeholders filled by the resolver pass below (needs the full `records` list
            # assembled first, so a target's ledger-entry-vs-no-emitted-handoff dangling check
            # can run against the complete `known_handoff_ids` set).
            "suggested_priority": _jq_or(fm.get("suggested_priority"), None),
            "pm_priority": None,
            "pm_priority_origin": None,
            "pm_priority_source_id": None,
            # Producer axis (C6a) — model + emit pass-through only; the resolver
            # that supplies the value is a separate chunk, so this carries null
            # until that chunk lands.
            "producer": _jq_or(fm.get("producer"), None),
            "_shipped_in_sha": shipped_sha_raw,
        })

    # Review: code-reviewer -- Finding 2 (28a20f28) -- surface the baton_class degrade
    # (if any) as an observable diagnostic rather than a silent None on every record;
    # mirrors the dangling-priority-ledger-reference diagnostic pattern below.
    if _baton_class_schema_error is not None:
        malformed.append({
            "path": None,
            "reason": (
                "baton_class derivation degraded for this emit run (every record's "
                f"baton_class is null): {_baton_class_schema_error}"
            ),
        })

    # Enrich shipped_in ({sha, date}) via a single git SHA→date resolution (bash §1 step 2/4).
    sha_dates = _resolve_shipped_in_dates(
        ctx, [r["_shipped_in_sha"] for r in records if r["_shipped_in_sha"] is not None]
    )
    for r in records:
        raw_sha = r.pop("_shipped_in_sha")
        if raw_sha is not None and isinstance(sha_dates.get(raw_sha), str):
            r["shipped_in"] = {"sha": raw_sha, "date": sha_dates[raw_sha]}
        else:
            r["shipped_in"] = None
        r["acceptance_criteria"] = _acceptance_criteria(ctx.repo_root, r["provenance"]["path"])

    # Priority-ledger resolution (C6a) — populates pm_priority/pm_priority_origin/
    # pm_priority_source_id per record via the SOLE resolver entrypoint
    # (priority_resolve.resolve_priority); this section never re-implements the
    # predecessor-spine walk (module docstring, priority_resolve.py). The ledger is
    # loaded ONCE here (not per-record inside resolve_priority) so the same snapshot
    # both drives every record's resolution AND backs the dangling-target check below.
    ledger_entries = load_priority_ledger()
    known_handoff_ids = {r["handoff_id"] for r in records}
    repo_root_str = str(ctx.repo_root)

    # C6b perf hoist — the id-index / corpus-wide parent-map build resolve_priority()
    # would otherwise redo once per record (360x over a real corpus, ~90% of a
    # 28s aggregation per the profile that motivated this) is invariant for this
    # whole collect() call; build it ONCE and share it across every record below.
    # See PriorityResolveCache's own docstring for why this is a pure speed-up, not
    # a behaviour change.
    priority_cache = PriorityResolveCache(repo_root_str)

    def _priority_node_id(meta: dict, node_path: str) -> Optional[str]:
        # Mirrors the exact derivation used to mint this section's own `handoff_id`
        # (`_resolve_handoff_id`) so ledger lookups made while walking ancestors key on
        # the SAME target_id this section emits for those ancestors — see
        # priority_resolve.py's node_id_fn contract ("pass the SAME derivation the
        # caller used to key start_target_id itself").
        return _resolve_handoff_id(ctx.repo_name, node_path, meta)[0]

    for r in records:
        provenance_path = r["provenance"].get("path")
        if not provenance_path:
            # No on-disk path to walk the predecessor spine from; fields stay null
            # (placeholders set at record construction).
            continue
        abs_path = str((ctx.repo_root / provenance_path).resolve())
        try:
            resolved = resolve_priority(
                abs_path,
                r["handoff_id"],
                node_id_fn=_priority_node_id,
                ledger_entries=ledger_entries,
                repo_root=repo_root_str,
                cache=priority_cache,
            )
        except Exception:
            # Fail-open, same posture as _query_records' subprocess degrade: a
            # resolution failure for one record must not abort the whole emission.
            continue
        origin = resolved.get("origin")
        r["pm_priority"] = resolved.get("effective_priority")
        r["pm_priority_origin"] = origin
        r["pm_priority_source_id"] = resolved.get("source_id") if origin == "inherited" else None

    # Dangling-target detection: a ledger entry (target_kind: handoff) whose target_id
    # matches no emitted handoff_id is a REPORTED diagnostic, not a silently-carried or
    # record-shaped value — the ledger holds assignments for targets defined elsewhere,
    # never a second work registry (chunk brief, PART 2).
    for target_id, entry in ledger_entries.items():
        if not isinstance(entry, dict) or entry.get("target_kind") != "handoff":
            continue
        if target_id in known_handoff_ids:
            continue
        malformed.append({
            "path": None,
            "reason": (
                f"dangling priority-ledger reference: target_id {target_id!r} "
                "(target_kind: handoff) resolves to no emitted handoff"
            ),
        })

    # Route every fully-assembled record through the pydantic HandoffSummary model — this is
    # the fix that makes the model load-bearing (module docstring "Contract-model-load-bearing
    # fix"): a record whose shape doesn't satisfy the contract (e.g. a future model field this
    # section forgets to populate) is quarantined here, LOUD, instead of silently reaching the
    # wire with a missing/wrong-shaped key. `model_dump()` re-serializes back to a plain dict —
    # downstream envelope code (content-hash stamping, LMA/shipped_sha/deliverable_status
    # enrichment) expects plain dicts, not model instances.
    #
    # pydantic + cockpit_schema deferred to first use here (not module scope) — cockpit_schema
    # is a large pydantic entity tree (~40ms) that otherwise loads on every eager `ops/__init__`
    # import even for read-only /pickup callers that never reach validation. Spec:
    # docs/plans/2026-07-24-canonical-resolution-engine.md task W0-1.
    from pydantic import ValidationError

    from coordinator_core.contract.cockpit_schema import HandoffSummary

    validated: list[dict] = []
    for r in records:
        try:
            dumped = HandoffSummary(**r).model_dump()
            # content_hash is absent-when-absent (schema: plain "type": "object", NOT
            # `anyOf`-null — unlike additional_predecessors/forked_from's genuine
            # `.nullable().optional()` combo). This section never populates it (stamped
            # later by envelope._stamp_content_hash, or left OMITTED when unresolvable);
            # `model_dump()` always materializes a default=None field, so strip the key
            # back out rather than emit a schema-invalid explicit null.
            if dumped.get("content_hash") is None:
                dumped.pop("content_hash", None)
            validated.append(dumped)
        except ValidationError as exc:
            malformed.append({
                "path": r["provenance"]["path"],
                "reason": f"contract validation failed: {exc}",
            })

    return validated, malformed
