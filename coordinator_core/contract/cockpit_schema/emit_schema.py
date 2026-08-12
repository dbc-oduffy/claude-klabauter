"""
emit_schema — pydantic port of example-doctrine-repo `coordinator/cockpit-contract/scripts/emit-schema.ts`.

Emits one `<name>.schema.json` per registered entity plus a bundled
`cockpit-contract.schema.json` (all entities under `$defs`), reproducing the
Zod `z.toJSONSchema()` emission SHAPE byte-for-byte — the canonical
cross-repo wire format (R7) this port must not silently reshape.

pydantic's own `model_json_schema()` / `TypeAdapter(...).json_schema()` does
NOT reproduce that shape unmodified; four post-processing passes close the
gap (§ recipe T4e-c):

1. **Inline, don't `$ref`.** pydantic hoists every nested `BaseModel` to a
   top-level `$defs` entry and points at it via `$ref`; Zod's emission
   inlines nested object shapes at the use-site with no `$defs` at all.
   `_resolve_refs` walks the raw schema and substitutes each `$ref` with its
   resolved (and recursively resolved) `$defs` entry, then the `$defs` map
   itself is discarded.
2. **Strip pydantic-only noise.** `title` (every field and every object get
   one; Zod never emits `title`) is dropped unconditionally. `description`
   is dropped ONLY off object-level schemas (submodel/entity class
   docstrings that have no Zod `.describe()` analogue) — a leaf field's
   `Field(description=...)` (the `.describe()` port) is preserved verbatim.
   `discriminator` (pydantic's auto-added `oneOf` + property-name mapping
   for a `Field(discriminator=...)` union) is dropped — Zod's
   `discriminatedUnion` emits a plain `oneOf` with no discriminator keyword.
3. **Re-inject what pydantic drops silently, same as Zod drops it.**
   `model_validator` logic (like Zod `.superRefine()`) never reaches
   `model_json_schema()`. `_inject_provenance_conditionals` ports
   `injectProvenanceConditionals()` verbatim — same detection heuristic
   (title OR `source_kind`-signature match), same four `allOf`/`if`/`then`
   clauses, appended exactly once per schema tree.
4. **Reproduce Zod-specific keyword injections pydantic doesn't do on its
   own.** Every Zod `.int()` field emits `minimum`/`maximum` pinned to
   JS's `Number.MIN_SAFE_INTEGER`/`MAX_SAFE_INTEGER` (`-9007199254740991` /
   `9007199254740991`) even with no explicit `.min()`/`.max()` — plain
   pydantic `int` does not. `_inject_safe_int_bounds` adds the missing
   bound only where absent, so a field that DOES carry an explicit
   `Field(ge=..., le=...)` (the port of `.nonnegative()`/`.max()`) keeps its
   own bound and only the missing side gets backfilled — mirrors the
   observed Zod behavior (`.int().nonnegative()` emits `minimum: 0` but
   STILL emits `maximum: 9007199254740991`, verified against
   `schema/backlog-history.schema.json`). Likewise `IsoDateTime`/`IsoDate`
   (`common.py`) carry no `format` keyword on their own — pydantic emits
   only the regex `pattern` from `StringConstraints`; Zod's `z.iso.datetime()`
   / `z.iso.date()` stamp `format: "date-time"` / `format: "date"` alongside
   the pattern. `_inject_iso_format` detects the two canonical patterns by
   identity against `common.py`'s own regex constants and stamps the
   matching `format` keyword.

Byte-identity ALSO requires reproducing Zod's own JSON Schema key ORDER —
`committed-emit-drift`-equivalent comparison is a raw string compare, not a
structural deep-equal (see `test/committed-emit-drift.test.ts`). `_reorder`
rebuilds every dict (at every depth) inserting keys per `_KEY_ORDER`,
matching the order observed across every `schema/*.schema.json` example this
port was built and verified against.

Known limitation: `_resolve_refs` does not cycle-detect. None of the 28
registered cockpit-contract entities are self-referential at the JSON-Schema
level (verified against `schema/*.json` at port time) — a future
self-referential entity would need `_resolve_refs` extended with a
recursion guard before this emitter could safely handle it.

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e
Source: coordinator/cockpit-contract/scripts/emit-schema.ts (example-doctrine-repo)
Negative-spec: this module does NOT own `ENTITY_SCHEMAS` (the name→model
registry) — that is the stage-2 (T4e-d) agent's job, wiring
`coordinator_core/contract/cockpit_schema/__init__.py`. `main()` below
imports it lazily and fails loud with a pointer to that stage if it isn't
there yet; every other function in this module is registry-agnostic and
independently testable against any `{name: BaseModel subclass | discriminated
union type}` mapping (see `emit_schemas`).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from coordinator_core.contract.cockpit_schema.common import (
    _ISO_DATE_PATTERN,
    _ISO_DATETIME_PATTERN,
)

# ---------------------------------------------------------------------------
# CONTRACT_VERSION — single literal source of truth (this module).
#
# The T4e-c recipe (§ 6, GIVES-PAUSE) flagged an open architecture question —
# where CONTRACT_VERSION canonically lives post-port, and whether it should
# be read in-process from this pydantic module or cross-checked against
# example-doctrine-repo's committed schema/*.json — as EM/PM/the Staff Engineer-adjudicated, NOT a
# BUILD-wave call. That question is still open; this module wins only the
# narrower "which literal is authoritative" sub-question, by being the
# dependency-free leaf that must stay independently importable/runnable
# (the `coordinator-cockpit-emit-schema` console-script entrypoint) without
# depending on `__init__.py`'s registry-wiring state — see this module's own
# docstring negative-spec. `__init__.py` imports and re-exports this
# constant rather than redeclaring it (see that module's own comment);
# `tests/test_contract_version_single_source.py` fails loud (not
# skip-gated) if a second literal is ever reintroduced.
# ---------------------------------------------------------------------------
# MINOR bump 3.3.0 -> 3.4.0 (C6a): adds four DERIVED HandoffSummary fields
# (pm_priority/pm_priority_origin/pm_priority_source_id/suggested_priority)
# and rides the already-landed owner-casing clause amendment (claude-klabauter
# 4a73368b, example-doctrine-repo 39a65721), which shipped without its own bump so it
# would ride this one. MINOR, not MAJOR, is deliberate: example-cockpit-repo's
# checkSchemaVersion() hard-throws on a MAJOR mismatch in either direction
# but tolerates emission-minor-greater-than-vendored on the same major (with
# a warning), so a minor bump lets cockpit keep ingesting without a
# lockstep re-vendor — they re-vendor once, for both changes together, on
# their own schedule rather than being blocked mid-emit.
#
# 3.4.0 -> 3.5.0 (review-integration, priority-ledger workstream, unrelated
# to exec-summary): landed between this comment's authoring and the C6
# bump below; see git history for that bump's own rationale.
#
# MINOR bump 3.5.0 -> 3.6.0 (C6, human-facing-doc-staleness-detector plan):
# adds `docs_staleness` (REQUIRED-WITH-NULL, D9) to `ExecSummary` — per-doc
# staleness verdicts from `coordinator_core.ops.doc_staleness`. Additive
# only: no existing required field changed, narrowed, or removed, same
# class as D19-D28/D32 §9 (example-doctrine-repo coordinator/cockpit-contract/
# DECISIONS.md) — cockpit confirmed this class in their 2026-07-28 reply.
# The plan's original target was "3.3.0 -> 3.4.0"; disk had already moved
# to 3.5.0 via the unrelated priority-ledger bump above by the time this
# chunk landed, so this bump targets HEAD's actual version, not the plan's
# stale literal (disk is truth).
#
# MINOR bump 3.7.0 -> 3.8.0 (C8a, baton-kind vocabulary widen): adds
# "roadmap-baton", "roadmap-seed", "goal-seed" to HandoffKind (entities/
# summaries.py) ahead of any on-disk record migrating to the D1 rename
# targets. ADDITIVE ONLY — every pre-existing HandoffKind value, including
# the three tokens being retired on the live surface ("spinoff-roadmap",
# "spinoff-roadmap-creator", "spinoff-goal"), stays in the Literal for
# archived-record read-compatibility, same reasoning as "spike-result".
# Same additive class as the 3.5.0->3.6.0 bump above.
# Spec backlink: docs/plans/2026-07-29-baton-kind-vocabulary-one-axis-per-field.md § D1/C8a.
#
# MINOR bump 3.8.0 -> 3.9.0 (C3a, baton-kind vocabulary publish): adds
# `baton_class` (REQUIRED-WITH-NULL, D9) to `HandoffSummary` (entities/
# summaries.py) — values continuation | deflection | intention | null,
# derived at emit time from `kind` by the one canonical function
# (frontmatter/baton_class.py), never read from frontmatter and never
# stored there. This is the field that lets a consumer separate batons
# laid toward a goal from batons deflected out of a session without
# hand-rolling a membership set over `kind`; the C8a widen above only
# renamed the tokens, it did not make the axis queryable.
#
# Same class as the 3.5.0->3.6.0 `docs_staleness` bump above — a new
# required-with-null property on an existing entity object is
# `nested-field-additive` per docs/wiki/schema-version-gate.md's own
# holding/non-holding split, so MINOR is the rule-correct bump independent
# of any consumer reply; cockpit's 2026-07-28 reply corroborates it as
# tolerable in practice, it does not carry the classification on its own.
# NULL is load-bearing, not defensive:
# HandoffKind deliberately retains "spike-result" so archived records are
# not dropped from the emission, and a spike result is not a baton, so it
# has no class. Legacy pre-rename kinds are NOT null — they canonicalise
# through the alias map, so an archived "spinoff-roadmap" emits "intention".
# Spec backlink: docs/plans/2026-07-29-baton-kind-vocabulary-one-axis-per-field.md § D2/C3a.
#
# MINOR bump 3.9.0 -> 3.10.0 (D42 synthesized widen, cockpit-contract half):
# adds "synthesized" to `Derivation` (provenance.py) — an agent-derived fact,
# as distinct from fetched (raw), structured (parsed), aggregated (rolled_up),
# or multi-source-derived (computed). Member-only additive on an existing
# enum: no member removed, no required field changed, so MINOR, same class as
# D22/D23/D28. Example-doctrine-repo ratified this in cockpit-contract DECISIONS.md D42
# (2026-08-03) and routed the emitter work here under the D31
# emitter-ownership boundary; the artifact-shape-contract half landed first
# (ops/emit_artifact_shape_contract.py, its own 3.1.0 row), leaving this
# emitter as the unexecuted second half — which the cross-package parity test
# (contract/cockpit_schema/tests/test_provenance_parity.py) then caught as a
# divergence. Example-doctrine-repo's bilateral version-bump assent for this bump is on record
# in cross-repo/inbox/2026-08-05-example-doctrine-repo-em-cockpit-derivation-synthesized-
# half-landed.md; example-doctrine-repo re-runs regen-cockpit-schema.py on their side once this
# lands, regenerating all 58 inlined enum sites plus the standalone envelope
# from this one literal.
# MINOR bump 3.10.0 -> 3.11.0 (scan-completeness on RoadmapSummary): adds
# `scan_incomplete` (boolean) and `scan_errors` (array of string) to
# RoadmapSummary, propagating a signal assemble_roadmap_dag already produces and
# roadmap_serve.py already returns, but which the emission leg dropped. Without
# it an unreadable handoff subtree reaches consumers as a silently-low
# roll_up.total, and any percentage derived from it renders confidently wrong
# with no degraded state — the defect example-cockpit-repo-em reported 2026-08-11.
#
# Both keys are REQUIRED and key-present-always (`false` / `[]` on a clean
# scan), deliberately departing from the nullable-never-optional convention the
# rest of this entity follows: a consumer that cannot distinguish absent from
# false cannot tell a clean scan from an old producer, which is the exact
# ambiguity the field exists to remove. claude-central-em ratified both the
# MINOR classification and the two D9 conditions (required/always-present, and
# scan_errors items pinned to string) on 2026-08-11, discharged the D21 consumer
# census (cockpit and rag both pass), and specified the D39 source-first release
# sequence: claude-klabauter widens and regenerates into example-doctrine-repo's schema/ out-dir, example-doctrine-repo
# commits the bundle and advances the release tag, claude-klabauter re-vendors, and only
# then does the emitter begin populating the keys.
CONTRACT_VERSION = "3.11.0"

# ---------------------------------------------------------------------------
# ProvenanceEnvelope conditional injection — ported verbatim from
# emit-schema.ts's injectProvenanceConditionals() / isProvenanceSite().
# Spec backlink: cockpit-contract/src/provenance.ts § ProvenanceEnvelope.superRefine
# ---------------------------------------------------------------------------

_GIT_BACKED_ENUM = ["github_graphql", "github_rest", "git_commit"]
_NON_GIT_ENUM = [
    "local_fs",
    "coordinator_artifact",
    "transcript_summary",
    "sec_edgar",
    "code_comparison",
]
_ALL_SOURCE_KINDS = frozenset(_GIT_BACKED_ENUM + _NON_GIT_ENUM)

_PROVENANCE_CONDITIONALS: list[dict[str, Any]] = [
    {
        "if": {
            "properties": {"source_kind": {"enum": _GIT_BACKED_ENUM}},
            "required": ["source_kind"],
        },
        "then": {"properties": {"ref": {"not": {"type": "null"}}}},
    },
    {
        "if": {
            "properties": {"source_kind": {"enum": _NON_GIT_ENUM}},
            "required": ["source_kind"],
        },
        "then": {"properties": {"ref": {"type": "null"}}},
    },
]

_ANCHORLESS_GUARD_CONDITIONALS: list[dict[str, Any]] = [
    {
        "if": {
            "properties": {"repo": {"const": ""}},
            "required": ["repo"],
        },
        "then": {
            "properties": {
                "entity_anchor": {
                    "type": "object",
                    "properties": {"value": {"not": {"const": ""}}},
                    "required": ["kind", "value"],
                },
            },
            "required": ["entity_anchor"],
        },
    },
]

_WELL_FORMED_ANCHOR_CONDITIONALS: list[dict[str, Any]] = [
    {
        "if": {
            "properties": {"entity_anchor": {"type": "object"}},
            "required": ["entity_anchor"],
        },
        "then": {
            "properties": {
                "entity_anchor": {
                    "properties": {
                        "kind": {"not": {"const": ""}},
                        "value": {"not": {"const": ""}},
                    },
                },
            },
        },
    },
]


def _is_provenance_site(obj: dict[str, Any]) -> bool:
    """
    Detect a ProvenanceEnvelope-shaped JSON Schema object.

    Signature-based only (properties include `source_kind` carrying exactly
    the 8-value source-kind enum, plus `derivation` and `observed_at`) — this
    single check subsumes the TS original's separate title-based path, since
    a top-level ProvenanceEnvelope schema always ALSO satisfies the
    signature match (the title-based branch was a TS-side optimization, not
    a distinct detection case).
    """
    props = obj.get("properties")
    if not isinstance(props, dict):
        return False
    sk = props.get("source_kind")
    if not isinstance(sk, dict):
        return False
    enum_arr = sk.get("enum")
    if not isinstance(enum_arr, list) or len(enum_arr) != len(_ALL_SOURCE_KINDS):
        return False
    if not all(v in _ALL_SOURCE_KINDS for v in enum_arr):
        return False
    if "derivation" not in props or "observed_at" not in props:
        return False
    return True


def _inject_provenance_conditionals(node: Any) -> Any:
    """
    Recursively walk a JSON Schema value. At every detected
    ProvenanceEnvelope site, append the 4 if/then allOf clauses. NOT
    idempotent — call exactly once per schema tree (mirrors the TS
    original's own warning: injection does not change the
    `properties.source_kind` signature it keys on, so a second pass would
    re-match and double-append).
    """
    if isinstance(node, list):
        return [_inject_provenance_conditionals(v) for v in node]
    if not isinstance(node, dict):
        return node

    processed = {k: _inject_provenance_conditionals(v) for k, v in node.items()}

    if _is_provenance_site(processed):
        existing = processed.get("allOf")
        existing = existing if isinstance(existing, list) else []
        processed["allOf"] = [
            *existing,
            *_PROVENANCE_CONDITIONALS,
            *_ANCHORLESS_GUARD_CONDITIONALS,
            *_WELL_FORMED_ANCHOR_CONDITIONALS,
        ]

    return processed


# ---------------------------------------------------------------------------
# $ref inlining — pydantic hoists nested BaseModels to $defs; Zod inlines.
# ---------------------------------------------------------------------------


def _resolve_refs(node: Any, defs: dict[str, Any]) -> Any:
    """shell-doc-ok: `$ref` and `$defs` are JSON Schema keywords, not shell
    expansions; the spelling is fixed by the JSON Schema spec.

    Inline every `$ref: "#/$defs/<Name>"` site with its (recursively
    resolved) `$defs[<Name>]` content. See module docstring's "Known
    limitation" for the no-cycle-detection caveat."""
    if isinstance(node, list):
        return [_resolve_refs(v, defs) for v in node]
    if not isinstance(node, dict):
        return node

    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        name = ref.rsplit("/", 1)[-1]
        resolved = _resolve_refs(defs[name], defs)
        extra = {k: v for k, v in node.items() if k != "$ref"}
        if not extra:
            return resolved
        merged = dict(resolved) if isinstance(resolved, dict) else {}
        merged.update(_resolve_refs(extra, defs))
        return merged

    return {k: _resolve_refs(v, defs) for k, v in node.items()}


# ---------------------------------------------------------------------------
# Optional (non-nullable) field unwrapping — T4e-d parity-oracle fix.
#
# Zod `.optional()` (absence-allowed, no accompanying `.nullable()`) ports to
# pydantic as `field: T | None = None` (the recipe's own § 1 mapping table
# row — needed so the field is omittable from Python call sites), but
# pydantic's `model_json_schema()` renders that AS IF it were nullable too:
# `anyOf: [T, {type: null}], default: null`. Zod's `.optional()` alone never
# allows an explicit `null` value once the key is present — only absence.
# ---------------------------------------------------------------------------


_ZOD_NULLABLE_OPTIONAL_MARKER = "x-zod-nullable-optional"


def _unwrap_optional_non_nullable(node: Any) -> Any:
    """
    Unwrap pydantic's `anyOf: [T, {type: null}], default: null` emission
    back to bare `T` for every NOT-required object property carrying that
    exact shape — matching Zod's `.optional()` emission (plain `T`, simply
    absent from `required`). A field porting Zod's `.nullable()` (required,
    present-as-null) is `T | None` with NO default — it IS in `required`
    and never reaches this branch.

    Exactly two fields in this corpus (`HandoffSummary.additional_
    predecessors` / `.forked_from`) combine Zod `.nullable().optional()` —
    absent-on-omit AND null-tolerant-when-present — which genuinely wants
    to KEEP the `anyOf: [T, null]` shape pydantic already produces. Those
    two fields carry the `x-zod-nullable-optional` `json_schema_extra`
    marker (see summaries.py) specifically so this pass skips them; the
    marker itself is stripped before final emission (`_strip_pydantic_
    noise`), never reaching the byte-compared output.
    """
    if isinstance(node, list):
        return [_unwrap_optional_non_nullable(v) for v in node]
    if not isinstance(node, dict):
        return node

    processed = {k: _unwrap_optional_non_nullable(v) for k, v in node.items()}

    props = processed.get("properties")
    if isinstance(props, dict):
        required = set(processed.get("required") or [])
        new_props: dict[str, Any] = {}
        for key, sub in props.items():
            any_of = sub.get("anyOf") if isinstance(sub, dict) else None
            if (
                key not in required
                and isinstance(sub, dict)
                and "default" in sub
                and sub["default"] is None
                and isinstance(any_of, list)
                and len(any_of) == 2
                and {"type": "null"} in any_of
                and not sub.get(_ZOD_NULLABLE_OPTIONAL_MARKER)
            ):
                non_null = next(s for s in any_of if s != {"type": "null"})
                extra = {
                    k2: v2 for k2, v2 in sub.items() if k2 not in ("anyOf", "default")
                }
                merged = {**non_null, **extra}
                new_props[key] = merged
            else:
                new_props[key] = sub
        processed["properties"] = new_props

    return processed


# ---------------------------------------------------------------------------
# pydantic-only noise stripping
# ---------------------------------------------------------------------------


def _strip_pydantic_noise(node: Any) -> Any:
    """
    Drop `title` unconditionally (Zod never emits it). Drop `description`
    only off object-level schemas (`properties` present — a submodel/entity
    class docstring, which has no Zod `.describe()` analogue); a leaf
    field's `description` (the `.describe()` port) is preserved. Drop
    `discriminator` (pydantic's auto-added discriminated-union mapping —
    Zod's `discriminatedUnion` emits a bare `oneOf`, no discriminator
    keyword).

    `properties`/`$defs` map KEYS are entity field/type NAMES, not schema
    keywords — an entity that declares a field literally named `title`
    (e.g. `PlanSummary.title`, `HandoffSummary.title`) must not have that
    property deleted as if it were the auto-generated object-level `title`
    noise. Only each such map's VALUES are recursively stripped; the
    top-level dict *containing* the `properties`/`$defs` map is still
    subject to the unconditional `title`/`discriminator` pop (that IS
    pydantic's own object-level noise).
    """
    if isinstance(node, list):
        return [_strip_pydantic_noise(v) for v in node]
    if not isinstance(node, dict):
        return node

    stripped: dict[str, Any] = {}
    for k, v in node.items():
        if k in ("properties", "$defs") and isinstance(v, dict):
            stripped[k] = {fk: _strip_pydantic_noise(fv) for fk, fv in v.items()}
        else:
            stripped[k] = _strip_pydantic_noise(v)
    stripped.pop("title", None)
    stripped.pop("discriminator", None)
    stripped.pop(_ZOD_NULLABLE_OPTIONAL_MARKER, None)
    # No entity in this corpus uses Zod `.default(...)` (recipe § 1 table,
    # verified) — `default` never appears in a committed schema.json. The
    # `_unwrap_optional_non_nullable` pass already drops it for plain
    # `.optional()` fields; the `.nullable().optional()` combo (marker
    # above) bypasses that pass, so it is stripped unconditionally here too.
    stripped.pop("default", None)
    if "properties" in stripped:
        stripped.pop("description", None)
    # A fixed-length tuple (`tuple[float, float]` — the `z.tuple([...])`
    # port, e.g. `IntelligenceSignal.confidence_interval`) — pydantic backs
    # `prefixItems` with `minItems`/`maxItems` pinned to the tuple arity;
    # Zod's `z.tuple()` emits `prefixItems` alone, no cardinality bounds.
    if "prefixItems" in stripped:
        stripped.pop("minItems", None)
        stripped.pop("maxItems", None)
    return stripped


# ---------------------------------------------------------------------------
# Zod-specific keyword injections pydantic does not perform on its own
# ---------------------------------------------------------------------------

_JS_MIN_SAFE_INTEGER = -9007199254740991
_JS_MAX_SAFE_INTEGER = 9007199254740991


def _inject_safe_int_bounds(node: Any) -> Any:
    """Every Zod `.int()` field emits `minimum`/`maximum` pinned to JS's
    safe-integer bounds even absent an explicit `.min()`/`.max()`; plain
    pydantic `int` does not. Backfill only the missing side, so an explicit
    `Field(ge=..., le=...)` port (`.nonnegative()`, etc.) is preserved."""
    if isinstance(node, list):
        return [_inject_safe_int_bounds(v) for v in node]
    if not isinstance(node, dict):
        return node

    processed = {k: _inject_safe_int_bounds(v) for k, v in node.items()}
    if processed.get("type") == "integer":
        processed.setdefault("minimum", _JS_MIN_SAFE_INTEGER)
        processed.setdefault("maximum", _JS_MAX_SAFE_INTEGER)
    return processed


def _inject_iso_format(node: Any) -> Any:
    """`IsoDateTime`/`IsoDate` (common.py) carry only a regex `pattern` from
    `StringConstraints` — pydantic does not add `format`. Zod's
    `z.iso.datetime()`/`z.iso.date()` stamp `format: "date-time"` /
    `format: "date"` alongside the pattern; detect by exact pattern-string
    identity against common.py's own regex constants and backfill."""
    if isinstance(node, list):
        return [_inject_iso_format(v) for v in node]
    if not isinstance(node, dict):
        return node

    processed = {k: _inject_iso_format(v) for k, v in node.items()}
    if processed.get("type") == "string" and "format" not in processed:
        pattern = processed.get("pattern")
        if pattern == _ISO_DATETIME_PATTERN:
            processed["format"] = "date-time"
        elif pattern == _ISO_DATE_PATTERN:
            processed["format"] = "date"
    return processed


def _inject_property_names(node: Any) -> Any:
    """`dict[str, T]` (the `z.record(z.string(), T)` port) emits
    `{"type": "object", "additionalProperties": {...}}` from pydantic alone;
    Zod's `z.record()` also stamps a `propertyNames: {"type": "string"}`
    sibling (verified against `schema/routine-signal.schema.json`'s `inputs`
    and `day-rollup.schema.json`'s `tshirt_counts`). Every dict-typed field
    in this corpus keys on `str` (no non-string-keyed record), so injection
    is unconditional on the object-without-`properties`-but-with-
    `additionalProperties`-object shape, not per-field-name special-cased.
    Fixed-shape objects (`.strict()`/regular BaseModels) always carry
    `properties`, so they never match this branch.

    `dict[str, Any]` (`z.record(z.string(), z.unknown())` — e.g.
    `MalformedRecords`'s per-section arrays, snapshot-envelope.py) is the
    one dict-value-type-agnostic case: pydantic emits `additionalProperties:
    true` (a bare bool, not a sub-schema dict) for `Any`; Zod's emission is
    `additionalProperties: {}` (an empty-but-present sub-schema — "any value
    allowed" spelled as a schema, not a boolean). Normalize `true` to `{}`
    here too, then apply the same `propertyNames` stamp."""
    if isinstance(node, list):
        return [_inject_property_names(v) for v in node]
    if not isinstance(node, dict):
        return node

    processed = {k: _inject_property_names(v) for k, v in node.items()}
    if processed.get("additionalProperties") is True:
        processed["additionalProperties"] = {}
    if (
        processed.get("type") == "object"
        and "properties" not in processed
        and isinstance(processed.get("additionalProperties"), dict)
        and "propertyNames" not in processed
    ):
        processed["propertyNames"] = {"type": "string"}
    return processed


def _nest_multivariant_nullable_union(node: Any) -> Any:
    """A multi-variant Zod union wrapped in `.nullable()` (e.g.
    `z.union([z.string(), z.number()]).nullable()` — `RoadmapDagNode.sprint`
    / `.wave`) emits a NESTED `anyOf: [{anyOf: [str, num]}, {type: null}]` —
    the null wraps the whole union, it is not flattened into the union's own
    member list. pydantic's `str | float | None` flattens to one 3-entry
    `anyOf: [str, num, null]` instead. Detect any anyOf with >= 3 entries
    where exactly one is the null-type schema and re-nest the remaining
    (2+) entries under an inner anyOf, matching Zod's non-flattened shape.
    A 2-entry `anyOf: [T, null]` (single-type nullable) needs no change —
    that already matches Zod's `.nullable()` on a scalar 1:1."""
    if isinstance(node, list):
        return [_nest_multivariant_nullable_union(v) for v in node]
    if not isinstance(node, dict):
        return node

    processed = {k: _nest_multivariant_nullable_union(v) for k, v in node.items()}
    any_of = processed.get("anyOf")
    if isinstance(any_of, list) and len(any_of) >= 3:
        null_entries = [s for s in any_of if s == {"type": "null"}]
        non_null = [s for s in any_of if s != {"type": "null"}]
        if len(null_entries) == 1 and len(non_null) >= 2:
            processed["anyOf"] = [{"anyOf": non_null}, {"type": "null"}]
    return processed


# ---------------------------------------------------------------------------
# Key-order reproduction — committed-emit-drift-equivalent comparison is a
# raw string compare (see module docstring), so dict key order must match
# Zod's own emission order at every depth, not just be structurally equal.
# ---------------------------------------------------------------------------

_KEY_ORDER = [
    "$schema",
    "$id",
    "title",
    "type",
    "enum",
    "const",
    "minLength",
    "maxLength",
    "format",
    "pattern",
    "minimum",
    "maximum",
    "anyOf",
    # Verified against every committed schema/*.json field carrying both a
    # constraint/union keyword and a `.describe()` string (e.g.
    # backlog-history's `repo` field — minLength then description;
    # intelligence-signal's `content_key` — anyOf then description): Zod
    # emits the constraint/union keyword(s) BEFORE `description`, not
    # after — `description` sits directly ahead of the structural
    # keywords (items/properties/...) instead.
    "description",
    "items",
    "prefixItems",
    "propertyNames",
    "properties",
    "required",
    "additionalProperties",
    "oneOf",
    "allOf",
    "if",
    "then",
    "not",
    "version",
]


def _reorder(node: Any) -> Any:
    """Reorder every JSON-Schema-KEYWORD dict per `_KEY_ORDER`. The
    `properties` map's own keys are entity FIELD NAMES, not schema
    keywords — an entity that happens to declare a field literally named
    `type` (e.g. `RoadmapDagEdge.type`) must NOT have that field name
    reordered as if it were the `type` keyword; only its VALUE (that
    field's own schema) is recursively reordered. Same guard applies to
    `$defs` (map of entity-name -> schema) and `required` (a plain list of
    field-name strings, not itself keyword-shaped)."""
    if isinstance(node, list):
        return [_reorder(v) for v in node]
    if not isinstance(node, dict):
        return node

    processed: dict[str, Any] = {}
    for k, v in node.items():
        if k in ("properties", "$defs") and isinstance(v, dict):
            # Field-name-keyed map — reorder each field's OWN schema, but
            # never reinterpret a field name as a schema keyword.
            processed[k] = {fk: _reorder(fv) for fk, fv in v.items()}
        else:
            processed[k] = _reorder(v)

    ordered: dict[str, Any] = {}
    for key in _KEY_ORDER:
        if key in processed:
            ordered[key] = processed[key]
    # Any keys not covered by _KEY_ORDER (should not occur against the
    # corpus this emitter was verified against) still surface, appended in
    # their original position, rather than being silently dropped.
    for key, value in processed.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


# ---------------------------------------------------------------------------
# Per-entity schema construction
# ---------------------------------------------------------------------------


def _raw_json_schema(entity: Any) -> dict[str, Any]:
    """Get pydantic's raw `model_json_schema()` (BaseModel subclass) or
    `TypeAdapter(...).json_schema()` (bare type alias, e.g. a
    `Field(discriminator=...)` union like `ScopedEmission` — not itself a
    `BaseModel`)."""
    if isinstance(entity, type) and issubclass(entity, BaseModel):
        return entity.model_json_schema()
    return TypeAdapter(entity).json_schema()


def build_entity_schema(entity: Any) -> dict[str, Any]:
    """
    Produce the Zod-emission-shaped, byte-comparable JSON Schema for one
    entity (pydantic `BaseModel` subclass or discriminated-union type alias)
    — everything `emit-schema.ts`'s per-entity loop does EXCEPT the
    version stamp and the version-desync guard (both handled by the caller,
    since they're not properties of the schema shape itself). Pure, no I/O
    — the unit this module's parity checks call directly.
    """
    raw = _raw_json_schema(entity)
    defs = raw.get("$defs", {})
    inlined = _resolve_refs({k: v for k, v in raw.items() if k != "$defs"}, defs)
    unwrapped = _unwrap_optional_non_nullable(inlined)
    stripped = _strip_pydantic_noise(unwrapped)
    with_bounds = _inject_safe_int_bounds(stripped)
    with_format = _inject_iso_format(with_bounds)
    with_property_names = _inject_property_names(with_format)
    with_nested_unions = _nest_multivariant_nullable_union(with_property_names)
    injected = _inject_provenance_conditionals(with_nested_unions)
    # z.toJSONSchema({ target: "draft-2020-12" }) stamps $schema on every
    # per-entity emission (top-level only — not on nested/inlined sites,
    # which pydantic never added $schema to in the first place).
    stamped = {"$schema": "https://json-schema.org/draft/2020-12/schema", **injected}
    return _reorder(stamped)


# ---------------------------------------------------------------------------
# Version-desync guard — ported verbatim from assertNoVersionDesync().
# ---------------------------------------------------------------------------


def _without_version_and_description(node: Any) -> Any:
    if isinstance(node, list):
        return [_without_version_and_description(v) for v in node]
    if not isinstance(node, dict):
        return node
    return {
        k: _without_version_and_description(v)
        for k, v in node.items()
        if k not in ("version", "description")
    }


def assert_no_version_desync(
    entity_name: str, fresh_json: dict[str, Any], committed_path: Path
) -> None:
    """
    Guard against a shape change landing without a CONTRACT_VERSION bump.
    No-op when the entity has no committed schema file yet (new entity).
    Fires only when BOTH hold: the freshly-computed shape (version and
    description excluded) differs from the committed shape, AND the
    committed file's `version` already equals the current CONTRACT_VERSION
    (nobody bumped it to cover the change).
    """
    if not committed_path.exists():
        return

    try:
        committed_json = json.loads(committed_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        # Review: code-reviewer (cockpit-schema-a F2, P2) — was print()+return,
        # a fail-open no-op that would let this guard silently never fire
        # again once the committed file went unparseable. This guard exists
        # solely to catch an unbumped CONTRACT_VERSION; a corrupt/unreadable
        # committed file is itself a signal something is wrong, so fail loud
        # (raise) rather than swallow.
        raise RuntimeError(
            f'emit_schema: committed schema file for entity "{entity_name}" is '
            f"unparseable, cannot run version-desync check — {committed_path} "
            f"({exc!r})"
        ) from exc

    committed_version = (
        committed_json.get("version") if isinstance(committed_json, dict) else None
    )
    shapes_match = _without_version_and_description(
        fresh_json
    ) == _without_version_and_description(committed_json)

    if not shapes_match and committed_version == CONTRACT_VERSION:
        raise RuntimeError(
            f'emit_schema: version-desync guard tripped for entity "{entity_name}" — '
            f'its schema shape changed but CONTRACT_VERSION ("{CONTRACT_VERSION}") was '
            f"NOT bumped. Bump CONTRACT_VERSION before re-running the emitter."
        )


# ---------------------------------------------------------------------------
# Emission loop + bundle assembly
# ---------------------------------------------------------------------------


def _schema_out_dir(out_dir: str | os.PathLike[str] | None = None) -> Path:
    if out_dir is not None:
        return Path(out_dir).resolve()
    env_override = os.environ.get("COCKPIT_SCHEMA_OUT_DIR")
    if env_override:
        return Path(env_override).resolve()
    # Review: code-reviewer (cockpit-schema-a F1, P1) — was 4 levels of ".."
    # (Path(__file__).parent / .. / .. / .. / .. / "schema"), which from
    # coordinator_core/contract/cockpit_schema/ resolves to the PARENT of the
    # claude-klabauter repo, silently writing every *.schema.json + bundle
    # outside the repo entirely. The docstring's "byte-identical to the TS
    # original" claim doesn't hold post-port: emit-schema.ts lived at
    # cockpit-contract/scripts/ (1 level below the cockpit-contract root, 1
    # ".." to its sibling schema/ dir); this module lives 3 levels below the
    # claude-klabauter repo root (coordinator_core/contract/cockpit_schema/), so
    # the equivalent depth is 3 "..", landing at <repo-root>/schema — inside
    # the repo, sibling to coordinator_core/.
    return (Path(__file__).parent / ".." / ".." / ".." / "schema").resolve()


def emit_schemas(
    entity_schemas: dict[str, Any],
    out_dir: str | os.PathLike[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Write one `<name>.schema.json` per entry in `entity_schemas` plus a
    bundled `cockpit-contract.schema.json`, mirroring `emit-schema.ts`'s
    emission loop + bundle assembly. `entity_schemas` is caller-supplied
    (this module does not own the registry — see module docstring) so it is
    independently testable against any subset without waiting on the
    stage-2 registry wiring.

    Returns `{name: schema_dict}` for every emitted entity (bundle excluded)
    — callers that only need the in-memory shapes (e.g. parity tests) don't
    need to re-read the just-written files.
    """
    if not entity_schemas:
        raise RuntimeError(
            "emit_schema: entity_schemas is empty — bad registry or "
            "regression. Refusing to emit an empty schema set."
        )

    schema_dir = _schema_out_dir(out_dir)
    schema_dir.mkdir(parents=True, exist_ok=True)

    bundle_defs: dict[str, Any] = {}
    emitted: dict[str, dict[str, Any]] = {}

    for name, entity in entity_schemas.items():
        shaped = build_entity_schema(entity)
        json_doc = {**shaped, "version": CONTRACT_VERSION}
        json_doc = _reorder(json_doc)
        out_path = schema_dir / f"{name}.schema.json"
        assert_no_version_desync(name, json_doc, out_path)
        out_path.write_text(json.dumps(json_doc, indent=2, ensure_ascii=False) + "\n")
        bundle_defs[name] = json_doc
        emitted[name] = json_doc

    bundle = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://coordinator.local/cockpit-contract.schema.json",
        "title": "Cockpit work-state contract",
        "description": f"Canonical work-state contract (tc-2), version {CONTRACT_VERSION}.",
        "version": CONTRACT_VERSION,
        # bundle_defs entries are already injected per-entity above; running
        # _inject_provenance_conditionals() again here would double every
        # provenance allOf block (mirrors the TS original's own warning).
        "$defs": bundle_defs,
    }
    (schema_dir / "cockpit-contract.schema.json").write_text(
        json.dumps(bundle, indent=2, ensure_ascii=False) + "\n"
    )

    print(f"emit_schema: emitted {len(emitted)} entity schemas + 1 bundle -> {schema_dir}")
    return emitted


def main(argv: list[str] | None = None) -> None:
    """
    Runnable entrypoint (also the `coordinator-cockpit-emit-schema` console
    script — see pyproject.toml). Parses argv BEFORE touching the registry or
    the filesystem: a console-script consumer expects `--help`/a bad flag to
    behave like every other CLI (usage text or a usage error, no side
    effects), not to fall through and emit 28 files into whatever directory
    happens to resolve.

    Lazily imports the entity registry — NOT owned by this module (see
    module docstring's negative-spec) — so this file stays
    importable/testable before the registry lands.
    """
    parser = argparse.ArgumentParser(
        prog="coordinator-cockpit-emit-schema",
        description=(
            "Emit cockpit-contract JSON schemas (one per entity + a bundle). "
            "See coordinator_core.contract.cockpit_schema.emit_schema's module "
            "docstring for the full byte-identity recipe."
        ),
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help=(
            "Directory to write the emitted schemas to. Takes precedence "
            "over COCKPIT_SCHEMA_OUT_DIR. Passed straight through to "
            "emit_schemas(out_dir=...) — see _schema_out_dir() for the full "
            "precedence chain, which this flag does not alter."
        ),
    )
    args = parser.parse_args(argv)

    try:
        from coordinator_core.contract.cockpit_schema import (  # type: ignore[attr-defined]
            ENTITY_SCHEMAS,
        )
    except ImportError as exc:
        raise SystemExit(
            "emit_schema: coordinator_core.contract.cockpit_schema.ENTITY_SCHEMAS "
            "is not wired yet — that registry lands in the stage-2 (T4e-d) build "
            "wave (__init__.py). This entrypoint is ready; it has nothing to "
            "iterate until then."
        ) from exc

    emit_schemas(ENTITY_SCHEMAS, out_dir=args.out_dir)


if __name__ == "__main__":
    main()
