"""
coordinator_core.ops.emit_artifact_shape_contract — emit a versioned JSON Schema
contract from the coordinator schema registry (example-doctrine-repo coordinator/schemas/*.yaml
+ *.schema.json).

shell-doc-ok: this changelog quotes real JSON-Schema `$defs`/`$ref`/`$id` pointer
syntax throughout (the artifact this module emits) — accurate documentation, not
a shell paste hazard. `>=`/`->` occurrences below are version-bump/rewrite prose.

Port source: example-doctrine-repo coordinator/bin/emit-artifact-shape-contract.js (642 lines).
Spec backlink: archive/specs/2026-06/2026-06-25-example-initiative-tc-4-fleet-machinery-contract-emit.md § Chunk B1
               docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md, BIG_PORT Wave B
               item emit-artifact-shape-contract

PURPOSE
Emits `artifact-shape-contract/artifact-shape-contract.schema.json` (written under the
Example-doctrine-repo coordinator/ tree, NOT claude-klabauter) — a stable, versioned JSON Schema contract
(draft-2020-12) carrying:
  (a) One JSON Schema per registered schema type (under `$defs`).
  (b) The cross-type liveness mapping as first-class contract data (tc-0 D2 forward
      seam) — so tc-5 (example-retrieval-repo store) can derive LIVE/BLOCKED/DONE without
      re-reading or re-implementing bin/query-records.js in another language.

DESIGN DECISIONS (tc-4 § Ratified design decisions, unchanged by this port)
  D1 — JSON Schema format (cockpit-contract parity); target: draft-2020-12.
  D2 — ONE registry-wide `version` ("1.0.0" originally); NOT per-type versions.
  D3 — Artifact named by CAPABILITY: `artifact-shape-contract`. Consumers
       (example-retrieval-repo, cockpit) are NOT part of the producer surface.

PATTERN REUSE (not coupling)
Reuses cockpit-contract emit patterns (cockpit-contract/scripts/emit-schema.ts:
versioned bundle + per-entity schema files). Source here is the coordinator YAML/JSON
Schema registry, not Zod TypeScript. Separate artifact, separate version line, no
shared module.

CONTRACT_VERSION history (condensed — full per-bump rationale lives in the JS oracle's
own header comment, example-doctrine-repo coordinator/bin/emit-artifact-shape-contract.js:36-49,
preserved there as the historical record; this port carries only the CURRENT pin):
  1.9.0  (2026-06-xx) session-events-summary $def removed (no vendored consumer, additive-safe).
  1.10.0-1.11.0 (2026-07-03) ProvenanceEnvelope sub-shape injected + same-day D9 fix.
  1.12.0 (2026-07-05, 2026-07-09) source_kind 'git_commit' + derivation 'computed' source/output resync.
  1.13.0 (2026-07-09) plan-tasks.schema.json registered (48th schema).
  1.14.0 (2026-07-10) source_kind 'transcript_summary'.
  1.15.0 (2026-07-12) source_kind 'sec_edgar'.
  1.16.0 (2026-07-14) entity_anchor sibling field (identity-space freeze, model A).
  1.17.0 (2026-07-14) source_kind 'code_comparison'.
  1.18.0 (2026-07-20) hoist source-schema `$defs` into the bundle root (repoIdentityTuple,
    provenance, capabilityEntry, indexEntry, + peers) — the internal `$ref: "#/$defs/X"` in a
    JSON-Schema-backed source resolves against the FLAT bundle root, so unhoisted defs were
    dangling. $defs-only; schema_count unchanged.
  1.19.0 (2026-07-21) percolate-store.schema.json registered (54th schema).
  2.0.0  (2026-07-22) DR-084 P4 narrow: handoff status/deployment_state enum values
    REMOVED (active/consumed/abandoned retired for open/claimed + continued/closed).
    Enum-removal is non-additive — consumers holding 1.x semantics would accept
    artifacts the narrowed contract rejects. MAJOR, mirroring the cockpit contract's
    own P3 MAJOR (3.0.0) for the same vocabulary flip. Erratum source:
    cross-repo/archive/2026-07-22-claude-central-em-return-to-doe-actioned.md Ask 2
    (example-doctrine-repo's 4bd0e4f1 regen shipped the narrowed body still stamped 1.19.0).
  2.1.0  (2026-07-27) handoff-archived read-tolerance widening, MINOR: (1) `status`
    enum gains `consumed`, (2) `origin_handoff_id` widened to anyOf[string, null],
    (3) `applies_to` widened to a recursive glob. All three additive/widening — no
    `$defs` removed, no enum value narrowed. Ratified example-doctrine-repo-side in
    example-doctrine-repo coordinator/artifact-shape-contract/DECISIONS.md 2.1.0 row; that row
    notes the bundle regen is claude-klabauter-run and lagged the ratification — this bump
    discharges that lag.
  3.0.0  (2026-07-28) MAJOR: schema_to_json_schema's `_isJsonSchema` branch now
    carries through top-level `allOf` and `additionalProperties` from `.schema.json`
    sources — previously silently dropped (only $schema/title/description/type/
    properties/required/applies_to were copied). Affects all 8 schemas that declare
    either keyword: cutover, handoff, handoff-archived, plan, plan-tasks,
    priority-ledger, spike-result, tier-u-grant. This is a correctness fix, not a
    feature: `coordinator/tests/test_handoff_corpus_conformance.py` validates real
    `state/handoffs/*.md` records against `$defs.handoff` via Draft202012Validator,
    so the drop meant every conditional-required rule (e.g. DR-084's `deployment_state:
    closed requires closed_reason`) and every `additionalProperties: false` closed-
    shape guard went silently unenforced. MAJOR, not MINOR, because restoring the
    constraint is NON-additive from a consumer's perspective — it can reject
    documents the loose bundle previously accepted. Empirically confirmed: replaying
    the fixed `handoff` $def against the live `state/handoffs/*.md` corpus surfaces 5
    pre-existing corpus defects (bad `kind` enum value, an invalid `predecessor`
    literal, 3 successor-handoffs missing `created`) that the bug had been silently
    masking; `handoff-archived` against `archive/handoffs/**` shows 0 new failures.
    Those 5 are reported as a separate defect, not fixed by this bump — they are
    active-corpus data issues, not an emitter/contract problem.
  3.1.0  (2026-08-03) MINOR: derivation gains 'synthesized' — a lawful fourth
    processing stage for an agent-synthesized secondary source (no lawful value
    existed for this today; production artifact dsrc-03 carried 'rolled_up', which
    its own producer documented as wrong). Governance-ratified example-doctrine-repo-side
    (cockpit-contract/DECISIONS.md D42, twin note artifact-shape-contract/
    DECISIONS.md) as the claude-klabauter-side half of the D31 emitter-ownership split — D42
    ratifies the enum widen, this bump lands the bytes. Additive-only: one enum
    array gains one member, no $defs removed, no field narrowed.
  3.2.0  (2026-08-03) MINOR: bundler now rewrites every `$defs.*` `$ref` carrying the
    `https://coordinator.local/schemas/<name>.schema.json` $id-convention prefix to its
    bundled intra-bundle location `#/$defs/<name>`, when `<name>` is a registered def —
    and refuses to emit (rc=1) if it names an unregistered one. Fixes
    `$defs.plan.properties.tasks.items.$ref`, previously left pointing at that
    non-resolving host: a stock `Draft202012Validator` raised `Unresolvable` for any
    plan record carrying a non-empty `tasks:` array (dormant only because `jsonschema`
    resolves `items` lazily, so `{"tasks": []}` passed). The referenced shape was
    already inlined as `$defs['plan-tasks']` — this only rewires the pointer.
    MINOR, not MAJOR, deliberately — contrast with 3.0.0. 3.0.0 was MAJOR because
    restoring dropped `allOf`/`additionalProperties` could reject documents the loose
    bundle previously **accepted**. Here nothing previously-accepted becomes rejected:
    `{"tasks": []}` passed before and still passes, and a non-empty `tasks:` array
    previously produced an `Unresolvable` **error**, not an acceptance. The change
    converts an unresolvable ref into a resolvable intra-bundle one — no `$defs`
    removed, no enum narrowed, no field/required removed. Reported by
    example-retrieval-repo-ue-addon-em (consumer-side containment, per-file demotion to a
    violation row), relayed and verified by example-doctrine-repo-em; contract-owner ruled `tasks`
    stays in-contract, no example-doctrine-repo-side schema edit needed (the source `$id` convention at
    `coordinator/schemas/plan.schema.json:120` is correct as authored — only the
    bundling pass had the gap). See
    cross-repo/inbox/2026-08-03-example-doctrine-repo-em-artifact-contract-external-ref-survives-bundling.md.
  4.0.0  (2026-08-04) MAJOR: `$defs.review.properties.reviewer` enum narrows from persona
    names to agent-registry role slugs — `the Staff Engineer`/`sid`/`the Data Science Reviewer`/`the Front-End Reviewer`/`the UX Reviewer`/`the Director of Engineering` are
    dropped; `staff-eng`/`staff-game-dev`/`staff-data-sci`/`senior-front-end`/`staff-ux`/
    `eng-director`/`vp-product` plus `code-reviewer`/`code-reviewer+staff-eng` remain.
    A persona name is meaningless as a wire value to an OSS consumer who renamed their
    agents or runs none at all; personas survive as human-facing display aliases only.
    Six enum members REMOVED — textbook non-additive, so MAJOR per the bump rule below,
    and consumers must re-vendor. Source narrow: example-doctrine-repo `coordinator/schemas/review.schema.json`
    1.0.0->2.0.0. `schema_count` unchanged at 61 — no $defs added or removed.
    Landed by example-doctrine-repo-em under per-session PM assent (no standing cross-repo grant);
    announced before the edit in
    cross-repo/inbox/2026-08-04-example-doctrine-repo-em-contract-version-bump-owed-4-0-0-and-im-landing-it.md.
    Recorded honestly: the example-doctrine-repo side first regenerated this bundle claiming NO bump was owed,
    citing the same-window-catch-up convention (the 2.1.0/2026-07-28 and 3.0.0/2026-07-31
    no-bump rows). That was wrong — those rows are catch-up regens for a narrow already
    major-bumped at source in an earlier commit, whereas this is the narrow's FIRST landing,
    structurally the 2.0.0 case. Caught by example-doctrine-repo-side review before the close, not by a consumer.
  5.0.0  (2026-08-05) hnd- id pattern narrow: `handoff_id`, `predecessor_id` and
    `origin_handoff_id` now carry `^hnd-(?!placeholder-replace-with)[a-z0-9-]+-[0-9a-f]{6}$`,
    excluding the scaffolder's placeholder slug. Pattern-narrow is non-additive by the same
    reasoning as the 2.0.0 enum-removal: a consumer holding 4.x semantics accepts ids the
    narrowed contract rejects. MAJOR. Cause: coordinator-doc-new minted these ids from the
    title slug at scaffold time, so a --title-less scaffold baked a placeholder into a DURABLE
    id; because such an id is well-formed it matched the old pattern, and a `blocked_by`
    naming one RESOLVED and silently cleared instead of dangling. Mint-site guard
    (claude-klabauter f67a1d859530) landed FIRST and the fleet-wide corpus sweep SECOND, so this
    narrow strands nothing — verified across 7 coordinator repos, 1588 id-field values, zero
    stranded. Mirrors handoff.schema.json's own 4.0.0 -> 5.0.0 (example-doctrine-repo 0391ab20c), which is the
    source this contract regenerates from.
  6.0.0  (2026-08-05) dlv- id pattern narrow: `deliverable_id`'s string arm gains
    `^dlv-(?!placeholder-replace-with)[0-9a-zA-Z][0-9a-zA-Z.-]*$` (null arm untouched — null is
    the documented pre-backfill value). Same false-clear class as 5.0.0 one namespace over, and
    MAJOR by the same reasoning: a consumer holding 5.x semantics accepts ids the narrowed
    contract rejects. Supersedes handoff.schema.json's 5.0.0 NEGATIVE SPEC clause ("this narrow
    does NOT cover `deliverable_id`, which has no pattern in any schema").
    Deliberately NOT a mirror of 5.0.0's character class. `[a-z0-9-]` strands 24 live ids
    fleet-wide, all legitimate `dlv-<stub_id>` mints, because mint_deliverable_id's
    mint-from-stub path passes stub_id through verbatim with no case-folding — 23 uppercase in
    example-doctrine-repo (`computed-skills-B*`, `agent-fleet-G*` families) and 1 dot-bearing in
    example-retrieval-repo-ue-addon. Hence case-permissive, `.`-admitting, and NOT anchored on
    `-[0-9a-f]{6}` (the stub-origin shape carries no hex suffix; the corpus holds
    trailing-dash-before-hex slug-truncation artifacts). Verified twice independently: claude-klabauter
    swept 7 repos / 2200 non-null values, zero rejections; example-doctrine-repo re-derived on their own corpus,
    675 values, zero rejections, and confirmed the 23-id stranding count separately.
    SEQUENCING — THIS CONSTANT MOVES BEFORE THE BODY IT DESCRIBES, DELIBERATELY. The schema
    edit is example-doctrine-repo's: `coordinator/schemas/handoff.schema.json` is authored there and VENDORED
    here (`check_schema_drift` is a byte-for-byte tamper-check against example-doctrine-repo HEAD), so at this
    commit claude-klabauter's vendored copy does NOT yet carry the pattern and this emitter's output body
    is unchanged. That is not a violation of the bump rule below, which forbids two different
    bodies sharing one stamp — not one body briefly spanning two stamps. It is a hard
    precondition: example-doctrine-repo's `coordinator/tests/test_artifact_shape_contract_freshness.py`
    regenerates the bundle in-memory through THIS module against THEIR live schemas and diffs
    it against their committed bundle, so with the constant still at 5.0.0 their only options
    were to commit a red freshness gate or to commit a changed body stamped 5.0.0 — handing
    example-retrieval-repo/cockpit a narrowed domain under an unchanged version. Both doors shut until
    this moves. No consumer is exposed by the ordering: the stamped artifact lives in example-doctrine-repo's
    tree, so until they regenerate, no committed bundle anywhere carries 6.0.0. Their landing
    commit (pattern + regen + pin refresh + handoff.schema.json 5.0.0 -> 6.0.0 +
    plan.schema.json 1.6.0 -> 2.0.0) closes the window. Cross-repo edits were declined in both
    directions per DR-127 — no standing commit grant exists either way.
  6.1.0  (2026-08-13) `peer-set-entry` enters the bundle: example-doctrine-repo registered
    `coordinator/schemas/peer-set-entry.schema.json` at dead2ed6b (per-repo peer-set entry,
    code-comparison C2) and this emitter has never emitted it. `schema_count` 62 -> 63,
    `$defs` +1, zero removed. MINOR per the bump rule below — the delta is entirely additive.
    Classified rather than counted, and verified independently on both sides: 15 `$defs` change
    shape, with zero enum narrows, zero required-additions and zero property removals across the
    whole bundle. The one change that scans as a removal is not one —
    `sizing-object.scout_evidence.items.type: string` becomes `items.anyOf: [string, object]`,
    a widen.
    SEQUENCING — same shape as 6.0.0 above, one severity down: the constant moves before the
    body it describes. Example-doctrine-repo's `test_artifact_shape_contract_freshness.py` regenerates in-memory
    through THIS module against THEIR live schemas, so with the constant at 6.0.0 their only
    doors were a red freshness gate or a changed body stamped 6.0.0 — two bodies under one
    stamp, which the bump rule forbids. No consumer is exposed in the window: the stamped
    artifact lives in example-doctrine-repo's tree, so until they regenerate no committed bundle anywhere carries
    6.1.0. They regenerate, commit the bundle, and own the DECISIONS.md row on their side; claude-klabauter
    owns only this constant. Cross-repo edits stay declined in both directions per DR-127.
    Requested in cross-repo/inbox/2026-08-13-example-doctrine-repo-em-contract-not-pinned-bump-6-1-0-owed-first.md.
Bump rule (unchanged from JS): additive $defs/enum-widen changes stay minor; any
non-additive change (enum-narrow, field/required removal) bumps MAJOR regardless of
whether a vendored consumer version-asserts yet — two different bundle bodies must
never share a version stamp.

Cross-repo data-layer reuse: YAML-dialect parsing (parse_yaml, load_schemas, match_schema,
parse_frontmatter — schema.js lines 33-397/524-737) is NOT re-derived here. This module
imports load_schemas from coordinator_core.frontmatter.schema_validate (T4d-g1a port,
already landed) — see that module's own docstring for the YAML-dialect negative-spec.
This module owns ONLY the artifact-shape-contract-specific translation layer:
fieldToJsonSchema/schemaToJsonSchema (schema.js lines 61-224), the LIVENESS_MAPPING and
SUB_SHAPES contract data (schema.js lines 227-556), and the emit/CLI-wiring surface
(schema.js lines 558-643).

Negative-spec (faithful port, not a redesign):
  - Unrecognised field descriptors emit `{description: "[emit-note] ..."}` (permissive,
    never silently dropped) — mirrors schema.js fieldToJsonSchema's fallback branches.
  - SUB_SHAPES keys colliding with a schemas/*.yaml|*.schema.json schema name is a
    fail-loud refusal-to-emit (rc=1), not a silent overwrite — mirrors the JS
    Object.assign collision guard (schema.js lines 602-609).
  - schema_count counts ONLY schemas/*.yaml|*.schema.json artifact types; injected
    SUB_SHAPES (ProvenanceEnvelope) are NOT counted — mirrors the JS comment at
    schema.js lines 600-602.
  - `emitted_at` is deliberately NOT emitted (dropped in the JS oracle to keep output
    byte-deterministic across runs for the committed-artifact drift guard) — do not
    reintroduce a timestamp field.

Exit codes (parity-critical):
  0 — contract emitted successfully.
  1 — business failure: SCHEMAS is empty (refusing to emit an empty contract), OR a
      SUB_SHAPES key collides with a registered schema name. Mirrors the JS oracle's
      two `process.exit(1)` call sites (schema.js lines 573-574, 606-608).
  2 — DEDICATED transport/config-failure code (coordinator root not resolvable via the
      EMIT_ARTIFACT_SHAPE_CONTRACT_COORDINATOR_ROOT env var, or the schemas directory is
      missing). The JS oracle has no equivalent state (it always resolves its own
      __dirname-relative COORDINATOR constant) — this rc is new surface introduced by
      the cross-repo split (this module runs claude-klabauter-side, schemas live example-doctrine-repo-side) and is
      deliberately a code the business logic never returns (porter-brief addendum § 3b).
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, List

from coordinator_core.frontmatter.schema_validate import load_schemas
from coordinator_core.lifecycle_constants import (
    HANDOFF_TERMINAL_DEPLOYMENT,
    HANDOFF_TERMINAL_STATUS,
)
from coordinator_core.ops.records_query import liveness as _records_liveness
from coordinator_core.session.declared_writes import declare_write

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONTRACT_VERSION = "6.1.0"

# Env var read by main() to locate the example-doctrine-repo coordinator/ root (schemas/ input,
# artifact-shape-contract/ default output). Set by the example-doctrine-repo-side polyglot trampoline,
# which computes this from its own __file__ location (bin/../ = coordinator/) before
# calling main(argv) — mirrors how the JS oracle derives COORDINATOR from __dirname.
COORDINATOR_ROOT_ENV = "EMIT_ARTIFACT_SHAPE_CONTRACT_COORDINATOR_ROOT"

# Output-dir override env var — literal name preserved from the JS oracle
# (schema.js line 56: ARTIFACT_CONTRACT_OUT_DIR) so existing test tooling that sets
# this var to redirect output to a tmp dir keeps working unchanged.
OUT_DIR_ENV = "ARTIFACT_CONTRACT_OUT_DIR"

# ---------------------------------------------------------------------------
# Minimal YAML-schema -> JSON Schema converter (port of schema.js lines 61-224)
# ---------------------------------------------------------------------------

SCALAR_TYPES: dict[str, dict] = {
    "string": {"type": "string"},
    "iso-date": {"type": "string", "format": "date"},
    "iso-datetime": {"type": "string", "format": "date-time"},
    "timestamp": {"type": "string", "format": "date-time"},
    "iso-date-or-datetime": {
        "anyOf": [
            {"type": "string", "format": "date"},
            {"type": "string", "format": "date-time"},
        ]
    },
    "boolean": {"type": "boolean"},
    "number": {"type": "number"},
    "list-of-string": {"type": "array", "items": {"type": "string"}},
    "string-or-null": {"type": ["string", "null"]},
    "number-or-null": {"type": ["number", "null"]},
    "string-or-list-of-string": {
        "anyOf": [
            {"type": "string"},
            {"type": "array", "items": {"type": "string"}},
        ]
    },
    "any": {
        "description": "heterogeneous hand-authored value — permissive by design (no type enforcement)"
    },
}


def field_to_json_schema(field_name: str, descriptor: Any) -> dict:
    """Convert a field descriptor from the coordinator YAML schema subset into a
    JSON Schema fragment. Port of schema.js fieldToJsonSchema (lines 103-149).

    Deep-copies SCALAR_TYPES entries (mirrors the JS structuredClone fix, F3) so the
    iso-date-or-datetime token's nested anyOf list is never shared by reference across
    fields — a mutation by one caller must not leak into every other field using it.
    """
    if isinstance(descriptor, str):
        scalar = SCALAR_TYPES.get(descriptor)
        if scalar is not None:
            return copy.deepcopy(scalar)
        return {"description": f'[emit-note] unrecognised field type "{descriptor}" for field "{field_name}"'}

    if isinstance(descriptor, dict):
        if descriptor.get("type") == "enum" and isinstance(descriptor.get("values"), list):
            frag: dict = {"enum": descriptor["values"]}
            if descriptor.get("description"):
                frag["description"] = descriptor["description"]
            return frag

        if descriptor.get("type") == "object" and isinstance(descriptor.get("fields"), dict):
            properties = {}
            for sub_name, sub_desc in descriptor["fields"].items():
                properties[sub_name] = field_to_json_schema(f"{field_name}.{sub_name}", sub_desc)
            frag = {"type": "object", "properties": properties}
            if descriptor.get("description"):
                frag["description"] = descriptor["description"]
            return frag

        descriptor_type = descriptor.get("type")
        if isinstance(descriptor_type, str) and descriptor_type in SCALAR_TYPES:
            frag = dict(SCALAR_TYPES[descriptor_type])
            if descriptor.get("description"):
                frag["description"] = descriptor["description"]
            return frag

        # Bare-nested-object: a plain map of field->descriptor with no `type` wrapper.
        # Used by bug-backlog, debt-backlog, improvement-queue, lesson-entry for their
        # `system` field. Equivalent to {type:'object', fields:{...}} — detect by
        # absence of a `type` key on the outer object and recurse over its entries.
        if "type" not in descriptor:
            properties = {}
            for sub_name, sub_desc in descriptor.items():
                properties[sub_name] = field_to_json_schema(f"{field_name}.{sub_name}", sub_desc)
            return {"type": "object", "properties": properties}

        # Nested object descriptor we don't recognise — permissive with a note.
        return {"description": f'[emit-note] unrecognised descriptor for field "{field_name}": {json.dumps(descriptor)}'}

    # null / missing
    return {"description": f'[emit-note] null/missing descriptor for field "{field_name}"'}


def schema_to_json_schema(schema_name: str, schema: dict, src_file: str) -> dict:
    """Convert a parsed coordinator YAML schema object (or JSON-Schema-backed schema)
    to a JSON Schema object. Port of schema.js schemaToJsonSchema (lines 159-224).
    """
    # JSON-Schema-backed schemas (stamped with `_isJsonSchema` by load_schemas) are
    # already in full JSON Schema format — pass their properties through directly
    # instead of running the YAML-subset translation (which would produce empty
    # properties, since their `required` is an array, not a field->descriptor map).
    if schema.get("_isJsonSchema") is True:
        emitted: dict = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": schema_name,
            "description": schema.get("description") or f"Coordinator artifact schema: {schema_name}. Source: schemas/{src_file}.",
            "type": schema.get("type") or "object",
        }
        if isinstance(schema.get("properties"), dict):
            emitted["properties"] = schema["properties"]
        if isinstance(schema.get("required"), list) and len(schema["required"]) > 0:
            emitted["required"] = schema["required"]
        if schema.get("applies_to"):
            emitted["x-coordinator-applies_to"] = schema["applies_to"]
        # Carry through cross-field/whole-document JSON Schema constraints declared
        # at the source's top level. Previously dropped silently — a strict validator
        # (e.g. coordinator/tests/test_handoff_corpus_conformance.py's
        # Draft202012Validator over $defs.handoff) enforced only properties/required,
        # missing every conditional-required rule (e.g. handoff's `deployment_state:
        # closed requires closed_reason`) and every `additionalProperties: false`
        # closed-shape guard. `if`/`then`/`else` are never top-level in this registry
        # (verified empirically — always nested inside an `allOf` clause), so only
        # `allOf` and `additionalProperties` need porting; extend this list if a
        # future source schema adds anyOf/oneOf/not/propertyNames/etc. at top level.
        if isinstance(schema.get("allOf"), list):
            emitted["allOf"] = schema["allOf"]
        if "additionalProperties" in schema:
            emitted["additionalProperties"] = schema["additionalProperties"]
        return emitted

    properties: dict = {}
    required: list = []

    required_block = schema.get("required")
    if isinstance(required_block, dict):
        for field, descriptor in required_block.items():
            properties[field] = field_to_json_schema(field, descriptor)
            required.append(field)

    optional_block = schema.get("optional")
    if isinstance(optional_block, dict):
        for field, descriptor in optional_block.items():
            properties[field] = field_to_json_schema(field, descriptor)
            # optional fields are NOT added to required[]

    json_schema: dict = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": schema_name,
        "description": f"Coordinator artifact schema: {schema_name}. Source: schemas/{src_file}.",
        "type": "object",
        "properties": properties,
    }

    if len(required) > 0:
        json_schema["required"] = required

    if schema.get("applies_to"):
        json_schema["x-coordinator-applies_to"] = schema["applies_to"]

    return json_schema


# ---------------------------------------------------------------------------
# Cross-type liveness mapping (first-class contract data)
# ---------------------------------------------------------------------------
# Transcribed verbatim from schema.js LIVENESS_MAPPING (lines 241-408), itself
# transcribed from bin/query-records.js liveness() + canonical-artifact-shapes.md
# § The Cross-Type Liveness Predicate. This is the forward seam for tc-5: example-retrieval-repo
# derives its LIVE/BLOCKED/DONE derivation FROM this published mapping — it does NOT
# re-read query-records.js.
#
# C8b dedup (2026-07-27, plan-line-item-resolution-model): this table used to be a
# second hand-maintained copy of the SAME per-status LIVE/BLOCKED/DONE answers that
# coordinator_core.ops.records_query.liveness() computes at runtime — the "verbatim
# transcribed from ... liveness()" note above is the tell that it was always meant
# to follow that function, not drift independently alongside it. The per-status
# "mapping"/"axes" leaf VALUES below are now derived by calling ``liveness()``
# directly (via ``_derive_status_mapping``/``_derive_axis_mapping`` below) rather
# than re-typed as literals, so the two representations cannot silently disagree
# again. Direction chosen deliberately: this contract-emitter module imports FROM
# records_query (the runtime engine, and the historically-original source per the
# transcription note above), not the reverse — records_query is a leaner,
# lower-level module (no schema-loading/JSON-Schema-translation machinery) and
# stays free of any dependency on this publishing/emission surface.
# The richer per-type STRUCTURE (combination_rule, axis/axes, note, default,
# spec references) stays hand-authored here — only the leaf status→result
# answers are mechanically derived; declarative shape and code-branch shape are
# different questions than the values a set of test inputs would reproduce.
# ---------------------------------------------------------------------------


def _derive_status_mapping(record_type: str, statuses: List[str]) -> dict:
    """Derive a status -> LIVE/BLOCKED/DONE mapping by calling
    ``records_query.liveness()`` for each status in *statuses*, instead of
    hand-transcribing the result. ``statuses`` is the fixed, curated list of
    values this contract chooses to publish for *record_type* (not derived
    from the schema's full enum) — this only removes the duplication of the
    *values*, it does not widen or narrow which statuses are documented.
    """
    return {status: _records_liveness({"status": status}, record_type) for status in statuses}


def _derive_axis_mapping(record_type: str, field: str, values: List[str]) -> dict:
    """Same as ``_derive_status_mapping`` but for a non-``status`` axis field
    (e.g. ``deployment_state``) evaluated in isolation — the other axis is
    left absent (empty string), matching how ``liveness()``'s single-field
    axis tables are exercised in the C8b parity check that landed alongside
    this helper (``test_liveness_mapping_derived_matches_records_query``).
    """
    return {value: _records_liveness({field: value}, record_type) for value in values}


# Explicit display-order tuples for the handoff axes (status, deployment_state).
# HANDOFF_TERMINAL_STATUS / HANDOFF_TERMINAL_DEPLOYMENT (lifecycle_constants,
# the SSOT) are unordered frozensets, but this contract is a vendored,
# byte-identity-checked artifact (example-doctrine-repo's test_artifact_shape_contract_freshness.py
# regenerates and diffs against a committed bundle) — a `sorted()` derivation
# would silently reorder consumer-facing bytes on any future Python/hash-seed
# change with no version bump. Order is therefore hand-authored here once, and
# checked against the SSOT below rather than trusted to stay in sync silently.
_HANDOFF_STATUS_DISPLAY_ORDER: tuple[str, ...] = ("claimed", "consumed", "superseded")

# deployment_state's display order additionally carries the three live
# (non-terminal) values, which have no SSOT constant — only the terminal tail
# is checked against HANDOFF_TERMINAL_DEPLOYMENT.
_HANDOFF_DEPLOYMENT_LIVE_DISPLAY_ORDER: tuple[str, ...] = ("awaiting_gate", "ready_to_fire", "in_flight")
_HANDOFF_DEPLOYMENT_TERMINAL_DISPLAY_ORDER: tuple[str, ...] = ("shipped", "continued", "closed", "abandoned")
_HANDOFF_DEPLOYMENT_DISPLAY_ORDER: tuple[str, ...] = (
    _HANDOFF_DEPLOYMENT_LIVE_DISPLAY_ORDER + _HANDOFF_DEPLOYMENT_TERMINAL_DISPLAY_ORDER
)


def _assert_axis_display_order_covers_ssot(
    axis_name: str, display_order: tuple[str, ...], ssot: frozenset[str], *, exact: bool
) -> None:
    """Fail-loud drift guard between a hand-authored display-order tuple above
    and its SSOT frozenset in coordinator_core.lifecycle_constants. ``exact``
    requires set equality (the ``status`` axis lists ONLY terminal values);
    otherwise requires the display order to be a superset (the
    ``deployment_state`` axis additionally lists non-terminal live values).
    Raises at import time — a narrowed or widened SSOT must never emit stale
    values into this vendored contract silently.
    """
    order_set = set(display_order)
    ok = order_set == ssot if exact else order_set >= ssot
    if ok:
        return
    missing = sorted(ssot - order_set)
    unexpected = sorted(order_set - ssot) if exact else []
    raise RuntimeError(
        f"coordinator_core/ops/emit_artifact_shape_contract.py: handoff.{axis_name} "
        f"display order has drifted from lifecycle_constants — missing from display "
        f"order: {missing}; unexpected in display order: {unexpected}. Update the "
        "display-order tuple in this module to match the SSOT, preserving intended "
        "display order (do NOT sort — see the comment above on vendored-bundle "
        "byte-identity)."
    )


_assert_axis_display_order_covers_ssot(
    "status", _HANDOFF_STATUS_DISPLAY_ORDER, HANDOFF_TERMINAL_STATUS, exact=True
)
_assert_axis_display_order_covers_ssot(
    "deployment_state", _HANDOFF_DEPLOYMENT_DISPLAY_ORDER, HANDOFF_TERMINAL_DEPLOYMENT, exact=False
)

LIVENESS_MAPPING: dict = {
    "version": CONTRACT_VERSION,
    "spec_backlink": "docs/wiki/canonical-artifact-shapes.md § The Cross-Type Liveness Predicate",
    "implementation_ref": "bin/query-records.js liveness(fm, type)",
    "note": "Rules evaluated in order; first match wins. Unknown values resolve LIVE (open posture).",
    "types": {
        "handoff": {
            "combination_rule": "two-axis",
            "note": "status and deployment_state combine; see axes. DR-084: status new-vocab is claimed (was consumed); superseded is archived-schema-only grandfather, never written. deployment_state new-vocab is continued|closed (was abandoned); read-side stays dual-tolerant on both axes.",
            "axes": {
                "status": _derive_axis_mapping(
                    "handoff", "status", list(_HANDOFF_STATUS_DISPLAY_ORDER)
                ),
                "deployment_state": _derive_axis_mapping(
                    "handoff",
                    "deployment_state",
                    list(_HANDOFF_DEPLOYMENT_DISPLAY_ORDER),
                ),
            },
            "combination_logic": [
                {
                    "condition": (
                        "status ∈ {" + ",".join(_HANDOFF_STATUS_DISPLAY_ORDER) + "} OR "
                        "deployment_state ∈ {" + ",".join(_HANDOFF_DEPLOYMENT_TERMINAL_DISPLAY_ORDER) + "}"
                    ),
                    "result": "DONE",
                },
                {"condition": "deployment_state == awaiting_gate", "result": "BLOCKED"},
                {"condition": "otherwise", "result": "LIVE"},
            ],
        },
        "handoff-archived": {
            "note": "Same schema and combination rule as handoff.",
            "combination_rule": "two-axis",
            "ref": "handoff",
        },
        "cross-repo-memo": {
            "combination_rule": "single-axis",
            "axis": "status",
            "mapping": _derive_status_mapping(
                "cross-repo-memo",
                ["open", "in_progress", "actioned", "reviewed", "action_taken", "closed", "superseded"],
            ),
            "default": "LIVE",
        },
        "plan": {
            "combination_rule": "single-axis",
            "axis": "status",
            "note": "deployment_state is IGNORED for plan (plans have no deployment_state).",
            # Derived like its single-axis siblings below (2026-07-27, superseding the
            # prior literal-dict holdout): example-doctrine-repo coordinator/tests/
            # test_plan_status_enum_parity.py now cross-checks this mapping's KEYS
            # against plan.schema.json's status enum by importing this module and
            # reading LIVENESS_MAPPING directly (value-based), rather than
            # regex-parsing a literal mapping block from this file's source text —
            # so a derived-call shape here no longer breaks that cross-repo parity
            # check the way a bare regex match on a literal dict would have.
            "mapping": _derive_status_mapping(
                "plan",
                ["draft", "reviewed", "approved", "executing", "landed", "implemented", "deferred", "abandoned", "superseded"],
            ),
            "default": "LIVE",
        },
        "decision": {
            "combination_rule": "single-axis",
            "axis": "status",
            "note": "An accepted decision record is terminal — no further lifecycle transitions expected.",
            "mapping": _derive_status_mapping(
                "decision", ["proposed", "accepted", "deprecated", "superseded"]
            ),
            "default": "LIVE",
        },
        "improvement-queue": {
            "combination_rule": "single-axis",
            "axis": "status",
            "query_type": "improvement",
            "mapping": _derive_status_mapping("improvement", ["open", "closed", "deferred"]),
            "default": "LIVE",
        },
        "bug-backlog": {
            "combination_rule": "single-axis",
            "axis": "status",
            "query_type": "bug",
            "mapping": _derive_status_mapping("bug", ["open", "closed", "wontfix", "deferred"]),
            "default": "LIVE",
            "notes": {
                "wontfix": "Terminal — conscious rejection, not deferred work.",
            },
        },
        "debt-backlog": {
            "combination_rule": "single-axis",
            "axis": "status",
            "query_type": "debt",
            "mapping": _derive_status_mapping("debt", ["open", "closed", "deferred"]),
            "default": "LIVE",
        },
        "lesson": {
            "combination_rule": "single-axis",
            "axis": "status",
            "note": "Status is derived at query time by parseLessonsFile from prose conventions; not a stored frontmatter field.",
            "mapping": _derive_status_mapping("lesson", ["resolved", "open"]),
            "default": "LIVE",
        },
        "roadmap": {
            "combination_rule": "single-axis",
            "axis": "status",
            "note": "Forward horizon/portfolio record. blocked resolves LIVE (in-flight), NOT the BLOCKED liveness bucket — that bucket is reserved for handoff awaiting_gate and plan deferred. Spec: example-initiative example-workstream example-repo Ask 1.",
            "mapping": _derive_status_mapping(
                "roadmap", ["planning", "active", "blocked", "shipped", "archived"]
            ),
            "default": "LIVE",
        },
        "tracker": {
            "combination_rule": "single-axis",
            "axis": "status",
            "note": "Current status board / action register; one per repo. Spec: example-initiative example-workstream example-repo Ask 5 promote.",
            "mapping": _derive_status_mapping("tracker", ["active", "archived"]),
            "default": "LIVE",
        },
        "health-status": {
            "combination_rule": "single-axis",
            "axis": "status",
            "note": "Periodic health summary/ledger. Liveness keys on status (lifecycle axis), NOT on health (posture axis — HEALTHY/WATCH/ACTION/CRITICAL). Spec: example-initiative example-workstream example-repo Ask 5 promote.",
            "mapping": _derive_status_mapping("health-status", ["active", "archived"]),
            "default": "LIVE",
        },
        "decision-guide": {
            "combination_rule": "single-axis",
            "axis": "status",
            "note": "Consolidated/distilled terminal shape of a DR corpus (container document; liveness keys on document currency, NOT per-decision lifecycle). Sibling of per-record `decision` — per-file decision remains the escape hatch for individually-tracked/contested decisions. Spec backlink: cross-repo/inbox/2026-06-27-example-stats-repo-decision-records-fleet-share.md § Q2.",
            "mapping": _derive_status_mapping("decision-guide", ["active", "archived"]),
            "default": "LIVE",
        },
    },
}

# ---------------------------------------------------------------------------
# Injected sub-shapes (NOT schemas/*.yaml|*.schema.json artifact types)
# ---------------------------------------------------------------------------
# Reusable cross-cutting shapes that emitted fleet records EMBED (via $ref), as
# opposed to the artifact TYPES generated from schemas/. Coordinator owns the
# canonical shape (ratified tri-plane boundary: contract-lives-in-coordinator
# polarity) and publishes it here for the fleet to vendor; coordinator itself
# writes no files of this shape, so it is NOT a schemas/ entry and is NOT
# counted in schema_count. PascalCase key distinguishes sub-shapes from the
# kebab-case artifact types. Transcribed verbatim from schema.js SUB_SHAPES
# (lines 423-556).
# Spec backlink: cross-repo/inbox/2026-07-03-add-provenance-envelope-to-artifact-contract.md
# ---------------------------------------------------------------------------

SUB_SHAPES: dict = {
    "ProvenanceEnvelope": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ProvenanceEnvelope",
        "description": "Cross-cutting provenance sub-shape embedded by emitted fleet work-state records (emission embeds provenance embeds artifact-shape-conformant record). Owned by coordinator-claude per the ratified tri-plane boundary; claude-klabauter emits conformant instances, example-retrieval-repo/cockpit vendor it. NOT a coordinator-local artifact type (no applies_to; coordinator writes no files of this shape).",
        "type": "object",
        "properties": {
            "source_kind": {
                "enum": ["github_graphql", "github_rest", "git_commit", "local_fs", "coordinator_artifact", "transcript_summary", "sec_edgar", "code_comparison"],
                "description": "How the fact was obtained. github_* and git_commit are git-backed and carry a non-null ref; local_fs, coordinator_artifact, transcript_summary, sec_edgar, and code_comparison are filesystem/artifact/consumer/regulatory/code-comparison-derived and carry no git ref.",
            },
            "repo": {
                "type": "string",
                "description": "Repo slug the fact pertains to.",
            },
            "ref": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "branch": {"type": "string"},
                            "sha": {"type": "string"},
                        },
                        "required": ["branch", "sha"],
                        "additionalProperties": False,
                    },
                    {"type": "null"},
                ],
                "description": "Git ref the fact was observed at. Present-as-null (D9): key always present; non-null for github_* sources, null for local_fs / coordinator_artifact / transcript_summary / sec_edgar / code_comparison. Bidirectional conditional enforces the polarity.",
            },
            "path": {
                "type": "string",
                "description": "Source path (repo-relative file path, or artifact locus) the fact was derived from.",
            },
            "observed_at": {
                "type": "string",
                "format": "date-time",
                "description": "ISO-8601 timestamp when the fact was observed/emitted.",
            },
            "derivation": {
                "enum": ["raw", "parsed", "rolled_up", "computed", "synthesized"],
                "description": "Processing stage of the fact: raw (as-fetched), parsed (structured), rolled_up (aggregated), computed (derived, D2), synthesized (agent-derived from other facts rather than fetched/parsed/aggregated, D42).",
            },
            "entity_anchor": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["kind", "value"],
                        "additionalProperties": False,
                    },
                    {"type": "null"},
                ],
                "description": 'Entity-first join anchor. Present-as-null (mirrors ref): non-null for entity-anchored facts (e.g. {kind: "competitor_uid", value: "..."}), null for repo-anchored facts. kind is an open string (DR-301), not a closed enum. Anchorless guard (allOf clause iii) enforces repo !== "" OR (entity_anchor !== null AND entity_anchor.value !== ""). Well-formedness guard (allOf clause iv) additionally enforces that a present entity_anchor has non-empty kind AND value, unconditional on repo.',
            },
        },
        "required": ["source_kind", "repo", "ref", "path", "observed_at", "derivation", "entity_anchor"],
        "additionalProperties": False,
        "allOf": [
            {
                # (i) github_* and git_commit sources must supply a non-null ref (D9 bidirectional conditional).
                "if": {
                    "properties": {"source_kind": {"enum": ["github_graphql", "github_rest", "git_commit"]}},
                    "required": ["source_kind"],
                },
                "then": {
                    "properties": {"ref": {"not": {"type": "null"}}},
                },
            },
            {
                # (ii) local_fs / coordinator_artifact / transcript_summary / sec_edgar / code_comparison must carry ref:null (D9 bidirectional conditional).
                "if": {
                    "properties": {"source_kind": {"enum": ["local_fs", "coordinator_artifact", "transcript_summary", "sec_edgar", "code_comparison"]}},
                    "required": ["source_kind"],
                },
                "then": {
                    "properties": {"ref": {"type": "null"}},
                },
            },
            {
                # (iii) identity-space-freeze anchorless guard (2026-07-14, model A): repo === ""
                # implies entity_anchor must be a non-null object with a non-empty value.
                "if": {
                    "properties": {"repo": {"const": ""}},
                    "required": ["repo"],
                },
                "then": {
                    "properties": {
                        "entity_anchor": {
                            "type": "object",
                            "properties": {
                                "value": {"not": {"const": ""}},
                            },
                            "required": ["kind", "value"],
                        },
                    },
                    "required": ["entity_anchor"],
                },
            },
            {
                # (iv) well-formedness guard, UNCONDITIONAL on repo (2026-07-14 hardening): a
                # present entity_anchor must have non-empty kind AND value, regardless of repo.
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
        ],
    },
}

# ---------------------------------------------------------------------------
# Cross-schema $ref rewrite (general — not plan-tasks-specific)
# ---------------------------------------------------------------------------
# Every source schema under schemas/*.schema.json declares its own `$id` under the
# `https://coordinator.local/schemas/<name>.schema.json` convention (correct as
# authored — it lets the file resolve standalone, outside this bundle, via an
# `$id`-keyed schema store). A sibling source schema that `$ref`s another one that
# way — e.g. plan.schema.json's `tasks.items.$ref` pointing at
# plan-tasks.schema.json — carries that same absolute URL straight through
# schema_to_json_schema's passthrough (schema_to_json_schema does not rewrite refs;
# it only copies `properties`/`required`/etc. verbatim). That host never resolves at
# validate time, so under a stock Draft202012Validator any record actually populating
# the referencing property (e.g. a plan with a non-empty `tasks:` array) raises
# `Unresolvable` — a whole-run abort, not a one-record failure. Live-dormant rather
# than safe: jsonschema resolves `items` lazily, so `{"tasks": []}` passes and the
# crash only fires once some plan actually populates the key.
#
# Every schema this convention could name is, by construction, already present in
# the bundle's own top-level `$defs` (it is either a registered artifact type, keyed
# by its own file-stem name, or was hoisted/injected above) — so the fix is a pointer
# rewrite, not a fetch: `https://coordinator.local/schemas/<name>.schema.json` ->
# `#/$defs/<name>`, whenever `<name>` is a key in the assembled `defs`. General over
# every `$defs` entry (not plan-tasks-specific) so a second such ref added later is
# caught by the same pass rather than needing its own bespoke fix. Cross-repo memo:
# cross-repo/inbox/2026-08-03-example-doctrine-repo-em-artifact-contract-external-ref-survives-bundling.md
# shell-doc-ok: quotes real JSON-Schema `$defs`/`$ref` pointer syntax, not shell.
_CROSS_SCHEMA_REF_PREFIX = "https://coordinator.local/schemas/"
_CROSS_SCHEMA_REF_SUFFIX = ".schema.json"


def _rewrite_cross_schema_refs(defs: dict) -> str | None:
    """Walk every `$defs` entry and rewrite in place any `$ref` VALUE carrying the
    `https://coordinator.local/schemas/<name>.schema.json` convention to
    `#/$defs/<name>`, when `<name>` is a key in *defs*.

    shell-doc-ok: quotes real JSON-Schema `$defs`/`$ref`/`$id` pointer syntax.

    Only the `$ref` key's value is ever rewritten — `$id` (including the bundle's own
    top-level `$id`, which lives outside `defs` entirely and is never passed here) and
    any string that merely happens to contain the URL inside a `description`/
    `$comment` are left untouched, since the match is keyed on the dict KEY `$ref`,
    not on string content anywhere in the tree.

    Returns None on success (defs mutated in place, key order preserved — no dict is
    rebuilt, only existing `$ref` values are overwritten, so json.dumps insertion
    order is unaffected). Returns a diagnostic message the moment a `$ref` carries the
    coordinator.local/schemas/ prefix but names a schema absent from *defs* — rewrites
    already applied to defs encountered earlier in the walk are harmless leftovers
    (still-correct pointer rewrites) since the caller refuses to emit ANY output on a
    non-None return; refusing to emit an unresolvable ref is this generator's
    established posture (see the empty-SCHEMAS and SUB_SHAPES-collision guards above).
    """

    def _target_name(ref_value: str) -> str:
        name = ref_value[len(_CROSS_SCHEMA_REF_PREFIX):]
        if name.endswith(_CROSS_SCHEMA_REF_SUFFIX):
            name = name[: -len(_CROSS_SCHEMA_REF_SUFFIX)]
        return name

    def _walk(node: Any, enclosing_def_name: str) -> str | None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str) and value.startswith(_CROSS_SCHEMA_REF_PREFIX):
                    name = _target_name(value)
                    if name not in defs:
                        return (
                            f'cross-schema $ref "{value}" in $defs.{enclosing_def_name} '
                            f'names unregistered schema "{name}"'
                        )
                    node[key] = f"#/$defs/{name}"
                else:
                    err = _walk(value, enclosing_def_name)
                    if err is not None:
                        return err
        elif isinstance(node, list):
            for item in node:
                err = _walk(item, enclosing_def_name)
                if err is not None:
                    return err
        return None

    for def_name, def_body in defs.items():
        err = _walk(def_body, def_name)
        if err is not None:
            return err
    return None


# ---------------------------------------------------------------------------
# Main emit
# ---------------------------------------------------------------------------


def _emit(coordinator_root: str) -> int:
    """Load schemas from `<coordinator_root>/schemas`, translate + emit the bundle.

    Returns the business exit code (0 success, 1 empty-schema-set or SUB_SHAPES
    collision) — port of schema.js main() (lines 562-643).
    """
    schemas_dir = os.path.join(coordinator_root, "schemas")
    if not os.path.isdir(schemas_dir):
        print(
            f"emit-artifact-shape-contract: schemas directory not found: {schemas_dir}",
            file=sys.stderr,
        )
        return 2  # dedicated transport/config-failure code — see module docstring

    out_dir = os.environ.get(OUT_DIR_ENV)
    out_dir = os.path.abspath(out_dir) if out_dir else os.path.join(coordinator_root, "artifact-shape-contract")

    schemas = load_schemas(schemas_dir)
    schema_names = sorted(k for k in schemas.keys() if not k.startswith("_"))

    if len(schema_names) == 0:
        print(
            "emit-artifact-shape-contract: SCHEMAS is empty — refusing to emit an empty contract.",
            file=sys.stderr,
        )
        return 1

    defs: dict = {}
    count = 0
    issues: List[str] = []

    # JSON-Schema-backed source schemas (schemas/*.schema.json) may declare their own
    # local `$defs` and `$ref` them internally — e.g. strategic-self-description's
    # repoIdentityTuple/provenance, capability-manifest's capabilityEntry,
    # fleet-capability-index's indexEntry. The emitted bundle is FLAT: every per-type
    # schema under `$defs` is a sibling with no nested `$id` boundary, so a source
    # schema's internal `$ref: "#/$defs/X"` resolves against the BUNDLE root `$defs`,
    # not the source's own `$defs` (which schema_to_json_schema's _isJsonSchema branch
    # does not carry through). Left unfixed, every such ref is dangling. Fix (general,
    # not repoIdentityTuple-specific): hoist EVERY source schema's `$defs` entries into
    # the bundle's top-level `$defs`, keyed by their own name. Port of JS oracle
    # emit-artifact-shape-contract.js:591-645 (contract v1.18.0).
    hoisted_defs: dict = {}

    for name in schema_names:
        schema = schemas[name]
        src_file = f"{name}.yaml"
        try:
            defs[name] = schema_to_json_schema(name, schema, src_file)
            count += 1

            if schema.get("_isJsonSchema") is True and isinstance(schema.get("$defs"), dict):
                for def_name, def_schema in schema["$defs"].items():
                    if def_name in hoisted_defs:
                        if json.dumps(hoisted_defs[def_name], sort_keys=True) != json.dumps(def_schema, sort_keys=True):
                            issues.append(
                                f'$defs hoist collision: "{def_name}" is defined differently by '
                                f'multiple source schemas (already hoisted, then redefined by "{name}")'
                            )
                        # Identical redefinition across source schemas — no-op, already hoisted.
                        continue
                    hoisted_defs[def_name] = def_schema
        except Exception as err:  # noqa: BLE001 — mirrors JS catch(err) { issues.push(err.message) }
            issues.append(f'schema "{name}": {err}')

    # Merge hoisted source-schema $defs into the bundle's top-level $defs. Collision
    # guard: a hoisted def name must not shadow an artifact type name (schema_count
    # entries take priority; a collision means a source schema chose a $defs name that
    # clashes with a registered artifact type — refuse rather than silently shadow).
    for key in hoisted_defs:
        if key in defs:
            print(
                f'emit-artifact-shape-contract: hoisted $defs key "{key}" collides with a '
                "schemas/*.yaml artifact type name — refusing to emit.",
                file=sys.stderr,
            )
            return 1
    defs.update(copy.deepcopy(hoisted_defs))

    if issues:
        print("emit-artifact-shape-contract: schema translation issues:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        # Do not fail — emit with issues noted (per spec: "note rather than silently drop").

    # Inject sub-shapes — NOT counted in schema_count. Fail-loud collision guard: the
    # PascalCase convention is the sole naming guard between artifact-type kebab-case
    # names and injected sub-shape PascalCase names; make the assumption explicit and
    # runtime-checked rather than silently overwriting on a future name collision.
    for key in SUB_SHAPES:
        if key in defs:
            print(
                f'emit-artifact-shape-contract: SUB_SHAPES key "{key}" collides with a schemas/*.yaml schema — refusing to emit.',
                file=sys.stderr,
            )
            return 1
    defs.update(copy.deepcopy(SUB_SHAPES))

    # Rewrite $id-style cross-schema $refs into their bundled #/$defs/<name> location
    # now that the full registered-name set (artifact types + hoisted + SUB_SHAPES)
    # is known. Must run after both merges above — a ref naming a hoisted-only or
    # SUB_SHAPES-only def would false-negative as "unregistered" if run earlier.
    # shell-doc-ok: quotes real JSON-Schema `$id`/`$defs`/`$ref` pointer syntax.
    ref_rewrite_error = _rewrite_cross_schema_refs(defs)
    if ref_rewrite_error is not None:
        print(
            f"emit-artifact-shape-contract: {ref_rewrite_error} — refusing to emit an "
            "unresolvable cross-schema $ref.",
            file=sys.stderr,
        )
        return 1

    # Build the bundle. Key order is load-bearing for byte-parity with the JS oracle's
    # object-literal order (json.dumps preserves dict insertion order, mirroring
    # JSON.stringify's own property-order preservation). `emitted_at` is deliberately
    # NOT included — it created per-run diff churn and broke vendor byte-equality
    # checks; `version` is the pinnable identity for consumers.
    bundle: dict = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://coordinator.local/artifact-shape-contract.schema.json",
        "$comment": 'handoff-archived.status includes "superseded" (retired as a write status 2026-06-26) intentionally — retained for archived/legacy/sibling read-tolerance. RETRACTED 2026-07-22: this comment previously asserted that the cockpit emitter validates archived records against handoff-archived.yaml before coerce-at-ingest, and therefore that narrowing the enum breaks emit. Verified false on both sides (claude-central-em retracted their mirrored copy the same day): the emit path reads via ops/ceremony/records_query, which performs no schema validation, then coerces in Python in ops/emit/sections/handoffs.py. No pre-coerce schema validation exists on the emit path at all, so narrowing this enum does NOT break emit. The read-tolerance rationale stands on its own; the emit-breakage claim did not, and had been read as a load-bearing constraint by at least two EMs. See DECISIONS.md for full rationale and version history.',
        "title": "Coordinator artifact shape contract",
        "description": f"Versioned JSON Schema contract for all coordinator tracked artifact types (tc-4 B1). Single registry-wide version {CONTRACT_VERSION}. Consumers vendor this file for a pinned shape; example-retrieval-repo/cockpit are consumers, not part of the producer surface.",
        "version": CONTRACT_VERSION,
        "schema_count": count,
        # Documents the implicit glob-routing strategy so consumers know
        # "most-specific-glob-wins" is the intended dispatch rule.
        "routing_strategy": "most-specific-glob-wins",
        "$defs": defs,
        "liveness_mapping": LIVENESS_MAPPING,
    }

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    out_file = os.path.join(out_dir, "artifact-shape-contract.schema.json")
    with open(out_file, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(bundle, indent=2, ensure_ascii=False) + "\n")
    # DR-276: declared AFTER the write lands, never before — the contract is a
    # report of what was ACTUALLY written, not of an intended surface.
    declare_write(out_file)

    print(f"emitted {count} schemas → {out_file}")
    print(f"contract version: {CONTRACT_VERSION}")
    if issues:
        print(f"translation issues (noted in stderr): {len(issues)}")

    return 0


def main(argv: List[str]) -> int:
    """CLI entry: resolve the example-doctrine-repo coordinator root from the environment and emit.

    argv is accepted (and currently unused, mirroring the JS oracle's own zero-args
    CLI) for signature parity with every other direct-import trampoline target in this
    migration (`def main(argv) -> int`).
    """
    coordinator_root = os.environ.get(COORDINATOR_ROOT_ENV)
    if not coordinator_root:
        print(
            f"emit-artifact-shape-contract: coordinator root not resolved "
            f"(expected {COORDINATOR_ROOT_ENV} in environment)",
            file=sys.stderr,
        )
        return 2  # dedicated transport/config-failure code — see module docstring
    return _emit(coordinator_root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
