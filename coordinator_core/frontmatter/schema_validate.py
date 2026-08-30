"""
coordinator_core.frontmatter.schema_validate

Schema-parameterized JSON Schema (draft-2020-12 subset) validator plus cross-field
rules engine. Python port of validateFrontmatter + applyCrossFieldRules from
DoE-claude coordinator/bin/lib/schema.js (W4 / JSON-Schema-backed path only).

Spec backlink:
  DoE-claude: coordinator/bin/lib/schema.js — validateRecord, validateJsonSchemaNode,
               CROSS_FIELD_RULES['handoff'], applyCrossFieldRules

Public surface (imported by C3/C4 executors and handoff.transition post-mutation gate):
  validate_frontmatter(fm_dict, schema_path) -> list[ErrorDict]
    Validates a parsed frontmatter dict against the JSON Schema at schema_path.
    Returns a (possibly empty) list of error dicts — empty means valid.
    Raises SchemaVersionError when the record's schema_version major exceeds the
    vendored schema's x-schema-version major, or when the record asserts a
    schema_version that the schema cannot validate (absent x-schema-version).

  check_schema_drift(schema_path, doe_repo_path) -> None
    Reads the vendored schema file and compares it byte-for-byte against the
    corresponding schema at DoE HEAD (git -C doe_repo_path show HEAD:coordinator/schemas/<name>).
    Raises SchemaDriftError on any divergence.

Error dict shape (mirrors JS {field, error, hint}):
  {"field": str, "error": str, "hint": str}

Negative-spec:
  - This module validates pre-parsed dicts, NOT raw YAML text.
    The caller must parse the frontmatter text into a dict before calling validate_frontmatter.
  - Cross-field rules are defined for schema names "handoff" and "cross-repo-memo".
    handoff-archived has no cross-field rules by design. The cross-repo-memo rule set is
    cross-field ONLY — no base-required validation (memos are foreign-authored; a sender's
    base-field slip must never block a legitimate receiver transition).
  - The JSON Schema validator supports: type, enum, required (array form), properties,
    anyOf, allOf, oneOf, format (date/date-time/uri), items (array), additionalProperties
    (both boolean false and schema-valued — schema-valued additionalProperties applies
    the given subschema to every key not declared in `properties`), unevaluatedProperties
    (boolean only — see _evaluated_property_keys's docstring for the exact tractable
    subset: properties/allOf/if-then/additionalProperties at the same schema node, one
    level of allOf/then composition; oneOf/anyOf/patternProperties/schema-valued
    additionalProperties in scope of unevaluatedProperties fail loud rather than
    silently under-validating).
    Cross-file $ref is not supported — fail-loud on encounter.
  - `load_schemas`/`match_schema`/`match_schema_for_path`/`parse_frontmatter` (below)
    DO support the legacy .yaml-dialect schema loader/matcher and the read-side
    frontmatter parser (dual-format: .yaml + .schema.json). The legacy-YAML FIELD
    VALIDATOR itself (schema.js `validateField`/`validateFrontmatter` lines
    1059-1186, plus `checkType`/`suggestNearMiss`/`NEAR_MISS_CANONICAL`) is now
    ported (T4d-g1b) as `_validate_legacy_field`/`_validate_legacy_yaml_frontmatter`,
    dispatched from the public `validate(schema_name, fields)` entry point alongside
    the JSON-Schema-backed path — see "Legacy-YAML field validator (T4d-g1b)" below.
    `applyCrossFieldRulesFor` dispatch-by-name, `validateFrontmatterDispatch`'s
    JSON-Schema-backed branch, `testNegativeCorpus`, and `checkReferentialIntegrity`
    remain out of scope — not needed by `validate()`'s callers.

Legacy-YAML data-layer port (T4d-g1a, DoE-claude
  scratch/subagent-sandbox/bash-to-python-engine-migration/recipe-T4d-g1-js-data-layer-cluster.md):
  Spec backlink: DoE-claude coordinator/bin/lib/schema.js — parseYaml (+ consumeBlockScalar,
    parseYamlLines, parseList, skipPast, parseInlineList, stripInlineComment, parseScalar),
    globToRegex, matchGlob, applyDefaultMatchMode, loadSchemas, matchSchema,
    matchSchemaForPath, parseFrontmatter.
  Public surface added:
    load_schemas(schemas_dir) -> dict          # {name: parsed, "_byGlob": [...], "_byKind": {...}}
    match_schema(repo_rel_path, frontmatter, schemas) -> dict | None
    match_schema_for_path(repo_rel_path, schemas) -> dict | None
    parse_frontmatter(content) -> dict          # {"frontmatter": dict | None, "body": str}
  Negative-spec (ported verbatim from schema.js, not reinterpreted):
    - parse_yaml is a restricted YAML subset (scalar key: value, list items, one level
      of nested mapping, block scalars `|`/`>`) — no anchors, no flow mappings beyond
      inline `[a, b]` lists, no multi-document streams.
    - globToRegex bracket-class passthrough scans for the FIRST `]` after `[` — a class
      with an embedded literal `]` as its first char (`[]]`) mis-terminates; unsupported
      by design (schema.js code-review F7 note, reproduced here).
    - Wildcards (`*`, `**`, `?`) at a path-segment start do not match a leading dot.
    - parse_frontmatter returns {"frontmatter": None, "body": content} (no-frontmatter)
      when the parsed YAML block is empty ({}) — an empty object is never a valid record.
"""
from __future__ import annotations

import datetime
import functools
import hashlib
import inspect
import json
import logging
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence, TypedDict

import yaml

from coordinator_core.frontmatter.baton_class import (
    _PRE_RENAME_ALIASES as _HANDOFF_KIND_PRE_RENAME_ALIASES,
)
from coordinator_core.frontmatter.baton_class import canonical_kind as _canonical_kind
from coordinator_core.frontmatter.baton_class import kind_values_for_canonical
from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.frontmatter.schema_shape import semantic_shape_hash
from coordinator_core.git_scope import (
    FOREIGN_REPO_GIT_TIMEOUT_SECONDS,
    foreign_repo_unusable_reason,
    scoped_cat_file_batch,
    scoped_git_env,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public error types
# ---------------------------------------------------------------------------

# Review: code-reviewer — F7: TypedDict enforces {field, error, hint} shape at static-analysis time
class ErrorDict(TypedDict):
    field: str
    error: str
    hint: str


def format_validation_errors(errors: list[ErrorDict]) -> str:
    """Render a list of ErrorDict into a single '; '-joined user-facing string.

    Single shared formatter for the many ops call sites that surface
    `validate_frontmatter`/cross-field-rule failures to a caller (exit-code
    stderr, MutateAbort messages, etc.) — replaces 13 duplicated
    `"; ".join(f"{e.get('field','?')}: {e.get('error','?')}" for e in errors)`
    call sites across coordinator_core/ops that dropped `hint` entirely, so an
    out-of-enum value's allowed-values list never reached the user even though
    the validator had already computed it.

    Each error renders as ``field: error (hint)`` when hint is a non-empty
    string, or ``field: error`` when hint is absent/empty/None — no dangling
    separator or literal "None"/"?" placeholder for a clean hint.

    Spec backlink: cross-repo/inbox/2026-07-23-example-cockpit-repo-em-coordinator-doc-new-category-no-validation.md
    """
    parts = []
    for e in errors:
        field = e.get('field') or '?'
        error = e.get('error') or '?'
        hint = e.get('hint')
        if hint:
            parts.append(f"{field}: {error} ({hint})")
        else:
            parts.append(f"{field}: {error}")
    return "; ".join(parts)


class SchemaVersionError(ValueError):
    """Raised when a record's schema_version is incompatible with the vendored schema.

    Consumer-side fail-loud:
      - record declares schema_version AND schema lacks x-schema-version → cannot compare.
      - record's schema_version major > schema's x-schema-version major → consumer is behind.
    """


class SchemaDriftError(RuntimeError):
    """Raised when a vendored schema file diverges from DoE HEAD at drift-check time."""


# ---------------------------------------------------------------------------
# Vendored per-row schema paths (consumed by C3's plan.tasks.mutate op)
# ---------------------------------------------------------------------------

_PLAN_TASKS_SCHEMA: Path = (
    Path(__file__).parent / "schemas" / "plan-tasks.schema.json"
)

with _PLAN_TASKS_SCHEMA.open("r", encoding="utf-8") as _plan_tasks_schema_f:
    _PLAN_TASKS_SCHEMA_DICT: dict = json.load(_plan_tasks_schema_f)


def _plan_tasks_schema_without_pm_approved_required(schema: dict | None = None) -> dict:
    """`schema` (default `_PLAN_TASKS_SCHEMA_DICT`, claude-klabauter's own vendored copy)
    minus the allOf branches that make `pm_approved` a REQUIRED key.

    Used for rows on a GOVERNED plan, where authorization is carried by the
    plan's `grouping_approvals` blocks and the per-row boolean is absent by
    design — leaving those branches live would reject every closed row on
    every governed plan at the schema layer, before any grouping check ran.

    This drops only the required-KEY branches. It does not weaken the legacy
    corpus (governed is False there, so the unmodified schema applies) and it
    does not weaken governed plans either: the hard authorization check moved
    UP to the grouping predicate, which is strictly stronger than a
    presence-only key check — both of the dropped branches document
    themselves as "presence-only / NON-hard-failing at this schema layer"
    precisely because the real enforcement was always meant to live behind
    the cross-field registration.

    Derived rather than hand-maintained so a future edit to the vendored
    schema's pm_approved branches cannot silently desync a copy here.

    Lives HERE (frontmatter layer), not in `ops/plan_tasks_mutate.py` where
    it originated — `write_guards/` must not import from `ops/` (that
    inverts the claude-klabauter layering), and the mutate op and both write guards
    (deny + advisory) all need the identical filtering LOGIC. One home in
    the layer both sides can already import from, per the 2026-07-29
    write-guard-bypass fix (see `check_plan_tasks_source`'s docstring).
    `ops/plan_tasks_mutate.py` imports this back rather than keeping its own
    copy. Note the word "logic," not "door": the mutate op and both write
    guards share this primitive directly, they do NOT route through
    `check_plan_tasks_source` — see that function's docstring for the
    residual gap this leaves (the ordering-then-grouping-then-per-row
    sequence is still hand-duplicated across the guards and this door).

    Takes an optional `schema` argument (rather than always operating on
    `_PLAN_TASKS_SCHEMA_DICT`) because the two write guards resolve their
    OWN `plan-tasks` schema object from DoE's vendored corpus
    (`coordinator/schemas/plan-tasks.schema.json`, a copy that is known to
    have drifted from claude-klabauter's own — see the "config-edit re-vendor gap"
    noted in cross-repo/inbox/2026-07-29-doe-claude-em-grouping-
    discriminator-correction.md — and is tracked separately by
    `check_schema_drift`). The `allOf`-branch shape this function strips is
    identical in both copies, so the SAME transform applies regardless of
    which physical schema object a caller holds; a write guard passes its
    own resolved schema in rather than silently substituting claude-klabauter's
    vendored copy (which would also change unrelated behaviour, e.g. the
    `change_kind` enum's `config-edit` value).
    """
    base = schema if schema is not None else _PLAN_TASKS_SCHEMA_DICT
    out = json.loads(json.dumps(base))
    out["allOf"] = [
        branch
        for branch in out.get("allOf", [])
        if "pm_approved" not in (branch.get("then", {}) or {}).get("required", [])
    ]
    return out


_PLAN_TASKS_SCHEMA_GOVERNED_DICT = _plan_tasks_schema_without_pm_approved_required()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _coerce_dates_to_strings(obj: Any) -> Any:
    """Coerce datetime.date and datetime.datetime values to ISO-8601 strings.

    PyYAML safe_load parses YAML bare dates (e.g. 2026-07-05) as datetime.date
    objects. The JSON Schema validator and cross-field rules expect string values
    for date fields (as they appear on disk), so we coerce at the dict level
    before validation. This is a one-way normalisation — does not affect the
    caller's original dict.

    Negative-spec: does not handle YAML anchors or custom tag objects.
    """
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    if isinstance(obj, datetime.date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _coerce_dates_to_strings(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_dates_to_strings(v) for v in obj]
    return obj


def _parse_semver(v: str) -> tuple[int, int, int] | None:
    """Parse 'MAJOR[.MINOR[.PATCH]]' into (major, minor, patch) or None on failure.

    Hand-rolled — no external semver package. Only MAJOR is used for version gating;
    MINOR and PATCH are parsed for correctness.
    """
    m = re.fullmatch(r'(\d+)(?:\.(\d+))?(?:\.(\d+))?', v.strip())
    if not m:
        return None
    return (
        int(m.group(1)),
        int(m.group(2)) if m.group(2) is not None else 0,
        int(m.group(3)) if m.group(3) is not None else 0,
    )


def _resolve_json_ref(ref: str, root_schema: dict) -> dict:
    """Resolve an in-file JSON Schema $ref.

    Only '#/$defs/Name' references are supported — cross-file refs are banned in
    the dependency-free validator context (no network/filesystem fetch permitted).
    Raises ValueError on unsupported or unresolvable refs.
    """
    if not isinstance(ref, str) or not ref.startswith('#/$defs/'):
        raise ValueError(
            f'JSON Schema $ref "{ref}" is not supported — only in-file '
            '#/$defs/... references are allowed. Cross-file $ref would require '
            'a fetch, which is banned in the dependency-free validator context.'
        )
    def_name = ref[len('#/$defs/'):]
    defs = root_schema.get('$defs')
    if not defs or def_name not in defs:
        raise ValueError(
            f'JSON Schema $ref "{ref}" not found — no $defs.{def_name} in schema.'
        )
    return defs[def_name]


def _json_type_ok(value: Any, type_spec: str | list) -> bool:
    """Return True if value satisfies the JSON Schema type specifier."""
    types = type_spec if isinstance(type_spec, list) else [type_spec]
    for t in types:
        if t == 'null' and value is None:
            return True
        if t == 'string' and isinstance(value, str):
            return True
        if t == 'number' and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if t == 'integer' and isinstance(value, int) and not isinstance(value, bool):
            return True
        if t == 'boolean' and isinstance(value, bool):
            return True
        if t == 'object' and isinstance(value, dict):
            return True
        if t == 'array' and isinstance(value, list):
            return True
    return False


def _evaluated_property_keys(value: dict, schema: dict, root_schema: dict) -> set[str]:
    """Compute the TRACTABLE SUBSET of "evaluated" keys for unevaluatedProperties: false.

    Full JSON Schema unevaluatedProperties semantics require tracking, per
    applicator (allOf/oneOf/anyOf/if-then-else/$ref/patternProperties/
    additionalProperties), which properties each *successfully-matching*
    branch annotated as evaluated — the single hardest keyword in the spec,
    and one this dependency-free validator does not implement generically
    (no annotation-collection machinery exists here at all).

    Supported at THIS schema node only:
      - properties (declared keys)
      - allOf — one level deep only: each direct allOf branch's own
        properties/additionalProperties, resolving a bare $ref one hop
      - if/then — when `if` matches the value, if's + then's properties
      - additionalProperties: true — evaluates every remaining key

    Fails loud (raises ValueError, matching `_resolve_json_ref`'s established
    idiom for unsupported schema shapes) rather than silently under-counting
    evaluated keys when it encounters:
      - patternProperties anywhere in scope (not implemented by this
        validator at all — cannot determine which keys it would cover)
      - oneOf/anyOf at THIS node (per-branch annotation tracking needed)
      - a schema-valued (non-boolean) additionalProperties
      - a second level of allOf/oneOf/anyOf/patternProperties/
        unevaluatedProperties nested inside an allOf branch or a then-branch

    This is deliberately narrower than the spec so that every case this
    function DOES accept is genuinely correct — silently passing an
    under-evaluated unevaluatedProperties would reproduce, inside its own
    fix, exactly the silent-under-validation defect this keyword gap is.
    """
    if 'patternProperties' in schema:
        raise ValueError(
            'unevaluatedProperties is not supported alongside patternProperties — '
            'this validator does not implement patternProperties at all, so it cannot '
            'determine which keys patternProperties would evaluate.'
        )
    if 'oneOf' in schema or 'anyOf' in schema:
        raise ValueError(
            'unevaluatedProperties is not supported at the same schema node as '
            'oneOf/anyOf — correctly computing which properties each candidate branch '
            'evaluates requires per-branch annotation tracking this dependency-free '
            'validator does not implement. Move unevaluatedProperties to a node without '
            'oneOf/anyOf, or restructure the schema.'
        )

    def _branch_properties(branch: Any) -> set[str]:
        resolved = branch
        if isinstance(resolved, dict) and '$ref' in resolved:
            resolved = _resolve_json_ref(resolved['$ref'], root_schema)
        if not isinstance(resolved, dict):
            return set()
        if (
            'oneOf' in resolved or 'anyOf' in resolved or 'allOf' in resolved
            or 'patternProperties' in resolved or 'unevaluatedProperties' in resolved
        ):
            raise ValueError(
                'unevaluatedProperties is not supported when an allOf/then branch itself '
                'nests allOf/oneOf/anyOf/patternProperties/unevaluatedProperties — only one '
                'level of composition is walked by this validator.'
            )
        keys = set(resolved.get('properties') or {})
        branch_additional = resolved.get('additionalProperties')
        if isinstance(branch_additional, dict):
            raise ValueError(
                'unevaluatedProperties is not supported alongside a schema-valued '
                'additionalProperties on an allOf/then branch — computing which keys '
                'it evaluates for unevaluatedProperties purposes requires per-key '
                'match tracking this validator does not implement, even though '
                'schema-valued additionalProperties is itself supported for plain '
                'validation.'
            )
        if branch_additional is True:
            keys |= set(value)
        return keys

    evaluated: set[str] = set(schema.get('properties') or {})

    additional = schema.get('additionalProperties')
    if isinstance(additional, dict):
        raise ValueError(
            'unevaluatedProperties is not supported alongside a schema-valued '
            'additionalProperties at the same node — computing which keys it '
            'evaluates for unevaluatedProperties purposes requires per-key match '
            'tracking this validator does not implement, even though schema-valued '
            'additionalProperties is itself supported for plain validation.'
        )
    if additional is True:
        evaluated |= set(value)

    for sub in schema.get('allOf') or []:
        evaluated |= _branch_properties(sub)

    if 'if' in schema and 'then' in schema:
        if_schema = schema['if']
        if_errors = _validate_json_schema_node(value, if_schema, root_schema, '')
        if len(if_errors) == 0:
            if isinstance(if_schema, dict):
                evaluated |= set(if_schema.get('properties') or {})
            evaluated |= _branch_properties(schema['then'])

    return evaluated


def _validate_json_schema_node(
    value: Any,
    schema: dict | None,
    root_schema: dict,
    path: str = '',
) -> list[ErrorDict]:
    """Recursively validate a value against a JSON Schema node (draft-2020-12 subset).

    Port of validateJsonSchemaNode from DoE-claude coordinator/bin/lib/schema.js.

    Supported keywords: $ref, anyOf, allOf, oneOf, type, enum, format
    (date/date-time), pattern, minLength, maxLength, minItems, uniqueItems, minimum, maximum,
    propertyNames, required (array form), properties, additionalProperties (both boolean
    false and schema-valued — a schema-valued additionalProperties recurses
    into every key not declared in this node's `properties`, same as
    `properties`/`propertyNames` do), items,
    if/then (object-level conditional; claude-klabauter-local addition —
    see handoff.schema.json's deployment_state==ready_to_fire ->
    gate_dependency-forbidden rule, added for C1's schema-hardening
    sub-step). const (used only inside an if-branch's properties, e.g.
    {"const": "ready_to_fire"}) and not (used only inside a then-branch to
    negate a nested schema, e.g. {"not": {"required": [...]}}) are supported
    as the minimal companions if/then needs — general-purpose const/not
    outside that combination is out of scope for this dependency-free subset.

    pattern is applied only to `type: string` values (a non-string value is
    not a pattern violation per spec — it is simply ignored, matching how
    `format` behaves above). Matching is an UNANCHORED partial match
    (`re.search`), exactly as JSON Schema specifies — `pattern` itself has no
    implicit anchoring; every pattern currently shipped in this repo's
    schemas that needs full-string anchoring supplies its own explicit
    `^`/`$`, so `re.search` reproduces each schema author's actual intent
    rather than silently changing it. The one exception on file
    (`gate_evidence.legs[].ref` for `kind: test-node-id`, pattern `::`) is
    deliberately unanchored — it asserts a pytest node id contains a literal
    `::` separator anywhere, not a full-string shape. An uncompilable pattern
    fails loud (raises ValueError, matching `_resolve_json_ref`'s established
    idiom) rather than silently skipping the check.

    unevaluatedProperties is supported ONLY as a boolean at the same schema
    node as `properties`/`allOf`/`if`-`then`/`additionalProperties` — see
    `_evaluated_property_keys` for the exact tractable subset and its
    fail-loud boundary (raises ValueError, matching `_resolve_json_ref`'s
    established fail-loud idiom for unsupported schema shapes, rather than
    silently passing a schema this validator cannot actually check).

    minLength (string values only), maxLength (string values only),
    minItems, uniqueItems and contains (array values only), and
    minimum / maximum (numeric values only, `bool` excluded — JSON Schema's
    `number` type does not include booleans even though Python's `bool`
    subclasses `int`) are size/magnitude bound checks, each ignored for a value of
    the wrong type exactly like `pattern`/`format` above (a type mismatch is
    caught by `type`, not by these). propertyNames validates every KEY of an
    object value against a nested subschema (keys are always strings, so
    this typically composes with `pattern`/`minLength`/`not` on the key
    text) — see handoff.schema.json's `gate_evidence.legs[]` object, which
    uses `propertyNames: {"not": {"enum": [...]}}` to block authors from
    writing the four resolver-only keys.

    Returns a list of error dicts {field, error, hint}. Empty list = valid.
    """
    if not schema or not isinstance(schema, dict):
        return []
    field = path or '(root)'
    errors: list[ErrorDict] = []

    # $ref — resolve before all other keywords.
    if '$ref' in schema:
        resolved = _resolve_json_ref(schema['$ref'], root_schema)
        return _validate_json_schema_node(value, resolved, root_schema, path)

    # anyOf — value must match at least one sub-schema.
    if 'anyOf' in schema:
        any_of = schema['anyOf']
        if isinstance(any_of, list):
            branch_errors = [
                _validate_json_schema_node(value, sub, root_schema, path)
                for sub in any_of
            ]
            matches_any = any(len(errs) == 0 for errs in branch_errors)
            if not matches_any:
                # Discriminator (message-text-only fix — see this repo's
                # CLAUDE.md note on this file's leniency-is-contract status;
                # no accept/reject behavior changes here, `matches_any` above
                # already decided pass/fail): a branch whose OWN `type`
                # keyword the value already satisfies, but that still
                # produced errors, failed on a narrower constraint (pattern,
                # enum, minLength, ...) — the "expected string or null (got
                # str)" wording below reads as a validator bug for that case,
                # because the value's type was never in question. Surface
                # that branch's own error instead of the generic
                # type-mismatch message.
                type_matched_errors = [
                    errs for sub, errs in zip(any_of, branch_errors)
                    if 'type' not in sub or _json_type_ok(value, sub['type'])
                ]
                if type_matched_errors:
                    closest = type_matched_errors[0][0]
                    errors.append({
                        'field': field,
                        'error': closest['error'],
                        'hint': closest.get('hint', ''),
                    })
                    return errors
                type_hints = ' or '.join(
                    s.get('type') or (str(s.get('enum')) if 'enum' in s else str(s))
                    for s in any_of
                )
                actual = 'null' if value is None else ('array' if isinstance(value, list) else type(value).__name__)
                errors.append({
                    'field': field,
                    'error': f'value does not match any allowed schema; expected {type_hints} (got {actual})',
                    'hint': f'Expected one of: {type_hints}',
                })
                return errors
            # anyOf matched — fall through to check sibling keywords.

    # allOf — value must match every sub-schema. Conjunction, not
    # short-circuiting: errors from every failing sub-schema are collected
    # (mirrors the `required`/`properties` accumulate-don't-bail idiom
    # below) so a schema author sees every allOf branch that rejected the
    # value in one pass, not just the first.
    if 'allOf' in schema:
        all_of = schema['allOf']
        if isinstance(all_of, list):
            for sub in all_of:
                errors.extend(_validate_json_schema_node(value, sub, root_schema, path))

    # oneOf — value must match EXACTLY one sub-schema. Unlike anyOf, the
    # failure mode is two-shaped (zero matches vs. multiple matches) and the
    # error must say which, per the schema-validator-keyword-gap ask — a bare
    # "oneOf failed" gives a schema author no way to tell overlapping
    # branches from no matching branch at all.
    if 'oneOf' in schema:
        one_of = schema['oneOf']
        if isinstance(one_of, list):
            matched_indices = [
                i for i, sub in enumerate(one_of)
                if len(_validate_json_schema_node(value, sub, root_schema, path)) == 0
            ]
            if len(matched_indices) != 1:
                type_hints = ' or '.join(
                    s.get('type') or (str(s.get('enum')) if 'enum' in s else str(s))
                    for s in one_of
                )
                actual = 'null' if value is None else ('array' if isinstance(value, list) else type(value).__name__)
                if not matched_indices:
                    errors.append({
                        'field': field,
                        'error': (
                            f'value does not match any allowed schema (oneOf requires exactly '
                            f'one match); expected {type_hints} (got {actual})'
                        ),
                        'hint': f'Expected exactly one of: {type_hints}',
                    })
                else:
                    errors.append({
                        'field': field,
                        'error': (
                            f'value matches {len(matched_indices)} schemas (indices '
                            f'{matched_indices}) but oneOf requires exactly one match'
                        ),
                        'hint': (
                            'Narrow the oneOf branches so they are mutually exclusive for this '
                            'value — overlapping branches both accepted it.'
                        ),
                    })
                return errors
            # oneOf matched exactly one — fall through to check sibling keywords.

    # const — value must equal the given literal exactly (used inside if-branch
    # properties, e.g. {"const": "ready_to_fire"}).
    if 'const' in schema:
        if value != schema['const']:
            errors.append({
                'field': field,
                'error': f'value does not match const "{schema["const"]}"',
                'hint': f'Expected exactly: {schema["const"]!r}',
            })
            return errors

    # not — value must NOT match the nested sub-schema (used inside a then-branch
    # to negate a nested schema, e.g. {"not": {"required": ["gate_dependency"]}}).
    if 'not' in schema:
        sub_errors = _validate_json_schema_node(value, schema['not'], root_schema, path)
        if len(sub_errors) == 0:
            errors.append({
                'field': field,
                'error': 'value must NOT match the negated schema',
                'hint': 'Remove or change the field(s) that satisfy the forbidden condition',
            })
            return errors

    # type — short-circuit on mismatch.
    if 'type' in schema:
        if not _json_type_ok(value, schema['type']):
            types = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
            actual = 'null' if value is None else ('array' if isinstance(value, list) else type(value).__name__)
            expected = ' or '.join(types)
            errors.append({
                'field': field,
                'error': f'expected {expected}, got {actual}',
                'hint': f'Provide a value of type: {expected}',
            })
            return errors

    # enum — value must be one of the listed values (strict equality). This
    # walker has no schema-identity or field-path context of its own (it
    # validates `gate_source.kind`, `verified_by.kind`, and
    # `gate_evidence.legs[].kind` the same way it validates the handoff-family
    # `kind` field), so it stays strict here by design. The handoff `kind`
    # field's D1 pre-rename alias tolerance is applied by the caller —
    # `_tolerate_handoff_kind_aliases` below — as a scoped post-process over
    # this function's returned errors, not as a change to this generic check.
    if 'enum' in schema:
        allowed = schema['enum']
        if isinstance(allowed, list) and value not in allowed:
            errors.append({
                'field': field,
                'error': f'invalid enum value "{value}"',
                'hint': f'Allowed values: {", ".join(str(v) for v in allowed)}.{_suggest_near_miss(value, allowed)}',
            })
            return errors

    # format — applied to string values only.
    if 'format' in schema and isinstance(value, str):
        fmt = schema['format']
        if fmt == 'date' and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            errors.append({
                'field': field,
                'error': f'invalid date format "{value}"',
                'hint': 'Use format YYYY-MM-DD',
            })
        elif fmt == 'date-time' and not re.match(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', value):
            errors.append({
                'field': field,
                'error': f'invalid date-time format "{value}"',
                'hint': 'Use ISO 8601 date-time format (YYYY-MM-DDTHH:MM:SSZ)',
            })
        # Review: code-reviewer — `format: uri` was silently accepted as a
        # no-op keyword (only date/date-time were implemented), the same
        # silent-non-enforcement class as `maximum` before it. RFC 3986
        # generic-syntax scheme check: an absolute URI must open with
        # `scheme:`, scheme = ALPHA *( ALPHA / DIGIT / "+" / "-" / "." ).
        # Deliberately conservative (no path/authority validation) — the
        # vendored schemas using this (strategic-self-description.schema.json
        # hero_asset / call_to_action.payload.uri) both expect an absolute
        # URL, and a scheme-presence check is the well-defined subset;
        # anything past that (percent-encoding, authority shape) is a
        # judgment call left unimplemented.
        elif fmt == 'uri' and not re.match(r'[A-Za-z][A-Za-z0-9+.-]*:', value):
            errors.append({
                'field': field,
                'error': f'invalid uri format "{value}"',
                'hint': 'Use an absolute URI with a scheme, e.g. https://example.com/path',
            })

    # pattern — applied to string values only (see docstring). Unanchored
    # partial match (re.search), per spec; every anchoring in this repo's
    # schemas is explicit in the pattern text itself. An uncompilable
    # pattern fails loud rather than silently passing every value.
    if 'pattern' in schema and isinstance(value, str):
        raw_pattern = schema['pattern']
        try:
            compiled = re.compile(raw_pattern)
        except re.error as exc:
            raise ValueError(
                f'schema pattern {raw_pattern!r} at {field} does not compile: {exc}'
            ) from exc
        if not compiled.search(value):
            errors.append({
                'field': field,
                'error': f'value "{value}" does not match pattern {raw_pattern}',
                'hint': f'Value must match pattern: {raw_pattern}',
            })

    # minLength — applied to string values only (see docstring); a non-string
    # value is not a violation, matching pattern/format above.
    if 'minLength' in schema and isinstance(value, str):
        min_length = schema['minLength']
        if len(value) < min_length:
            errors.append({
                'field': field,
                'error': f'value length {len(value)} is less than minLength {min_length}',
                'hint': f'Value must be at least {min_length} character(s) long',
            })

    # maxLength — applied to string values only (see docstring); a non-string
    # value is not a violation, matching pattern/format/minLength above.
    if 'maxLength' in schema and isinstance(value, str):
        max_length = schema['maxLength']
        if len(value) > max_length:
            errors.append({
                'field': field,
                'error': f'value length {len(value)} is greater than maxLength {max_length}',
                'hint': f'Value must be at most {max_length} character(s) long',
            })

    # minimum — applied to numeric values only; bool is deliberately excluded
    # even though it subclasses int in Python (see docstring).
    if 'minimum' in schema and isinstance(value, (int, float)) and not isinstance(value, bool):
        min_value = schema['minimum']
        if value < min_value:
            errors.append({
                'field': field,
                'error': f'value {value} is less than minimum {min_value}',
                'hint': f'Value must be >= {min_value}',
            })

    # maximum — exact mirror of `minimum` above, same bool exclusion for the
    # same reason. Added when `research-synthesis.schema.json` was vendored:
    # it bounds `coverage_score` with `maximum`, which this validator had
    # ignored, so the constraint would have shipped as a silent no-op — the
    # same failure class as `pattern` shipping unenforced as a fleet-shared
    # MAJOR. Any further numeric keyword (exclusiveMinimum/exclusiveMaximum/
    # multipleOf) is still unimplemented and must be added here AND to
    # test_schema_keyword_coverage's `_SUPPORTED_KEYWORDS` before a schema
    # using it is vendored.
    if 'maximum' in schema and isinstance(value, (int, float)) and not isinstance(value, bool):
        max_value = schema['maximum']
        if value > max_value:
            errors.append({
                'field': field,
                'error': f'value {value} is greater than maximum {max_value}',
                'hint': f'Value must be <= {max_value}',
            })

    # Object-level keywords.
    if isinstance(value, dict):
        # required (JSON Schema array form) — key must be present; null value is allowed.
        if isinstance(schema.get('required'), list):
            for req in schema['required']:
                if req not in value:
                    req_path = f'{path}.{req}' if path else req
                    errors.append({
                        'field': req_path,
                        'error': 'required field missing',
                        'hint': f'Add "{req}:" to the record',
                    })

        # dependentRequired — {"a": ["b", "c"]}: if key `a` is present, `b` and
        # `c` must be too. Presence-conditioned, never value-conditioned, so a
        # null `a` still triggers it (JSON Schema draft 2019-09 semantics), and
        # an absent `a` imposes nothing.
        #
        # Added when plan.schema.json 2.9.0 was vendored: it makes
        # `prime_exit_criterion.falsifier.promotion_reason` depend on
        # `promotion`. Vendoring without this would have shipped that
        # constraint as a silent no-op -- the `pattern`-as-unenforced-MAJOR
        # failure class test_schema_keyword_coverage exists to catch, and did.
        #
        # Widens what this validator REJECTS, so it is deliberately the
        # narrowest reading: a record is newly invalid only if it carries the
        # dependent key and omits one of its requirements. Nothing in the
        # corpus at 2.8.0 carries `promotion_reason` at all, so no live record
        # changes verdict (`_coerce_dates_to_strings`' leniency contract is
        # untouched -- this adds a key-presence rule, never a type coercion).
        deps = schema.get('dependentRequired')
        if isinstance(deps, dict):
            for dep_key, dep_reqs in deps.items():
                if dep_key not in value or not isinstance(dep_reqs, list):
                    continue
                for req in dep_reqs:
                    if req not in value:
                        req_path = f'{path}.{req}' if path else req
                        errors.append({
                            'field': req_path,
                            'error': f'required when "{dep_key}" is present',
                            'hint': f'Add "{req}:" alongside "{dep_key}", or remove "{dep_key}"',
                        })

        # properties — validate each declared property that is present.
        # Review: code-reviewer — F4: removed `is not None` guard so null values are validated
        # by _validate_json_schema_node (which correctly fails type/enum checks on None).
        props = schema.get('properties')
        if isinstance(props, dict):
            for prop_name, prop_schema in props.items():
                if prop_name in value:
                    prop_path = f'{path}.{prop_name}' if path else prop_name
                    errors.extend(
                        _validate_json_schema_node(value[prop_name], prop_schema, root_schema, prop_path)
                    )

        # if/then — object-level conditional. When the value matches the `if`
        # sub-schema, the `then` sub-schema is also applied (errors merged in).
        # No `else` branch is supported — not needed by any current schema.
        if 'if' in schema and 'then' in schema:
            if_errors = _validate_json_schema_node(value, schema['if'], root_schema, path)
            if len(if_errors) == 0:
                errors.extend(
                    _validate_json_schema_node(value, schema['then'], root_schema, path)
                )

        # propertyNames — validate every KEY of the object against a nested
        # subschema (keys are always strings, so this typically composes
        # with pattern/minLength/not on the key text; see
        # handoff.schema.json's gate_evidence.legs[] object).
        if 'propertyNames' in schema:
            prop_names_schema = schema['propertyNames']
            for key in value:
                key_path = f'{path}.{key}' if path else key
                errors.extend(
                    _validate_json_schema_node(key, prop_names_schema, root_schema, key_path)
                )

        # additionalProperties: false — reject undeclared keys.
        additional_props = schema.get('additionalProperties')
        if additional_props is False:
            declared = set((schema.get('properties') or {}).keys())
            for key in value:
                if key not in declared:
                    key_path = f'{path}.{key}' if path else key
                    errors.append({
                        'field': key_path,
                        'error': f'additional property "{key}" not allowed',
                        'hint': 'Remove this property or add it to the schema properties',
                    })
        # additionalProperties: <schema> — validate every undeclared key's
        # value against the given subschema (standard JSON Schema semantics;
        # declared keys are validated by `properties` above, not here).
        elif isinstance(additional_props, dict):
            declared = set((schema.get('properties') or {}).keys())
            for key in value:
                if key not in declared:
                    key_path = f'{path}.{key}' if path else key
                    errors.extend(
                        _validate_json_schema_node(value[key], additional_props, root_schema, key_path)
                    )

        # unevaluatedProperties — see _evaluated_property_keys for the exact
        # tractable subset (properties/allOf/if-then/additionalProperties at
        # this node, one level of allOf/then composition) and its fail-loud
        # boundary. `true` is a no-op (nothing is left "unevaluated" by
        # definition); `false` rejects any key outside the computed
        # evaluated set; any other value (a schema object) is unsupported
        # and fails loud — silently passing it would reproduce the
        # silent-under-validation defect this keyword gap itself is.
        if 'unevaluatedProperties' in schema:
            unevaluated_spec = schema['unevaluatedProperties']
            if unevaluated_spec is False:
                evaluated_keys = _evaluated_property_keys(value, schema, root_schema)
                for key in value:
                    if key not in evaluated_keys:
                        key_path = f'{path}.{key}' if path else key
                        errors.append({
                            'field': key_path,
                            'error': f'unevaluated property "{key}" not allowed',
                            'hint': (
                                'Remove this property, or add it to properties (or an allOf/then '
                                'branch\'s properties) so it counts as evaluated.'
                            ),
                        })
            elif unevaluated_spec is not True:
                raise ValueError(
                    f'unevaluatedProperties value {unevaluated_spec!r} is not supported — '
                    'only boolean true/false are implemented by this dependency-free validator.'
                )

    # Array-level keywords.
    if isinstance(value, list):
        # minItems — array values only.
        if 'minItems' in schema:
            min_items = schema['minItems']
            if len(value) < min_items:
                errors.append({
                    'field': field,
                    'error': f'array length {len(value)} is less than minItems {min_items}',
                    'hint': f'Provide at least {min_items} item(s)',
                })

        # uniqueItems — array values only. Implemented rather than tolerated:
        # an unimplemented keyword in a shipped schema validates as a silent
        # no-op, which is the defect class test_schema_keyword_coverage
        # exists to catch (the same one that shipped `pattern` as a
        # fleet-shared MAJOR while unenforced). Items are compared by their
        # canonical JSON form, so unhashable dict/list members compare by
        # value rather than raising.
        if schema.get('uniqueItems') is True:
            _seen: list[str] = []
            for item in value:
                try:
                    _key = json.dumps(item, sort_keys=True, default=str)
                except (TypeError, ValueError):
                    _key = repr(item)
                _seen.append(_key)
            if len(_seen) != len(set(_seen)):
                errors.append({
                    'field': field,
                    'error': 'array contains duplicate items but uniqueItems is true',
                    'hint': 'Remove the duplicate entries',
                })

        # contains — array values only. At least ONE element must validate
        # against the subschema; JSON Schema does not require every element to,
        # and does not report WHICH element failed (there is no failing element,
        # only a missing satisfying one). So the sub-errors are discarded and a
        # single miss is reported against the array itself.
        #
        # Implemented rather than tolerated, for the same reason `uniqueItems`
        # above is: plan.schema.json 2.10.0 ships four `contains` branches under
        # `gated_exit_criteria`/`allOf` to require each fleet brightline be
        # present, and an unimplemented keyword validates as a silent no-op --
        # the schema would have asserted the brightlines and enforced nothing.
        # test_schema_keyword_coverage caught it on the re-vendor, which is what
        # that test is for.
        if 'contains' in schema:
            contains_schema = schema['contains']
            if not any(
                not _validate_json_schema_node(item, contains_schema, root_schema, path)
                for item in value
            ):
                errors.append({
                    'field': field,
                    'error': 'no array item matches the required `contains` shape',
                    'hint': 'Add an item satisfying the contains subschema',
                })

        if 'items' in schema:
            items_schema = schema['items']
            for i, item in enumerate(value):
                item_path = f'{path}[{i}]' if path else f'[{i}]'
                errors.extend(_validate_json_schema_node(item, items_schema, root_schema, item_path))

    return errors


# ---------------------------------------------------------------------------
# Handoff `kind` enum — D1 pre-rename alias tolerance (post-process seam).
#
# `_validate_json_schema_node` above stays a generic, schema-identity-free
# JSON Schema walker (it is also the enum check for `gate_source.kind`,
# `verified_by.kind`, and `gate_evidence.legs[].kind` — unrelated closed
# enums in other record families that must keep strict equality). Threading
# schema identity through that recursive walker just to scope one field
# would widen the enum check's blast radius for every caller. The caller
# that needs this tolerance — `validate_frontmatter()`, the claim path's
# (ops.handoff_transition) post-mutation READ-side re-validation — DOES know
# its own schema_name, so the alias tolerance is applied there, once, as a
# post-process over the returned error list. Deliberately NOT applied to
# `_dispatch_validate()` (backing `validate()`/`validate_frontmatter_obj()`,
# which the write-guard deny path's strict-mode schema-shape check runs
# through) — D1 narrowed the on-disk enum deliberately, so a WRITE of a new
# legacy spelling must still be rejected under
# COORDINATOR_SCHEMA_STRICT=1; only a READ of an already-on-disk legacy
# record should stop treating it as broken. Mirrors the same accept-but-
# don't-write-new distinction the write-guard deny path's own
# `_evaluate_handoff_kind_enum` docstring already draws ("alias tolerance is
# a READER contract, not a writer one" —
# `validate_frontmatter_schema_deny.py`). Reuses `baton_class._PRE_RENAME_ALIASES` /
# `canonical_kind()` rather than re-spelling the alias table — see that
# module's "Vocabulary bridge" section.
# ---------------------------------------------------------------------------

_HANDOFF_KIND_SCHEMA_NAME = 'handoff'


def _tolerate_handoff_kind_aliases(
    errors: list[ErrorDict], schema_name: str | None, schema: dict, frontmatter: dict | None,
) -> list[ErrorDict]:
    """Drop a `kind` enum error from `errors` when the raw value is a still-
    live D1 pre-rename alias, and enrich a genuine reject's hint with the
    alias table. SCOPED to `schema_name == "handoff"` and the top-level
    `kind` field only (never `gate_source.kind` etc — those have field paths
    other than the bare `"kind"` this checks for).

    Read-side tolerance only, matching D1's "reader contract, not a writer
    one" split: this does not make the on-disk enum accept a legacy spelling
    for NEW writes — it makes a pre-C10 legacy record's post-mutation
    re-validation (the claim/pickup-assemble path) stop treating an
    unmigrated legacy `kind:` as a validation failure.
    """
    if schema_name != _HANDOFF_KIND_SCHEMA_NAME or not frontmatter or 'kind' not in frontmatter:
        return errors
    raw_kind = frontmatter.get('kind')
    if raw_kind is None:
        return errors
    raw_str = str(raw_kind)
    enum_values = list((schema.get('properties') or {}).get('kind', {}).get('enum') or [])
    if not enum_values or raw_str in enum_values:
        return errors
    def is_kind_enum_error(e: ErrorDict) -> bool:
        return e['field'] == 'kind' and e['error'] == f'invalid enum value "{raw_kind}"'

    if _canonical_kind(raw_str) in enum_values:
        return [e for e in errors if not is_kind_enum_error(e)]
    alias_clause = ', '.join(
        f'{retired} -> {target}' for retired, target in _HANDOFF_KIND_PRE_RENAME_ALIASES.items()
    )
    filtered: list[ErrorDict] = []
    for error in errors:
        if is_kind_enum_error(error):
            error = dict(error)
            error['hint'] = (
                f'{error["hint"]} Retired pre-rename names ({alias_clause}) also validate — '
                'this value is not one of them either.'
            )
        filtered.append(error)
    return filtered


def _tolerate_handoff_kind_aliases_in_result(
    result: dict, schema_name: str | None, schema: dict, frontmatter: dict | None,
) -> dict:
    """Apply `_tolerate_handoff_kind_aliases` to a `{"ok", "errors"}` result.

    The SAME tolerance rule as `validate_frontmatter()`'s post-process, reached
    through the only other shape a READ path sees it in: the result dict
    `validate_frontmatter_obj()` returns. Both readers call the one alias
    function via this adapter, so `--file`/whole-tree lint can neither lag
    behind the claim path's vocabulary nor drift ahead of it — a legacy `kind:`
    diverging between two readers is the defect this seam exists to make
    structurally impossible.

    Negative-spec: this is a CALLER-side wrapper, deliberately not folded into
    `validate_frontmatter_obj()`/`_dispatch_validate()`. Those also back the
    write-guard deny path's strict-mode shape check, where D1's narrowed enum
    must keep rejecting a NEW write of a legacy spelling ("alias tolerance is a
    READER contract, not a writer one"). Widening the shared dispatch would
    silently widen the writer too.
    """
    if result.get('ok'):
        return result
    tolerated = _tolerate_handoff_kind_aliases(
        list(result.get('errors') or []), schema_name, schema, frontmatter,
    )
    return {'ok': True} if not tolerated else {'ok': False, 'errors': tolerated}


# ---------------------------------------------------------------------------
# Cross-field rules — handoff schema only.
#
# Port of CROSS_FIELD_RULES['handoff'] from DoE-claude coordinator/bin/lib/schema.js.
# Each rule is a callable (fm_dict) -> ErrorDict | None.
# Negative-spec: handoff-archived has no cross-field rules by design.
# ---------------------------------------------------------------------------

def _cf_awaiting_gate_needs_dependency(fm: dict) -> ErrorDict | None:
    """An ``awaiting_gate`` baton must carry at least one of the three gate-naming
    fields: ``gate_dependency`` (deprecated single-string form, C8: retired into
    ``blocking_notes`` on the mutating paths but still accepted here so
    not-yet-migrated handoffs keep validating), a non-empty ``blocked_by`` list
    (structured predecessor edges), or a non-empty ``blocking_notes`` string
    (freeform advisory prose — the migration target). Relaxed from a hard
    ``gate_dependency``-required rule (C3) precisely so the gate-prose migration
    off ``gate_dependency`` and onto ``blocking_notes``/``blocked_by`` is
    possible at all: a baton with none of the three still fails, which is the
    rule's entire remaining point.
    """
    if fm.get('deployment_state') == 'awaiting_gate':
        dep = fm.get('gate_dependency')
        has_dep = bool(dep) and str(dep).strip() != ''

        blocked_by = fm.get('blocked_by')
        has_blocked_by = bool(blocked_by) and len(blocked_by) > 0

        notes = fm.get('blocking_notes')
        has_notes = bool(notes) and str(notes).strip() != ''

        if not (has_dep or has_blocked_by or has_notes):
            return {
                'field': 'gate_dependency',
                'error': (
                    'awaiting_gate requires at least one of gate_dependency (deprecated), '
                    'blocked_by, or blocking_notes'
                ),
                'hint': (
                    'Name the gate: a non-empty blocked_by list, a blocking_notes string '
                    'describing the condition, or (deprecated) a one-line gate_dependency.'
                ),
            }
    return None


def _cf_ready_to_fire_no_dependency(fm: dict) -> ErrorDict | None:
    if fm.get('deployment_state') == 'ready_to_fire':
        dep = fm.get('gate_dependency')
        if dep and str(dep).strip() != '':
            return {
                'field': 'gate_dependency',
                'error': 'must be empty or omitted when deployment_state=ready_to_fire',
                'hint': (
                    'A handoff cannot be ready_to_fire while it has a gate_dependency. '
                    'Either clear gate_dependency or set deployment_state=awaiting_gate.'
                ),
            }
    return None


def _cf_ready_to_fire_no_gate_evidence(fm: dict) -> ErrorDict | None:
    """Parallels ``_cf_ready_to_fire_no_dependency`` for the structured
    ``gate_evidence:`` block (C7): a ``ready_to_fire`` record cannot carry
    stale evidence any more than it can carry a stale ``gate_dependency``
    sentence — both are gate-naming fields whose only legal home is a
    still-gated ``awaiting_gate``/``in_flight`` node. The mutating
    transitions (``handoff_transition._strip_gate_evidence``) strip the
    block on the same paths that strip ``gate_dependency``; this rule is
    the schema-side backstop for handoffs this engine never wrote.
    """
    if fm.get('deployment_state') == 'ready_to_fire':
        gate_evidence = fm.get('gate_evidence')
        if gate_evidence is not None:
            return {
                'field': 'gate_evidence',
                'error': 'must be omitted when deployment_state=ready_to_fire',
                'hint': (
                    'A handoff cannot be ready_to_fire while it has a gate_evidence: '
                    'block. Either remove gate_evidence or set '
                    'deployment_state=awaiting_gate.'
                ),
            }
    return None


def _cf_ready_to_fire_no_unresolved_blocked_by(fm: dict) -> ErrorDict | None:
    """A ``ready_to_fire`` handoff must not carry an unresolved ``blocked_by``
    entry. This is the schema-side backstop for the syntactic contradiction
    reproduced in ``docs/plans/2026-08-19-gate-notes-are-advisory-blocked-by-
    derives-readiness.md`` § Problem (AC6): ``validate_frontmatter`` returned
    zero errors for ``deployment_state: ready_to_fire`` + ``blocked_by:
    [stb-...]`` because only the deprecated ``gate_dependency`` axis was
    guarded (``_cf_ready_to_fire_no_dependency``,
    ``_cf_ready_to_fire_no_gate_evidence``) — ``blocked_by`` had no
    equivalent.

    Cross-field rules receive only the frontmatter dict, with no resolution
    index against the corpus, so this rule CANNOT resolve ``blocked_by`` and
    must not try — that is `reconcile/gate_eval.py :: evaluate_gate_triage`'s
    job, wired in at write time (C1). It fires on the syntactic contradiction
    only: a ``blocked_by`` entry with no corresponding
    ``no_longer_blocked_by``/``gate_cleared_by`` entry clearing it. This is
    the schema-side backstop for records this engine never wrote directly.
    """
    if fm.get('deployment_state') == 'ready_to_fire':
        blocked_by = fm.get('blocked_by')
        if blocked_by:
            cleared = set(fm.get('no_longer_blocked_by') or []) | set(
                fm.get('gate_cleared_by') or []
            )
            unresolved = [b for b in blocked_by if b not in cleared]
            if unresolved:
                return {
                    'field': 'blocked_by',
                    'error': 'must be empty or fully cleared when deployment_state=ready_to_fire',
                    'hint': (
                        'A handoff cannot be ready_to_fire while blocked_by names an '
                        'unresolved blocker. Either clear the blocker (moving it into '
                        'no_longer_blocked_by / gate_cleared_by) or set '
                        'deployment_state=awaiting_gate.'
                    ),
                }
    return None


def _cf_awaiting_gate_not_pickup_ready(fm: dict) -> ErrorDict | None:
    """A ``deployment_state: awaiting_gate`` baton must not also carry
    ``pickup_ready: true`` — a gated baton advertising pickup-readiness is a
    self-contradiction: the field that a picking-up EM reads as "is this
    fire-ready" would say yes while the gate axis says no.

    Cross-repo memo:
    cross-repo/inbox/2026-08-06-example-market-data-repo-em-pickup-ready-true-under-unmet-gate.md
    resolved the readiness-vs-authorial-intent fork in favor of readiness answer,
    per handoff.schema.json's own field description ("Positive pickup-authorized
    signal. Absence triggers a non-blocking warning at /pickup time.") and the
    SUCCEEDED-BATON CARVE-OUT docstring in coordinator_core/session/claims.py,
    which treats ``pickup_ready: true`` as literally re-advertising the baton to
    /pickup.
    """
    if fm.get('deployment_state') == 'awaiting_gate' and fm.get('pickup_ready') is True:
        return {
            'field': 'pickup_ready',
            'error': 'must not be true when deployment_state=awaiting_gate',
            'hint': (
                'A gated baton must not advertise pickup-readiness. Either omit '
                'pickup_ready (or set it false) while the gate is unmet, or set '
                'deployment_state=ready_to_fire once the gate clears.'
            ),
        }
    return None


def _cf_gate_dependency_not_path_shaped(fm: dict) -> ErrorDict | None:
    """A ``gate_dependency`` value must not be a path (dialect D3) — the field
    is deprecated single-string prose, not a filesystem pointer. Path-shaped
    means it contains ``tasks/``, contains ``archive/``, or ends in ``.md``.

    D3 grew because the field's only worked example (the OSS mirror's
    spinoff skill) taught ``gate_dependency: <filename>``, pointing at a
    handoff/plan file instead of naming the gate. That upstream source is
    fixed alongside this rule (C2b); this check stops new D3 instances from
    landing while the ~7 existing ones are migrated off it.
    """
    dep = fm.get('gate_dependency')
    if not dep or not isinstance(dep, str):
        return None
    dep_str = dep.strip()
    if not dep_str:
        return None
    is_path_shaped = 'tasks/' in dep_str or 'archive/' in dep_str or dep_str.endswith('.md')
    if is_path_shaped:
        return {
            'field': 'gate_dependency',
            'error': f'gate_dependency looks like a file path ("{dep_str}"), not a gate description',
            'hint': (
                'gate_dependency is deprecated single-line prose, not a pointer to a file. '
                'If this names a baton/predecessor to wait on, put its slug in blocked_by. '
                'If it is advisory prose about the condition, put it in blocking_notes. '
                'Do not put tasks/, archive/, or .md paths in gate_dependency.'
            ),
        }
    return None


#: Kinds that identify a ROADMAP BATON for the three cross-field rules below.
#:
#: ``roadmap-baton`` is canonical (D1 of the baton-kind vocabulary migration);
#: ``spinoff-roadmap`` is its permanently-retained pre-rename value. Both are
#: accepted, and that dual-acceptance is load-bearing rather than transitional:
#: these rules gate whether ``roadmap_id`` / ``stub_id`` / ``wave`` are legal on
#: a record, so a rule that recognised only ONE spelling would invalidate every
#: roadmap baton written under the other. Recognising only ``spinoff-roadmap``
#: is precisely the defect this set fixes -- it made all 39 migrated roadmap
#: batons fail validation the moment their ``kind`` was renamed, because
#: ``roadmap_id`` became "permitted only when kind=spinoff-roadmap".
#:
#: A half-migrated fleet is the normal state of a fleet vocabulary change, so
#: do not add a removal date. Deliberately NOT derived via ``baton_class()``:
#: that returns ``intention`` for ``roadmap-seed`` too, which would wrongly
#: make roadmap_id/wave legal on a seed stub. Sourced from the canonical
#: ``_PRE_RENAME_ALIASES`` table via ``kind_values_for_canonical()`` instead
#: of re-declaring the retired/successor pair as a literal collection here
#: (AC4 -- see ``test_baton_class_is_the_only_membership_set.py``).
_ROADMAP_BATON_KINDS = frozenset(kind_values_for_canonical('roadmap-baton'))


def _cf_graph_fields_roadmap_only(fm: dict) -> ErrorDict | None:
    graph_fields = ['roadmap_id']
    present = [f for f in graph_fields if fm.get(f) is not None]
    if present and fm.get('kind') not in _ROADMAP_BATON_KINDS:
        return {
            'field': ', '.join(present),
            'error': f'permitted only when kind=roadmap-baton (current kind: {fm.get("kind") or "unset"})',
            'hint': (
                'roadmap_id is roadmap-only. '
                'Remove it, or set kind: roadmap-baton if this is a roadmap stub. '
                '(blocks/blocked_by are permitted on any kind.)'
            ),
        }
    return None


#: Operator escape hatch for `_cf_spinoff_roadmap_requires_graph`, minted as a
#: CONDITION OF RATIFICATION under SC-DR-016 (DoE-claude
#: `coordinator/docs/wiki/scoped-safety-commits.md` § "The deny-from-day-one
#: class is the self-contained oracle, not the command string"). That record
#: clears this rule to ship deny-from-day-one without an SC-DR-003 warn-first
#: soak, on the grounds that its oracle is self-contained -- but it clears it
#: only WITH a hatch: SC-DR-014's criterion (3) is defined in terms of an
#: override path, so a deny carrying none does not qualify under SC-DR-016 at
#: all. The spelling is fixed by that record and is not ours to shorten.
#:
#: Pre-launch only, and an OPERATOR's knob rather than an agent's: the guard
#: processes that evaluate this rule are spawned per event and read their
#: payload on stdin, so nothing reachable from inside a live session can put
#: this variable into the deciding process's environment (see
#: `docs/reference/guard-override-keys.md` § Security context).
_ROADMAP_GRAPH_OVERRIDE_ENV_VAR = 'COORDINATOR_OVERRIDE_ROADMAP_GRAPH_FIELDS'


def _cf_spinoff_roadmap_requires_graph(fm: dict) -> ErrorDict | None:
    if fm.get('kind') not in _ROADMAP_BATON_KINDS:
        return None
    if os.environ.get(_ROADMAP_GRAPH_OVERRIDE_ENV_VAR, '0') == '1':
        return None
    required = ['roadmap_id', 'stub_id', 'wave', 'blocks', 'blocked_by']
    missing = [f for f in required if fm.get(f) is None]
    if missing:
        # Deferred so the deny-path-only pointer text does not put
        # `bash_guards._helpers` (and its `subagent_sandbox.engine` import) on
        # this module's import graph -- `schema_validate` is imported on the
        # per-invocation hot path, the renderer is needed on the deny leg only.
        # NO OVERRIDE POINTER HERE, and that is deliberate (2026-08-13,
        # docs/plans/2026-08-13-guard-messages-stop-handing-agents-the-keys.md,
        # C1/D5). `operator_override_note` is audience-gated now and takes a
        # required `payload=`; this module never receives one. It is reached
        # through `validate_frontmatter`, whose signature is a cross-repo
        # contract DoE-claude imports by file path, so threading a payload
        # down to here is out of bounds -- not a local defect to fix later.
        #
        # The pointer is therefore dropped for every audience rather than
        # rendered blind. Under AC-1 the subagent case must not see it at
        # all; under AC-2 an EM message MAY carry it, never must. What
        # survives is the refusal and the route, which is complete on its own.
        return {
            'field': ', '.join(missing),
            'error': 'required for kind=roadmap-baton',
            'hint': (
                'Roadmap batons must declare their identifier (roadmap_id, stub_id), '
                'serialization order (wave), and graph edges (blocks, blocked_by — empty list ok). '
                'See skills/roadmap-planning/SKILL.md § Phase 2.1.'
            ),
        }
    return None


def _cf_roadmap_id_implies_kind(fm: dict) -> ErrorDict | None:
    if fm.get('roadmap_id') and fm.get('kind') not in _ROADMAP_BATON_KINDS:
        return {
            'field': 'roadmap_id',
            'error': f'present but kind is "{fm.get("kind") or "unset"}" — roadmap_id requires kind: roadmap-baton',
            'hint': 'Either set kind: roadmap-baton (if this is a roadmap stub), or remove roadmap_id (if not).',
        }
    return None


def _cf_cost_enum(fm: dict) -> ErrorDict | None:
    cost = fm.get('cost')
    if cost is None:
        return None
    allowed = ['T0', 'T1', 'T2', 'T3']
    if str(cost) not in allowed:
        return {
            'field': 'cost',
            'error': f'invalid cost value "{cost}"',
            'hint': f'Allowed values: {", ".join(allowed)}. T0 = trivial (minutes); T1 = small (<1h); T2 = medium (1-4h); T3 = large (multi-day).',
        }
    return None


def _cf_category_required_post_cutoff(fm: dict) -> ErrorDict | None:
    created = fm.get('created')
    if created and str(created) < '2026-05-29':
        return None
    cat = fm.get('category')
    if not cat or str(cat).strip() == '':
        return {
            'field': 'category',
            'error': 'required for handoffs created on or after 2026-05-29',
            'hint': 'Set category to one of: roadmap, infra, bug, docs, research, refactor, uncategorized, queue-derived-baton',
        }
    return None


def _cf_summary_required_post_cutoff(fm: dict) -> ErrorDict | None:
    created = fm.get('created')
    if created and str(created) < '2026-05-29':
        return None
    summary = fm.get('summary')
    if not summary or str(summary).strip() == '':
        return {
            'field': 'summary',
            'error': 'required for handoffs created on or after 2026-05-29',
            'hint': 'Add a one-line summary (≤140 chars) describing the session work',
        }
    return None


def _cf_summary_length_cap(fm: dict) -> ErrorDict | None:
    created = fm.get('created')
    if created and str(created) < '2026-05-29':
        return None
    summary = fm.get('summary')
    if summary and len(str(summary)) > 140:
        return {
            'field': 'summary',
            'error': f'summary exceeds 140 characters (got {len(str(summary))})',
            'hint': 'Keep summary to one concise line of 140 characters or fewer',
        }
    return None


def _cf_supersedes_spinoff_only(fm: dict) -> ErrorDict | None:
    supersedes = fm.get('supersedes')
    if supersedes is None or str(supersedes).strip() == '':
        return None
    if fm.get('kind') != 'spinoff':
        return {
            'field': 'supersedes',
            'error': f'permitted only when kind=spinoff (current kind: {fm.get("kind") or "unset"})',
            'hint': (
                'supersedes: on a baton is the conditional-live orientation-supersession field; '
                'it belongs only on a kind: spinoff install/orientation baton, not spinoff-roadmap. '
                'Distinct from the terminal memo supersedes:/superseded_by: coupling.'
            ),
        }
    return None


def _cf_claimed_by_required(fm: dict) -> ErrorDict | None:
    """Rule A3a-1 (DR-084): status=claimed + claimed_at present → claimed_by required.

    DR-084 P4 (C7): the old-vocabulary status=consumed/consumed_at/consumed_by
    grandfather branch retired — DoE's narrowed schema (d652253c) no longer
    admits status=consumed at all, and the corpus migration (C5+C8) already
    re-expressed every such record.
    """
    if fm.get('status') != 'claimed':
        return None
    if not fm.get('claimed_at'):
        return None
    claimed_by = fm.get('claimed_by')
    if not claimed_by or str(claimed_by).strip() == '':
        return {
            'field': 'claimed_by',
            'error': 'required when status=claimed and claimed_at is present',
            'hint': (
                'claimed_by identifies the claiming session (from --session-id). '
                'It is written by cs_claim_handoff; absent here indicates a partial or hand-edited claim. '
                'Set claimed_by to the session id.'
            ),
        }
    return None


def _cf_closed_reason_required(fm: dict) -> ErrorDict | None:
    """DR-084 (plan C5): deployment_state=closed <=> closed_reason present (bidirectional).

    Mirrors the shipped/shipped_in coupling. AUTOMATED WRITERS MAY NEVER STAMP
    'closed' (sole exception: the PM-authorized C8 archive migration re-labeling) —
    this rule only enforces the field pairing, not who may write it.
    """
    deployment_state = fm.get('deployment_state')
    closed_reason = fm.get('closed_reason')
    has_closed_reason = closed_reason is not None and str(closed_reason).strip() != ''
    if deployment_state == 'closed' and not has_closed_reason:
        return {
            'field': 'closed_reason',
            'error': 'required when deployment_state=closed',
            'hint': (
                'closed_reason must be one of: cancelled, displaced, stale. '
                'Set closed_reason to describe why this handoff was deliberately stopped.'
            ),
        }
    if has_closed_reason and deployment_state != 'closed':
        return {
            'field': 'closed_reason',
            'error': 'permitted only when deployment_state=closed',
            'hint': 'closed_reason is coupled to deployment_state=closed; clear it or set deployment_state=closed.',
        }
    return None


def _cf_continued_into_required(fm: dict) -> ErrorDict | None:
    """DR-084 (plan C5): deployment_state=continued => continued_into required.

    The anti-loophole tooth: without this, 'continued' becomes liveness-guess
    abandonment under a new name. continued_into must name the successor
    handoff (id or path) as positive succession proof.
    """
    if fm.get('deployment_state') != 'continued':
        return None
    continued_into = fm.get('continued_into')
    if not continued_into or str(continued_into).strip() == '':
        return {
            'field': 'continued_into',
            'error': 'required when deployment_state=continued',
            'hint': 'Set continued_into to the successor handoff id or path (positive succession proof).',
        }
    return None


def _cf_shipped_in_required(fm: dict) -> ErrorDict | None:
    """Rule A3a-2: deployment_state=shipped + created >= 2026-05-29 → shipped_in required."""
    if fm.get('deployment_state') != 'shipped':
        return None
    created = fm.get('created')
    if not created or str(created) < '2026-05-29':
        return None
    shipped_in = fm.get('shipped_in')
    if not shipped_in or str(shipped_in).strip() == '':
        return {
            'field': 'shipped_in',
            'error': 'required when deployment_state=shipped (for handoffs created on or after 2026-05-29)',
            'hint': (
                'Set shipped_in to the commit SHA or plan path where this work landed. '
                'Written by /workstream-complete or /handoff on transition to shipped.'
            ),
        }
    return None


# Spinoff fork kinds sharing the predecessor:none-by-design contract.
# Port of schema.js:1113 `spinoffKinds` (Rule A3a-3), extended 2026-07-07 to include
# spinoff-goal and spinoff-roadmap-creator as sister fork kinds (DR-013/014).
#
# Baton-kind vocabulary migration: carries BOTH each pre-rename value and its D1
# target — spinoff-goal/goal-seed and spinoff-roadmap-creator/roadmap-seed — so a
# record validates identically before and after its kind is migrated. Retained
# permanently, not time-boxed: sibling repos still carry pre-rename values after
# this repo's own records have migrated.
#
# Deliberately NOT expressed as `baton_class(kind) == 'deflection'`. That would
# both WIDEN this set (roadmap-seed derives `intention`, yet belongs here as the
# rename of spinoff-roadmap-creator, so the class boundary and this rule's
# membership genuinely cross) and change which records the predecessor:none
# contract binds. Membership preservation beats derivation — the same finding
# recorded at the other four former membership sets.
#
# Each retired/successor pair is sourced from the canonical `_PRE_RENAME_ALIASES`
# table via `kind_values_for_canonical()` instead of being spelled as a literal
# collection here (AC4 -- see `test_baton_class_is_the_only_membership_set.py`).
_SPINOFF_KINDS = (
    'spinoff',
    *kind_values_for_canonical('goal-seed'),
    *kind_values_for_canonical('roadmap-seed'),
)


def _cf_spinoff_predecessor_none(fm: dict) -> ErrorDict | None:
    """Rule A3a-3: kind in {spinoff, spinoff-goal, spinoff-roadmap-creator} → predecessor must be 'none' or null.

    Port of schema.js:1111-1126. These three kinds are all forks authored mid-session
    (or PM-directed) with no predecessor handoff to continue.
    """
    if fm.get('kind') not in _SPINOFF_KINDS:
        return None
    pred = fm.get('predecessor')
    if pred is None or pred == 'none':
        return None
    # undefined (missing) is caught by required-field check; skip here.
    return {
        'field': 'predecessor',
        'error': f'must be "none" or null for kind={fm.get("kind")} (got "{pred}")',
        'hint': (
            'Spinoff kinds (spinoff, spinoff-goal, spinoff-roadmap-creator) fork from the '
            'current session and have no predecessor baton to continue. '
            'Set predecessor: none (or null). If this is a continuation, use kind: session-handoff instead.'
        ),
    }


def _cf_forked_from_spinoff_only(fm: dict) -> ErrorDict | None:
    """forked_from: permitted only on kind=spinoff.

    Port of schema.js:1167-1181. Negative-spec: spinoff-goal and spinoff-roadmap-creator
    are also fork kinds (predecessor:none-by-design, see _cf_spinoff_predecessor_none)
    but are intentionally EXCLUDED from the forked_from allowlist — they are born from
    a PM directive, not from a running session's baton, so there is no branch-point
    ancestry to record. forked_from is a spinoff-only DAG primitive, governed by this
    dedicated kind-gate and NOT by the predecessor:none rule above.
    """
    forked_from = fm.get('forked_from')
    if forked_from is None or str(forked_from).strip() == '':
        return None
    if fm.get('kind') != 'spinoff':
        return {
            'field': 'forked_from',
            'error': f'permitted only when kind=spinoff (current kind: {fm.get("kind") or "unset"})',
            'hint': (
                'forked_from records the branch-point ancestry for spinoff lineage/rendering. '
                'Set kind: spinoff or remove forked_from. spinoff-goal and spinoff-roadmap-creator '
                'do not carry fork ancestry (PM-directive origin, no baton branch-point).'
            ),
        }
    return None


# ---------------------------------------------------------------------------
# origin-axis cross-field rules (Rules C2-1 through C2-5).
#
# Port of DoE-claude coordinator/bin/lib/schema.js:1283-1553.
# origin_session / origin_handoff / origin_plan_id / origin_goal_id record where a
# fork was spawned FROM — a DISTINCT axis from predecessor (continuation spine),
# forked_from (branch-point ancestry), and deliverable_id (dlv- grouping key). All
# four origin_* fields are nullable/optional (backfill=null for pre-existing forks).
#
# DR-014/DR-015 backlink: origin_* isolation is load-bearing for LoE effort-isolation
# (DR-014) and rag's namespace-disjoint recursive-CTE (DR-015) — an id in the wrong
# namespace silently returns zero rows in rag's origin_edges walk.
#
# Spec backlink: docs/plans/2026-07-07-spinoff-provenance-ancestry.md § C2, § AC3
# ---------------------------------------------------------------------------

def _cf_origin_plan_id_prefix(fm: dict) -> ErrorDict | None:
    """Rule C2-1a: origin_plan_id (when non-null) must carry a 'pln-' prefixed id."""
    val = fm.get('origin_plan_id')
    if val is None:
        return None
    if not str(val).startswith('pln-'):
        return {
            'field': 'origin_plan_id',
            'error': f'origin_plan_id "{val}" does not begin with "pln-" (Rule C2-1: kind-prefixed id well-formedness)',
            'hint': (
                'origin_plan_id must carry a pln-prefixed plan id '
                '(e.g. pln-structured-originating-session-8b505c). Namespace-disjointness is '
                "required for rag's origin_edges recursive-CTE."
            ),
        }
    return None


def _cf_origin_handoff_path_prefix(fm: dict) -> ErrorDict | None:
    """Rule C2-1b: origin_handoff (when non-null) must be a 'state/handoffs/' path."""
    val = fm.get('origin_handoff')
    if val is None:
        return None
    if not str(val).startswith('state/handoffs/'):
        return {
            'field': 'origin_handoff',
            'error': f'origin_handoff "{val}" does not begin with "state/handoffs/" (Rule C2-1: kind-prefixed id well-formedness)',
            'hint': (
                'origin_handoff must be a handoff path (e.g. state/handoffs/2026-07-07-my-baton.md). '
                "Namespace-disjointness is required for rag's origin_edges recursive-CTE."
            ),
        }
    return None


def _cf_origin_session_non_empty(fm: dict) -> ErrorDict | None:
    """Rule C2-1c: origin_session (when non-null) must be a non-empty string (UUID, no prefix)."""
    val = fm.get('origin_session')
    if val is None:
        return None
    if str(val).strip() == '':
        return {
            'field': 'origin_session',
            'error': (
                'origin_session must be a non-empty string (UUID) when present and non-null '
                '(Rule C2-1: kind-prefixed id well-formedness)'
            ),
            'hint': (
                'origin_session carries a session UUID (globally unique; no prefix needed). '
                'An empty string is not a valid UUID.'
            ),
        }
    return None


def _cf_origin_goal_id_entry_prefix(fm: dict) -> ErrorDict | None:
    """Rule C2-1d: each origin_goal_id[] entry (when the field is an array) must be 'goal-' prefixed.

    Array cardinality is enforced by _cf_origin_goal_id_array_cardinality (Rule C2-2b);
    here we check entry prefixes only — skip prefix validation if the field is not an
    array (that cardinality rule will fire instead).
    """
    val = fm.get('origin_goal_id')
    if val is None or not isinstance(val, list):
        return None
    for i, entry in enumerate(val):
        if not str(entry).startswith('goal-'):
            return {
                'field': 'origin_goal_id',
                'error': f'origin_goal_id[{i}] "{entry}" does not begin with "goal-" (Rule C2-1: kind-prefixed id well-formedness)',
                'hint': (
                    'Each origin_goal_id entry must carry a goal-prefixed id '
                    '(e.g. goal-shipping-velocity), matching the goal.schema.json id field '
                    'scheme. Prefixes "gol-" and "g-" are not valid.'
                ),
            }
    return None


def _cf_origin_scalar_fields_reject_arrays(fm: dict) -> ErrorDict | None:
    """Rule C2-2a: origin_session / origin_handoff / origin_plan_id are scalar-only — reject arrays.

    LOAD-BEARING (DR-014/DR-015): rag ingests these origin_* fields via typed scalar
    columns (fast equality path). An unexpected array crammed into a scalar field lands
    as a JSON string in the scalar filter and SILENTLY RETURNS ZERO ROWS.
    """
    for field in ('origin_session', 'origin_handoff', 'origin_plan_id'):
        val = fm.get(field)
        if val is None:
            continue
        if isinstance(val, list):
            return {
                'field': field,
                'error': (
                    f'{field} must be a scalar string (not an array) — per-kind cardinality '
                    "contract (Rule C2-2). An unexpected array in a scalar origin field lands as "
                    "a JSON string in rag's scalar column filter and silently returns zero rows."
                ),
                'hint': (
                    f'{field} carries at most one value (0..1 cardinality). Use a plain string, '
                    'not a YAML list. DR-014/DR-015 require explicit scalar-vs-array cardinality '
                    'per field.'
                ),
            }
    return None


def _cf_origin_goal_id_array_cardinality(fm: dict) -> ErrorDict | None:
    """Rule C2-2b: origin_goal_id is array-only — reject bare scalars.

    rag fans array entries into N edge rows in the origin_edges CQRS projection;
    a bare scalar bypasses the fan-out.
    """
    val = fm.get('origin_goal_id')
    if val is None or isinstance(val, list):
        return None
    return {
        'field': 'origin_goal_id',
        'error': (
            f'origin_goal_id must be an array (string[] | null), not a bare scalar "{val}" — '
            "per-kind cardinality contract (Rule C2-2). rag fans array entries into N edge rows "
            'in origin_edges; a bare scalar bypasses the fan-out.'
        ),
        'hint': (
            'Use YAML list syntax for origin_goal_id, e.g. ["goal-shipping-velocity"]. '
            "DR-014/DR-015 require origin_goal_id to be an array; a scalar bypasses rag's "
            'fan-out into origin_edges rows.'
        ),
    }


def _cf_origin_handoff_self_reference(fm: dict) -> ErrorDict | None:
    """Rule C2-3: origin_handoff must not equal the record's own path.

    Best-effort: the cross-field rule interface (fm dict only, no file path context)
    means this rule only fires when the caller injects the record's own path via the
    optional fm['_filePath'] sentinel. Full cycle detection across the fleet is the
    walker's job (walk-handoff-dag.js visited-set) + cockpit depth-cap; this rule
    catches only the degenerate self-loop at validation time.

    Negative-spec: '_filePath' is a validator-internal convention, never written into
    the YAML frontmatter of a record. Callers without path context silently skip this
    guard (partial acyclicity, matching the JS oracle).
    """
    origin_handoff = fm.get('origin_handoff')
    if origin_handoff is None:
        return None
    own_path = fm.get('_filePath')
    if own_path is None:
        return None
    norm_origin = str(origin_handoff).replace('\\', '/')
    norm_own = str(own_path).replace('\\', '/')
    if norm_origin == norm_own:
        return {
            'field': 'origin_handoff',
            'error': (
                f'origin_handoff "{origin_handoff}" equals the record\'s own path — self-reference '
                'creates a trivial cycle (Rule C2-3: direct self-reference rejection)'
            ),
            'hint': (
                'origin_handoff must point to a DIFFERENT handoff baton — the one that was active '
                'when this fork was spawned, not this record itself. Remove the self-reference.'
            ),
        }
    return None


def _cf_origin_predecessor_none_invariant(fm: dict) -> ErrorDict | None:
    """Rule C2-4: predecessor:none invariant preserved for spinoff kinds when origin_* is present.

    Reinforces _cf_spinoff_predecessor_none (Rule A3a-3) from the origin-axis
    perspective: origin_* fields are a DISTINCT provenance axis from the continuation
    spine — setting origin_* does NOT change the expectation that predecessor: none
    (or null) holds for all spinoff kinds. Only fires when at least one origin_* field
    is present (non-null); both this rule and A3a-3 fire on the same underlying
    condition, giving the author two clear signals — no duplication of A3a-3's logic.
    """
    if fm.get('kind') not in _SPINOFF_KINDS:
        return None
    has_origin_field = any(
        fm.get(f) is not None
        for f in ('origin_session', 'origin_handoff', 'origin_plan_id', 'origin_goal_id')
    )
    if not has_origin_field:
        return None
    pred = fm.get('predecessor')
    if pred is None or pred == 'none':
        return None
    return {
        'field': 'predecessor',
        'error': (
            f'origin_* provenance fields are present on a spinoff kind ({fm.get("kind")}) but '
            f'predecessor is "{pred}" — spinoff kinds must have predecessor: none (or null) '
            'regardless of origin_* presence (Rule C2-4: predecessor:none invariant)'
        ),
        'hint': (
            'The origin_* axis is DISTINCT from the continuation spine. origin_* records where '
            'this fork was spawned FROM; predecessor: none records that it does NOT continue a '
            'prior baton. Both are simultaneously correct for spinoffs. Set predecessor: none '
            '(or null). DR-014/DR-015.'
        ),
    }


def _cf_forked_from_origin_handoff_equality(fm: dict) -> ErrorDict | None:
    """Rule C2-5: forked_from / origin_handoff never-silently-disagree invariant.

    forked_from (branch-point ancestry, spinoff-only, PM-directed, archival-guard
    member) and origin_handoff (originating-session provenance, all fork kinds,
    auto-captured, LoE- and archival-excluded) are ORTHOGONAL axes, not aliases. They
    usually name the same spawning baton; this rule enforces EQUALITY-WHEN-BOTH-SET,
    with NO precedence and NO presence-coupling: requiring forked_from whenever
    origin_handoff is set would break the spinoff-goal/spinoff-roadmap-creator case,
    where forked_from is validation-illegal but origin_handoff is legal (Rule C2-4).
    Equality is enforced ONLY over the both-present intersection.

    Negative-spec (normalisation): the same forward-slash normalisation as
    _cf_origin_handoff_self_reference (C2-3) is used — backslash-to-forward-slash
    ONLY. Case, trailing slashes, and './'-relative prefixes are NOT normalized.
    """
    forked_from = fm.get('forked_from')
    origin_handoff = fm.get('origin_handoff')

    def _is_set(v: Any) -> bool:
        return v is not None and str(v).strip() != '' and v != 'none'

    if not _is_set(forked_from) or not _is_set(origin_handoff):
        return None
    norm_forked_from = str(forked_from).replace('\\', '/')
    norm_origin_handoff = str(origin_handoff).replace('\\', '/')
    if norm_forked_from != norm_origin_handoff:
        return {
            'field': 'origin_handoff',
            'error': (
                f'origin_handoff "{origin_handoff}" does not match forked_from "{forked_from}" — '
                'lineage-integrity divergence (Rule C2-5: never-silently-disagree invariant)'
            ),
            'hint': (
                'forked_from (branch-point ancestry) and origin_handoff (originating-session '
                'provenance) both name the spawning baton; when both are set they must be '
                'identical. They may legitimately differ only in that forked_from may be ABSENT '
                'where origin_handoff is set — origin_handoff covers spinoff-goal/'
                'spinoff-roadmap-creator where forked_from is illegal. If they genuinely reference '
                'different handoffs, one is wrong.'
            ),
        }
    return None


def _cf_additional_predecessors_integrity(fm: dict) -> ErrorDict | None:
    """additional_predecessors: no duplicates, no entry duplicating primary predecessor."""
    ap = fm.get('additional_predecessors')
    if ap is None:
        return None
    if not isinstance(ap, list):
        return {
            'field': 'additional_predecessors',
            'error': 'must be an array of strings',
            'hint': 'Use YAML list syntax for additional_predecessors, e.g. ["state/handoffs/foo.md"]',
        }
    seen: set[str] = set()
    for entry in ap:
        if entry in seen:
            return {
                'field': 'additional_predecessors',
                'error': f'duplicate entry "{entry}" — each predecessor path must appear at most once',
                'hint': 'Remove the duplicate entry from additional_predecessors.',
            }
        seen.add(entry)
    primary = fm.get('predecessor')
    if primary is not None and primary != 'none':
        for entry in ap:
            if entry == primary:
                return {
                    'field': 'additional_predecessors',
                    'error': f'entry "{entry}" duplicates the primary predecessor field',
                    'hint': (
                        'The primary predecessor is already declared in predecessor:. '
                        'Remove it from additional_predecessors to avoid double-counting on LoE traversal.'
                    ),
                }
    return None


def _cf_deliverable_id_prefix(fm: dict) -> ErrorDict | None:
    did = fm.get('deliverable_id')
    if did is None:
        return None
    if not str(did).startswith('dlv-'):
        return {
            'field': 'deliverable_id',
            'error': f'deliverable_id "{did}" does not begin with "dlv-"',
            'hint': (
                'deliverable_id is minted by bin/mint-deliverable-id and always begins with "dlv-". '
                'Inherit the id from the parent artifact (plan or roadmap stub); '
                'do not hand-author a value without the prefix.'
            ),
        }
    return None


def _cf_initiative_non_empty(fm: dict) -> ErrorDict | None:
    initiative = fm.get('initiative')
    if initiative is None:
        return None
    if str(initiative).strip() == '':
        return {
            'field': 'initiative',
            'error': 'initiative FK must be a non-empty string when present and non-null',
            'hint': (
                'Set initiative to a valid initiative id (e.g. "example-fleet-pro-launch") from '
                'state/initiatives/<id>.yaml, or null if this work does not belong to a named initiative.'
            ),
        }
    return None


def _cf_execution_stamp_required(fm: dict) -> ErrorDict | None:
    # foreign-identity: OUT-OF-CLASS — the "DoE-claude coordinator/bin/lib/schema.js"
    # citation below is a port-provenance note inside this function's docstring,
    # not agent-facing rendered runtime text
    """H-CROSS-EXEC-1: handoff_phase=execution requires the full FOUR-field
    execution-authorization stamp (execution_authorized_{by,at,sha,note}), all
    present-and-non-empty — the plan-execute-session-split.md pinned contract.
    Gated on a going-forward created-date cutoff (defense-in-depth: the
    handoff_phase===execution trigger already excludes the historical corpus
    since the field is new; the cutoff's only live effect is exempting a
    backdated created<cutoff going-forward handoff, an accepted low-probability
    residual since the emitter controls created).
    Spec backlink: docs/plans/2026-07-17-execution-handoff-phase-doe-contract.md § C2

    Port of DoE-claude coordinator/bin/lib/schema.js CROSS_FIELD_RULES['handoff']
    H-CROSS-EXEC-1 (~line 1221). Comparison is string-lexicographic (not a date
    object) — safe for ISO dates (YYYY-MM-DD and YYYY-MM-DDTHH:MM:SSZ); non-ISO
    created values are absent in the post-2026-04 corpus.
    """
    created = fm.get('created')
    if created and str(created) < '2026-07-17':
        return None
    if fm.get('handoff_phase') != 'execution':
        return None
    stamp_fields = [
        'execution_authorized_by',
        'execution_authorized_at',
        'execution_authorized_sha',
        'execution_authorized_note',
    ]
    missing = [f for f in stamp_fields if fm.get(f) is None or str(fm.get(f)).strip() == '']
    if missing:
        return {
            'field': ', '.join(missing),
            'error': 'required when handoff_phase=execution',
            'hint': (
                'Stamp the full four-field execution authorization '
                '(execution_authorized_by/_at/_sha/_note) per plan-execute-session-split.md '
                '§ Pinned conventions before marking handoff_phase: execution.'
            ),
        }
    return None


#: Kinds on which `handoff_phase` is admitted (H-CROSS-EXEC-2). The roadmap
#: side MUST resolve through ``kind_values_for_canonical('roadmap-baton')``,
#: never a bare ``kind == 'roadmap-baton'`` literal: that canonical resolves to
#: ``{roadmap-baton, spinoff-roadmap}`` and real roadmap batons on disk carry
#: the retired ``spinoff-roadmap`` spelling, so a literal gate would compile,
#: read correctly, pass a hand-written canonical-name test, and never fire on a
#: single real baton. ``session-handoff`` has no alias today; it is written
#: through the same union shape so the gate stays correct when that changes.
_HANDOFF_PHASE_KINDS = frozenset({'session-handoff'}) | _ROADMAP_BATON_KINDS


def _cf_handoff_phase_kind_gate(fm: dict) -> ErrorDict | None:
    # foreign-identity: OUT-OF-CLASS — the "DoE-claude coordinator/bin/lib/schema.js"
    # citation below is a port-provenance note inside this function's docstring,
    # not agent-facing rendered runtime text
    """H-CROSS-EXEC-2: handoff_phase PRESENT (either 'continuation' or 'execution')
    requires kind: session-handoff OR canonical kind: roadmap-baton —
    handoff_phase is the preparation axis declared on those kinds, not a field
    with unrestricted-presence semantics elsewhere. No cutoff — the kind-gate
    applies regardless of the handoff_phase value. Kind enum:
    coordinator/schemas/handoff.schema.json (`properties.kind.enum`).
    Spec backlink: docs/plans/2026-07-17-execution-handoff-phase-doe-contract.md § C2;
    roadmap-baton admission ratified in DoE-claude DR-126
    (`docs/decisions/DR-126-roadmap-baton-lifecycle-model-a-second-o.md`),
    schema description landed DoE-side at `feef6527f`.

    Negative-spec: admitting handoff_phase on roadmap-baton is NOT stamping it.
    Widening this VALIDATION gate must not widen any STAMPING predicate —
    `handoff_phase_stamp.py`'s own pre-write kind guard and the
    push-side-write-discipline unconditional `continuation` stamp stay
    session-handoff-only. Backfilling roadmap batons with a phase is an explicit
    anti-scope of DR-126.

    Port of DoE-claude coordinator/bin/lib/schema.js CROSS_FIELD_RULES['handoff']
    H-CROSS-EXEC-2 (retired; the live home for these rules is
    `_HANDOFF_CROSS_FIELD_RULES` below).
    """
    if fm.get('handoff_phase') is None:
        return None
    if fm.get('kind') not in _HANDOFF_PHASE_KINDS:
        return {
            'field': 'handoff_phase',
            'error': (
                f'present but kind is "{fm.get("kind") or "unset"}" — '
                'handoff_phase requires kind: session-handoff or roadmap-baton'
            ),
            'hint': (
                'handoff_phase is the preparation axis declared on kind: session-handoff '
                'and kind: roadmap-baton. Either set one of those kinds (if this is a '
                'continuation/execution baton), or remove handoff_phase (if not).'
            ),
        }
    return None


# Ownership axis fields (mcollab-03/oaxis-03 merge, C2): the RACI-backbone flat
# scalar keys `is_unowned`/`_cf_owner_axis_scalar` police. Kept as a single
# tuple so the predicate and the cross-field rule below stay in lockstep —
# adding a new axis (e.g. a future companion field) means editing one place.
_OWNERSHIP_AXIS_FIELDS = (
    'owner',
    'assignee',
    'claimant',
    'consumed_by_human',
    'to_human',
    'created_by_human',
)


def is_unowned(v: object) -> bool:
    """Canonical is_unowned(v) predicate (the Data Science Reviewer F1, P2): absent | null | whitespace-only.

    Single source of truth for the three-way YAML "unowned" byte-pattern
    hazard (`owner:` -> null, `owner: ""` -> empty string, missing key ->
    absent) — every reader (this write-time validator today; a future
    fleet-wide ownership read-model) must call this rather than re-deriving
    the equivalence per-consumer, or they silently diverge.

    Spec backlink: DoE-claude
      docs/plans/2026-07-19-mcollab-multi-axis-ownership-schema-publication.md
      § P2 (is_unowned(v) := (v absent) OR (v is null) OR (v.trim()=='')),
      § C2 (Python port of the original JS predicate
      `(v===undefined)||(v===null)||(String(v).trim()==='')`).

    Negative-spec: this predicate does not itself enforce that only
    absent|real-slug reach disk — that enforcement is `_cf_owner_axis_scalar`
    below, and only under COORDINATOR_SCHEMA_STRICT=1 (default WARN mode does
    not block a write). is_unowned() is the read-time correctness backstop
    regardless of what actually landed on disk.
    """
    if v is None:
        return True
    return isinstance(v, str) and v.strip() == ''


def _cf_owner_axis_scalar(fm: dict) -> ErrorDict | None:
    """Owner-axis cross-field rule (mcollab-03/oaxis-03 merge, C2): reject an
    empty/whitespace-only string on any ownership axis.

    The ownership axis is a scalar: an unowned record (absent/null) and a
    record naming an explicit owner are both valid; this rule exists only to
    catch the malformed in-between — a written empty or whitespace-only
    string, which is_unowned() treats as equivalent to absent/null but which
    must never itself land on disk (only two forms may: absent | a real
    slug). Mirrors the multi-field-loop shape of
    _cf_origin_scalar_fields_reject_arrays above.

    Spec backlink: DoE-claude
      docs/plans/2026-07-19-mcollab-multi-axis-ownership-schema-publication.md
      § C2, § AC6.

    Negative-spec: this rule does NOT normalize (rewrite "" -> None/absent).
    The write-time hook (validate-frontmatter-schema.js/.py) has no code path
    that rewrites file content — every branch terminates without mutating the
    write; default WARN mode nudges via additionalContext and lets the write
    proceed unchanged, COORDINATOR_SCHEMA_STRICT=1 denies. Reject-with-hint is
    therefore the only implementable mechanism at this layer; is_unowned() is
    what makes the "only absent|real-slug reach disk" claim hold unconditionally
    at *read* time even when a strict-mode-disabled write let an empty string
    through.
    """
    for field in _OWNERSHIP_AXIS_FIELDS:
        val = fm.get(field)
        if val is None:
            continue
        if isinstance(val, str) and val.strip() == '':
            return {
                'field': field,
                'error': (
                    # Review: code-reviewer — F4: message must cover both '' and
                    # whitespace-only, since the check below (val.strip() == '')
                    # fires on both and the prior wording ("not an empty string")
                    # read as inaccurate for a visually-non-empty whitespace value.
                    'must be omitted or null, not an empty string or a whitespace-only '
                    'string — is_unowned(v) treats absent/null/""/whitespace-only '
                    'identically, but only two forms may reach disk'
                ),
                'hint': 'Remove the key entirely, or set it to null, rather than an empty or whitespace-only string.',
            }
    return None


# Ordered list of cross-field rule functions for the "handoff" schema.
# Applied in order; each returns ErrorDict | None.
# No __skip__ sentinel needed (Python: early-return on None, collect all non-None).
def _cf_disposition_shape(
    items: list | None,
    *,
    field_name: str,
    open_token: str,
    requires_ref: frozenset = frozenset(),
    forbids_ref: frozenset = frozenset(),
    detail_exempt: frozenset = frozenset(),
) -> ErrorDict | None:
    """Shared shape-only cross-field validator for a disposition-carrying
    entry list — extracted out of _cf_carried_items_shape's real content
    (`docs/plans/2026-07-27-plan-line-item-resolution-model.md` § C2) so
    both it and the plan-tasks spine's per-row
    `_cf_plan_tasks_disposition_shape` share ONE implementation and one
    error-message shape across both artifacts, rather than two
    independently-driftable copies of the same iterate/reject/require-detail
    logic (D1: `carried_items[].disposition` and the plan-tasks spine's
    `disposition` carry the SAME meaning on sibling artifacts — reuse, not a
    fresh parallel vocabulary).

    Checks, per entry in `items`: entry must be a dict; `disposition_detail`
    must be a non-empty string whenever `disposition` != `open_token` AND
    `disposition` is not in `detail_exempt`; `disposition_ref` must be
    present (non-empty) whenever `disposition` is in `requires_ref`;
    `disposition_ref` must be ABSENT whenever `disposition` is in
    `forbids_ref`. Returns the FIRST violation found (an ErrorDict), or
    None if `items` is None (nothing to check — presence is the base
    JSON-Schema's job, not this function's) or every entry is well-formed.

    `detail_exempt` names dispositions that need no `disposition_detail`
    despite being non-open — plan-tasks.schema.json's `$comment` on the
    `spun_off`/`backlogged` conditional is explicit that `coded` is
    evidence of work done, not a scope decision, and is therefore NOT a
    CLOSED disposition; `close_out_and_stamp.py`'s auto-resolution to
    `coded` writes only `disposition` + `disposition_ref`, with no detail,
    and must remain schema-valid. Defaults to empty, so
    `_cf_carried_items_disposition_shape`'s behaviour is unchanged.

    Negative-spec: does NOT validate disposition_ref's SHAPE (e.g. a SHA
    pattern vs. a path pattern) or any pm_approved authorization gate —
    those checks have no handoff analogue (carried_items carries neither
    field) and are NOT shared; they live entirely in the plan-tasks-specific
    caller (`_cf_plan_tasks_disposition_shape`) alongside its own additional
    checks, not here. Do not add disposition-token-specific ref-shape logic
    to this function — it stays the two enums' common subset only (D2's
    per-token ref/detail matrix has no analogue on the other artifact — see
    D1/D2 in the originating plan).
    `detail_exempt` (like `requires_ref`/`forbids_ref`) is caller-supplied
    vocabulary, not token-specific logic resident here — `coded` itself
    must never appear as a literal in this function's body.
    """
    if items is None:
        return None
    if not isinstance(items, list):
        return {
            'field': field_name,
            'error': 'must be an array of objects',
            'hint': f'Use YAML list-of-mapping syntax for {field_name}.',
        }
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            return {
                'field': field_name,
                'error': f'{field_name}[{idx}] is not an object',
                'hint': f'Each {field_name} entry is a mapping carrying its own disposition.',
            }
        # Default to open_token when the key is absent — plan-tasks rows omit
        # `disposition` far more often than not (schema default: open), and an
        # absent key must read as the default, not as a mismatched None.
        disposition = item.get('disposition', open_token)
        if disposition != open_token and disposition not in detail_exempt:
            detail = item.get('disposition_detail')
            if not detail or not str(detail).strip():
                return {
                    'field': field_name,
                    'error': (
                        f'{field_name}[{idx}] has disposition {disposition!r} but no '
                        'non-empty disposition_detail'
                    ),
                    'hint': (
                        f'Every disposition other than {open_token!r} requires '
                        'disposition_detail naming the rationale.'
                    ),
                }
        if disposition in requires_ref:
            ref = item.get('disposition_ref')
            if not ref or not str(ref).strip():
                return {
                    'field': field_name,
                    'error': (
                        f'{field_name}[{idx}] has disposition {disposition!r} but no '
                        'non-empty disposition_ref'
                    ),
                    'hint': f'disposition {disposition!r} requires disposition_ref.',
                }
        if disposition in forbids_ref:
            ref = item.get('disposition_ref')
            if ref not in (None, ''):
                return {
                    'field': field_name,
                    'error': (
                        f'{field_name}[{idx}] has disposition {disposition!r} but carries '
                        'a disposition_ref'
                    ),
                    'hint': (
                        f'disposition {disposition!r} has no forward pointer — remove '
                        'disposition_ref.'
                    ),
                }
    return None


def _cf_carried_items_shape(fm: dict) -> ErrorDict | None:
    """carried_items: each entry needs a non-empty disposition_detail whenever
    disposition != 'carried'.

    Shape-only validation — the ceremony-time counterpart is
    coordinator_core.ops.handoff_carry_gate.evaluate_gate, run by /handoff
    before the file is written. This rule only guards against a malformed
    carried_items entry reaching disk at all (e.g. a disposition_detail-less
    'closed' entry, which is the exact 'operator remembers' shape the field
    exists to eliminate).

    Negative-spec: carry DEPTH is never validated here or anywhere else. The
    former positive-int `carry_count` check and the third-carry refusal it fed
    are gone (PM ruling 2026-08-06, DR-268) — carries are indefinite, and a
    legacy carry_count still sitting in an old handoff's frontmatter is inert.
    The disposition/disposition_detail shape check delegates to the shared
    `_cf_disposition_shape` (C2).
    """
    items = fm.get('carried_items')
    if items is None:
        return None
    if not isinstance(items, list):
        return {
            'field': 'carried_items',
            'error': 'must be an array of objects',
            'hint': 'Use YAML list-of-mapping syntax for carried_items (see handoff.schema.json).',
        }
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            return {
                'field': 'carried_items',
                'error': f'carried_items[{idx}] is not an object',
                'hint': 'Each carried_items entry is a mapping with carry_id/description/disposition.',
            }
    return _cf_disposition_shape(items, field_name='carried_items', open_token='carried')


# ---------------------------------------------------------------------------
# Cross-field rule — gate_evidence.legs[] shape (C1,
# docs/plans/2026-07-26-structured-sibling-evidence-gates.md § C1).
#
# gate_evidence extends cutover.schema.json's confirmed_consumers[].verified_by
# discriminated union to a second record kind (handoff's awaiting_gate legs) —
# the SAME vocabulary, not a parallel one (ratification Finding 5/6). The four
# kinds carried verbatim (test-node-id, probe-op-key, commit-sha,
# sibling-commitment-ref) dispatch through the SAME _verified_by_ref_shape_error
# helper _cutover_cf_verified_by_kind_and_ref_shape uses below — extended, not
# duplicated; the leg-shape definition exists exactly once. The five kinds
# gate_evidence adds beyond cutover's four (commit-ancestor, file-exists,
# frontmatter-field, deadline, human) have no cutover analogue and are
# dispatched inline here.
#
# Most of this is ALSO declared in handoff.schema.json's own per-kind allOf
# (walked generically by _validate_json_schema_node — verified empirically:
# a leg missing repo already fails base shape validation without this rule
# at all). This rule is deliberate defense-in-depth against that schema
# declaration drifting independently of the Python enforcement, the exact
# precedent _cutover_cf_gate_source_kind_enum already sets for a
# schema-declared enum with no vendored schema file backing it. The one
# check with NO schema-level equivalent — the JSON Schema subset this repo
# implements has no array-uniqueness keyword — is leg_id uniqueness; that is
# the genuinely non-redundant half of this rule.
# ---------------------------------------------------------------------------

_GATE_EVIDENCE_LEG_KINDS = frozenset({
    'test-node-id', 'probe-op-key', 'commit-sha', 'sibling-commitment-ref',
    'commit-ancestor', 'file-exists', 'frontmatter-field', 'deadline', 'human',
})
_GATE_EVIDENCE_REPO_EXEMPT_KINDS = frozenset({'human', 'deadline'})
_GATE_EVIDENCE_COMMIT_ANCESTOR_REF_RE = re.compile(r'^[^@\s]+@[^@\s]+$')
_GATE_EVIDENCE_FRONTMATTER_FIELD_REF_RE = re.compile(r'^[^#\s]+#[^#\s]+$')
_GATE_EVIDENCE_DEADLINE_REF_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:\d{2})?)?$'
)


def _cf_gate_evidence_legs_shape(fm: dict) -> ErrorDict | None:
    """gate_evidence.legs[]: kind-enum, per-kind repo/ref/expected/reason/note
    shape, and leg_id uniqueness (C1).

    Mirrors _cutover_cf_verified_by_kind_and_ref_shape's idiom (return on the
    first violation found) and reuses its shared ref-shape dispatch for the
    four kinds carried verbatim from cutover.schema.json — see the module
    section header above for the extend-not-duplicate rationale.
    """
    gate_evidence = fm.get('gate_evidence')
    if not isinstance(gate_evidence, dict):
        return None
    legs = gate_evidence.get('legs')
    if not isinstance(legs, list):
        return None

    seen_leg_ids: set[str] = set()
    for i, leg in enumerate(legs):
        if not isinstance(leg, dict):
            continue

        leg_id = leg.get('leg_id')
        if isinstance(leg_id, str) and leg_id:
            if leg_id in seen_leg_ids:
                return {
                    'field': f'gate_evidence.legs[{i}].leg_id',
                    'error': f'duplicate leg_id "{leg_id}" — leg_id must be unique within legs[]',
                    'hint': (
                        'Give each leg its own leg_id — per-leg result attribution depends '
                        'on uniqueness (the evaluator defaults an absent leg_id to '
                        '"<unknown>"; two defaulted legs would report identically).'
                    ),
                }
            seen_leg_ids.add(leg_id)

        kind = leg.get('kind')
        if kind is None:
            continue
        if kind not in _GATE_EVIDENCE_LEG_KINDS:
            return {
                'field': f'gate_evidence.legs[{i}].kind',
                'error': f'invalid enum value "{kind}"',
                'hint': f'Allowed values: {", ".join(sorted(_GATE_EVIDENCE_LEG_KINDS))}.',
            }

        repo = leg.get('repo')
        if kind not in _GATE_EVIDENCE_REPO_EXEMPT_KINDS and not (isinstance(repo, str) and repo):
            return {
                'field': f'gate_evidence.legs[{i}].repo',
                'error': 'required field missing',
                'hint': (
                    f'kind: {kind} requires repo — a registry key (e.g. doe_claude, '
                    'claude_klabauter), never inferred from context (ruling 6).'
                ),
            }

        ref = leg.get('ref')
        if kind in _CUTOVER_VERIFIED_BY_KINDS:
            shape_error = _verified_by_ref_shape_error(kind, ref)
            if shape_error is not None:
                error, hint = shape_error
                return {'field': f'gate_evidence.legs[{i}].ref', 'error': error, 'hint': hint}
        elif kind == 'commit-ancestor':
            if not isinstance(ref, str) or not _GATE_EVIDENCE_COMMIT_ANCESTOR_REF_RE.match(ref):
                return {
                    'field': f'gate_evidence.legs[{i}].ref',
                    'error': f'ref "{ref}" is not "<commit-ish>@<target-ref>"',
                    'hint': (
                        'kind: commit-ancestor requires ref to name both ends, e.g. '
                        '"abc123@refs/heads/main" — a bare SHA implies an implicit default '
                        'target and re-opens the silent-wrong-tree failure the mandatory '
                        'repo: rule exists to prevent.'
                    ),
                }
        elif kind == 'file-exists':
            if not isinstance(ref, str) or not ref:
                return {
                    'field': f'gate_evidence.legs[{i}].ref',
                    'error': 'ref must be a non-empty repo-relative path',
                    'hint': 'kind: file-exists expects a repo-relative path in ref.',
                }
            expected = leg.get('expected')
            if not isinstance(expected, bool):
                return {
                    'field': f'gate_evidence.legs[{i}].expected',
                    'error': f'expected must be boolean for kind: file-exists (got {expected!r})',
                    'hint': 'file-exists compares the observed existence against a boolean expected.',
                }
            note = leg.get('note')
            if not isinstance(note, str) or not note:
                return {
                    'field': f'gate_evidence.legs[{i}].note',
                    'error': 'required non-empty on kind: file-exists',
                    'hint': (
                        'note must state what the existence check actually proves — '
                        'existence alone is never satisfaction.'
                    ),
                }
        elif kind == 'frontmatter-field':
            if not isinstance(ref, str) or not _GATE_EVIDENCE_FRONTMATTER_FIELD_REF_RE.match(ref):
                return {
                    'field': f'gate_evidence.legs[{i}].ref',
                    'error': f'ref "{ref}" is not "<repo-relative-path>#<field-name>"',
                    'hint': 'kind: frontmatter-field expects exactly one "#", non-empty on both sides.',
                }
            if leg.get('expected') is None:
                return {
                    'field': f'gate_evidence.legs[{i}].expected',
                    'error': 'required non-null scalar for kind: frontmatter-field',
                    'hint': (
                        'A null/absent expected collapses this into file-exists and re-opens '
                        'the existence-is-not-satisfaction trap.'
                    ),
                }
        elif kind == 'deadline':
            if not isinstance(ref, str) or not _GATE_EVIDENCE_DEADLINE_REF_RE.match(ref):
                return {
                    'field': f'gate_evidence.legs[{i}].ref',
                    'error': f'ref "{ref}" is not an absolute ISO-8601 date',
                    'hint': (
                        'kind: deadline expects an absolute ISO-8601 date/date-time, never '
                        'a relative expression ("in 2 weeks", "+7d").'
                    ),
                }
            if repo:
                return {
                    'field': f'gate_evidence.legs[{i}].repo',
                    'error': 'repo is not permitted on kind: deadline',
                    'hint': 'deadline has no sibling I/O — remove repo.',
                }
        elif kind == 'human':
            reason = leg.get('reason')
            if not isinstance(reason, str) or not reason.strip():
                return {
                    'field': f'gate_evidence.legs[{i}].reason',
                    'error': 'required non-empty on kind: human',
                    'hint': 'A reasonless human leg is a hiding place, not honest-indeterminate.',
                }
            if repo or ref is not None:
                return {
                    'field': f'gate_evidence.legs[{i}]',
                    'error': 'repo/ref are not permitted on kind: human',
                    'hint': 'human legs are declared-unresolvable — reason carries the entire justification.',
                }
    return None


# ---------------------------------------------------------------------------
# Cross-field rule — plan-tasks spine row disposition (C2).
#
# plan-tasks.schema.json's x-schema-name is a PER-ROW schema (one task
# object), not a whole-spine schema — a spine row reaches this rule one row
# at a time via validate_frontmatter's normal schema_name dispatch
# (_apply_cross_field_rules), exactly like every other schema registered in
# _CROSS_FIELD_RULES_BY_SCHEMA below. Spec backlink:
# docs/plans/2026-07-27-plan-line-item-resolution-model.md § C2, D2/D3/D4.
# ---------------------------------------------------------------------------

# DR-096 singular-referent pattern for disposition: coded — a single 7-40
# char hex SHA. No ranges, comma-lists, or branch names (D2).
_PLAN_TASKS_CODED_SHA_RE = re.compile(r'^[0-9a-f]{7,40}$')

# Review: code-reviewer (Finding 2) — `_PLAN_TASKS_CLOSED_DISPOSITIONS` was
# deleted here. It had exactly two call sites before this workstream (the
# pm_approved gate and `check_plan_tasks_grouping_approval`'s row-scan); both
# moved to `_PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS` below, and D5's
# grouping logic runs entirely off `_PLAN_TASKS_GROUPING_BY_DISPOSITION`
# (below), which never consulted this set. There was never a second call
# site needing the wide {spun_off, backlogged, wont_do} set; the "closed but
# ungated `spun_off` still needs the wide set for partitioning" premise in
# this workstream's plan/commit message did not hold up against the actual
# call graph. The three-way CLOSED vocabulary (row is no longer open work,
# 'coded' excluded as evidence-of-work-done rather than a scope decision) is
# now just a prose concept, not a frozenset — see
# `_PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS` immediately below for the
# actual pm_approved/grouping-approval gate set.

# Dispositions that actually require pm_approved (D4, as narrowed by DoE's
# 2026-08-05 ruling — cross-repo/inbox/2026-08-05-doe-claude-em-plan-tasks-
# five-exits-ruling.md). `spun_off` was relaxed: moving a row to another
# plan doesn't drop work (nothing leaves the corpus), so there is no scope
# cut for the PM to ratify, and the EM now self-issues it. `backlogged` and
# `wont_do` DO drop work — which of the two is picked is a PM judgment the
# EM does not self-issue, and DoE's PM explicitly retained that call. Do
# not re-widen this to include `spun_off` (or narrow it further) without a
# fresh PM ruling — mirrors plan-tasks.schema.json's own allOf branch
# narrowing the same enum.
_PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS = frozenset({'backlogged', 'wont_do'})

# The GOVERNED leg's own set, widened for `spun_off` on 2026-08-30 when
# plan.schema.json 2.13.0 was vendored and brought `grouping_approvals.
# spun_off` with it (DR-183's claude-klabauter half, unblocked by that key arriving).
# Deliberately a SECOND set rather than one widened in place: the legacy
# per-row `pm_approved` leg above stays `{'backlogged', 'wont_do'}`, because
# widening it retroactively invalidates 16 `spun_off` rows across 10 legacy
# plans written correctly under DoE's 2026-08-05 relaxation, with no repair
# short of forging assent. A governed plan has opted into the block contract
# by carrying the key, so it can author a `spun_off` block; a legacy plan
# cannot. See `check_plan_tasks_grouping_approval`'s docstring.
_PLAN_TASKS_GOVERNED_PM_APPROVAL_GATED_DISPOSITIONS = frozenset(
    {'backlogged', 'wont_do', 'spun_off'}
)


def _is_single_repo_relative_path(ref: str) -> bool:
    """True if `ref` looks like a single repo-relative path.

    DR-096's singular-referent constraint (D2): no comma-lists, no ranges,
    no leading slash (not repo-relative), no '..' traversal, no whitespace
    (which would indicate multiple space-separated entries). Deliberately
    permissive beyond that — this is a shape guard against the exact
    pollution DR-096 documents (SHA ranges, comma-lists, branch names, free
    prose), not a full path-syntax validator.
    """
    if not isinstance(ref, str) or not ref.strip():
        return False
    if ',' in ref or any(c.isspace() for c in ref):
        return False
    if ref.startswith('/'):
        return False
    if '..' in ref:
        return False
    return True


def _cf_plan_tasks_disposition_shape(fm: dict, *, governed: bool = False) -> ErrorDict | None:
    """Hard-reject cross-field validator for one plan-tasks spine row's
    disposition fields — the hard-failing layer of D4's enforcement matrix.
    plan-tasks.schema.json itself stays presence-only/non-hard-failing per
    that same matrix; this function is where a malformed row actually
    rejects.

    Reuses `_cf_disposition_shape` (wrapping the single row `fm` in a
    one-element list) for the shape checks shared with carried_items
    (non-open needs detail, per-token ref requirement/prohibition), then
    layers the checks that have NO handoff analogue:
      - pm_approved is not True on the two PM-GATED dispositions (backlogged /
        wont_do — NOT coded, NOT spun_off; D3: coded is evidence of work
        done, not a scope decision, so it needs no PM approval; spun_off was
        relaxed by DoE's 2026-08-05 ruling, since moving a row to another
        plan drops no work).
      - case_against is non-empty on the same two PM-GATED dispositions
        (backlogged / wont_do — NOT spun_off, NOT coded): a closed scope-cut
        row must carry the argument for the cut on the record, not just the
        approval checkbox. Presence+non-blank only — plan-tasks.schema.json
        1.6.0 already makes the key required via its own allOf conditional;
        this leg is the hard-failing enforcement that schema layer leaves
        non-hard-failing (cross-repo DoE memo, 2026-08-06). Detecting a
        "vacuous" (e.g. strawman) case_against is explicitly out of scope —
        that is not a validator's job.
      - disposition_ref's per-disposition SHAPE (D2): a single 7-40 char hex
        SHA for coded, a single repo-relative path for spun_off/backlogged.

    `governed=True` SUPPRESSES the `pm_approved` leg only (the D2 ref-shape
    leg, the case_against leg, and every `_cf_disposition_shape` leg still
    apply — case_against argues the scope cut itself, independent of which
    authorization mechanism ratified it). On a plan under
    the 2026-07-29 grouping-approval contract, authorization is carried by
    the plan's `grouping_approvals` blocks and checked by
    `check_plan_tasks_grouping_approval`; leaving this leg live there would
    reject every closed row on every governed plan, since governed plans do
    not set the per-row boolean at all.

    Whether a plan is governed is PLAN-scoped knowledge this function cannot
    see — it receives one row. So the flag is passed IN by the source-scoped
    caller that can answer it, and defaults to False, which is today's
    behaviour exactly. The rules-table invocation
    (`_PLAN_TASKS_CROSS_FIELD_RULES`, one positional arg) is unchanged and
    still gets the legacy predicate, which is correct: `validate_frontmatter`
    only ever sees one row's dict and can never know the plan is governed.

    Negative-spec: does NOT implement the ordering lint (closed rows sort
    last) — that is C3's rule, homed separately in this module beside this
    function. Does NOT implement the grouping-approval predicate — that is
    source-scoped and homed beside the ordering lint, for the signature
    reason spelled out in its own banner. Does NOT duplicate any check
    plan-tasks.schema.json's own allOf/if/then already performs as
    presence-only (required-key checks) — this function only adds the checks
    that schema layer deliberately leaves non-hard-failing (see its own
    $comment blocks).
    """
    error = _cf_disposition_shape(
        [fm],
        field_name='disposition',
        open_token='open',
        requires_ref=frozenset({'coded', 'spun_off', 'backlogged'}),
        forbids_ref=frozenset({'wont_do'}),
        detail_exempt=frozenset({'coded'}),
    )
    if error is not None:
        return error

    disposition = fm.get('disposition')
    if disposition in _PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS and not governed:
        if fm.get('pm_approved') is not True:
            return {
                'field': 'pm_approved',
                'error': (
                    f'disposition {disposition!r} requires pm_approved: true '
                    f'(got {fm.get("pm_approved")!r})'
                ),
                'hint': (
                    'backlogged/wont_do are scope decisions and need PM '
                    'ratification (D4, as narrowed 2026-08-05) — coded and '
                    'spun_off do not.'
                ),
            }

    if disposition in _PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS:
        case_against = fm.get('case_against')
        if case_against is None or not str(case_against).strip():
            return {
                'field': 'case_against',
                'error': (
                    f'disposition {disposition!r} requires a non-empty '
                    f'case_against (got {case_against!r})'
                ),
                'hint': (
                    'case_against is the argument for why the work is NOT '
                    'getting done — backlogged/wont_do are scope cuts, and a '
                    'scope cut needs the reasoning on the record, not just '
                    'the PM checkbox (spun_off is excluded: moving a row to '
                    'another plan drops no work, so there is nothing to '
                    'argue against; DoE 2026-08-05 ruling).'
                ),
            }

    ref = fm.get('disposition_ref')
    if disposition == 'coded' and ref is not None:
        if not _PLAN_TASKS_CODED_SHA_RE.match(str(ref)):
            return {
                'field': 'disposition_ref',
                'error': f'coded disposition_ref {ref!r} is not a single 7-40 char hex SHA',
                'hint': (
                    'disposition_ref for coded is a single commit SHA — no ranges, '
                    'comma-lists, or branch names (DR-096).'
                ),
            }
    elif disposition in ('spun_off', 'backlogged') and ref is not None:
        if not _is_single_repo_relative_path(str(ref)):
            return {
                'field': 'disposition_ref',
                'error': f'{disposition} disposition_ref {ref!r} is not a single repo-relative path',
                'hint': (
                    'disposition_ref is a single repo-relative path — no comma-lists or '
                    'ranges (DR-096).'
                ),
            }
    return None


def _is_parseable_iso_date(value: Any) -> bool:
    """True iff `value` is a non-blank string parseable as an ISO-8601 date.

    Deliberately narrow (a calendar date, not an event description) — see
    `_cf_queue_disposition_shape`'s own docstring for the unresolved tension
    this narrowness creates against an event-trigger grant already on disk.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.date.fromisoformat(value.strip())
    except ValueError:
        return False
    return True


# Non-open, non-deferred `status` values across all three queue-family
# schemas (bug-backlog/debt-backlog/improvement-queue) — exempted from
# `_cf_disposition_shape`'s "non-open needs a non-empty detail" leg the same
# way `_cf_plan_tasks_disposition_shape` exempts `coded`: a routine close or
# a bug wontfix is a resolution, not a park, and owes no `why_blocked`.
_QUEUE_STATUS_DETAIL_EXEMPT = frozenset({'closed', 'wontfix'})


def _cf_queue_disposition_shape(
    fm: dict, local_queue_corpus: bool = False
) -> ErrorDict | None:
    """Hard-reject cross-field validator for the queue-family (bug-backlog /
    debt-backlog / improvement-queue) `status: deferred` grant shape — the
    hard-failing sibling to C2's presence-only `allOf` branch vendored onto
    all three schemas (docs/plans/2026-08-27-a-queue-deferral-is-a-grant-
    the-pm-issues.md § C3). That branch requires the five grant fields be
    PRESENT; this function is where a record whose grant is present but
    HOLLOW (`pm_approved: false`, a blank `case_against`, ...) actually
    rejects.

    Reuses `_cf_disposition_shape` (:2077) via field aliasing rather than a
    fresh iterate/reject/require-detail copy, exactly as
    `_cf_plan_tasks_disposition_shape` (:2476) does over the same primitive:
    the synthetic single-entry list passed in maps `status` -> the
    primitive's `disposition` key and `why_blocked` -> its
    `disposition_detail` key (so "a deferred row needs a non-empty
    why_blocked" reuses the primitive's non-open-needs-detail leg, with
    `_QUEUE_STATUS_DETAIL_EXEMPT` standing in for `detail_exempt` so a
    routine `closed`/`wontfix` owes nothing), and `deferred_by` -> its
    `disposition_ref` key (so "a deferred row needs a non-empty deferred_by"
    reuses the primitive's `requires_ref` leg). The primitive's own
    error/hint strings name `disposition_detail`/`disposition_ref` literally;
    those are rewritten to the real field names below rather than left
    pointing at vocabulary the caller never declared.

    Layered on top, with no primitive analogue (mirroring how
    `_cf_plan_tasks_disposition_shape` layers its own `pm_approved`/
    `case_against` checks after its own primitive call):
      - `pm_approved` must be literal `true` — absent and `false` both refuse.
      - `case_against` must be present and non-blank.
      - `deferred_until` must be present and non-blank, in EITHER form: a
        calendar date, or the condition the grantor named.

        THE ISO-DATE-ONLY RULE WAS WITHDRAWN 2026-08-29, and the reason is
        worth keeping. It was authored as specified against a plan that had
        not noticed the case, and it refused a record that was doing the right
        thing: a real PM ruling on disk parks an entry "until a THIRD consumer
        appears", and the record says in its own `deferred_until` that it is
        recording a condition verbatim "rather than a fabricated date". A rule
        that refuses the honest record and would have been satisfied by an
        invented date is pointed the wrong way — it penalises the accuracy it
        exists to protect, and the only way to comply is the fabrication this
        whole plan forbids one field over.

        The real requirement was never "a date". It is that a park comes back.
        A date is one way; a named condition is another, and the mechanism owes
        the reader a backstop for the second — see
        `coordinator_core/orientation/expired_grant_signal.py`, which surfaces
        a condition-form grant once it has sat long enough that nobody is
        plausibly still watching for the condition. Refusal is not the tool for
        this: refusing the write does not make anyone revisit the park, it just
        stops the record being edited.

    Negative-spec: does NOT check WHO granted the deferral — the self-grant
    discriminator needs the authoring session/agent, which lives only in the
    write-guard payload this pure (schema, content) validator never sees;
    that check is C4's, co-located with C4's own independent evaluator. Does
    NOT widen `_cf_disposition_shape` itself to understand `status` or
    `why_blocked` — the aliasing lives entirely in the synthetic entry built
    here, per the primitive's own negative-spec against per-token vocabulary
    creeping into the shared function.

    SCOPED TO CLAUDE-KLABAUTER'S OWN CORPUS, and inert by default — `local_queue_corpus`
    must be passed true or this rule does nothing. DoE-claude imports THIS FILE
    by path (`Path(claude_klabauter_root) / "coordinator_core" / "frontmatter" /
    "schema_validate.py"`, e.g. their `test_artifact_corpus_validates_against_
    schema.py`) with no re-vendor and no version gate, so an unscoped rule here
    changes enforcement on their corpus the instant it lands on our disk. That
    is the seam the version gate exists to prevent, and it is why DoE asked for
    this rule to stay claude-klabauter-scoped and opt in on their own schedule
    (`cross-repo/inbox/2026-08-28-doe-claude-em-doe-schema-branch-already-
    landed-and-scoping-answers.md`; adopted as this plan's C6 decision).

    MEASURED, not supposed — the unscoped first draft was verified to reject a
    real `/debt-triage` Step 6b class 4 park (DoE `19f6b1551`: `pm_approved`,
    `deferred_by: /debt-triage <session-id>`, `deferred_until`, `why_blocked`,
    and NO `case_against`, which their ceremony does not stamp) through DoE's
    own `validate_frontmatter_obj` seam against DoE's own schema object. That
    is Queue Terminus outcome class 4 breaking in another repo — exactly the
    harm C4's external gate was built to prevent, arriving a chunk early
    through a different door.

    The discriminator is the SCHEMA'S provenance, because it is the only one
    this layer has: a record path never reaches a pure (schema, content)
    validator. `validate_frontmatter` passes true only when resolving against
    claude-klabauter's own vendored `_SCHEMAS_DIR`; a caller-supplied foreign schema
    object (`validate_frontmatter_obj`, the seam DoE's port was given) never
    does. Default false is the fail-safe direction: a claude-klabauter path that forgets
    to opt in loses enforcement here and still has C4's write-guard deny above
    it, whereas a default of true silently re-breaks a sibling repo.
    """
    if not local_queue_corpus:
        return None
    status = fm.get('status', 'open')
    error = _cf_disposition_shape(
        [{
            'disposition': status,
            'disposition_detail': fm.get('why_blocked'),
            'disposition_ref': fm.get('deferred_by'),
        }],
        field_name='status',
        open_token='open',
        requires_ref=frozenset({'deferred'}),
        detail_exempt=_QUEUE_STATUS_DETAIL_EXEMPT,
    )
    if error is not None:
        error = dict(error)
        error['error'] = (
            error['error']
            .replace('disposition_detail', 'why_blocked')
            .replace('disposition_ref', 'deferred_by')
        )
        error['hint'] = (
            error['hint']
            .replace('disposition_detail', 'why_blocked')
            .replace('disposition_ref', 'deferred_by')
        )
        error['field'] = 'why_blocked' if 'why_blocked' in error['error'] else 'deferred_by'
        return error

    if status != 'deferred':
        return None

    if fm.get('pm_approved') is not True:
        return {
            'field': 'pm_approved',
            'error': (
                f"status 'deferred' requires pm_approved: true (got "
                f"{fm.get('pm_approved')!r})"
            ),
            'hint': (
                'a queue deferral is a grant the PM issues, not a status an '
                'EM types — pm_approved must be the literal boolean true.'
            ),
        }

    case_against = fm.get('case_against')
    if case_against is None or not str(case_against).strip():
        return {
            'field': 'case_against',
            'error': (
                f"status 'deferred' requires a non-empty case_against "
                f"(got {case_against!r})"
            ),
            'hint': (
                'case_against is the argument that lost — a deferral without '
                'it is a park with no record of what was weighed against it.'
            ),
        }

    deferred_until = fm.get('deferred_until')
    if deferred_until is None or not str(deferred_until).strip():
        return {
            'field': 'deferred_until',
            'error': (
                f"status 'deferred' requires a non-empty deferred_until "
                f"(got {deferred_until!r})"
            ),
            'hint': (
                'deferred_until says when the park comes back: a calendar date '
                '(YYYY-MM-DD), or the condition the grantor named. Either is '
                'accepted; nothing is not.'
            ),
        }
    return None


# `writes` landed as an optional field on plan-tasks.schema.json 1.7.0
# (2026-07-19-pcli-phase2-stubs-claude-klabauter-contracts.md C2). Enforcement below is
# going-forward only, gated on THIS date — the date `_cf_plan_tasks_writes_declared`
# itself was wired in, not the field's own schema-bump date. A plan created
# before this cutoff was authored under a regime where declaring `writes` was
# not yet an expectation; retro-fitting ~any pre-existing open row would turn
# an ordinary close-out write into a rejection for a gap the author had no
# way to know about. Mirrors `_cf_category_required_post_cutoff` /
# `_cf_summary_required_post_cutoff`'s going-forward-cutoff idiom.
_PLAN_TASKS_WRITES_REQUIRED_CUTOFF = '2026-08-19'


def _cf_plan_tasks_writes_declared(
    row: dict, *, plan_created: str | None = None, governed: bool = False
) -> ErrorDict | None:
    """Hard-reject cross-field validator: a non-deferred, OPEN plan-tasks row
    must declare `writes` — the key must be PRESENT, not necessarily
    non-empty. Sibling to `_cf_plan_tasks_disposition_shape` above, same
    per-row schema, registered alongside it in `_PLAN_TASKS_CROSS_FIELD_RULES`.

    Fires only when BOTH:
      - the row is non-deferred (`deferred` absent or false — a deferred row
        is a harvest candidate, not a dispatch candidate, and has nothing to
        declare yet), AND
      - `disposition` is `open` (absent, per the schema default, counts as
        open) — a coded/spun_off/backlogged/wont_do row is history, and
        history does not retroactively acquire a write-surface declaration.

    Fails on an ABSENT `writes` key OR a present-but-null value (`writes:`
    with nothing after it, or `writes: null`) — matching `spine_read`'s AC2
    UNDECLARED predicate exactly, not mere key presence — with ONE carve-out:
    a row carrying a `depends_on` edge whose `gate_kind` is `epistemic-premise`
    passes with `writes` undeclared.

    NEGATIVE SPEC — do not "simplify" this into "an absent key is always an
    error, tell the author to write `writes: []`". That inverts the two
    values' meanings and was the first version of this rule.
    `spine_read`'s AC2 is the authority: an ABSENT key yields the UNDECLARED
    sentinel, meaning "unknown, treat as colliding with everything", which is
    what forces wave separation; `writes: []` is a structurally different
    value meaning the positive claim "writes nothing". They are not
    interchangeable, and steering an author with an unknown surface toward
    `writes: []` is actively harmful: `pathspec.commit_pathspec` drops a
    declared-empty row from a SHARED wave's pathspec silently, so its
    executor's work never reaches a commit.

    An `epistemic-premise` edge is precisely the case where the surface
    cannot be known yet — the schema defines that gate_kind as "the
    predecessor decides whether this row should exist at all, so there is no
    interface to pin and nothing to author against until its verdict lands".
    The same discriminator holds this row out of the wave graph in
    `wave_map._compute_held_out`; the two must agree, so change them
    together or not at all.

    What this rule actually catches, then, is the un-alibi'd omission: a row
    with no write surface declared and no epistemic gate justifying the
    silence — the shape that reaches `dispatch.emit` and cannot be fired.

    `plan_created` is PLAN-scoped context a lone row cannot derive from
    itself — the same `rule_kwargs` forwarding seam `governed=` already uses
    (see `_apply_cross_field_rules`'s docstring). A caller that does not
    forward it (or forwards `None`) gets NO enforcement from this rule, by
    design: this function cannot distinguish "the plan really has no
    created date" from "the caller never wired plan_created through," and
    both must read as "cannot confirm this plan is post-cutoff" — the safe
    default given the retro-safety concern above, mirroring how
    `_apply_cross_field_rules` treats an unforwarded `governed` as its safe
    default (False) rather than guessing. `check_plan_tasks_source` (below)
    and both write guards' `_plan_tasks_spine_errors`
    (`validate_frontmatter_schema_deny.py`,
    `validate_frontmatter_schema_advisory.py`) now forward it from the plan's
    own frontmatter, so an unforwarded call is the exception case, not the
    normal one — the safe default above still matters for any other caller
    that invokes this rule without threading `plan_created` through.

    `governed` is accepted (unused) only so `_apply_cross_field_rules`'s
    kwarg-filter-by-declared-param mechanism does not need this function to
    special-case its absence — declaring it costs nothing and keeps the
    signature uniform with its sibling rule.
    """
    if row.get('deferred') is True:
        return None
    disposition = row.get('disposition', 'open')
    if disposition != 'open':
        return None
    if not plan_created or str(plan_created) < _PLAN_TASKS_WRITES_REQUIRED_CUTOFF:
        return None
    if row.get('writes') is not None:
        return None
    depends_on = row.get('depends_on')
    if isinstance(depends_on, list) and any(
        isinstance(edge, dict) and edge.get('gate_kind') == 'epistemic-premise'
        for edge in depends_on
    ):
        return None
    row_label = row.get('id') or '(row)'
    return {
        'field': 'writes',
        'error': f"plan-tasks row {row_label}: 'writes' is required on a non-deferred open row.",
        'hint': (
            "List the paths this chunk writes. Omit 'writes' only on a row gated "
            "'epistemic-premise', whose surface a predecessor names; 'writes: []' "
            "means it writes nothing."
        ),
    }


# ---------------------------------------------------------------------------
# Ordering lint — closed rows sort to the bottom of the spine (D5, C3).
#
# This is NOT a per-row cross-field rule. `_cf_plan_tasks_disposition_shape`
# above validates ONE row at a time — plan-tasks.schema.json's
# x-schema-name is a per-row schema, dispatched one row per call by
# `_apply_cross_field_rules` (see the "Cross-field rule — plan-tasks spine
# row disposition (C2)" banner above). Ordering is a WHOLE-SPINE property no
# single row can answer on its own, so this lint takes a plan's raw markdown
# SOURCE, locates the fenced `` ```yaml plan-tasks `` block itself (reusing
# `coordinator_core.frontmatter.body_blocks.locate_fenced_block` — never a
# fresh parser; see that module's docstring for the two documented
# fenced-block traps this reuse avoids, and this plan's Anti-scope section),
# and inspects row ORDER rather than any one row's fields.
#
# Consequently it is NOT registered in `_PLAN_TASKS_CROSS_FIELD_RULES` /
# `_CROSS_FIELD_RULES_BY_SCHEMA` and is never reached by
# `validate_frontmatter` (which only ever sees one row's frontmatter dict,
# never the plan's full source text). Callers needing this check import and
# call `check_plan_tasks_ordering` directly against a plan's full source
# text.
#
# WIRED CALLER (2026-07-29): `coordinator_core.ops.plan_tasks_mutate._resolve`,
# which runs this against the spine's CURRENT on-disk text before writing a
# new disposition — a precondition on the existing order, so resolve cannot
# compound an already-invalid spine. Until that wiring this function had zero
# production callers and had never rejected anything.
#
# The banner previously also named "C6's plan-coverage-checker" as an
# intended caller. That was never implementable and the expectation is
# withdrawn, not pending: plan-coverage-checker is a markdown agent prompt
# in DoE-claude (`coordinator/agents/plan-coverage-checker.md`), not a
# Python module, so it cannot import or call anything. Surfacing this lint
# to that agent needs something executable to run it and hand the agent a
# result — a design question nobody has taken, deliberately left unclaimed
# here rather than named as though it were merely unwired.
# (Raised by doe-claude-em, 2026-07-29.)
#
# Negative-spec: does NOT validate row SHAPE (missing fields, bad
# disposition_ref, missing pm_approved) — that is
# `_cf_plan_tasks_disposition_shape`'s surface, applied per row elsewhere in
# this same pipeline. Does NOT flag a spine whose fenced block is ABSENT or
# MALFORMED (per `body_blocks.LocateStatus`) — locating/parsing the fence
# correctly is `locate_fenced_block`'s and its other callers' concern; this
# lint answers exactly one question (is the row order valid) and answers
# "nothing to check" rather than raising when the spine itself can't be
# read cleanly.
#
# Spec backlink: docs/plans/2026-07-27-plan-line-item-resolution-model.md
# § C3, D5.
# ---------------------------------------------------------------------------

def _plan_tasks_row_disposition(row: dict) -> str:
    """Row's disposition, defaulting to 'open' per the schema default (D1).

    Mirrors `coordinator_core.ops.plan_tasks_render._disposition` —
    duplicated rather than imported. `plan_tasks_render.py` is an `ops/`
    module that itself depends on `frontmatter/body_blocks.py`; importing
    it back FROM `frontmatter/schema_validate.py` would invert that
    layering (frontmatter is the lower-level module here) for one
    three-line helper, which is not worth the coupling.
    """
    value = row.get('disposition')
    return value if isinstance(value, str) and value else 'open'


def check_plan_tasks_ordering(source: str) -> ErrorDict | None:
    """Ordering lint (D5): fails a plan whose `` ```yaml plan-tasks `` spine
    has a row from a LATER grouping before a row from an EARLIER one.

    Valid shape: every `do` row precedes every `spun_off` row, which
    precedes every `defer` row, which precedes every `ruled_out` row (C3,
    2026-08-05: `spun_off` split out of `defer` into its own grouping — see
    `_PLAN_TASKS_GROUPING_BY_DISPOSITION`'s own comment for why). Rows may
    appear in any relative order WITHIN `spun_off`, `defer`, and
    `ruled_out`; the ONE exception is a sub-order inside `do` itself (DoE's
    finding 2, 2026-07-29): every `open` row must precede every `coded`
    row. Groupings are derived from `disposition` and stored nowhere — see
    `_PLAN_TASKS_GROUPING_BY_DISPOSITION`; the `do` sub-order is
    `_PLAN_TASKS_SUBORDER_BY_DISPOSITION`.

    Rank is a `(band_rank, sub_rank)` tuple, and Python tuple comparison
    does the rest: a later band always outranks an earlier one regardless
    of sub_rank, and within the same band only sub_rank decides. This is
    deliberately the same function widened twice (first from a two-group
    to a three-group partition, now with a sub-rank) rather than gaining a
    second, independent placement checker beside it: two placement
    authorities over the same spine can disagree, and the disagreement
    surfaces as an unfixable plan (each checker demanding an order the
    other rejects).

    Returns `None` (nothing to check) when the fenced block is absent,
    malformed, fails to parse as YAML, or does not parse to a list of
    mapping rows — see the negative-spec in the banner above this function.
    """
    rows = _plan_tasks_spine_rows(source)
    if rows is None:
        return None

    seen_rank = (-1, -1)
    seen_id: Any = None
    seen_grouping = ''
    seen_disposition = ''
    for row in rows:
        row_id = row.get('id', '?')
        disposition = _plan_tasks_row_disposition(row)
        grouping = _plan_tasks_row_grouping(row)
        rank = (
            _PLAN_TASKS_GROUPING_ORDER.index(grouping),
            _PLAN_TASKS_SUBORDER_BY_DISPOSITION.get(disposition, 0),
        )
        if rank < seen_rank:
            if rank[0] == seen_rank[0]:
                return {
                    'field': 'plan-tasks',
                    'error': (
                        f'row {row_id!r} is disposition {disposition!r} but '
                        f'appears after row {seen_id!r} (disposition '
                        f'{seen_disposition!r}) within the {grouping!r} '
                        f'grouping — an open row must sort above a coded '
                        f'row inside do (D5 sub-order)'
                    ),
                    'hint': (
                        "Within the ```yaml plan-tasks``` `do` grouping, "
                        "every row with disposition 'open' must precede "
                        "every row with disposition 'coded' — live work "
                        "reads first, shipped work sinks, same as the "
                        "band partition one level up."
                    ),
                }
            return {
                'field': 'plan-tasks',
                'error': (
                    f'row {row_id!r} is in grouping {grouping!r} but appears '
                    f'after row {seen_id!r} in grouping {seen_grouping!r} — '
                    f'the spine must sort do, then spun_off, then defer, '
                    f'then ruled_out (D5)'
                ),
                'hint': (
                    "Order the ```yaml plan-tasks``` block so every 'do' row "
                    "(disposition open/coded) precedes every 'spun_off' row, "
                    "which precedes every 'defer' row (disposition "
                    "backlogged), which precedes every 'ruled_out' row "
                    "(wont_do) — the spine reads top-down live-work-first "
                    "for humans and head/tail for the sidecar "
                    "unresolved-head projection."
                ),
            }
        if rank > seen_rank:
            seen_rank = rank
            seen_id = row_id
            seen_grouping = grouping
            seen_disposition = disposition
    return None


# ---------------------------------------------------------------------------
# Grouping approval — the authorization predicate, at SOURCE scope (2026-07-29).
#
# Contract: cross-repo/archive/2026-07-29-doe-claude-em-grouping-approval-contract.md
# (actioned; moved from inbox/ to archive/ — see line ~2411 below), as amended
# by our reply (DoE-claude
# cross-repo/inbox/2026-07-29-claude-klabauter-em-grouping-approval-contract-confirmed.md).
#
# WHY THIS IS NOT A PER-ROW CROSS-FIELD RULE. DoE's memo asked us to extend
# `_cf_plan_tasks_disposition_shape` above. We could not, and said so. That
# function receives ONE spine row and is registered in
# `_PLAN_TASKS_CROSS_FIELD_RULES`, a table whose fixed
# `(fm) -> ErrorDict | None` signature is shared with the ~30-entry
# `_HANDOFF_CROSS_FIELD_RULES` immediately below it. This predicate needs two
# things no single row carries: the PLAN's frontmatter (to answer governed vs
# legacy) and the FULL spine membership (to recompute a grouping digest).
# Threading either through would change the invocation contract for both
# tables — a far larger blast radius than the check warrants. So it lives
# here, at source scope, beside the ordering lint that already parses the
# whole plan for the same reason.
#
# The memo's other half of that instruction WAS right and is honoured: the
# shared `_cf_disposition_shape` (:1482) is NOT extended. Its docstring
# disclaims any `pm_approved` opinion precisely because `carried_items` calls
# it, and extending it would break the handoff write path.
#
# Negative-spec: does NOT gate `disposition: coded` — a shipped row is
# evidence of work done, not a scope decision; the `do` grouping is approved
# as a block at plan-review altitude, never checked per row at write time.
# Does NOT accept an approved grouping as sufficient on its own —
# `disposition_detail` remains separately required, because a closed row
# needs an excellent REASON (disposition_detail) and recorded ASSENT (the
# grouping block), two distinct requirements in two distinct slots.
# ---------------------------------------------------------------------------

_PLAN_TASKS_GROUPING_ORDER = ('do', 'spun_off', 'defer', 'ruled_out')

# Membership is DERIVED from `disposition` and stored nowhere. There is no
# field anywhere that places a row in a grouping independently of its
# disposition, and that is the design invariant, not an implementation
# detail: "move this row into the deferred grouping" is not an action that
# exists. The only way a row reaches `defer` is to set a closed disposition
# on it — which is already the write every gate here guards. A selection UI
# can REQUEST that a row be closed; it cannot place a row in a grouping
# without also making the one gated write.
#
# `spun_off` occupies its OWN grouping (C3, 2026-08-05), split out of
# `defer`: DoE's 2026-08-05 ruling relaxed spun_off's pm_approved gate
# (moving a row to another plan drops no work), and leaving it lumped in
# with `backlogged` under one digest would flood the one grouping digest
# the PM actually needs to read with cheap, ungated moves, camouflaging the
# backlogged rows that still need a PM word. Placed between 'do' and
# 'defer' in `_PLAN_TASKS_GROUPING_ORDER` above — spun_off is ungated, like
# `do`, but it IS a closed disposition (row is no longer open work), so it
# sorts after all live/shipped work and before the two PM-gated groupings,
# matching D5's cheapest-exit-first ordering intent.
_PLAN_TASKS_GROUPING_BY_DISPOSITION = {
    'open': 'do',
    'coded': 'do',
    'spun_off': 'spun_off',
    'backlogged': 'defer',
    'wont_do': 'ruled_out',
}

# Sub-order WITHIN the `do` grouping only (2026-07-29, DoE's finding 2):
# `open` must sort above `coded` — live work reads first, shipped work
# sinks, the same principle the three-band partition already applies one
# level up. Every disposition not named here defaults to 0 via `.get`, so
# `defer` and `ruled_out` stay unordered internally; only `do` has more
# than one disposition mapped into it, so it is the only band a sub-order
# can mean anything for.
_PLAN_TASKS_SUBORDER_BY_DISPOSITION = {
    'open': 0,
    'coded': 1,
}

_GROUPING_DIGEST_RE = re.compile(r'^sha256:[0-9a-f]{64}$')

# Every refusal below routes here. It names the ONE correct next action —
# ask the PM — and deliberately supplies no command, no CLI invocation, and
# no field name to satisfy. That is not a style preference: the defect this
# whole contract exists to fix was a gate that printed its own key
# (`ops/plan_tasks_mutate.py`'s `_PM_APPROVAL_OFFER`, deleted with this
# change), whose refusal text was correct and whose offer handed over the
# command that defeated it. A message that reads as a missing-field nit
# teaches a well-meaning EM to satisfy the field, reproducing that same
# failure one layer up. The better alternative on offer is: go get a
# decision.
_GROUPING_APPROVAL_HINT = (
    'Ask the PM to approve this grouping. Which work gets cut is the PM\'s '
    'call, not the authoring agent\'s — there is deliberately no command '
    'that satisfies this from inside the session. Present the cut-set (the '
    'rows in this grouping and why each one is being closed) and record the '
    'PM\'s own words as pm_utterance when they approve it.'
)


def _plan_tasks_spine_rows(source: str) -> list[dict] | None:
    """The plan's spine rows, or None when the spine cannot be read cleanly.

    Shared by the ordering lint and the approval predicate so the two never
    disagree about what the spine contains. Returns None (rather than
    raising) when the fenced block is absent, malformed, fails to parse as
    YAML, or does not parse to a list of mapping rows — locating and parsing
    the fence correctly is `locate_fenced_block`'s concern, not these
    lints'.
    """
    result = locate_fenced_block(source)
    if result.status is not LocateStatus.LOCATED:
        return None

    try:
        rows = yaml.safe_load(result.body) or []
    except yaml.YAMLError:
        return None

    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return None
    return rows


def _plan_tasks_row_grouping(row: dict) -> str:
    """Which grouping this row belongs to, derived from its disposition.

    An unrecognized disposition maps to 'do' — the ungated grouping. That is
    the safe direction for a LINT: an unknown token is a schema-validity
    problem for the enum check to report in its own voice, and treating it
    as gated here would refuse a row for the wrong reason and send the
    author to the PM over what is really a typo.
    """
    return _PLAN_TASKS_GROUPING_BY_DISPOSITION.get(
        _plan_tasks_row_disposition(row), 'do'
    )


def compute_grouping_digest(rows: list[dict], grouping: str) -> str:
    """`sha256:<hex>` over the sorted set of `(row id, disposition)` pairs for
    the rows currently in `grouping`, and nothing else.

    The `sha256:` prefix is load-bearing, never bare 40-hex. A membership
    digest names nothing in any object store, so the ONLY operation that
    consumes it is recompute-and-compare. Emitting bare hex would put a
    cheap, syntactically valid, WRONG question ("does this resolve to a real
    git object?") right next to the correct one — which is exactly the
    substitution that passed review in the `execution_authorized_sha`
    pattern this design was modelled on. A confused reader who tries
    `git cat-file` on a prefixed digest gets nothing, rather than a
    misleadingly plausible answer.

    Negative-spec — the digest does NOT cover:
      - row ORDER in the fence (it is a sorted SET, not a sequence);
      - row body prose, or the plan's prose anywhere;
      - section rendering;
      - any other frontmatter field on the row (owner, detail, refs, and
        `pm_approved` itself all move without touching it);
      - whitespace or YAML-style differences.

    Consequently: reordering rows, reformatting a section, or editing an
    unrelated row's prose all leave EVERY grouping's digest unchanged.
    Adding, removing, or re-dispositioning a single row changes exactly the
    digest(s) of the grouping(s) whose membership that row touches, and no
    others.

    Residual NOT covered: `id` carries no character-set constraint at the
    schema layer (`plan-tasks.schema.json`'s `id` is `type: string,
    minLength: 1`, no pattern), and the payload uses `\x00`/`\n` as
    delimiters — so a crafted id containing either byte could in principle
    make two distinct membership sets serialize to the same payload (e.g.
    `id: "C1\nC2\x00wont_do"` colliding with separate rows `C1` and `C2`
    sharing that disposition). Accepted today, not fixed: ids are
    PM/author-typed short slugs in a cooperative-authorship context, not
    adversarial input, and the digest recipe is a ratified cross-repo
    contract mirrored byte-for-byte in a sibling repo — tightening the `id`
    pattern is a bilateral decision, not this repo's to make unilaterally.

    That narrow scope is the point. Whole-body digests (the
    `execution_authorized_sha` recipe) are correct at plan-execution
    altitude but actively wrong here: an approval that expires because
    someone edited an unrelated chunk trains a re-stamp reflex, and the
    reflex is dangerous precisely because it is SANCTIONED procedure — an EM
    who widens the cut-set and re-stamps in the same motion looks identical
    from outside to one following the book. Scoping to membership alone
    makes any mismatch ALWAYS substantive, because there is nothing else the
    digest could have reacted to, so there is no "just a reformat, re-stamp
    it" path to hide a widened cut-set inside.
    """
    if grouping not in _PLAN_TASKS_GROUPING_ORDER:
        raise ValueError(
            f'unknown grouping {grouping!r} — expected one of '
            f'{_PLAN_TASKS_GROUPING_ORDER}'
        )

    members = {
        (str(row.get('id', '')), _plan_tasks_row_disposition(row))
        for row in rows
        if _plan_tasks_row_grouping(row) == grouping
    }
    payload = '\n'.join(f'{row_id}\x00{disposition}' for row_id, disposition in sorted(members))
    return 'sha256:' + hashlib.sha256(payload.encode('utf-8')).hexdigest()


def is_governed_plan(fm: dict) -> bool:
    """True when this plan's frontmatter puts it under the grouping-approval
    contract; False means LEGACY and today's per-row `pm_approved` gate.

    The discriminator is bare presence of the `grouping_approvals` KEY —
    nothing else. This deliberately does NOT require a `schema_version`
    conjunct. An earlier version of this predicate required BOTH the key
    AND `schema_version >= (1, 2)`, reasoning that presence alone would let
    a plan written against an older version read as
    governed-with-three-empty-blocks. That reasoning had no producer: no
    plan schema in this repo declares `schema_version` at all, so the
    version leg was always `None` and always failed — `is_governed_plan`
    returned False for EVERY plan, including one carrying a fully populated,
    PM-approved `grouping_approvals` block. The gate could never fire. See
    cross-repo/inbox/2026-07-29-doe-claude-em-grouping-discriminator-correction.md
    and the ratified contract text at
    cross-repo/archive/2026-07-29-doe-claude-em-grouping-approval-contract.md:84.

    Presence is sufficient on its own because it is non-forgeable in both
    directions: a plan cannot claim legacy while carrying the key, and
    cannot silently lose governance by omitting an unrelated field.

    A plan that carries the key with a MALFORMED value (not a mapping of
    the three groupings, or missing the block a closed row needs) is a
    malformed GOVERNED plan, not a legacy one — it does NOT fall back to
    the per-row `pm_approved` gate. `check_plan_tasks_grouping_approval`
    fails loud on that shape rather than returning None.
    """
    return 'grouping_approvals' in fm


def check_plan_tasks_grouping_approval(source: str) -> ErrorDict | None:
    """Authorization predicate for PM-GATED spine rows on a GOVERNED plan.

    A row with a PM-gated disposition (`backlogged` | `wont_do` —
    `_PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS`) is admissible if and only
    if BOTH:
      1. the block for that row's grouping — `defer` for backlogged,
         `ruled_out` for wont_do — reads `status: approved`; AND
      2. that block's recorded `digest` equals a FRESH recomputation over
         the plan's current spine membership for that grouping.

    `spun_off` IS gated here as of 2026-08-30, when plan.schema.json 2.13.0
    was vendored carrying `grouping_approvals.spun_off` — the fourth key DR-183
    (2026-08-29) waited on, reversing the 2026-08-05 ruling that "the EM
    self-issues it now". Until that key existed the gate could not be built:
    `spun_off` occupies its own grouping (C3), `grouping_approvals` declared
    only `do`/`defer`/`ruled_out` under `additionalProperties: false`, and
    gating it then would have rejected every governed plan with a `spun_off`
    row with no author-side remedy — approving the grouping impossible,
    authoring the block itself a schema violation. The key could not originate
    here either: plan.schema.json is vendored byte-for-byte and
    `check_schema_drift` enforces that parity with no pin or tolerance escape,
    so DoE-claude authored and bumped it and claude-klabauter re-vendored.

    The widen is GOVERNED-only: `spun_off` joins the set this check scans
    (`_PLAN_TASKS_GOVERNED_PM_APPROVAL_GATED_DISPOSITIONS`), while
    `_PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS` (the LEGACY per-row
    `pm_approved` leg in `_cf_plan_tasks_disposition_shape`) stays
    `{'backlogged', 'wont_do'}`. That is deliberately two sets, not one set
    widened in one place; see the note on them above.

    WHY THE LEGACY LEG DOES NOT WIDEN. DoE's DR-183 ask included the legacy
    branch (plan-tasks.schema.json `allOf[5]`), and taking it would
    retroactively invalidate 16 `spun_off` rows across 10 legacy plans under
    docs/plans/ — 7 of them still live. Those rows were written correctly
    under the rules then in force (DoE's own 2026-08-05 five-exits
    relaxation). The only way to make them valid again is to write
    `pm_approved: true` onto rows no PM ever approved, which forges assent in
    the exact field DR-183 exists to make meaningful; the alternative is
    leaving live plans red for a rule they could not have followed. Neither
    is a migration, so neither is taken.

    This is not a carve-out invented for the occasion — it is the
    grouping-approval contract's own migration model, stated three paragraphs
    down and enforced at the `is_governed_plan` boundary: a plan enters this
    contract by ACQUIRING the block, and "authoring the block IS the
    migration event". Every affected plan is legacy, i.e. pre-contract by
    construction. DR-183 binds forward, at the boundary where the contract
    already says governance begins.

    Tripwire: test_plan_tasks_grouping_approval.py ::
    TestSpunOffGateIsBlockedOnTheFourthGroupingKey.

    On a governed plan there is NO partial legacy tolerance for any row,
    including rows that predate the block — authoring the block IS the
    migration event.

    Returns None on a LEGACY plan (no `grouping_approvals` key at all —
    see `is_governed_plan`): that corpus keeps the per-row `pm_approved is
    not True` check in `_cf_plan_tasks_disposition_shape`, untouched. Also
    returns None when the spine cannot be read cleanly, per
    `_plan_tasks_spine_rows`.

    A GOVERNED plan whose `grouping_approvals` value is not itself a
    mapping of the three groupings never falls back to None/legacy — that
    would silently readmit the per-row gate on a plan that has already
    opted into the block contract. It fails loud instead, naming the
    shape defect, same as a missing or unapproved per-grouping block does
    below.

    Plan-level approval is the SUM of the three block statuses, computed at
    read time here and never stored — a stored rollup would be a second home
    for a derivable truth, and the two would drift.
    """
    parsed = parse_frontmatter(source)
    fm = parsed.get('frontmatter')
    if not isinstance(fm, dict) or not is_governed_plan(fm):
        return None

    rows = _plan_tasks_spine_rows(source)
    if rows is None:
        return None

    blocks = fm['grouping_approvals']
    if not isinstance(blocks, dict):
        return {
            'field': 'grouping_approvals',
            'error': (
                f'grouping_approvals is present but is a '
                f'{type(blocks).__name__}, not a mapping of do/defer/'
                f'ruled_out blocks — this plan has already opted into the '
                f'grouping-approval contract by carrying the key, so it '
                f'cannot fall back to the legacy per-row pm_approved gate; '
                f'authoring and approving the block correctly IS the '
                f'migration event'
            ),
            'hint': _GROUPING_APPROVAL_HINT,
        }

    for row in rows:
        disposition = _plan_tasks_row_disposition(row)
        if disposition not in _PLAN_TASKS_GOVERNED_PM_APPROVAL_GATED_DISPOSITIONS:
            continue

        row_id = row.get('id', '?')
        grouping = _plan_tasks_row_grouping(row)
        block = blocks.get(grouping)

        if not isinstance(block, dict) or block.get('status') != 'approved':
            status = block.get('status', 'pending') if isinstance(block, dict) else 'absent'
            return {
                'field': f'grouping_approvals.{grouping}',
                'error': (
                    f'row {row_id!r} is disposition {disposition!r}, which puts '
                    f'it in the {grouping!r} grouping, but that grouping reads '
                    f'status {status!r} — closing a row is a scope decision and '
                    f'needs the PM\'s recorded assent before it can stand'
                ),
                'hint': _GROUPING_APPROVAL_HINT,
            }

        recorded = block.get('digest')
        if not isinstance(recorded, str) or not _GROUPING_DIGEST_RE.match(recorded):
            return {
                'field': f'grouping_approvals.{grouping}.digest',
                'error': (
                    f'grouping {grouping!r} is approved but its digest '
                    f'{recorded!r} is not a well-formed sha256:<64-hex> '
                    f'membership digest'
                ),
                'hint': (
                    'The digest must carry its sha256: prefix — a bare hex '
                    'string reads as a git object id, which this value is '
                    'not and can never be resolved as. Re-approving the '
                    'grouping recomputes it correctly. ' + _GROUPING_APPROVAL_HINT
                ),
            }

        fresh = compute_grouping_digest(rows, grouping)
        if recorded != fresh:
            return {
                'field': f'grouping_approvals.{grouping}.digest',
                'error': (
                    f'grouping {grouping!r} was approved over a different '
                    f'cut-set than the spine now holds (recorded {recorded}, '
                    f'current {fresh}) — row {row_id!r} is inside that '
                    f'grouping, so its closure is not covered by the '
                    f'recorded approval'
                ),
                'hint': (
                    'This digest covers ONLY the (row id, disposition) pairs '
                    'in this grouping, so a mismatch is always substantive: '
                    'rows were added, removed, or re-dispositioned since the '
                    'PM approved it. There is no reformat or reordering that '
                    'could have caused this, and therefore nothing to '
                    're-stamp past. ' + _GROUPING_APPROVAL_HINT
                ),
            }
    return None


def check_plan_tasks_source(source: str) -> ErrorDict | None:
    """Every plan-tasks check that needs the plan's full SOURCE, in one door.

    Runs, in order: the ordering lint, the grouping-approval predicate, then
    per-row validation (base JSON-Schema shape, THEN the cross-field rules)
    with the governed flag resolved from the plan's own frontmatter and
    applied to BOTH legs. Returns the first error, or None.

    This exists so callers cannot accidentally run the row rules WITHOUT the
    governed flag while believing they validated a governed plan — the row
    rules default to the legacy predicate (correctly, since a bare row dict
    can never know better on its own), so a source-scoped caller that
    reached for them directly would reject every closed row on every
    governed plan. One door removes that trap rather than documenting it.

    EXTENDED 2026-07-29 (write-guard-bypass fix): this door used to run only
    the cross-field leg and defer base shape validation to
    `validate_frontmatter`'s own per-row call — but that left BOTH write
    guards (`validate_frontmatter_schema_deny.py` and
    `..._advisory.py`) calling the raw, ungoverned
    `_PLAN_TASKS_SCHEMA_DICT` (with its `pm_approved`-required `allOf`
    branches still live) directly, alongside `_apply_cross_field_rules`
    called with no `governed=` kwarg at all (silently defaulting False).
    Reproduced empirically: a fully-approved governed plan with a
    `spun_off` row and no `pm_approved` key produced TWO spurious errors —
    one from the schema's own `required: [pm_approved]` branch, one from
    the cross-field rule's ungoverned default — even though
    `check_plan_tasks_grouping_approval` had already cleared the row.

    NOT YET TRUE, correction as of code review 2026-07-29: neither write
    guard actually calls this door. `validate_frontmatter_schema_deny.py`'s
    `_plan_tasks_spine_errors` and the advisory sibling's mirror of the same
    name each independently reimplement this function's row-loop body —
    filtering the schema and calling `_apply_cross_field_rules(...,
    governed=governed)` themselves — rather than calling
    `check_plan_tasks_source`. What IS true: both guards import and share
    the low-level primitives this door composes
    (`_plan_tasks_schema_without_pm_approved_required`, `is_governed_plan`,
    `_apply_cross_field_rules`), so the *meaning of "governed" for a row*
    cannot drift between them. What is NOT true: the
    ordering-then-grouping-then-per-row *sequence* this door encodes is
    hand-duplicated in three places (here, and in each guard's
    `_plan_tasks_spine_errors`), and nothing enforces the three stay in
    lockstep — the same class of split this door exists to prevent, one
    level up. Rewiring both guards to call this door directly is the open
    follow-up; it was not done in the 2026-07-29 slice because the guards'
    return shape (`list[dict]` with per-row `tasks[id].field` labelling) does
    not fit this door's single-`ErrorDict`-or-`None` contract, and reshaping
    that contract was judged out of scope for that change.

    Negative-spec: does NOT replace `validate_frontmatter` for schemas OTHER
    than plan-tasks, and does not change `validate_frontmatter`'s own
    behaviour for plan-tasks rows reached through some other caller — this
    function is the door for SOURCE-scoped plan callers (the write guards,
    the mutate op) specifically.
    """
    error = check_plan_tasks_ordering(source)
    if error is not None:
        return error

    error = check_plan_tasks_grouping_approval(source)
    if error is not None:
        return error

    parsed = parse_frontmatter(source)
    fm = parsed.get('frontmatter')
    governed = is_governed_plan(fm) if isinstance(fm, dict) else False

    rows = _plan_tasks_spine_rows(source)
    if rows is None:
        return None

    schema = _PLAN_TASKS_SCHEMA_GOVERNED_DICT if governed else _PLAN_TASKS_SCHEMA_DICT
    for row in rows:
        shape_errors = _validate_json_schema_node(row, schema, schema)
        if shape_errors:
            return shape_errors[0]
        cf_errors = _apply_cross_field_rules(
            row, 'plan-tasks', governed=governed,
            plan_created=fm.get('created') if isinstance(fm, dict) else None,
        )
        if cf_errors:
            return cf_errors[0]
    return None


_PLAN_TASKS_CROSS_FIELD_RULES = [
    _cf_plan_tasks_disposition_shape,
    _cf_plan_tasks_writes_declared,
]


_HANDOFF_CROSS_FIELD_RULES = [
    _cf_awaiting_gate_needs_dependency,
    _cf_awaiting_gate_not_pickup_ready,
    _cf_ready_to_fire_no_dependency,
    _cf_ready_to_fire_no_gate_evidence,
    _cf_ready_to_fire_no_unresolved_blocked_by,
    _cf_gate_dependency_not_path_shaped,
    _cf_execution_stamp_required,
    _cf_handoff_phase_kind_gate,
    _cf_graph_fields_roadmap_only,
    _cf_spinoff_roadmap_requires_graph,
    _cf_roadmap_id_implies_kind,
    _cf_cost_enum,
    _cf_category_required_post_cutoff,
    _cf_summary_required_post_cutoff,
    _cf_summary_length_cap,
    _cf_supersedes_spinoff_only,
    _cf_claimed_by_required,
    _cf_closed_reason_required,
    _cf_continued_into_required,
    _cf_shipped_in_required,
    _cf_spinoff_predecessor_none,
    _cf_forked_from_spinoff_only,
    _cf_additional_predecessors_integrity,
    _cf_deliverable_id_prefix,
    _cf_initiative_non_empty,
    _cf_origin_plan_id_prefix,
    _cf_origin_handoff_path_prefix,
    _cf_origin_session_non_empty,
    _cf_origin_goal_id_entry_prefix,
    _cf_origin_scalar_fields_reject_arrays,
    _cf_origin_goal_id_array_cardinality,
    _cf_origin_handoff_self_reference,
    _cf_origin_predecessor_none_invariant,
    _cf_forked_from_origin_handoff_equality,
    _cf_owner_axis_scalar,
    _cf_carried_items_shape,
    _cf_gate_evidence_legs_shape,
]

# ---------------------------------------------------------------------------
# Cross-field rules — cross-repo-memo schema.
#
# Port of CROSS_FIELD_RULES['cross-repo-memo'] from DoE-claude coordinator/bin/lib/schema.js:1332-1522.
# Each rule is a callable (fm_dict) -> dict | None.
# The grandfather rule returns {'__skip__': True} when created < 2026-05-22;
# _apply_cross_field_rules detects this sentinel and returns [] immediately.
# Negative-spec: cross-field ONLY — no base-required validation (memos are foreign-authored).
# ---------------------------------------------------------------------------

def _memo_cf_grandfather(fm: dict) -> dict | None:
    """Grandfather cutoff: memos with created < 2026-05-22 are skipped entirely.

    Port of schema.js:1335-1346. Returns {'__skip__': True} to signal _apply_cross_field_rules
    to return [] immediately, bypassing all subsequent rules.
    """
    if not fm.get('created'):
        return None
    # Review: code-reviewer — F5: str() handles both str and datetime.date (isoformat
    # str() output is YYYY-MM-DD, comparable with '<' on ISO strings). Public-API
    # coercion via _coerce_dates_to_strings is belt-and-suspenders, not a prerequisite.
    if str(fm['created']) < '2026-05-22':
        return {'__skip__': True}
    return None


def _memo_cf_in_progress_needs_picked_up_by(fm: dict) -> ErrorDict | None:
    """status=in_progress requires non-empty picked_up_by (claim attribution).

    Port of schema.js:1365-1376.
    Spec backlink: docs/plans/2026-06-21-memo-pickup-claim-lock-and-routed-plan-reconcile.md § C2
    """
    if fm.get('status') != 'in_progress':
        return None
    picked_up_by = fm.get('picked_up_by')
    if not picked_up_by or str(picked_up_by).strip() == '':
        return {
            'field': 'picked_up_by',
            'error': 'required when status=in_progress',
            'hint': (
                'Set picked_up_by to the claiming session id when a memo is claimed at '
                'pickup-start (status: in_progress). Cleared on release back to open.'
            ),
        }
    return None


def _memo_cf_actioned_decision_requires_realized_by(fm: dict) -> ErrorDict | None:
    """status=actioned + decision=accepted|partial requires well-formed realized_by.

    Port of schema.js:1392-1419.
    Well-formed: 'inline', contains '/', or hex SHA (7-64 chars, case-insensitive).
    Spec backlink: docs/plans/2026-06-23-memo-pickup-realization-claim-visibility.md § C1
    """
    if fm.get('status') != 'actioned':
        return None
    decision = fm.get('decision')
    if decision not in ('accepted', 'partial'):
        return None
    v = '' if fm.get('realized_by') is None else str(fm['realized_by']).strip()
    if v == '':
        return {
            'field': 'realized_by',
            'error': f'required when status=actioned and decision={decision}',
            'hint': (
                'Set realized_by to where the work landed: a plan path '
                '(docs/plans/*.md or tasks/<feature>/todo.md), a commit SHA, or the '
                'sentinel "inline". An accepted/partial memo realizes work and must carry '
                'a claim-of-record so a peer session does not re-realize it.'
            ),
        }
    # Well-formed check: 'inline', path (contains '/'), or hex SHA (7-64 chars).
    well_formed = (
        v == 'inline'
        or '/' in v
        or bool(re.fullmatch(r'[0-9a-fA-F]{7,64}', v))
    )
    if not well_formed:
        return {
            'field': 'realized_by',
            'error': f'malformed realized_by "{v}" when status=actioned and decision={decision}',
            'hint': (
                'realized_by must be one of: the sentinel "inline", a path containing "/" '
                '(e.g. docs/plans/2026-06-23-foo.md, tasks/<feature>/todo.md), or a hex '
                'commit SHA (7–64 hex chars). A bare word reads as authoritative but points nowhere.'
            ),
        }
    return None


def _memo_cf_action_taken_requires_companions(fm: dict) -> ErrorDict | None:
    """status=action_taken requires action_taken_at AND decision.

    Port of schema.js:1422-1436. action_taken is a grandfathered-only status value
    retained for backward compat with pre-2026-05-23 lifecycle memos.
    """
    if fm.get('status') != 'action_taken':
        return None
    missing = []
    if not fm.get('action_taken_at') or str(fm.get('action_taken_at', '')).strip() == '':
        missing.append('action_taken_at')
    if not fm.get('decision') or str(fm.get('decision', '')).strip() == '':
        missing.append('decision')
    if missing:
        return {
            'field': ', '.join(missing),
            'error': 'required when status=action_taken',
            'hint': (
                f'Set {" and ".join(missing)} when marking a memo action_taken. '
                'decision must be one of: accepted, declined, partial, superseded.'
            ),
        }
    return None


def _memo_cf_closed_requires_companions(fm: dict) -> ErrorDict | None:
    """status=closed requires closed_at, action_taken_at, AND decision.

    Port of schema.js:1439-1454.
    """
    if fm.get('status') != 'closed':
        return None
    missing = []
    if not fm.get('closed_at') or str(fm.get('closed_at', '')).strip() == '':
        missing.append('closed_at')
    if not fm.get('action_taken_at') or str(fm.get('action_taken_at', '')).strip() == '':
        missing.append('action_taken_at')
    if not fm.get('decision') or str(fm.get('decision', '')).strip() == '':
        missing.append('decision')
    if missing:
        return {
            'field': ', '.join(missing),
            'error': 'required when status=closed',
            'hint': (
                f'Set {", ".join(missing)} when closing a memo. '
                'A closed memo must have a complete action record.'
            ),
        }
    return None


def _memo_cf_superseded_requires_superseded_by(fm: dict) -> ErrorDict | None:
    """status=superseded requires superseded_by.

    Port of schema.js:1457-1466.
    """
    if fm.get('status') != 'superseded':
        return None
    superseded_by = fm.get('superseded_by')
    if not superseded_by or str(superseded_by).strip() == '':
        return {
            'field': 'superseded_by',
            'error': 'required when status=superseded',
            'hint': 'Set superseded_by to the path of the memo that supersedes this one (inverse of supersedes:).',
        }
    return None


def _memo_cf_central_only_requires_to(fm: dict) -> ErrorDict | None:
    """delivery_mode=central-only requires to:.

    Port of schema.js:1470-1480.
    """
    if fm.get('delivery_mode') != 'central-only':
        return None
    to_field = fm.get('to')
    if not to_field or str(to_field).strip() == '':
        return {
            'field': 'to',
            'error': 'required when delivery_mode=central-only',
            'hint': (
                'Specify the receiver EM identifier in "to:" even for central-only delivery. '
                'Used for workday-start surfacing and audit trail.'
            ),
        }
    return None


def _memo_cf_summary_length_cap(fm: dict) -> ErrorDict | None:
    """summary must not exceed 120 characters when present.

    Port of schema.js:1490-1499. Memo cap is 120 chars (distinct from handoff cap of 140).
    The __skip__ grandfather guard ensures this only fires for post-cutoff memos.
    """
    summary = fm.get('summary')
    if summary is None:
        return None
    if len(str(summary)) > 120:
        return {
            'field': 'summary',
            'error': f'summary exceeds 120 characters (got {len(str(summary))})',
            'hint': 'Keep summary to one concise line of 120 characters or fewer',
        }
    return None


def _memo_cf_disposition_superseded_requires_companions(fm: dict) -> ErrorDict | None:
    """disposition_superseded=true requires superseding_note, superseding_realized_by,
    superseded_at, AND status in (actioned, superseded).

    Native-only rule, no JS oracle (post-DR-210 claude-klabauter owns cross-repo-memo tooling
    outright) — the append-only supersede-disposition mechanism
    (coordinator_core.ops.memo_transition._handle_supersede /
    _apply_supersede_fields) records that a reversed verdict now governs an
    already-actioned memo WITHOUT touching the original decision/decision_note/
    realized_by/actioned_note fields, which remain readable as history. This rule
    is the presence-triggered completeness gate for that shape — mirrors
    _memo_cf_closed_requires_companions's "presence of the terminal marker implies
    its companion fields" pattern, not a hard schema `required` addition (no
    previously-valid memo carries disposition_superseded at all, so this can never
    retroactively invalidate the existing corpus — additive-only, same discipline
    as _memo_cf_kind_enum).

    Spec: cross-repo/inbox/2026-08-12-example-retrieval-repo-em-git-index-lock-reaper.md —
    actioned with a `negotiate` disposition later reversed by PM ruling within the
    hour, with no prior mechanism to record the reversal without silently
    overwriting the original actioned_note.
    """
    if not fm.get('disposition_superseded'):
        return None
    missing = []
    if not fm.get('superseding_note') or str(fm.get('superseding_note', '')).strip() == '':
        missing.append('superseding_note')
    if not fm.get('superseding_realized_by') or str(fm.get('superseding_realized_by', '')).strip() == '':
        missing.append('superseding_realized_by')
    if not fm.get('superseded_at') or str(fm.get('superseded_at', '')).strip() == '':
        missing.append('superseded_at')
    if fm.get('status') not in ('actioned', 'superseded'):
        missing.append('status (must be actioned or superseded)')
    if missing:
        return {
            'field': ', '.join(missing),
            'error': 'required when disposition_superseded is true',
            'hint': (
                'A disposition_superseded=true memo must also carry superseding_note '
                '(the reversal rationale), superseding_realized_by (a pointer to what '
                'realized the reversal: a commit SHA, a memo, or a baton), and '
                'superseded_at (a timestamp), and must already be status=actioned or '
                'status=superseded — you cannot supersede a disposition that was never '
                'made.'
            ),
        }
    return None


def _memo_cf_kind_enum(fm: dict) -> ErrorDict | None:
    """kind must be one of ask|consult|fyi|proposal when present; absent/null is valid.

    Port of schema.js:1509-1520.
    'ack' is NOT a valid kind — acknowledgement is receipt-state, not sender-declared kind.
    """
    kind = fm.get('kind')
    if kind is None:
        return None
    valid_kinds = ['ask', 'consult', 'fyi', 'proposal']
    if str(kind) not in valid_kinds:
        return {
            'field': 'kind',
            'error': f'invalid enum value "{kind}" for kind',
            'hint': (
                f'kind must be one of: {", ".join(valid_kinds)}. '
                "Absent is also valid (reader applies 'ask' default). "
                "Note: 'ack' is not a kind — acknowledgement is receipt-state."
            ),
        }
    return None


def _memo_cf_distill_fate(fm: dict) -> ErrorDict | None:
    """distill_fate=ratification requires a well-formed in_repo_capture pointing at an
    in-repo home.

    Port of schema.js:2236-2280 (Finding #11 / #12). A ratification stamp claims a memo
    settled ownership/seam durably; that claim is meaningless if "captured" can resolve
    to a ~/.claude memory pointer — memory is for cross-session pointers, not decision
    content (finding #12's delete-guard hole). Valid in-repo homes: docs/decisions/,
    docs/wiki/, state/cross-repo-commitments/, or a canonical plan/spec path
    (docs/plans/*.md) — or the coordinator/docs/... source-repo equivalents. A
    ~/.claude-rooted path (absolute or the literal '~/.claude' prefix) MUST fail —
    detect-then-fail-loud, not detect-then-silently-pick. distill_fate itself is
    optional/back-compat (absent is valid on legacy memos, backfilled by the claude-klabauter
    memo.triage op); ephemeral and commitment fates impose no in_repo_capture
    requirement here.
    Spec backlink: docs/plans/2026-07-12-distill-rebuild-claude-klabauter-reliant.md § C2
    """
    distill_fate = fm.get('distill_fate')
    if distill_fate is None:
        return None
    valid_fates = ('ephemeral', 'commitment', 'ratification')
    if str(distill_fate) not in valid_fates:
        return {
            'field': 'distill_fate',
            'error': f'invalid enum value "{distill_fate}" for distill_fate',
            'hint': (
                f'distill_fate must be one of: {", ".join(valid_fates)}. '
                'Absent is also valid (legacy memos backfilled by the claude-klabauter memo.triage op).'
            ),
        }
    if distill_fate != 'ratification':
        return None
    capture = '' if fm.get('in_repo_capture') is None else str(fm['in_repo_capture']).strip()
    if capture == '':
        return {
            'field': 'in_repo_capture',
            'error': 'required when distill_fate=ratification',
            'hint': (
                "Set in_repo_capture to the in-repo path where this memo's decision was "
                'durably captured (docs/decisions/, docs/wiki/, state/cross-repo-commitments/, '
                'or a canonical plan/spec path — or the coordinator/docs/... source-repo '
                'equivalents when captured in the coordinator plugin source repo). A '
                'ratification stamp is meaningless without a durable in-repo capture (finding #12).'
            ),
        }
    # Consumer-repo doc homes live at docs/decisions/, docs/wiki/, docs/plans/*.md.
    # The coordinator plugin SOURCE repo nests its canonical doctrine homes one level
    # deeper, under coordinator/docs/..., because coordinator/ is the plugin root
    # resolved live via --plugin-dir. Both forms are legitimate in-repo homes.
    doc_home_suffixes = ('docs/decisions/', 'docs/wiki/')
    is_home_path = (
        any(capture.startswith(suffix) or capture.startswith(f'coordinator/{suffix}') for suffix in doc_home_suffixes)
        or capture.startswith('state/cross-repo-commitments/')
        or (
            (capture.startswith('docs/plans/') or capture.startswith('coordinator/docs/plans/'))
            and capture.endswith('.md')
        )
    )
    # Review note (parity with schema.js): isClaudeMemoryPointer is currently subsumed
    # by !is_home_path (the allowlist prefixes never overlap .claude paths), but the
    # explicit check is deliberate defense-in-depth — kept for parity with the oracle.
    is_claude_memory_pointer = (
        capture.startswith('~/.claude')
        or (capture.startswith('/Users/') and '/.claude/' in capture)
        or '/.claude/' in capture
    )
    if is_claude_memory_pointer or not is_home_path:
        return {
            'field': 'in_repo_capture',
            'error': f'malformed in_repo_capture "{capture}" when distill_fate=ratification',
            'hint': (
                'in_repo_capture must point at an in-repo home: docs/decisions/, docs/wiki/, '
                'state/cross-repo-commitments/, or a canonical plan/spec path (docs/plans/*.md) '
                '— or the coordinator/docs/... source-repo equivalents (coordinator/docs/decisions/, '
                'coordinator/docs/wiki/, coordinator/docs/plans/*.md) when captured in the coordinator '
                'plugin source repo. A ~/.claude path is a memory pointer, not durable capture, and '
                'always fails this rule (finding #12).'
            ),
        }
    return None


# Ordered list of cross-field rule functions for the "cross-repo-memo" schema.
# Grandfather rule MUST be first — returns {'__skip__': True} to short-circuit all
# remaining rules when created < 2026-05-22.
# Port of CROSS_FIELD_RULES['cross-repo-memo'] from DoE-claude coordinator/bin/lib/schema.js:1332-1522.
_MEMO_CROSS_FIELD_RULES = [
    _memo_cf_grandfather,
    _memo_cf_in_progress_needs_picked_up_by,
    _memo_cf_actioned_decision_requires_realized_by,
    _memo_cf_action_taken_requires_companions,
    _memo_cf_closed_requires_companions,
    _memo_cf_superseded_requires_superseded_by,
    _memo_cf_central_only_requires_to,
    _memo_cf_summary_length_cap,
    _memo_cf_kind_enum,
    _memo_cf_distill_fate,
    _memo_cf_disposition_superseded_requires_companions,
]

# ---------------------------------------------------------------------------
# Cross-field rules — cutover schema.
#
# Hand-authored port of coordinator/schemas/cutover.schema.json's allOf block
# (DoE-claude, C1) — `_validate_json_schema_node` does not implement the
# `allOf`/`minItems` keywords, so the schema's own if/then couplings are
# inert at runtime without this. Same shape as _HANDOFF_CROSS_FIELD_RULES:
# each rule is a callable (fm_dict) -> ErrorDict | None.
# Spec backlink: docs/plans/2026-07-25-cutover-state-machine.md § AC2
# (Review: the Director of Engineering-cutover-review F1, F3, F7).
# ---------------------------------------------------------------------------

_CUTOVER_GATE_SOURCE_KINDS = {'value-vocabulary'}
_CUTOVER_VERIFIED_BY_KINDS = {'test-node-id', 'probe-op-key', 'commit-sha', 'sibling-commitment-ref'}
_CUTOVER_SIBLING_COMMITMENT_REF_RE = re.compile(
    r'^(state/cross-repo-commitments/)?\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]*\.yaml$'
)


def _cutover_cf_phase_requires_confirmed_consumers(fm: dict) -> ErrorDict | None:
    """phase: retiring|retired requires a non-empty confirmed_consumers (F1).

    Port of cutover.schema.json allOf[0] (retiring|retired -> confirmed_consumers
    minItems 1) — the exact coupling AC2/F1 need enforced structurally.
    """
    if fm.get('phase') not in ('retiring', 'retired'):
        return None
    consumers = fm.get('confirmed_consumers')
    if isinstance(consumers, list) and len(consumers) > 0:
        return None
    return {
        'field': 'confirmed_consumers',
        'error': 'must be a non-empty array when phase is "retiring" or "retired"',
        'hint': (
            'A consumer-visible break requires confirmed_consumers naming who '
            'confirmed the old form is safe to retire. Add at least one entry, '
            'or move phase back to "dual-write" until consumers are confirmed.'
        ),
    }


def _cutover_cf_retired_requires_gate_source(fm: dict) -> ErrorDict | None:
    """phase: retired requires gate_source present (F1).

    Port of cutover.schema.json allOf[1]. gate_source is also unconditionally
    required at the schema's top level, so this rule is redundant defense
    against a future relaxation of that top-level `required` — kept to mirror
    the schema's own declared coupling.
    """
    if fm.get('phase') != 'retired':
        return None
    if fm.get('gate_source') is not None:
        return None
    return {
        'field': 'gate_source',
        'error': 'required field missing',
        'hint': (
            'phase: retired requires gate_source — the derivation that proved '
            'no consumer still needs the old form. Add "gate_source:" to the record.'
        ),
    }


def _cutover_cf_gate_source_kind_enum(fm: dict) -> ErrorDict | None:
    """gate_source.kind must be one of the closed derivation-kind enum (F3).

    Port of cutover.schema.json allOf[2]. Also enforced directly by the base
    validator via gate_source.properties.kind's own inline enum; kept to
    mirror the schema's declared coupling and guard against that inline enum
    being dropped independently of this cross-field rule.
    """
    gate_source = fm.get('gate_source')
    if not isinstance(gate_source, dict):
        return None
    kind = gate_source.get('kind')
    if kind is None or kind in _CUTOVER_GATE_SOURCE_KINDS:
        return None
    return {
        'field': 'gate_source.kind',
        'error': f'invalid enum value "{kind}"',
        'hint': f'Allowed values: {", ".join(sorted(_CUTOVER_GATE_SOURCE_KINDS))}.',
    }


def _verified_by_ref_shape_error(kind: str, ref: Any) -> tuple[str, str] | None:
    """Shared per-kind `ref` shape dispatch for the four `verified_by`/
    `gate_evidence` leg kinds carried VERBATIM from cutover.schema.json's
    `confirmed_consumers[].verified_by.kind` union into handoff.schema.json's
    `gate_evidence.legs[].kind` (C1) — test-node-id, probe-op-key, commit-sha,
    sibling-commitment-ref. `gate_evidence` extends this SAME discriminated
    union to a second record kind; it is not a parallel vocabulary, so this
    dispatch lives exactly ONCE and both `_cutover_cf_verified_by_kind_and_ref_shape`
    and `_cf_gate_evidence_legs_shape` call it — extended, not duplicated.

    Returns (error, hint) if `ref` is malformed for `kind`, else None. `kind`
    values outside this shared four are the caller's own concern (this
    function only recognizes the four it carries verbatim).
    """
    if kind == 'test-node-id':
        if not isinstance(ref, str) or '::' not in ref:
            return (
                f'ref "{ref}" does not look like a pytest node id (missing "::")',
                'kind: test-node-id expects a ref like "path/to/test_x.py::test_y".',
            )
    elif kind == 'commit-sha':
        if not isinstance(ref, str) or not re.fullmatch(r'[0-9a-fA-F]{7,64}', ref):
            return (
                f'ref "{ref}" is not a full or abbreviated commit SHA',
                'kind: commit-sha expects a 7-64 character hex SHA.',
            )
    elif kind == 'probe-op-key':
        if not isinstance(ref, str) or len(ref) < 1:
            return (
                'ref must be a non-empty op-key string',
                'kind: probe-op-key expects a non-empty invokable op-key.',
            )
    elif kind == 'sibling-commitment-ref':
        if not isinstance(ref, str) or not _CUTOVER_SIBLING_COMMITMENT_REF_RE.match(ref):
            return (
                f'ref "{ref}" does not look like a cross-repo-commitment record filename',
                (
                    'kind: sibling-commitment-ref expects a state/cross-repo-commitments/'
                    '*.yaml filename, bare or path-qualified, e.g. '
                    '"2026-07-25-sibling-confirms-x-a1b2c3d4e5f6.yaml".'
                ),
            )
    return None


def _cutover_cf_verified_by_kind_and_ref_shape(fm: dict) -> ErrorDict | None:
    """confirmed_consumers[].verified_by.kind must be a recognized kind, and
    .ref must be shaped for that kind (F7).

    Port of cutover.schema.json allOf[3]. The kind-enum half is also enforced
    directly by the base validator via verified_by.properties.kind's own
    inline enum; the ref-shape-per-kind half (test-node-id needs "::",
    commit-sha needs a hex SHA, probe-op-key needs non-empty) is allOf-only
    in the schema and has no other enforcement path. Ref-shape dispatch
    itself lives in `_verified_by_ref_shape_error`, shared verbatim with
    `_cf_gate_evidence_legs_shape` (C1) — not re-implemented here.
    """
    consumers = fm.get('confirmed_consumers')
    if not isinstance(consumers, list):
        return None
    for i, entry in enumerate(consumers):
        if not isinstance(entry, dict):
            continue
        verified_by = entry.get('verified_by')
        if not isinstance(verified_by, dict):
            continue
        kind = verified_by.get('kind')
        if kind is None:
            continue
        if kind not in _CUTOVER_VERIFIED_BY_KINDS:
            return {
                'field': f'confirmed_consumers[{i}].verified_by.kind',
                'error': f'invalid enum value "{kind}"',
                'hint': f'Allowed values: {", ".join(sorted(_CUTOVER_VERIFIED_BY_KINDS))}.',
            }
        shape_error = _verified_by_ref_shape_error(kind, verified_by.get('ref'))
        if shape_error is not None:
            error, hint = shape_error
            return {
                'field': f'confirmed_consumers[{i}].verified_by.ref',
                'error': error,
                'hint': hint,
            }
    return None


_CUTOVER_CROSS_FIELD_RULES = [
    _cutover_cf_phase_requires_confirmed_consumers,
    _cutover_cf_retired_requires_gate_source,
    _cutover_cf_gate_source_kind_enum,
    _cutover_cf_verified_by_kind_and_ref_shape,
]

# Map schema name → list of cross-field rule functions.
# handoff-archived has no cross-field rules (relaxed-schema sibling).
# cross-repo-memo rules are cross-field ONLY (no base-required validation).
_QUEUE_CROSS_FIELD_RULES = [
    _cf_queue_disposition_shape,
]

_CROSS_FIELD_RULES_BY_SCHEMA: dict[str, list] = {
    'handoff': _HANDOFF_CROSS_FIELD_RULES,
    'handoff-archived': [],
    'cross-repo-memo': _MEMO_CROSS_FIELD_RULES,
    'cutover': _CUTOVER_CROSS_FIELD_RULES,
    'plan-tasks': _PLAN_TASKS_CROSS_FIELD_RULES,
    'bug-backlog': _QUEUE_CROSS_FIELD_RULES,
    'debt-backlog': _QUEUE_CROSS_FIELD_RULES,
    'improvement-queue': _QUEUE_CROSS_FIELD_RULES,
}


@functools.lru_cache(maxsize=None)
def _rule_parameter_names(rule: Any) -> frozenset[str]:
    """Cached `inspect.signature(rule).parameters` keys.

    `_apply_cross_field_rules` calls this once per rule per row; introspecting
    the same handful of rule functions on every row of every validated plan is
    pure repeated cost with a single answer per rule, so it's memoized here
    keyed on the rule function itself. Safe against `_CROSS_FIELD_RULES_BY_SCHEMA`
    being rebuilt: the cache key is the function object, not the table, so a
    rebuilt table pointing at the same (or new) function objects still resolves
    correctly — a new function object simply gets its own cache entry.
    """
    return frozenset(inspect.signature(rule).parameters)


def _apply_cross_field_rules(
    fm: dict, schema_name: str, **rule_kwargs: Any
) -> list[ErrorDict]:
    """Apply cross-field rules for the given schema name.

    Port of applyCrossFieldRules from DoE-claude coordinator/bin/lib/schema.js.
    Returns a (possibly empty) list of error dicts.

    __skip__ sentinel: when a rule returns a dict with {'__skip__': True}, all remaining
    rules are skipped and an empty list is returned immediately. This implements the
    grandfather mechanism (schema.js:1533-1534) used by the cross-repo-memo rule set.

    `rule_kwargs` are forwarded verbatim to every rule in the set, for
    PLAN-scoped context a per-row rule cannot derive from its own row.
    Today's only use is `governed=` for the plan-tasks set (the
    grouping-approval contract). Rules that do not accept a given keyword
    are called without it, so adding a keyword never breaks an unrelated
    rule set — the caller states the context, and only rules that opted in
    by declaring the parameter receive it.
    """
    rules = _CROSS_FIELD_RULES_BY_SCHEMA.get(schema_name, [])
    errors: list[ErrorDict] = []
    for rule in rules:
        accepted = {
            key: value
            for key, value in rule_kwargs.items()
            if key in _rule_parameter_names(rule)
        }
        violation = rule(fm, **accepted)
        if violation is None:
            continue
        # __skip__ sentinel: pre-cutoff grandfather fires — skip all remaining rules.
        # Port of: if (violation.__skip__) return []; (schema.js:1534)
        if isinstance(violation, dict) and violation.get('__skip__'):
            return []
        errors.append(violation)  # type: ignore[arg-type]  # violation is ErrorDict here
    return errors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_frontmatter(fm_dict: dict, schema_path: str | Path) -> list[ErrorDict]:
    """Validate a parsed frontmatter dict against the JSON Schema at schema_path.

    Schema-parameterized: schema_path is always passed in by the caller; never
    hardcoded inside this function.

    Returns a (possibly empty) list of error dicts. Empty list means the record
    is valid according to both shape and cross-field rules.

    Raises:
        SchemaVersionError: when the record declares schema_version AND:
            - the schema file has no x-schema-version to compare against, OR
            - the record's major version exceeds the schema's major version.
        json.JSONDecodeError: if schema_path is not valid JSON.
        FileNotFoundError: if schema_path does not exist.

    Error dict shape: {"field": str, "error": str, "hint": str}
    """
    schema_path = Path(schema_path)
    with schema_path.open('r', encoding='utf-8') as f:
        schema = json.load(f)

    # Normalise datetime.date/datetime objects to ISO strings (PyYAML safe_load coercion).
    fm_dict = _coerce_dates_to_strings(fm_dict)

    schema_name: str | None = schema.get('x-schema-name')

    # Phase 0: schema_version gate (consumer-side fail-loud).
    #
    # If the frontmatter asserts schema_version, the consumer must be able to
    # compare it against the vendored schema's x-schema-version. Fail loud when:
    #   (a) schema lacks x-schema-version — cannot validate; inference would be unsafe.
    #   (b) record's major > schema's major — consumer schema is behind the producer.
    record_version_raw = fm_dict.get('schema_version')
    if record_version_raw is not None:
        schema_version_raw = schema.get('x-schema-version')
        if schema_version_raw is None:
            raise SchemaVersionError(
                f'record declares schema_version="{record_version_raw}" but '
                f'schema "{schema_name or schema_path.name}" has no x-schema-version — '
                'cannot validate version compatibility. '
                'Update the vendored schema to include x-schema-version.'
            )
        rv = _parse_semver(str(record_version_raw))
        sv = _parse_semver(str(schema_version_raw))
        if rv is not None and sv is not None and rv[0] > sv[0]:
            raise SchemaVersionError(
                f'record schema_version "{record_version_raw}" major ({rv[0]}) exceeds '
                f'vendored schema x-schema-version "{schema_version_raw}" major ({sv[0]}) — '
                'refuse-on-newer-read: upgrade the vendored schema before consuming records '
                'at this version. See docs/wiki/schema-version-gate.md.'
            )

    # Phase 1: shape validation via the dep-free JSON Schema subset validator.
    shape_errors = _validate_json_schema_node(fm_dict, schema, schema, '')
    shape_errors = _tolerate_handoff_kind_aliases(shape_errors, schema_name, schema, fm_dict)

    # Phase 2: cross-field rules.
    # Review: code-reviewer — F3: `schema_name or ''` is dead — the `if schema_name`
    # guard already ensures schema_name is truthy in the true-branch.
    # `local_queue_corpus` is consumed only by `_cf_queue_disposition_shape`,
    # which is inert without it (see that rule's docstring for the measured
    # cross-repo breakage that made the scoping load-bearing). Forwarded via the
    # opt-in keyword mechanism `_apply_cross_field_rules` already documents, so
    # no other rule set sees it. True only when the schema being validated
    # against is claude-klabauter's OWN vendored copy — a caller validating against a
    # schema from another tree is not validating claude-klabauter's corpus.
    cross_errors = (
        _apply_cross_field_rules(
            fm_dict,
            schema_name,
            local_queue_corpus=_is_claude_klabauter_vendored_schema(schema_path),
        )
        if schema_name
        else []
    )

    return shape_errors + cross_errors


def _is_claude_klabauter_vendored_schema(schema_path: str | Path) -> bool:
    """True when `schema_path` is claude-klabauter's own vendored schema copy.

    The provenance discriminator behind `_cf_queue_disposition_shape`'s
    claude-klabauter-scoping. Resolved rather than string-compared so a relative path, a
    `..`-laden one, or a differently-cased Windows drive letter all answer
    correctly; any resolution failure answers False, which is the inert
    direction for every rule keyed on it.
    """
    try:
        return Path(schema_path).resolve().parent == _SCHEMAS_DIR.resolve()
    except (OSError, ValueError):
        return False


def validate_memo_cross_fields(fm_dict: dict) -> list[ErrorDict]:
    """Apply cross-repo-memo cross-field rules only (no base-required validation).

    Port of applyCrossFieldRulesFor('cross-repo-memo', fm) from DoE-claude
    coordinator/bin/lib/schema.js, consumed by memo-transition.js:validateMemoFrontmatter.

    Cross-field only — memos are foreign-authored; a sender's base-field slip must never
    block a legitimate receiver transition. Only cross-field consistency rules that the
    receiver's transition might violate are checked.

    Coerces datetime.date/datetime to ISO strings before running rules — PyYAML safe_load
    coerces a bare 'created: 2026-05-22' to datetime.date, and a date < str comparison
    raises TypeError. Coercion is safe (does not mutate the caller's dict).

    Negative-spec: does not run JSON Schema shape validation (no schema file). The caller
    is responsible for ensuring the dict is a parsed frontmatter (e.g. via split_frontmatter
    + yaml.safe_load), not raw YAML text.
    """
    fm = _coerce_dates_to_strings(fm_dict)
    return _apply_cross_field_rules(fm, 'cross-repo-memo')


# DR-210 §2(a) (docs/decisions/DR-210-claude-klabauter-native-tooling-ownership-strangler.md:89)
# assumed a single HEAD-compare fixture: "pulled from DoE-HEAD at check-time, NOT
# co-vendored with the pinned emitter". This plan REFINES that into two structurally
# separate checks: check_schema_drift below is a GATING pinned-SHA tamper-check (does
# the vendored copy still equal what claude-klabauter PINNED at ref?), and
# check_schema_drift_advisory is a NON-GATING HEAD-vs-pin signal (has DoE moved since?).
# Refinement, not an override — DR-210's HEAD-compare intent survives in the advisory.
def check_schema_drift(
    schema_path: str | Path, doe_repo_path: str | Path, ref: str = "HEAD"
) -> None:
    """Tamper-check: prove the vendored schema still equals what claude-klabauter PINNED at ref.

    Reads the vendored schema at schema_path and compares it byte-for-byte against
    the corresponding file in the DoE repo at the given ref (default HEAD) via:
        git -C doe_repo_path show <ref>:coordinator/schemas/<schema_filename>

    This is a TAMPER-check, not a staleness check: called with ref pinned to the
    landing SHA the vendored copy was cut from, it is expected to be ALWAYS GREEN —
    a failure here means the vendored file was locally edited/corrupted, not that DoE
    has since moved on. For "has DoE moved since the pin" use
    check_schema_drift_advisory instead (non-gating, never raises).

    Raises:
        SchemaDriftError: if the vendored schema diverges from the DoE ref.
        SchemaDriftError: if git is unavailable or the DoE ref path cannot be read.

    Negative-spec: this is a pure comparison; it does NOT update the vendored file.
    Call this function from tests to detect silent drift (e.g. a formatter reformat).

    Negative-spec (git scoping): `doe_repo_path` is a DIFFERENT repository from the
    one this process runs in, and `git -C` alone does not scope to it — an inherited
    `GIT_DIR` (git exports one to every hook it runs, often as a relative `"."`)
    still wins over discovery. Unscoped, this `git show` reads whichever schema the
    LOCAL repo happens to have at that path and reports the byte difference as DoE
    drift, with a direction inferred from the wrong side. The read therefore runs
    with `git_scope.scoped_git_env()` and is preceded by
    `foreign_repo_unusable_reason`, so "could not reach the DoE clone" raises as
    exactly that rather than as a tamper finding.
    """
    schema_path = Path(schema_path)
    doe_repo_path = Path(doe_repo_path)

    schema_filename = schema_path.name
    doe_schema_ref = f'coordinator/schemas/{schema_filename}'

    unusable = foreign_repo_unusable_reason(doe_repo_path)
    if unusable is not None:
        raise SchemaDriftError(
            f'Cannot read DoE {ref} schema "{doe_schema_ref}": the DoE clone at '
            f'{doe_repo_path} could not be read as a git repository ({unusable}). '
            'This is NOT a drift finding — the comparison never ran.'
        )

    # Review: code-reviewer — F4 (Wave B): stdin=DEVNULL + CREATE_NO_WINDOW to match the
    # _run_git hardening pattern used by this slice's sibling modules.
    result = subprocess.run(
        ['git', '-C', str(doe_repo_path), 'show', f'{ref}:{doe_schema_ref}'],
        capture_output=True,
        text=True,
        encoding='utf-8',
        timeout=FOREIGN_REPO_GIT_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
        env=scoped_git_env(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise SchemaDriftError(
            f'Cannot read DoE {ref} schema "{doe_schema_ref}": {result.stderr.strip()}. '
            f'Ensure doe_repo_path ({doe_repo_path}) is a valid git repo with the schema at {ref}.'
        )

    doe_content = result.stdout
    local_content = schema_path.read_text(encoding='utf-8')

    if local_content != doe_content:
        # Negative-spec: this message MUST NOT prescribe an unconditional
        # downward `cp`. It used to, and that string is a twice-confirmed trap:
        # (a) cross-repo/inbox 2026-07-30 (example-market-data-repo-em) -- a vendored
        # schema that was AHEAD of DoE had its newer content deleted by a reader
        # who did what the message said, turning the gate green as a regression;
        # (b) state/lessons/0000-00-00-sibling-schema-files-opposite-vendor-
        # contracts.yaml -- the same `cp` applied to a co-located SIBLING
        # clobbered ~31 lines of claude-klabauter-owned validator rules, because
        # mirror-vs-fork is a per-file property and co-location in schemas/ is
        # not evidence of a shared contract. Both contents are in hand at this
        # raise, so direction is stated, never guessed by the reader.
        direction = _infer_drift_direction(local_content, doe_content)
        remedy = {
            DIRECTION_WE_AHEAD: (
                'Our vendored copy is AHEAD of DoE (reconciliation pending upstream). '
                'The fix is UPWARD -- propagate our additions into '
                f'{doe_schema_ref} in DoE. Copying DoE down would delete them.'
            ),
            DIRECTION_WE_BEHIND: (
                'DoE is AHEAD of our vendored copy. Re-vendor downward via '
                'bin/claude-klabauter-revendor-schema.py (which also moves any pin) -- '
                'not a bare cp.'
            ),
            DIRECTION_BOTH: (
                'BOTH sides changed independently -- reconcile by hand. A '
                'blind re-vendor in either direction drops one side.'
            ),
        }[direction]
        raise SchemaDriftError(
            f'Vendored schema "{schema_filename}" diverges from DoE {ref} '
            f'({doe_repo_path}:{doe_schema_ref}). {remedy} '
            'Before applying this remedy to any OTHER file in the same '
            "directory, read that file's own drift test first: siblings here "
            'may be intentionally divergent forks, not mirrors. '
            'Do NOT reformat the vendored file (see .prettierignore).'
        )


# Direction vocabulary for check_schema_drift_advisory's diverged=True overload.
# Spec backlink: cross-repo/inbox/2026-07-23-example-cockpit-repo-em-coordinator-doc-new-category-no-validation.md
# — the memo that surfaced "a drift report that cannot tell 'we are ahead,
# reconciliation pending' from 'we are stale, re-vendor now' is unactionable".
DIRECTION_WE_AHEAD = "we-are-ahead"
DIRECTION_WE_BEHIND = "we-are-behind"
DIRECTION_BOTH = "both"


def _parse_semver_tuple(version: str) -> tuple[int, ...] | None:
    """Best-effort `MAJOR.MINOR.PATCH`-shaped string -> int tuple, or None.

    Deliberately narrow: only accepts a dot-separated run of digit groups
    (no pre-release/build metadata parsing) — sufficient for the
    `x-schema-version` vocabulary every vendored schema in this repo uses.
    Returns None rather than raising on anything else, so a caller can
    degrade to "cannot compare" instead of crashing on an unexpected string.
    """
    parts = version.split(".")
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None


# Leaf paths (as produced by _flatten_json) that check_schema_ahead_of_doe's
# leaf-retention check is told NOT to require claude-klabauter's copy to retain
# byte-for-byte -- UNIVERSAL entries only, i.e. ones that apply to every
# schema in the registry, not to one consumer's content. A consumer-specific
# exemption (e.g. a single schema's deliberately-rewritten description)
# belongs on that schema's `_QUEUE_SCHEMA_AHEAD_PINS` entry via
# `exempt_paths`, not here -- see check_schema_ahead_of_doe's `exempt_paths`
# parameter. Review: eng-director P2-3 -- a review-trail-specific path
# (`properties.scope_kind.description`) previously lived in this module-level
# constant and silently applied to every future schema entering the registry.
#
#   - x-schema-version is a single token that REPLACES rather than extends,
#     so retention cannot hold by construction: "1.1.0" is not a substring
#     of "1.2.0". It is not thereby unchecked -- check 3 asserts it is
#     strictly greater, which is the stronger statement.
#   - x-bump-class is a single token that REPLACES rather than extends
#     ("nested-field-additive" is not a substring of "major"), and carries
#     no schema semantics of its own.
#   - x-bump-note is NOT listed here: an ahead-bump APPENDS to the note, so
#     DoE's text is expected to stay a literal substring of claude-klabauter's -- the
#     same append-extend shape prose leaves like `description` have. It is
#     covered by `_PROSE_ANNOTATION_LEAF_KEYS` below instead (Review:
#     code-reviewer P1 -- the prior version of this comment claimed
#     x-bump-note was covered by the description-suffix rule, but the
#     predicate only matched a literal last-segment of "description", so
#     x-bump-note never actually hit it; a legitimate ahead-bump note append
#     failed the gate on the very case this carve-out exists for).
_AHEAD_RETENTION_EXEMPT_PATHS: frozenset[tuple] = frozenset(
    {
        ("x-schema-version",),
        ("x-bump-class",),
    }
)

# Leaf key names (the LAST segment of a flattened path) whose values are
# free-form prose annotations that legitimately grow over an ahead-bump --
# DoE's value being a substring of claude-klabauter's is an honest append, not a
# rewrite, for exactly these keys and no others. A NAMED set, not a suffix
# match or "x-" prefix glob: a glob would silently absorb any future
# code-consumed `x-*` key that is NOT prose (see DoE-claude's equivalent
# allowlist, named for the same reason). `$comment` is not listed here --
# it is stripped by `_strip_comment_annotations` before flattening, so it
# never reaches this retention check at all (D1's ruling: `$comment`
# carries no schema semantics).
_PROSE_ANNOTATION_LEAF_KEYS: frozenset[str] = frozenset({"description", "x-bump-note"})


def check_schema_ahead_of_doe(
    schema_path: str | Path,
    doe_repo_path: str | Path,
    *,
    doe_ref: str,
    reason: str,
    provenance: str,
    exempt_paths: frozenset = frozenset(),
    local_shape_hash: str | None = None,
) -> None:
    """Gating check for a pin declared "claude-klabauter leads DoE, awaiting upward vendor".

    `exempt_paths` is caller-trusted, same trust level as the module-level
    `_AHEAD_RETENTION_EXEMPT_PATHS` constant it replaced the hardcoded form
    of: the registry entry authoring it is developer-reviewed config, not an
    externally-reachable input, and this function does not constrain its
    breadth. A caller passing something broad enough (e.g. every leaf DoE's
    schema has) neuters the retention check to a no-op for that call, with
    nothing short of code review to catch it. Review: code-reviewer P2/P3.

    Sibling to `check_schema_drift` (the byte-identity tamper-pin): where that
    function asserts the vendored copy is UNCHANGED from a DoE ref, this one
    asserts the DECLARED-AHEAD state is still honest, by checking four things:

      1. DoE's copy at `doe_ref` is byte-identical to DoE's copy at the most
         recent commit touching this path on ANY local ref/branch in
         `doe_repo_path` (`git log --all`, not `HEAD` — a clone's checked-out
         branch is incidental, not the fact being asked about) — if DoE has
         moved since `doe_ref`, the ahead-claim is stale and this raises
         loudly rather than silently comparing against a ref DoE has already
         superseded. A dirty worktree on that path is refused rather than
         silently read past ("cannot check" is the honest answer, not
         "unmoved").
      2. Claude-klabauter's vendored copy RETAINS every leaf DoE's copy (at `doe_ref`)
         declares: exact equality, except (a) the named universal exemptions
         in `_AHEAD_RETENTION_EXEMPT_PATHS`, (b) any path in the caller-supplied
         `exempt_paths` (per-pin, consumer-specific exemptions belong here —
         see `_AHEAD_RETENTION_EXEMPT_PATHS`'s docstring comment), and (c) a
         shared leaf whose path ends in a key named in
         `_PROSE_ANNOTATION_LEAF_KEYS` (`description`, `x-bump-note`), where
         DoE's value being a substring of claude-klabauter's is treated as an honest
         append-extend rather than a rewrite. This is deliberately narrower than a general
         substring rule: retention is a DoE-leaf-presence check, not a claim
         that claude-klabauter's schema *validates* a superset of what DoE's does (leaf
         addition can be a narrowing — `additionalProperties: false`, a new
         `required` entry — which this check cannot and does not detect;
         name it "retention", not "superset" or "containment").
      3. Claude-klabauter's top-level `x-schema-version` parses as strictly greater
         than DoE's `x-schema-version` at `doe_ref`.
      4. If `local_shape_hash` is supplied, claude-klabauter's CURRENT vendored bytes
         hash to it (`_canonical_schema_text` + sha256) — the ahead-state's
         own local-tamper pin, since `check_schema_drift`'s ordinary byte-pin
         does not run while a schema is ahead-pinned (see
         `_local_shape_hash`).

    `reason` and `provenance` are not compared against anything — they exist
    so the caller is forced to state, at the call site, WHY this schema is
    allowed to be ahead and what commit/memo authorized it; both are folded
    into any raised SchemaDriftError so a failure is self-explanatory without
    a second lookup.

    Raises:
        SchemaDriftError: on any of the four checks above failing, on the
            DoE repo/ref being unreadable, on a dirty DoE worktree at this
            path, or on either side's schema text not parsing as a JSON
            object.

    Negative-spec: unlike `check_schema_drift`, this function is not byte-exact
    by design — it exists because byte-exactness is the wrong question to ask
    of a schema that is deliberately, legitimately ahead. It is still a real
    gate: every one of the checks above can fail and does raise, and any
    check this function CANNOT decide (a dirty DoE worktree, an unparseable
    version) fails closed rather than passing.

    When DoE later vendors claude-klabauter's version (or later), remove this schema's
    entry from the ahead-pin registry and restore an ordinary
    `check_schema_drift` byte-pin call at the new shared SHA — that is the
    designed exit path, not a permanent parking spot.
    """
    schema_path = Path(schema_path)
    doe_repo_path = Path(doe_repo_path)

    schema_filename = schema_path.name
    doe_schema_ref = f'coordinator/schemas/{schema_filename}'

    unusable = foreign_repo_unusable_reason(doe_repo_path)
    if unusable is not None:
        raise SchemaDriftError(
            f'Cannot read DoE schema "{doe_schema_ref}": the DoE clone at '
            f'{doe_repo_path} could not be read as a git repository ({unusable}). '
            'This is NOT an ahead-pin finding — the comparison never ran.'
        )

    def _run_git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ['git', '-C', str(doe_repo_path), *args],
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=FOREIGN_REPO_GIT_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            env=scoped_git_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _show(ref: str) -> str:
        result = _run_git('show', f'{ref}:{doe_schema_ref}')
        if result.returncode != 0:
            raise SchemaDriftError(
                f'Cannot read DoE {ref} schema "{doe_schema_ref}": {result.stderr.strip()}. '
                f'Ensure doe_repo_path ({doe_repo_path}) is a valid git repo with the schema at {ref}.'
            )
        return result.stdout

    # Check 1 resolves "has DoE moved" against the most recent commit touching
    # this path on ANY local ref (git log --all), never `HEAD` -- a sibling
    # clone's checked-out branch is incidental and shared-tree branch
    # switches are routine (see check_schema_ahead_of_doe's docstring).
    # Review: eng-director P2-2.
    dirty = _run_git('status', '--porcelain', '--', doe_schema_ref)
    if dirty.returncode != 0:
        raise SchemaDriftError(
            f'Cannot determine DoE worktree state for "{doe_schema_ref}" '
            f'in {doe_repo_path}: {dirty.stderr.strip()}.'
        )
    if dirty.stdout.strip():
        raise SchemaDriftError(
            f'Cannot check ahead-pin for "{schema_filename}": DoE clone at '
            f'{doe_repo_path} has uncommitted changes to "{doe_schema_ref}". '
            'A dirty DoE worktree cannot be read as "DoE has not moved" -- '
            'commit or discard the change in the DoE clone before re-running.'
        )

    all_refs_head = _run_git('log', '--all', '-1', '--format=%H', '--', doe_schema_ref)
    if all_refs_head.returncode != 0 or not all_refs_head.stdout.strip():
        raise SchemaDriftError(
            f'Cannot resolve the most recent commit touching "{doe_schema_ref}" '
            f'across all local refs in {doe_repo_path}: {all_refs_head.stderr.strip()}.'
        )
    doe_pinned_content = _show(doe_ref)
    doe_latest_content = _show(all_refs_head.stdout.strip())

    if doe_pinned_content != doe_latest_content:
        raise SchemaDriftError(
            f'Ahead-pin for "{schema_filename}" is STALE: DoE has moved '
            f'"{doe_schema_ref}" since the recorded ref {doe_ref} (it no '
            'longer matches the most recent commit touching that path across '
            'all local refs in the DoE clone). Re-derive the ahead-pin against '
            'the new DoE state, or -- if DoE has vendored claude-klabauter\'s version -- '
            'remove this ahead-pin entirely and restore an ordinary '
            'check_schema_drift byte-pin at the new shared SHA.\n'
            f'Recorded reason: {reason}\nRecorded provenance: {provenance}'
        )

    local_content = schema_path.read_text(encoding='utf-8')

    if local_shape_hash is not None:
        computed_hash = _local_shape_hash(local_content)
        if computed_hash != local_shape_hash:
            raise SchemaDriftError(
                f'Ahead-pin for "{schema_filename}" failed its local tamper-pin: '
                f'the vendored copy\'s canonical hash ({computed_hash}) does not '
                f'match the recorded local_shape_hash ({local_shape_hash}). While '
                'a schema is ahead-pinned, check_schema_drift\'s ordinary byte-pin '
                'does not run against it -- this is that state\'s own local-tamper '
                'pin, and it caught an unrecorded local edit. If the edit is '
                'intentional, update local_shape_hash in the ahead-pin registry '
                'entry to match.\n'
                f'Recorded reason: {reason}\nRecorded provenance: {provenance}'
            )

    local_parsed = _parse_schema_dict(local_content)
    doe_parsed = _parse_schema_dict(doe_pinned_content)
    if local_parsed is None or doe_parsed is None:
        raise SchemaDriftError(
            f'Ahead-pin for "{schema_filename}" could not be checked: one of '
            'the vendored copy or the DoE copy did not parse as a JSON object.'
        )

    local_flat = _flatten_json(_strip_comment_annotations(local_parsed))
    doe_flat = _flatten_json(_strip_comment_annotations(doe_parsed))

    all_exempt_paths = _AHEAD_RETENTION_EXEMPT_PATHS | frozenset(exempt_paths)

    not_retained: list[str] = []
    for path, doe_value in doe_flat.items():
        if path in all_exempt_paths:
            continue
        if path not in local_flat:
            not_retained.append(f'{".".join(str(p) for p in path)} (absent locally)')
            continue
        local_value = local_flat[path]
        if local_value == doe_value:
            continue
        is_prose_annotation_path = bool(path) and path[-1] in _PROSE_ANNOTATION_LEAF_KEYS
        if (
            is_prose_annotation_path
            and isinstance(local_value, str)
            and isinstance(doe_value, str)
            and doe_value in local_value
        ):
            continue
        not_retained.append(f'{".".join(str(p) for p in path)} (not retained: doe={doe_value!r} local={local_value!r})')

    if not_retained:
        raise SchemaDriftError(
            f'Ahead-pin for "{schema_filename}" failed the DoE-leaf-retention '
            f'check -- claude-klabauter\'s copy no longer retains DoE\'s ref {doe_ref} '
            f'content at: {"; ".join(not_retained)}.\n'
            f'Recorded reason: {reason}\nRecorded provenance: {provenance}'
        )

    local_version = _read_schema_version(local_content)
    doe_version = _read_schema_version(doe_pinned_content)
    local_semver = _parse_semver_tuple(local_version) if local_version else None
    doe_semver = _parse_semver_tuple(doe_version) if doe_version else None
    if local_semver is None or doe_semver is None:
        raise SchemaDriftError(
            f'Ahead-pin for "{schema_filename}" could not compare '
            f'x-schema-version (local={local_version!r}, doe={doe_version!r}) '
            '-- both sides must carry a parseable MAJOR.MINOR.PATCH version.'
        )
    if not (local_semver > doe_semver):
        raise SchemaDriftError(
            f'Ahead-pin for "{schema_filename}" requires claude-klabauter\'s '
            f'x-schema-version ({local_version}) to be strictly greater than '
            f'DoE\'s at ref {doe_ref} ({doe_version}), but it is not.\n'
            f'Recorded reason: {reason}\nRecorded provenance: {provenance}'
        )


def _local_shape_hash(local_content: str) -> str:
    """sha256 hex digest of a schema's canonical text -- the ahead-pin's own
    local-tamper pin (check_schema_ahead_of_doe's `local_shape_hash` check).

    Uses `_canonical_schema_text` (default strip_comments=True) so a
    `$comment`-only edit is not flagged as tamper, matching D1's ruling that
    `$comment` carries no schema semantics (see `_strip_comment_annotations`).
    Returns a hex digest rather than the raw text so a registry entry records
    a short, comparable token rather than embedding a full schema copy.
    """
    canonical = _canonical_schema_text(local_content)
    if canonical is None:
        # Unparseable content: hash the raw text so the pin still detects a
        # change (fails closed) rather than raising here and obscuring the
        # real problem (surfaced instead by the _parse_schema_dict check that
        # runs immediately after this is called).
        canonical = local_content
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _flatten_json(node: Any, prefix: tuple = ()) -> dict[tuple, Any]:
    """Flatten a parsed-JSON value into {path-tuple: leaf} for a structural diff.

    dict keys and list indices both become path segments, so a field added,
    removed, or reordered inside a nested `properties`/`allOf` block surfaces as
    a path present on only one side, not just a description string edit.
    """
    if isinstance(node, dict):
        out: dict[tuple, Any] = {}
        for key, value in node.items():
            out.update(_flatten_json(value, prefix + (key,)))
        return out
    if isinstance(node, list):
        out = {}
        for index, value in enumerate(node):
            out.update(_flatten_json(value, prefix + (index,)))
        return out
    return {prefix: node}


def _strip_comment_annotations(node: Any) -> Any:
    """Recursively drop every `$comment` KEY from a parsed-JSON value.

    D1 ruling (docs/plans/2026-08-03-vendored-schema-drift-canonical-normalization.md
    D1, ratified — not an open question): the vendored copy exists to mirror
    schema SEMANTICS, and `$comment` is by JSON-Schema definition a
    non-semantic annotation carrying no validation meaning, so a prose-only
    `$comment` edit is not drift. This is a separate concern from
    `_canonical_schema_text`'s formatting-blindness (whitespace/key-order/
    indentation) — that pass makes two byte-different-but-equal-JSON texts
    collapse to the same string; this pass additionally drops annotation keys
    that carry no semantic content at all, at every nesting depth, in both
    dict and list contexts.

    Negative-spec: only a dict KEY literally named `$comment` is dropped. A
    string reading "$comment" that appears as a VALUE — an array element, or
    the value of some other key — is an ordinary leaf value and survives
    untouched, exactly like any other string. Key-position and value-position
    are not interchangeable here; conflating them is the obvious way to get a
    recursive strip subtly wrong.

    Pure function: never mutates `node`, returns a new structure. Non-dict,
    non-list leaves are returned as-is.

    Spec backlink:
    cross-repo/inbox/2026-08-03-doe-claude-em-drift-normalize-yes-but-comment-survives-canonicalization.md
    """
    if isinstance(node, dict):
        return {
            key: _strip_comment_annotations(value)
            for key, value in node.items()
            if key != "$comment"
        }
    if isinstance(node, list):
        return [_strip_comment_annotations(item) for item in node]
    return node


def _canonical_schema_text(text: str, *, strip_comments: bool = True) -> str | None:
    """Canonical-JSON form of a schema text, for a formatting- and
    (optionally) annotation-blind comparison.

    Parses `text`, optionally strips every `$comment` KEY via
    `_strip_comment_annotations` (see that function for the D1 ruling and the
    key-vs-value negative-spec) when `strip_comments` is True (the default),
    then re-serializes with sorted keys and no incidental whitespace
    (`json.dumps(..., sort_keys=True, separators=(",", ":"))`). With the
    default, two texts that are the same JSON value modulo whitespace, key
    order, indentation, trailing newline, or `$comment` prose collapse to the
    same string.

    `strip_comments=False` performs the formatting-only normalization alone
    (no `$comment` strip) — the seam `check_schema_drift_advisory` uses to
    tell a genuine reformat apart from a `$comment`-only delta: comparing the
    `strip_comments=False` form on both sides answers "did formatting alone
    explain the match", independent of whatever the default-True comparison
    already answered.

    Returns None when `text` does not parse as JSON. Never raises — matches
    `_infer_drift_direction`'s degrade-never-raise contract in this module, so
    a malformed vendored schema falls back to the caller's byte comparison
    instead of raising out of a "just check for drift" call.
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    node = _strip_comment_annotations(parsed) if strip_comments else parsed
    return json.dumps(node, sort_keys=True, separators=(",", ":"))


def _infer_drift_direction(local_content: str, doe_content: str) -> str:
    """Best-effort AHEAD / BEHIND / BOTH read on a byte-diverged schema pair.

    Structural pass (preferred): flatten both sides' parsed JSON to leaf paths.
    A path present only locally is a local addition (AHEAD signal); a path
    present only on DoE's side is a DoE addition we haven't re-vendored
    (BEHIND signal). For a path both sides declare with a differing leaf value
    (e.g. a description string edited on one side), string containment gives a
    directional hint — the shorter string being a substring of the longer one
    reads as "the other side extended it"; anything else (values diverged in
    both directions, or non-string leaves that merely differ) cannot be
    directionally attributed and folds into BOTH, never a guessed AHEAD/BEHIND.

    Falls back to plain text containment when either side fails to parse as
    JSON — a malformed vendored file is exactly the case a structural diff
    cannot run over, but direction is still worth a best-effort answer rather
    than silence.

    Negative-spec: never raises — a comparison this uncertain by nature must
    degrade to the conservative BOTH reading, never a wrong-but-confident
    AHEAD/BEHIND. Only called when the two texts are already known to differ.
    """
    try:
        local_json = json.loads(local_content)
        doe_json = json.loads(doe_content)
    except (json.JSONDecodeError, ValueError):
        if local_content in doe_content:
            return DIRECTION_WE_BEHIND
        if doe_content in local_content:
            return DIRECTION_WE_AHEAD
        return DIRECTION_BOTH

    local_flat = _flatten_json(local_json)
    doe_flat = _flatten_json(doe_json)

    ahead = any(path not in doe_flat for path in local_flat)
    behind = any(path not in local_flat for path in doe_flat)

    for path, local_value in local_flat.items():
        if path not in doe_flat:
            continue
        doe_value = doe_flat[path]
        if local_value == doe_value:
            continue
        if isinstance(local_value, str) and isinstance(doe_value, str):
            if local_value != doe_value and local_value in doe_value:
                behind = True
            elif doe_value != local_value and doe_value in local_value:
                ahead = True
            else:
                ahead = behind = True
        else:
            ahead = behind = True

    if ahead and not behind:
        return DIRECTION_WE_AHEAD
    if behind and not ahead:
        return DIRECTION_WE_BEHIND
    return DIRECTION_BOTH


def _parse_schema_dict(content: str) -> dict | None:
    """Parse a schema JSON string to a dict, or None on any parse/shape failure.

    Shared single-parse seam for every top-level schema-annotation reader in this
    module (`_read_schema_version`, `_read_bump_class`, `_read_bump_note`, and any
    future top-level key readers) — the JSON parse happens exactly once per schema
    string. Callers must not add a second `json.loads` over the same content; add
    a new key read via `_read_schema_string_key` instead.

    Never raises — mirrors the never-raises contract of every caller in this file.
    """
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _read_schema_string_key(content: str, key: str) -> str | None:
    """Best-effort top-level string-key read out of a schema JSON string.

    Returns None on any failure to produce a confident string value: the text
    fails to parse as JSON, parses to something other than a JSON object, has no
    top-level `key`, or that key's value is not a string. Shared implementation
    behind `_read_schema_version`, `_read_bump_class`, and `_read_bump_note` — see
    `_parse_schema_dict` for the single-parse-per-string contract.

    Never raises — mirrors the never-raises contract of every caller in this file.
    """
    parsed = _parse_schema_dict(content)
    if parsed is None:
        return None
    value = parsed.get(key)
    return value if isinstance(value, str) else None


def _read_schema_version(content: str) -> str | None:
    """Best-effort top-level `x-schema-version` read out of a schema JSON string.

    Returns None on any failure to produce a confident string value: the text
    fails to parse as JSON, parses to something other than a JSON object, has no
    top-level `x-schema-version` key, or that key's value is not a string. This is
    the sole extraction point for `x-schema-version` — check_schema_drift_advisory
    and its callers (schema_drift_watch, the vendor_drift doctor probe) only pass
    the result through; they must never re-parse the version themselves (see
    schema_drift_watch's module docstring "SHAPE TO AVOID" note).

    Never raises — mirrors the never-raises contract of the function that calls it.
    """
    return _read_schema_string_key(content, "x-schema-version")


def _read_bump_class(content: str) -> str | None:
    """Best-effort top-level `x-bump-class` read out of a schema JSON string.

    Same contract as `_read_schema_version`: best-effort, None on any parse/shape
    failure, never a guess. Closed vocabulary on the producer side
    (`top-level-array-additive` / `nested-field-additive` / `major` — DR-097,
    cross-repo/inbox/2026-07-27-doe-claude-em-bump-class-shipped-and-a-correction.md)
    but this reader does not validate membership — it surfaces whatever string is
    present, verbatim, same as `_read_schema_version` does for the version string.
    Adoption is partial upstream (some schemas lack the key today); absence is an
    ordinary None here, never an error.

    Negative-spec: this function classifies nothing — it does not decide whether a
    bump class "holds" (holding rules are axis-dependent per DR-097 § Reconciliation
    and are out of scope for the drift watch entirely). Callers must not derive a
    hold/no-hold verdict from this value.

    Never raises — mirrors the never-raises contract of every caller in this file.
    """
    return _read_schema_string_key(content, "x-bump-class")


def _read_bump_note(content: str) -> str | None:
    """Best-effort top-level `x-bump-note` read out of a schema JSON string.

    Same contract as `_read_bump_class`: best-effort, None on any parse/shape
    failure or absence, never a guess. Optional one-line human note accompanying
    `x-bump-class` — see that function's docstring for the memo backlink.

    Never raises — mirrors the never-raises contract of every caller in this file.
    """
    return _read_schema_string_key(content, "x-bump-note")


_BUMP_ADDS_MISSING = object()


def _resolve_bump_adds_path(schema: dict, path: str) -> Any:
    """Traverse `schema` by a dot-separated key path, returning the node found.

    Returns the module-private `_BUMP_ADDS_MISSING` sentinel (never `None`,
    which is a legitimate JSON leaf value) the moment any path segment does
    not resolve to a dict key — including the degenerate case where an
    intermediate node isn't a dict at all (e.g. the path walks through a
    list or a scalar). No JSON Schema `$ref`/`allOf`/`oneOf` resolution:
    entries are meant to name the literal authored path a bump adds, not an
    evaluated/composed shape.
    """
    node: Any = schema
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return _BUMP_ADDS_MISSING
        node = node[part]
    return node


def _bump_adds_entry_resolves(schema: dict, entry: Any) -> bool:
    """Does one `x-bump-adds` entry actually resolve in `schema`?

    Two entry shapes:
      - a bare dot-path (e.g. "properties.foo.properties.bar") — resolves
        iff `_resolve_bump_adds_path` finds a node there.
      - "path:value" — resolves iff the node at `path` is itself a list
        containing `value`, or a dict with an `enum` key whose list
        contains `value`. Lets a bump claim "this enum gained a member"
        without claiming the whole enum key is new.

    Non-string entries never resolve — `x-bump-adds` is a list of strings,
    not a schema-shaped structure to recurse into.

    Negative-spec: the "path:value" split is unescaped and ambiguous on two
    axes, both fail-closed (reported as an unresolved entry, never a silent
    pass), never raised as a distinct "malformed" error:
      - A property name that itself contains a literal "." (valid JSON
        Schema, e.g. `properties["a.b"]`) is indistinguishable from a
        two-level dot-path `a.b` — this looks for nested key `b` under `a`
        instead of the flat key `"a.b"` and reports "does not resolve"
        rather than naming the ambiguity.
      - `entry.partition(":")` splits on the FIRST colon only, so
        `"properties.kind:alpha:beta"` treats `"alpha:beta"` as the enum
        value, not an error — deterministic but undocumented before this
        note. Review: code-reviewer P2.
    """
    if not isinstance(entry, str) or not entry:
        return False
    path, sep, value = entry.partition(":")
    if not sep:
        return _resolve_bump_adds_path(schema, path) is not _BUMP_ADDS_MISSING
    node = _resolve_bump_adds_path(schema, path)
    if node is _BUMP_ADDS_MISSING:
        return False
    if isinstance(node, list):
        enum_values: Any = node
    elif isinstance(node, dict):
        enum_values = node.get("enum")
    else:
        return False
    return isinstance(enum_values, list) and value in enum_values


def check_bump_adds_self_consistency(schema: dict) -> list[ErrorDict]:
    """Assert every `x-bump-adds` entry resolves in its OWN schema's bytes.

    `x-bump-adds` is a structured sibling to the prose `x-bump-note`: a list
    of property paths / enum values a bump claims to introduce. This closes
    the SELF-consistency gap — nothing previously compared a schema's own
    bump note to its own bytes (every existing drift/pin guard compares side
    A to side B, or file to pin). See
    state/improvement-queue/2026-08-10-vendored-schemas-have-no-machine-checkab-405eb7f5c628.yaml.

    Three-way key state, load-bearing (do not collapse to a boolean):
      - key ABSENT: the schema hasn't adopted the convention. Inert — `[]`
        errors, same as every claude-klabauter schema today. This is deliberate, not
        a stopgap: about a third of live bump notes describe removals or
        narrowings with nothing to add, and adoption is opt-in per schema.
      - `x-bump-adds: []`: an explicit, affirmative claim that this bump
        adds nothing (a removal/narrowing bump). Valid — `[]` errors, same
        outcome as absent, but a DIFFERENT state: this schema has adopted
        the convention and is asserting "checked, nothing to add" rather
        than "never considered it".
      - non-empty list: every entry must resolve via
        `_bump_adds_entry_resolves`, or this reports one ErrorDict per
        unresolved entry. A `x-bump-adds` value that isn't a list at all is
        itself a shape error (reported, not raised — this function never
        raises, matching every other check_* in this module's advisory
        surface).

    Negative-spec: this does NOT parse `x-bump-note` prose, does NOT compare
    against DoE HEAD or any pinned ref, and does NOT resolve `$ref` — it is
    a closed-world check of one schema dict against itself. It also does
    NOT enforce that every schema adopt `x-bump-adds` — key-absent is a
    permanently valid state, not a transitional one this function flags.

    Callers own reading the schema JSON into a dict; this function never
    touches disk or a repo path, mirroring the synthetic-schema-dict shape
    every test in test_schema_bump_adds_self_consistency.py exercises it
    with.
    """
    errors: list[ErrorDict] = []
    if not isinstance(schema, dict) or "x-bump-adds" not in schema:
        return errors

    adds = schema["x-bump-adds"]
    if not isinstance(adds, list):
        errors.append({
            'field': 'x-bump-adds',
            'error': 'x-bump-adds must be a list of strings',
            'hint': 'use [] to affirmatively claim a no-additions bump, or omit the key entirely if not yet adopted',
        })
        return errors

    for entry in adds:
        if not _bump_adds_entry_resolves(schema, entry):
            errors.append({
                'field': 'x-bump-adds',
                'error': f'claimed addition does not resolve in this schema: {entry!r}',
                'hint': 'entries are dot-paths into the schema (e.g. "properties.foo"), optionally suffixed ":value" to assert an enum member exists at that path',
            })

    return errors


def read_schema_version(content: str) -> str | None:
    """Public seam for `x-schema-version` extraction out of a schema JSON string.

    Same contract as the private implementation it delegates to: best-effort,
    never raises, None on any failure to produce a confident string value. Exists
    because the drift-scan seam's consumers legitimately need to read the version
    out of a schema blob they fetched themselves (a `git show` of a vendored file
    at some ref), which no field of the scan result can hand them. Callers outside
    this module use this name, not the underscore-prefixed one — a private-name
    import is an undeclared coupling that breaks silently on refactor.
    """
    return _read_schema_version(content)


def check_schema_drift_advisory(schema_path: str | Path, doe_repo_path: str | Path) -> dict:
    """Advisory: compare the vendored schema against DoE HEAD, never raising.

    Non-gating counterpart to check_schema_drift. Always compares the vendored copy
    against DoE HEAD (not a pin) and returns a result dict rather than raising — this
    is the "DoE has moved, consider re-vendoring" SIGNAL, kept structurally separate
    from the gating tamper-check so the two can never collide on one test/CI gate.

    Returns:
        dict with keys:
            schema (str): the vendored schema filename.
            diverged (bool): True if the vendored copy differs from DoE HEAD under a
                canonical-JSON comparison (via `_canonical_schema_text`), not a raw
                byte comparison. Both sides are parsed, have every `$comment` KEY
                (at any nesting depth, in objects and inside arrays) stripped via
                `_strip_comment_annotations`, and re-serialized with sorted keys
                and no incidental whitespace before comparing — so neither a
                reformat (whitespace, key order, indentation, trailing newline)
                NOR a prose-only `$comment` edit reports drift. D1 ruling
                (docs/plans/2026-08-03-vendored-schema-drift-canonical-normalization.md
                D1, ratified — not an open question): the vendored copy exists to
                mirror schema SEMANTICS, and `$comment` is by JSON-Schema
                definition a non-semantic annotation carrying no validation
                meaning, so a `$comment`-only delta is not drift. Negative-spec:
                this does NOT touch `$comment` as a *value* — a string literally
                reading "$comment" sitting in an array, or as some other key's
                value, is an ordinary leaf and still reports diverged=True on
                edit, exactly like any other value change. When either side fails
                to parse as JSON, the comparison falls back to the raw byte test,
                so a malformed vendored file is never normalized into looking
                clean. False both when the copy matches AND when the comparison
                could not be performed at all — see `determinate` to tell those
                two apart.
            determinate (bool): True iff the comparison actually ran to a verdict
                (both sides read successfully). False when the DoE repo/schema or the
                vendored file could not be read, i.e. "could not determine", NOT
                "no drift". Machine-readable discriminator for the `diverged=False`
                overload above, so a cadence consumer can surface an indeterminate
                run as indeterminate instead of sniffing `detail` prose or silently
                reporting green. Additive key (2026-07-22, drift-watch wiring); the
                schema/diverged/detail contract above is unchanged.
            direction (str | None): DIRECTION_WE_AHEAD / DIRECTION_WE_BEHIND /
                DIRECTION_BOTH when diverged=True and determinate=True — see
                _infer_drift_direction. None whenever diverged is False (matched
                or indeterminate; there is nothing to be ahead/behind ON).
                Additive key (2026-07-23, directionality wiring) — the schema/
                diverged/determinate/detail contract above is unchanged.
            divergence_kind (str | None): "shape" or "prose-only", orthogonal to
                `direction` — whether the delta touches validation SHAPE
                (schema_shape.semantic_shape_hash differs between the two parsed
                sides) or is confined to prose (description/$comment/x-bump-note;
                hashes match). None whenever diverged is False, OR when diverged
                is True but either side failed to parse as JSON (the
                text-containment fallback path has no parsed structure to hash —
                never guessed). Additive key (2026-08-13 parity-tail exchange:
                10 of 12 then-drifted schemas carried DIRECTION_BOTH's "reconcile
                by hand" prose despite byte-identical validation shape) — the
                schema/diverged/determinate/direction/detail contract above is
                unchanged.
            local_version (str | None): the vendored schema's top-level
                `x-schema-version` value, via `_read_schema_version`. None when
                the vendored file could not be read/parsed, or lacks the key, or
                the key's value isn't a string — never a guess. Populated
                whenever the vendored content was actually read, independent of
                whether the DoE side was readable. Additive key (2026-07-26,
                cross-repo schema-version surfacing — see
                cross-repo/inbox/2026-07-26-doe-claude-em-schema-drift-watch-seam-and-tolerance-ratification.md);
                the schema/diverged/determinate/direction/detail contract above
                is unchanged.
            doe_version (str | None): the same read applied to DoE HEAD's schema
                text. None when the DoE side could not be read (git failure,
                schema absent at HEAD) or its content failed the same parse.
                Populated whenever DoE's content was actually fetched, even if
                the vendored side then turned out to be unreadable. Additive key
                (2026-07-26, same backlink as local_version).
            local_bump_class (str | None): the vendored schema's top-level
                `x-bump-class` value, via `_read_bump_class`. Same
                populated-whenever-readable / None-on-any-failure contract as
                local_version. Adoption is partial upstream, so None is the
                ordinary "not yet adopted on this schema" case, not an error.
                Additive key (2026-07-27, bump-class surfacing — see
                cross-repo/inbox/2026-07-27-doe-claude-em-bump-class-shipped-and-a-correction.md);
                the schema/diverged/determinate/direction/detail/local_version/
                doe_version contract above is unchanged.
            doe_bump_class (str | None): the same read applied to DoE HEAD's
                schema text. Same contract as doe_version. Additive key
                (2026-07-27, same backlink as local_bump_class).
            doe_bump_note (str | None): DoE HEAD's optional top-level
                `x-bump-note` value, via `_read_bump_note` — a one-line human
                note accompanying `x-bump-class`. Same None-on-any-failure
                contract; not read from the vendored/local side (the note is a
                producer-authored annotation, only meaningful off DoE HEAD).
                Additive key (2026-07-27, same backlink as local_bump_class).
            detail (str): human-readable explanation.

    Negative-spec (bump-class fields): this function classifies nothing about the
    surfaced bump class — no hold/no-hold verdict is derived or emitted here.
    Holding rules are axis-dependent (DR-097 § Reconciliation) and out of scope.

    Negative-spec: never raises SchemaDriftError or any other exception on a normal
    comparison failure/mismatch — callers (e.g. tests) must not fail a test suite off
    this function's result. When the DoE repo is unreadable, returns diverged=False
    (with determinate=False) and an explanatory detail — unreadable is not evidence
    of divergence, and must never be reported as drift.

    Cadence consumer: coordinator_core.frontmatter.schema_drift_watch aggregates the
    whole vendored set for the claude-klabauter.schema.vendor_drift doctor probe — through
    `check_schema_drift_advisory_batch`, not through this one-element seam. A caller
    holding a SET must use the batch form: this one costs two processes per schema by
    construction, which is the amplification the batch exists to close.

    Negative-spec: the gating sibling `check_schema_drift` is byte-exact ON PURPOSE
    and must NOT be given this canonical-JSON treatment — its own docstring names
    the case it exists to catch ("detect silent drift (e.g. a formatter
    reformat)"), which is exactly the class of difference this function's
    canonicalization is designed to absorb. The two functions want opposite things
    from the same two byte strings: the tamper-check wants byte identity, this
    advisory wants semantic identity. That asymmetry is the design, not an
    inconsistency to reconcile.

    Spec backlink:
    cross-repo/inbox/2026-08-03-doe-claude-em-drift-normalize-yes-but-comment-survives-canonicalization.md
    """
    return check_schema_drift_advisory_batch([schema_path], doe_repo_path)[0]


def _advisory_indeterminate(schema_filename: str, detail: str) -> dict:
    """The advisory's diverged=False, determinate=False shape — "the comparison never ran".

    One constructor for every indeterminate exit so a new one cannot ship a
    different key set. Never carries a version or bump-class field: those are
    read off content this path did not obtain.
    """
    return {
        'schema': schema_filename,
        'diverged': False,
        'determinate': False,
        'direction': None,
        'divergence_kind': None,
        'local_version': None,
        'doe_version': None,
        'local_bump_class': None,
        'doe_bump_class': None,
        'doe_bump_note': None,
        'detail': detail,
    }


def check_schema_drift_advisory_batch(
    schema_paths: "Sequence[str | Path]", doe_repo_path: str | Path
) -> list[dict]:
    """Advisory over N vendored schemas against DoE HEAD, in TWO spawns whatever N is.

    The batched primary; `check_schema_drift_advisory` is its one-element call, not
    the other way round. The per-schema form spawned twice PER SCHEMA — a
    `foreign_repo_unusable_reason` probe whose answer does not vary with
    `schema_path` at all, and a `git show HEAD:<path>` — so the drift watch's loop
    over the vendored set was a 2N-process walk. Hoisting the invariant probe and
    replacing `show` with `git_scope.scoped_cat_file_batch` makes the whole set cost two,
    the same shape `schema_drift_watch.check_source_drift_advisory_batch` already
    uses against the cockpit clone.

    Results are returned in `schema_paths` order, one dict per input, in exactly the
    field shape `check_schema_drift_advisory` documents — that function's docstring
    is the contract for every key here.

    Negative-spec:
      - NEVER raises, and never reports an unreadable side as drift.
      - A batch is never all-or-nothing at the SCHEMA level: one token git cannot
        resolve makes that one entry indeterminate and leaves the rest comparable.
        A failure of the batch call ITSELF is indeterminate for every entry — that
        is "no comparison ran", not "no drift".
    """
    paths = [Path(p) for p in schema_paths]
    if not paths:
        return []
    doe_repo_path = Path(doe_repo_path)

    # An unreachable — or wrongly-resolved — DoE clone is INDETERMINATE, never
    # drift. `git -C` does not scope to a foreign repo on its own (see
    # check_schema_drift's git-scoping negative-spec and
    # `coordinator_core/git_scope.py`); without the confinement check, an
    # inherited GIT_DIR yields a determinate=True verdict computed against the
    # local repo's copy of the same path. Loop-invariant by inspection: the probe
    # reads the DoE clone, which does not vary across `schema_paths`.
    unusable = foreign_repo_unusable_reason(doe_repo_path)
    if unusable is not None:
        return [
            _advisory_indeterminate(
                path.name,
                f'DoE repo ({doe_repo_path}) could not be read as a git '
                f'repository ({unusable}) — drift could not be determined; '
                'this is not a claim that the vendored schema has diverged.',
            )
            for path in paths
        ]

    refs = {path: f'coordinator/schemas/{path.name}' for path in paths}
    blobs = scoped_cat_file_batch(
        doe_repo_path, sorted({f'HEAD:{ref}' for ref in refs.values()})
    )
    if blobs is None:
        return [
            _advisory_indeterminate(
                path.name,
                f'Could not run git against DoE repo ({doe_repo_path}): '
                '`git cat-file --batch` did not complete.',
            )
            for path in paths
        ]

    results: list[dict] = []
    for path in paths:
        doe_schema_ref = refs[path]
        doe_content = blobs.get(f'HEAD:{doe_schema_ref}')
        if doe_content is None:
            results.append(_advisory_indeterminate(
                path.name,
                f'Cannot read DoE HEAD schema "{doe_schema_ref}". '
                f'DoE repo path ({doe_repo_path}) unreadable or missing this schema at HEAD.',
            ))
            continue
        results.append(_advisory_compare(path, doe_schema_ref, doe_repo_path, doe_content))
    return results


def _advisory_compare(
    schema_path: Path, doe_schema_ref: str, doe_repo_path: Path, doe_content: str
) -> dict:
    """Reduce one vendored schema and its already-fetched DoE HEAD text to a verdict dict.

    Pure of subprocess: every git read happens once, above, for the whole batch.
    `check_schema_drift_advisory`'s docstring is the contract for the returned keys,
    including why the comparison is canonical-JSON rather than byte-exact.

    Line endings are normalised on the DoE side because the local side normalises
    too: `Path.read_text` opens in universal-newline mode, and so did the
    `subprocess.run(..., text=True)` `git show` this batch replaced. Decoding
    `cat-file --batch`'s bytes directly does not, so without this a CRLF blob would
    report as drift against an identical LF vendored copy on Windows — a false DRIFT
    introduced by the batching, not by anything upstream moved.
    """
    schema_filename = schema_path.name
    doe_content = doe_content.replace('\r\n', '\n').replace('\r', '\n')
    doe_version = _read_schema_version(doe_content)
    doe_bump_class = _read_bump_class(doe_content)
    doe_bump_note = _read_bump_note(doe_content)
    try:
        local_content = schema_path.read_text(encoding='utf-8')
    except OSError as exc:
        return {
            'schema': schema_filename,
            'diverged': False,
            'determinate': False,
            'direction': None,
            'divergence_kind': None,
            'local_version': None,
            'doe_version': doe_version,
            'local_bump_class': None,
            'doe_bump_class': doe_bump_class,
            'doe_bump_note': doe_bump_note,
            'detail': f'Could not read vendored schema at {schema_path}: {exc}',
        }

    local_version = _read_schema_version(local_content)
    local_bump_class = _read_bump_class(local_content)

    bytes_differ = local_content != doe_content
    if bytes_differ:
        local_canonical = _canonical_schema_text(local_content)
        doe_canonical = _canonical_schema_text(doe_content)
        canonical_match = (
            local_canonical is not None
            and doe_canonical is not None
            and local_canonical == doe_canonical
        )
        if canonical_match:
            local_format_only = _canonical_schema_text(local_content, strip_comments=False)
            doe_format_only = _canonical_schema_text(doe_content, strip_comments=False)
            formatting_alone_explains_match = (
                local_format_only is not None
                and doe_format_only is not None
                and local_format_only == doe_format_only
            )
            if formatting_alone_explains_match:
                match_detail = (
                    f'Vendored schema "{schema_filename}" matches DoE HEAD after '
                    'canonical JSON normalization (formatting-only delta: '
                    'whitespace, key order, or trailing newline).'
                )
            else:
                match_detail = (
                    f'Vendored schema "{schema_filename}" matches DoE HEAD after '
                    'canonical JSON normalization and $comment annotation '
                    'stripping (comment prose differs; no semantic delta — '
                    'D1 ruling).'
                )
            return {
                'schema': schema_filename,
                'diverged': False,
                'determinate': True,
                'direction': None,
                'divergence_kind': None,
                'local_version': local_version,
                'doe_version': doe_version,
                'local_bump_class': local_bump_class,
                'doe_bump_class': doe_bump_class,
                'doe_bump_note': doe_bump_note,
                'detail': match_detail,
            }

        direction = _infer_drift_direction(local_content, doe_content)
        direction_prose = {
            DIRECTION_WE_AHEAD: 'we are ahead of DoE HEAD (reconciliation pending upstream)',
            DIRECTION_WE_BEHIND: 'DoE HEAD is ahead of our pin — re-vendor now',
            DIRECTION_BOTH: 'both sides changed independently — reconcile by hand, a blind re-vendor would drop our side',
        }[direction]

        # divergence_kind: orthogonal axis to `direction` — does the delta touch
        # VALIDATION SHAPE or only PROSE? Computed via schema_shape.semantic_shape_hash,
        # the repo's one canonical shape hash (see that module's docstring) — never a
        # second hash implementation. None when either side fails to parse as JSON:
        # there is no parsed structure to hash, and the text-containment fallback this
        # function already used for `diverged` has no shape opinion to report.
        # Spec backlink: 2026-08-13 parity-tail exchange (DoE-EM flagged that "reconcile
        # by hand" on a punctuation-only diff trains readers to stop reading it).
        divergence_kind: str | None
        try:
            local_parsed = json.loads(local_content)
            doe_parsed = json.loads(doe_content)
        except (json.JSONDecodeError, ValueError):
            divergence_kind = None
        else:
            divergence_kind = (
                'prose-only'
                if semantic_shape_hash(local_parsed) == semantic_shape_hash(doe_parsed)
                else 'shape'
            )

        if divergence_kind == 'prose-only':
            kind_prose = (
                'prose-only — validation shape is identical, the delta is confined to '
                'description/$comment/x-bump-note text. A blind re-vendor still drops '
                'claude-klabauter\'s prose (regressed once, commit 1825e7771) — reconcile by hand'
            )
        else:
            kind_prose = direction_prose

        return {
            'schema': schema_filename,
            'diverged': True,
            'determinate': True,
            'direction': direction,
            'divergence_kind': divergence_kind,
            'local_version': local_version,
            'doe_version': doe_version,
            'local_bump_class': local_bump_class,
            'doe_bump_class': doe_bump_class,
            'doe_bump_note': doe_bump_note,
            'detail': (
                f'Vendored schema "{schema_filename}" diverges from DoE HEAD '
                f'({doe_repo_path}:{doe_schema_ref}) — {kind_prose}.'
            ),
        }

    return {
        'schema': schema_filename,
        'diverged': False,
        'determinate': True,
        'direction': None,
        'divergence_kind': None,
        'local_version': local_version,
        'doe_version': doe_version,
        'local_bump_class': local_bump_class,
        'doe_bump_class': doe_bump_class,
        'doe_bump_note': doe_bump_note,
        'detail': f'Vendored schema "{schema_filename}" matches DoE HEAD.',
    }


# =============================================================================
# T4d-g1a — legacy-YAML data layer: restricted YAML parser, glob matcher,
# schema loader/matcher, read-side frontmatter parser.
#
# Port of DoE-claude coordinator/bin/lib/schema.js lines 20-818 (parseYaml family,
# globToRegex/matchGlob, loadSchemas, matchSchema, matchSchemaForPath,
# parseFrontmatter). See module docstring "Legacy-YAML data-layer port" section
# for the full spec backlink and negative-spec.
#
# The legacy-YAML field VALIDATOR (validateField/validateFrontmatter) is ported
# below in "Legacy-YAML field validator (T4d-g1b)". Not included: the JSON-
# Schema-backed branch of validateFrontmatterDispatch (already covered by
# validate_frontmatter()/_validate_json_schema_node() above),
# applyCrossFieldRulesFor's public-wrapper form, testNegativeCorpus, and
# checkReferentialIntegrity — not needed by validate()'s callers.
# =============================================================================

# ---------------------------------------------------------------------------
# Restricted YAML parser (parseYaml family)
# ---------------------------------------------------------------------------

# Block-scalar indicator: `|` (literal) or `>` (folded), optionally followed by
# a chomping modifier (`-` strip, `+` keep) and/or an explicit indentation
# digit, in either order, then optional trailing whitespace/comment.
_BLOCK_SCALAR_RE = re.compile(r'^([|>])([+-]?[0-9]?|[0-9]?[+-]?)\s*(#.*)?$')

# Mapping-item discriminator: the slice after '- ' is a mapping key when it
# matches 'key:' or 'key: value'. A bare ':' in a URL (e.g. 'http://x') must
# NOT trigger the mapping path.
_LIST_ITEM_MAPPING_RE = re.compile(r'^[A-Za-z0-9_-]+:(\s|$)')


def _lstrip_len(s: str) -> int:
    """Length of leading whitespace — mirrors JS `raw.length - raw.trimStart().length`."""
    return len(s) - len(s.lstrip())


def _js_number(text: str) -> float | int | None:
    """Mirror JS `Number(text)` for the parse_scalar numeric-literal check.

    Returns None when JS `Number(text)` would produce NaN (i.e. text is not a
    valid JS numeric literal). Empty string maps to 0 in JS but parse_scalar's
    caller already special-cases text === '' via the `text !== ''` guard, so we
    don't need to replicate that quirk here — just don't crash on it.
    """
    t = text.strip()
    if t == '':
        return None
    try:
        if re.fullmatch(r'[+-]?\d+', t):
            return int(t)
        return float(t)
    except ValueError:
        return None


def _strip_inline_comment(text: str) -> str:
    """Strip a YAML-style trailing inline comment from a scalar text.

    Port of schema.js stripInlineComment (lines 347-378). A `#` begins a
    comment only when preceded by whitespace (or at string start) AND followed
    by whitespace (or end-of-string); a `#` inside a quoted span is literal.
    YAML single-quote escape (`''` -> literal `'`) is intentionally NOT
    unfolded here — mirrors the JS limitation verbatim (see JS docstring).
    """
    in_single = False
    in_double = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"' and not in_single:
            in_double = not in_double
        elif c == "'" and not in_double:
            if in_single and i + 1 < n and text[i + 1] == "'":
                i += 2
                continue
            in_single = not in_single
        elif c == '#' and not in_single and not in_double:
            prev_ws = i == 0 or text[i - 1].isspace()
            next_ws = i + 1 >= n or text[i + 1].isspace()
            if prev_ws and next_ws:
                return text[:i].rstrip()
        i += 1
    return text


def _parse_scalar(text: str) -> Any:
    """Port of schema.js parseScalar (lines 380-397)."""
    text = _strip_inline_comment(text)
    if text == 'null' or text == '~':
        return None
    if text == 'true':
        return True
    if text == 'false':
        return False
    n = _js_number(text)
    # Review: code-reviewer P1 — mirror the JS oracle's `isFinite(n)` guard in
    # parseScalar (schema.js:386). _js_number("Infinity")/(-Infinity)/(NaN)
    # succeed via Python float() with no ValueError, so without this guard
    # _js_number_str(n) crashes (OverflowError/ValueError on int(inf)/int(nan))
    # and that crash was silently swallowed by parse_frontmatter's blanket
    # `except Exception` into a false "no frontmatter" result for the whole
    # record. The oracle instead falls through cleanly to returning the
    # literal string for non-finite numeric text — do the same here.
    if n is not None and math.isfinite(n) and text != '' and _js_number_str(n) == text:
        return n
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return text[1:-1]
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1].replace("''", "'")
    return text


def _js_number_str(n: float | int) -> str:
    """Mirror JS `String(Number)` formatting closely enough for the
    parse_scalar round-trip check (`String(n) === text`).

    JS renders whole-valued floats without a trailing `.0` (`String(5.0) ===
    '5'`) and has no int/float type split. int stays as-is; float that is
    integral renders without a decimal point.
    """
    if isinstance(n, int):
        return str(n)
    if n == int(n) and abs(n) < 1e21:
        return str(int(n))
    return repr(n)


def _consume_block_scalar(lines: list[str], start: int, key_indent: int) -> tuple[str, int]:
    """Port of schema.js consumeBlockScalar (lines 59-94). Returns (value, next_line)."""
    body_lines: list[str] = []
    i = start
    last_content_line = -1
    while i < len(lines):
        raw = lines[i]
        if raw.strip() == '':
            body_lines.append('')
            i += 1
            continue
        indent = _lstrip_len(raw)
        if indent <= key_indent:
            break
        body_lines.append(raw)
        last_content_line = len(body_lines) - 1
        i += 1

    trimmed_body = [] if last_content_line == -1 else body_lines[: last_content_line + 1]
    strip_indent = 0
    for l in trimmed_body:
        if l.strip() != '':
            strip_indent = _lstrip_len(l)
            break
    text = '\n'.join('' if l == '' else l[strip_indent:] for l in trimmed_body)
    return text, i


def _parse_inline_list(text: str) -> list:
    """Port of schema.js parseInlineList (lines 304-324)."""
    inner = text[1:-1]
    if not inner.strip():
        return []
    items = [_parse_scalar(s.strip()) for s in inner.split(',')]
    bad = [idx for idx, v in enumerate(items) if v is None or v == '']
    if bad:
        raise ValueError(
            f'schema.js: malformed inline list — item(s) at position(s) '
            f'[{", ".join(str(b) for b in bad)}] parsed to null/empty in {text}. '
            'Remediation: check the inline list for trailing commas or empty items.'
        )
    return items


def _parse_yaml_lines(lines: list[str], start: int, base_indent: int) -> tuple[Any, int]:
    """Port of schema.js parseYamlLines (lines 100-190). Returns (value, next_line)."""
    result: dict[str, Any] = {}
    i = start
    n = len(lines)

    while i < n:
        raw = lines[i]
        trimmed = raw.rstrip()

        if trimmed == '' or trimmed.lstrip().startswith('#'):
            i += 1
            continue

        indent = _lstrip_len(raw)

        if indent < base_indent:
            break

        stripped_start = trimmed.lstrip()
        if stripped_start.startswith('- ') or stripped_start == '-':
            value = _parse_list(lines, i, base_indent)
            next_line = _skip_past(lines, i, base_indent)
            return value, next_line

        colon_idx = trimmed.find(':')
        if colon_idx == -1:
            i += 1
            continue

        key = trimmed[:colon_idx].strip()
        rest = trimmed[colon_idx + 1:].strip()

        if rest == '' or rest.startswith('#'):
            next_line = i + 1
            while next_line < n:
                peek_raw = lines[next_line]
                peek_stripped = peek_raw.strip()
                if peek_stripped and not peek_stripped.startswith('#'):
                    break
                next_line += 1
            if next_line < n:
                next_raw = lines[next_line]
                next_indent = _lstrip_len(next_raw)
                next_trimmed = next_raw.strip()
                next_is_list_item = next_trimmed.startswith('- ') or next_trimmed == '-'
                if next_indent > indent or (next_indent == indent and next_is_list_item):
                    nested_value, nested_next = _parse_yaml_lines(lines, next_line, next_indent)
                    result[key] = nested_value
                    i = nested_next
                    continue
            result[key] = None
        elif _BLOCK_SCALAR_RE.match(rest):
            block_value, block_next = _consume_block_scalar(lines, i + 1, indent)
            result[key] = block_value
            i = block_next
            continue
        else:
            stripped = _strip_inline_comment(rest)
            if stripped.startswith('[') and stripped.endswith(']'):
                result[key] = _parse_inline_list(stripped)
            else:
                result[key] = _parse_scalar(stripped)
        i += 1

    return result, i


def _parse_list(lines: list[str], start: int, base_indent: int) -> list:
    """Port of schema.js parseList (lines 208-266)."""
    result: list = []
    i = start
    n = len(lines)
    while i < n:
        raw = lines[i]
        trimmed = raw.rstrip().lstrip()
        if trimmed == '' or trimmed.startswith('#'):
            i += 1
            continue
        indent = _lstrip_len(raw)
        if indent < base_indent:
            break
        if trimmed.startswith('- '):
            item_content = trimmed[2:].strip()
            if _LIST_ITEM_MAPPING_RE.match(item_content):
                dash_indent = indent
                continuation_indent = dash_indent + 2
                cont_lines: list[str] = []
                k = i + 1
                while k < n:
                    cont_raw = lines[k]
                    cont_trimmed = cont_raw.rstrip()
                    if cont_trimmed == '':
                        cont_lines.append('')
                        k += 1
                        continue
                    cont_indent = _lstrip_len(cont_raw)
                    if cont_indent >= continuation_indent:
                        cont_lines.append(cont_raw)
                        k += 1
                    elif cont_trimmed.lstrip().startswith('#'):
                        k += 1
                    else:
                        break
                merged_lines = [item_content] + [
                    l[continuation_indent:] for l in cont_lines
                ]
                obj, _ = _parse_yaml_lines(merged_lines, 0, 0)
                result.append(obj)
                i = k
            else:
                result.append(_parse_scalar(item_content))
                i += 1
        elif trimmed == '-':
            result.append(None)
            i += 1
        else:
            break
    return result


def _skip_past(lines: list[str], start: int, base_indent: int) -> int:
    """Port of schema.js skipPast (lines 268-302)."""
    i = start
    n = len(lines)
    while i < n:
        raw = lines[i]
        trimmed = raw.rstrip().lstrip()
        if trimmed == '' or trimmed.startswith('#'):
            i += 1
            continue
        indent = _lstrip_len(raw)
        if indent < base_indent:
            break
        if trimmed.startswith('- ') or trimmed == '-':
            dash_indent = indent
            i += 1
            while i < n:
                cont_raw = lines[i]
                cont_trimmed = cont_raw.rstrip()
                if cont_trimmed == '' or cont_trimmed.lstrip().startswith('#'):
                    i += 1
                    continue
                cont_indent = _lstrip_len(cont_raw)
                if cont_indent > dash_indent:
                    i += 1
                else:
                    break
        else:
            break
    return i


def parse_yaml(text: str) -> Any:
    """Parse a restricted-YAML string into a plain Python object.

    Port of schema.js parseYaml (lines 33-36). Restricted to the subset used
    in coordinator schemas and frontmatter — scalar `key: value`, list items,
    one level of nested mapping, block scalars (`|`/`>`). Does NOT handle
    anchors, multi-line flow strings, or flow mappings beyond inline `[a, b]`
    lists.
    """
    lines = text.split('\n')
    value, _ = _parse_yaml_lines(lines, 0, 0)
    return value


# ---------------------------------------------------------------------------
# Glob matcher — port of schema.js globToRegex/matchGlob (lines 425-481).
# ---------------------------------------------------------------------------

_GLOB_ESCAPE_CHARS = set('.+^${}()|\\')


def _glob_to_regex(pattern: str) -> re.Pattern:
    """Convert a glob pattern to a compiled regex. Port of schema.js globToRegex.

    See module docstring negative-spec for the dotfile-exclusion invariant and
    the bracket-class first-`]` scan limitation (both reproduced verbatim from
    the JS oracle, not reinterpreted).
    """
    p = pattern.replace('\\', '/')
    re_parts: list[str] = []
    i = 0
    seg_start = True
    n = len(p)
    while i < n:
        c = p[i]
        if c == '*' and i + 1 < n and p[i + 1] == '*':
            re_parts.append(r'(?!\.).*' if seg_start else '.*')
            i += 2
            if i < n and p[i] == '/':
                i += 1
            seg_start = True
        elif c == '*':
            re_parts.append(r'(?!\.)[^/]*' if seg_start else '[^/]*')
            i += 1
            seg_start = False
        elif c == '?':
            re_parts.append('[^/.]' if seg_start else '[^/]')
            i += 1
            seg_start = False
        elif c == '[':
            close_idx = p.find(']', i + 1)
            if close_idx != -1:
                re_parts.append(p[i:close_idx + 1])
                i = close_idx + 1
                seg_start = False
            else:
                re_parts.append(r'\[')
                i += 1
                seg_start = False
        elif c in _GLOB_ESCAPE_CHARS:
            re_parts.append('\\' + c)
            i += 1
            seg_start = False
        else:
            re_parts.append(c)
            i += 1
            seg_start = (c == '/')
    return re.compile('^' + ''.join(re_parts) + '$')


def match_glob(pattern: str, file_path: str) -> bool:
    """Port of schema.js matchGlob (lines 478-481)."""
    normalised = file_path.replace('\\', '/')
    return _glob_to_regex(pattern).match(normalised) is not None


# ---------------------------------------------------------------------------
# Schema loader/matcher — port of schema.js loadSchemas/matchSchema/
# matchSchemaForPath (lines 509-737).
# ---------------------------------------------------------------------------

def _apply_default_match_mode(parsed: dict) -> None:
    """Port of schema.js applyDefaultMatchMode (lines 509-515). Mutates parsed in place."""
    if parsed.get('match_mode'):
        return
    applies_to = parsed.get('applies_to')
    if not isinstance(applies_to, str):
        return
    if re.search(r'\.(yaml|json)$', applies_to, re.IGNORECASE):
        parsed['match_mode'] = 'whole-document-yaml'


def load_schemas(schemas_dir: str | Path) -> dict[str, Any]:
    """Load all *.yaml and *.schema.json schema files from schemas_dir.

    Port of schema.js loadSchemas (lines 524-694). Returns
    {schema_name: parsed_schema, "_byGlob": [{"glob": ..., "schemaName": ...}, ...],
    "_byKind": {kind_value: schema_name}}.

    Dual-format loader: .yaml (legacy dialect) and .schema.json (JSON Schema
    draft-2020-12 subset) coexist; a .schema.json entry is stamped with
    parsed["_isJsonSchema"] = True (JS uses a non-enumerable property — Python
    has no enumerable/non-enumerable distinction on dict keys, so callers that
    iterate schema fields must skip the "_isJsonSchema" sentinel key explicitly,
    same as they already must skip "_byGlob"/"_byKind" on the top-level schemas dict).

    Fail-loud on: duplicate schema name registration, non-string applies_to,
    duplicate kind within one schema's own kinds list, duplicate kind ownership
    across two schemas, malformed JSON in a .schema.json file.
    """
    schemas_dir = Path(schemas_dir)
    schemas: dict[str, Any] = {'_byGlob': [], '_byKind': {}}

    yaml_files = sorted(f for f in os.listdir(schemas_dir) if f.endswith('.yaml'))
    for file in yaml_files:
        raw = (schemas_dir / file).read_text(encoding='utf-8')
        parsed = parse_yaml(raw)
        name = parsed.get('schema') or file[: -len('.yaml')]
        if name in schemas and name not in ('_byGlob', '_byKind'):
            raise ValueError(f'duplicate schema name "{name}" — a schema with this name is already registered')
        schemas[name] = parsed
        _apply_default_match_mode(parsed)
        if parsed.get('applies_to'):
            if not isinstance(parsed['applies_to'], str):
                kind = 'array' if isinstance(parsed['applies_to'], list) else type(parsed['applies_to']).__name__
                raise ValueError(
                    f'schema "{name}": applies_to must be a string glob pattern, got {kind}. '
                    'To cover multiple locations, use a brace-expansion glob or register separate schema files.'
                )
            schemas['_byGlob'].append({'glob': parsed['applies_to'], 'schemaName': name})

        kind_values: list[str] = []
        kinds_field = parsed.get('kinds')
        if isinstance(kinds_field, list):
            for v in kinds_field:
                if isinstance(v, str) and v:
                    kind_values.append(v)
                else:
                    # Review: code-reviewer P2 — mirror schema.js's stderr warning
                    # (schema.js:563) for a skipped non-string kinds element; the
                    # Python port previously filtered silently, making a typo'd
                    # kinds: entry invisible instead of diagnosable.
                    print(f'schema "{name}": skipping non-string kinds element: {json.dumps(v)}', file=sys.stderr)
        elif isinstance(parsed.get('kind'), str):
            kind_values.append(parsed['kind'])
        if len(set(kind_values)) != len(kind_values):
            raise ValueError(f'schema "{name}" declares a duplicate kind in its own kinds: list')
        if kind_values and not parsed.get('applies_to'):
            # Review: code-reviewer P2 — mirror schema.js's stderr warning
            # (schema.js:577) for kinds/kind declared with no applies_to (the
            # schema is kind-validated but invisible to query-records enumeration).
            print(f'schema "{name}": declares kinds/kind but has no applies_to — will be kind-validated but not enumerated by query-records', file=sys.stderr)
        for kind_value in kind_values:
            existing = schemas['_byKind'].get(kind_value)
            if existing is not None and existing != name:
                raise ValueError(f'duplicate kind "{kind_value}" declared by both {existing} and {name}')
            schemas['_byKind'][kind_value] = name

    json_schema_files = sorted(f for f in os.listdir(schemas_dir) if f.endswith('.schema.json'))
    for js_file in json_schema_files:
        raw_text = (schemas_dir / js_file).read_text(encoding='utf-8')
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as e:
            raise ValueError(f'schema file "{js_file}" is not valid JSON: {e}') from e

        name = parsed.get('x-schema-name') if isinstance(parsed.get('x-schema-name'), str) and parsed.get('x-schema-name') else js_file[: -len('.schema.json')]
        parsed['_isJsonSchema'] = True

        if name in schemas and name not in ('_byGlob', '_byKind'):
            raise ValueError(f'duplicate schema name "{name}" — a schema with this name is already registered')
        schemas[name] = parsed

        _apply_default_match_mode(parsed)

        if isinstance(parsed.get('applies_to'), str) and parsed.get('applies_to'):
            schemas['_byGlob'].append({'glob': parsed['applies_to'], 'schemaName': name})

        json_kind_values: list[str] = []
        raw_kinds_arr = parsed.get('x-kinds') if isinstance(parsed.get('x-kinds'), list) else (
            parsed.get('kinds') if isinstance(parsed.get('kinds'), list) else None
        )
        raw_kind_str = parsed.get('x-kind') if isinstance(parsed.get('x-kind'), str) else (
            parsed.get('kind') if isinstance(parsed.get('kind'), str) else None
        )
        if raw_kinds_arr is not None:
            for v in raw_kinds_arr:
                if isinstance(v, str) and v:
                    json_kind_values.append(v)
                else:
                    # Review: code-reviewer P2 — mirror schema.js's stderr warning
                    # (schema.js:653) for a skipped non-string x-kinds/kinds element.
                    print(f'schema "{name}": skipping non-string x-kinds/kinds element: {json.dumps(v)}', file=sys.stderr)
        elif raw_kind_str is not None:
            json_kind_values.append(raw_kind_str)
        if len(set(json_kind_values)) != len(json_kind_values):
            raise ValueError(f'schema "{name}" declares a duplicate kind in its own x-kinds/kinds list')
        if json_kind_values and not (isinstance(parsed.get('applies_to'), str) and parsed.get('applies_to')):
            # Review: code-reviewer P2 — mirror schema.js's stderr warning
            # (schema.js:664) for x-kinds/kinds declared with no applies_to.
            print(f'schema "{name}": declares x-kinds/kinds but has no applies_to — will be kind-validated but not enumerated by query-records', file=sys.stderr)
        for kind_value in json_kind_values:
            existing = schemas['_byKind'].get(kind_value)
            if existing is not None and existing != name:
                raise ValueError(f'duplicate kind "{kind_value}" declared by both {existing} and {name}')
            schemas['_byKind'][kind_value] = name

    def _specificity_key(entry: dict) -> tuple[int, int]:
        glob = entry['glob']
        wildcard_count = glob.count('*')
        return (wildcard_count, -len(glob))

    schemas['_byGlob'].sort(key=_specificity_key)

    return schemas


# ---------------------------------------------------------------------------
# Registry-derivation vendored contract — port of query-records.js
# _buildTypeToGlob (DoE-claude coordinator/bin/query-records.js:211-272).
#
# These two dicts are literal, hand-copied vendored contract (renames /
# overrides the schema-name-vs-query-type registries disagree on), NOT
# derivable from load_schemas() output — mirrored here so a caller building
# the registry's {query_type: glob} map natively (no node/query-records.js
# subprocess) gets byte-identical derivation to the live JS oracle. Any
# change to the JS originals must be mirrored here in the same edit.
# ---------------------------------------------------------------------------

# Schema name -> query --type name (five mismatched pairs). None means the
# schema is explicitly excluded from Part 1 derivation (handled via the
# Part-2 supplement in build_type_to_glob instead).
# Port of query-records.js _SCHEMA_NAME_TO_QUERY_TYPE (:126-134).
_SCHEMA_NAME_TO_QUERY_TYPE: dict[str, str | None] = {
    'completion-entry': 'completion',
    'lesson-entry': None,
    'bug-backlog': 'bug',
    'debt-backlog': 'debt',
    'improvement-queue': 'improvement',
}

# Glob overrides for schemas whose applies_to cannot be used verbatim as the
# registry glob (glob-engine incompatibility or intentional divergence).
# Port of query-records.js _GLOB_OVERRIDES (:136-141).
_GLOB_OVERRIDES: dict[str, str] = {
    'handoff-archived': 'archive/handoffs/**/*.md',
    'cross-repo-memo': 'cross-repo/inbox/*.md',
}

#: ``docs/plans/*.md`` entries that index the directory rather than being
#: plans in it. `coordinator_core.ops.ceremony.renderers` — the plan
#: collector, which excludes these same two basenames from being counted as
#: plans — independently carries an identical frozenset today. Collapsing
#: the two into this one definition (frontmatter is the lower-level module,
#: so the import direction is ceremony -> frontmatter, never the reverse)
#: is pending only because renderers.py sat under a live peer session's
#: claim when this landed; the two sets must not be allowed to drift.
_PLAN_DIR_INDEX_FILENAMES: frozenset[str] = frozenset({'INDEX.md', 'README.md'})

#: Record directories whose glob admits every `*.md` beneath them, so a
#: directory index authored in one is otherwise routed to that directory's
#: record schema and reported as a record missing every required field.
_RECORD_DIR_INDEX_PREFIXES: tuple[str, ...] = ('docs/plans/', 'docs/decisions/')


def _is_plan_dir_index_routing_excluded(normalised_repo_rel_path: str, frontmatter: dict | None) -> bool:
    """True when `normalised_repo_rel_path` (already backslash-normalised) is
    an `INDEX.md`/`README.md` directory index in one of
    `_RECORD_DIR_INDEX_PREFIXES` with no declared `kind:` — see `match_schema`'s
    second-amendment docstring.
    Shared by `match_schema` (per-file resolution) and `_run_tree_walk`'s
    whole-tree collection loop (:6519 area), which otherwise falls back to
    validating an unresolved file against whichever schema's glob collected
    it — a fallback that would silently defeat this exclusion for the
    whole-tree lint if the two call sites diverged.
    """
    in_record_dir = any(
        normalised_repo_rel_path.startswith(prefix) for prefix in _RECORD_DIR_INDEX_PREFIXES
    )
    if not (in_record_dir and normalised_repo_rel_path.count('/') == 2):
        return False
    basename = normalised_repo_rel_path.rsplit('/', 1)[-1]
    no_declared_kind = frontmatter is None or frontmatter.get('kind') is None
    return basename in _PLAN_DIR_INDEX_FILENAMES and no_declared_kind

# Part-2 supplements: non-schema'd / special-cased types with no *.yaml or
# *.schema.json file of their own (lesson is per-entry YAML keyed off
# lesson-entry with a non-standard type name; handoff-ledger is synthetic,
# parsed from Session Ledger tables rather than frontmatter).
# Port of query-records.js _buildTypeToGlob Part 2 (:247-263).
_TYPE_TO_GLOB_SUPPLEMENTS: dict[str, str] = {
    'lesson': 'state/lessons/*.yaml',
    'handoff-ledger': 'state/handoffs/*.md',
}


def build_type_to_glob(schemas_dir: str | Path) -> dict[str, str]:
    """Derive the {query_type: glob} registry map natively from schemas_dir.

    Port of query-records.js _buildTypeToGlob's typeToGlob derivation
    (:211-272), Part 1 (:229-245) + Part 2 (:247-263) — Part 3 (plan-sidecar
    regex derivation, :265-268) is out of scope here, this function returns
    only the type->glob map. Built on load_schemas() (this module's own port
    of loadSchemas) rather than re-parsing schema files, so both the JSON
    Schema validator and this registry-parity check share one schema-loading
    engine.

    Any caller that needs "is query type X recognised by the registry"
    without shelling out to `node query-records.js` gets it via
    `query_type in build_type_to_glob(schemas_dir)` — the dict-membership
    check IS the registry-recognition check.
    """
    schemas = load_schemas(schemas_dir)
    type_to_glob: dict[str, str] = {}
    for entry in schemas['_byGlob']:
        schema_name = entry['schemaName']
        glob = entry['glob']
        if schema_name == 'lesson-entry':
            continue
        query_type = _SCHEMA_NAME_TO_QUERY_TYPE.get(schema_name, schema_name)
        if query_type is None:
            continue
        type_to_glob[query_type] = _GLOB_OVERRIDES.get(schema_name, glob)
    type_to_glob.update(_TYPE_TO_GLOB_SUPPLEMENTS)
    return type_to_glob


def match_schema(repo_rel_path: str, frontmatter: dict | None, schemas: dict) -> dict | None:
    """Resolve schema for a file using archive-path-first (handoff-archived
    only), then kind-first, then glob-fallback strategy.

    Port of schema.js matchSchema (lines 708-726), narrowly amended
    2026-07-26 (PM-ratified): archived handoff records are immutable
    history and must validate against the relaxed handoff-archived schema,
    not the live handoff schema's current vocabulary.

    Negative-spec — do NOT generalise this into a blanket glob-before-kind
    inversion: every real archived handoff carries `kind: session-handoff`
    (or a sibling spinoff kind), so under the ORIGINAL kind-first order
    handoff-archived.schema.json was dead infrastructure — `frontmatter['kind']`
    always resolved via `_byKind` to the live `handoff` schema before the
    archive-path glob ever got a chance to fire (`_byKind` is populated
    uniformly from BOTH the legacy-YAML `kinds`/`kind` fields and the
    JSON-Schema `x-kinds`/`x-kind` fields — see `load_schemas` — so this
    dead-infrastructure diagnosis applies across both schema dialects, not
    just one). Reordering resolution for
    ALL schemas to fix this would change resolution for every other
    kind-classified schema too (much bigger blast radius than the one
    archived-handoff defect this fixes) — so the archive-path check below
    is scoped to exactly the `handoff-archived` glob and nothing else.
    Everything else keeps the original kind-first, glob-fallback order.

    Path-vs-kind precedence for non-handoff content under archive/handoffs/
    (Review: code-reviewer — F2): the archive-path check is additionally
    gated on kind — a file whose `kind:` is present but resolves (via
    `_byKind`) to some OTHER schema than `handoff` is NOT force-routed to
    handoff-archived; it falls through to normal kind-first resolution.
    Only a file with no declared kind, or a kind that resolves to `handoff`,
    is path-forced to handoff-archived. This protects a misfiled non-handoff
    record (by mistake, or a future co-located record type) under
    archive/handoffs/ from being validated against a schema its own `kind:`
    never asked for.

    Second narrow amendment, 2026-08-06 (this fix): a `docs/plans/*.md`
    entry named `INDEX.md` or `README.md` (the SAME
    `_PLAN_DIR_INDEX_FILENAMES` set the plan collector in
    `coordinator_core.ops.ceremony.renderers` already excludes from being
    counted as plans — see that module's docstring) resolves to None
    (no schema) UNLESS it declares a `kind:` — mirroring the archive-path
    kind-gate above, a file that explicitly opts into a kind is still
    routed via `_byKind`. `docs/plans/INDEX.md` is a GENERATED index, not a
    plan — it deliberately carries no frontmatter — so routing it to the
    `plan` schema's glob-fallback produced a permanent false-positive
    "missing required fields" violation on every render. This is a routing
    exclusion, not a schema change: the renderer must NOT start emitting
    plan frontmatter into INDEX.md, since that would make the generated
    index count itself as a plan on the next render (exactly what
    `_PLAN_DIR_INDEX_FILENAMES` exists to prevent).

    Negative-spec: does not blanket-skip `docs/plans/` by filename alone —
    only these two specific, already-excluded-by-the-collector basenames,
    and only absent a declared `kind:`.

    Returns {"schemaName": str, "schema": dict} or None.
    """
    normalised = repo_rel_path.replace('\\', '/')

    if _is_plan_dir_index_routing_excluded(normalised, frontmatter):
        return None

    # Archive-path precedence, scoped to handoff-archived only (see
    # negative-spec above). Reuses the SAME glob literal schema_validate.py
    # already carries in _GLOB_OVERRIDES (the query-records.js-parity
    # registry-glob override, defined above this function) rather than
    # re-deriving or re-copying the 'archive/handoffs/**/*.md' string —
    # this repo already carries that literal in two places
    # (this module's _GLOB_OVERRIDES and coordinator_core/ops/records_query.py's
    # _TYPE_TO_GLOB); this precedence check must not become a third/fourth copy.
    archived_glob = _GLOB_OVERRIDES.get('handoff-archived')
    declared_kind = frontmatter.get('kind') if frontmatter is not None else None
    # kind-gate (F2): only force handoff-archived when there is no declared
    # kind at all, or the declared kind is itself a handoff-family kind
    # (resolves to 'handoff' via _byKind) — reuses the SAME dispatch table
    # _byKind already carries rather than hand-listing the handoff kinds
    # (session-handoff/spinoff/spinoff-roadmap/spinoff-goal/
    # spinoff-roadmap-creator/recovery/spike-result) a second time.
    kind_permits_archive_route = (
        declared_kind is None
        or schemas['_byKind'].get(str(declared_kind)) == 'handoff'
    )
    if (
        archived_glob is not None
        and 'handoff-archived' in schemas
        and kind_permits_archive_route
        and match_glob(archived_glob, normalised)
    ):
        return {'schemaName': 'handoff-archived', 'schema': schemas['handoff-archived']}

    if frontmatter is not None and frontmatter.get('kind') is not None:
        kind_value = str(frontmatter['kind'])
        schema_name = schemas['_byKind'].get(kind_value)
        if schema_name is not None:
            return {'schemaName': schema_name, 'schema': schemas[schema_name]}

    for entry in schemas['_byGlob']:
        if match_glob(entry['glob'], normalised):
            return {'schemaName': entry['schemaName'], 'schema': schemas[entry['schemaName']]}
    return None


def match_schema_for_path(repo_rel_path: str, schemas: dict) -> dict | None:
    """Find the schema that matches repo_rel_path (path-only, no frontmatter).

    Port of schema.js matchSchemaForPath (lines 735-737). Delegates to
    match_schema with frontmatter=None.
    """
    return match_schema(repo_rel_path, None, schemas)


# ---------------------------------------------------------------------------
# Read-side frontmatter parser — port of schema.js parseFrontmatter (lines 756-818).
# ---------------------------------------------------------------------------

_FM_CLOSE_RE = re.compile(r'^---\s*$', re.MULTILINE)


def parse_frontmatter(content: str) -> dict[str, Any]:
    """Extract YAML frontmatter from markdown content.

    Port of schema.js parseFrontmatter. Expects optional "---\\n...\\n---\\n"
    delimiters at the start, optionally preceded by one or more HTML comment
    blocks (<!-- ... -->) and surrounding whitespace. Returns
    {"frontmatter": dict | None, "body": str}.

    When frontmatter is present, body is the content AFTER the closing ---
    delimiter; a leading HTML comment (if any) is excluded from body.

    Negative-spec: an unclosed leading <!-- comment, or a parsed YAML block
    that is empty ({}), both result in {"frontmatter": None, "body": content}
    (whole original content, comment included) — mirrors the JS oracle's
    no-frontmatter fallback exactly, including its "return original content"
    behavior for the unclosed-comment case (not "content minus the dangling
    comment fragment").
    """
    cursor = 0
    while True:
        ws_match = re.match(r'^\s*', content[cursor:])
        ws_len = len(ws_match.group(0)) if ws_match else 0
        after_ws = cursor + ws_len
        if content[after_ws:after_ws + 4] == '<!--':
            close_idx = content.find('-->', after_ws + 4)
            if close_idx == -1:
                return {'frontmatter': None, 'body': content}
            cursor = close_idx + 3
        else:
            cursor = after_ws
            break

    remaining = content[cursor:]
    if not remaining.startswith('---'):
        return {'frontmatter': None, 'body': content}

    after_first = remaining[3:]
    first_newline = after_first.find('\n')
    if first_newline == -1:
        return {'frontmatter': None, 'body': content}

    if after_first[:first_newline].strip() != '':
        return {'frontmatter': None, 'body': content}

    rest = after_first[first_newline + 1:]
    close_match = _FM_CLOSE_RE.search(rest)
    if close_match is None:
        return {'frontmatter': None, 'body': content}

    yaml_block = rest[: close_match.start()]
    body = re.sub(r'^---\s*\n?', '', rest[close_match.start():])
    try:
        fm = parse_yaml(yaml_block)
        if fm is None or not isinstance(fm, dict) or len(fm) == 0:
            return {'frontmatter': None, 'body': content}
        return {'frontmatter': fm, 'body': body}
    except Exception as exc:
        # Broad by design: any parse_yaml failure on the delimited block is
        # treated identically to "no frontmatter" — mirrors the JS oracle's
        # negative-spec fallback (see docstring), not just the explicitly
        # documented empty-dict/unclosed-comment cases. Debug-logged so a
        # genuinely malformed frontmatter block is still discoverable.
        logger.debug("parse_frontmatter: parse_yaml failed, treating as no frontmatter: %s", exc)
        return {'frontmatter': None, 'body': content}


# =============================================================================
# Legacy-YAML field validator (T4d-g1b) — port of schema.js validateField /
# validateFrontmatter's YAML-dialect branch, plus their checkType /
# suggestNearMiss / NEAR_MISS_CANONICAL helpers.
#
# Spec backlink: DoE-claude coordinator/bin/lib/schema.js — NEAR_MISS_CANONICAL
#   (line 1026), suggestNearMiss (line 1049), validateField (line 1059),
#   validateFrontmatter YAML-dialect branch (lines 1134-1186), checkType
#   (line 2535).
# Negative-spec: the JSON-Schema-backed branch of validateFrontmatter
#   (schema.js lines 1128-1132, `isJsonSchemaBacked(schema)`) is NOT re-ported
#   here — it is already covered by this module's own validate_frontmatter()/
#   _validate_json_schema_node() above. The two validators are independent by
#   design (ccos-1 W2.5 constraint) and must not be merged.
# =============================================================================

# Curated near-miss -> canonical enum-value map (design-as-offers). Port of
# schema.js NEAR_MISS_CANONICAL (line 1026). Documented-immutable by
# convention — Python has no Object.freeze equivalent for a dict literal.
_NEAR_MISS_CANONICAL: dict[str, str] = {
    'ratified': 'accepted',
    'rejected': 'deprecated',
}


def _suggest_near_miss(value: Any, allowed: list) -> str:
    """Suggest a canonical replacement for a near-miss enum value.

    Port of schema.js suggestNearMiss (line 1049). Looks up `value`
    (case-insensitively) in `_NEAR_MISS_CANONICAL`; only emits a suggestion if
    the canonical replacement is itself a legal member of `allowed` (keeps the
    global near-miss map safe across unrelated enums). Returns a leading-space
    " Did you mean 'x'?" suffix, or '' when no safe suggestion applies.
    """
    key = str(value).lower()
    canonical = _NEAR_MISS_CANONICAL.get(key)
    if not canonical:
        return ''
    if not isinstance(allowed, list) or not any(str(a).lower() == canonical for a in allowed):
        return ''
    return f" Did you mean '{canonical}'?"


def _js_typeof(value: Any) -> str:
    """Approximate JS `typeof` for legacy-validator error-message parity.

    Port-support helper (schema.js error messages interpolate `typeof value`
    directly; this reproduces the same string for values PyYAML/json produce).
    `None` maps to 'object' (JS `typeof null === 'object'`); lists and dicts
    both map to 'object' (JS `typeof [] === 'object'` too — array-vs-object
    disambiguation, where a caller needs it, is done via an explicit
    list-isinstance check, not via this helper, mirroring schema.js's own
    `Array.isArray` guards alongside `typeof`).
    """
    if value is None:
        return 'object'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, (int, float)):
        return 'number'
    if isinstance(value, str):
        return 'string'
    return 'object'


def _check_type(field: str, value: Any, type_: str) -> ErrorDict | None:
    """Port of schema.js checkType (line 2535). Legacy scalar-type spec check.

    Only the four legacy scalar type tags are recognised (string, iso-date,
    number, boolean). An unrecognised type tag silently passes — mirrors
    schema.js falling through with no `else` branch (not an error at this
    layer).
    """
    if type_ == 'string':
        if not isinstance(value, str):
            return {
                'field': field,
                'error': f'expected string, got {_js_typeof(value)}',
                'hint': f'Provide a string value for "{field}"',
            }
    elif type_ == 'iso-date':
        if not isinstance(value, str) or not re.match(r'^\d{4}-\d{2}-\d{2}', value):
            return {
                'field': field,
                'error': f'expected ISO date (YYYY-MM-DD), got "{value}"',
                'hint': 'Use format YYYY-MM-DD',
            }
    elif type_ == 'number':
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return {
                'field': field,
                'error': f'expected number, got {_js_typeof(value)}',
                'hint': 'Provide a numeric value',
            }
    elif type_ == 'boolean':
        if not isinstance(value, bool):
            return {
                'field': field,
                'error': f'expected boolean, got {_js_typeof(value)}',
                'hint': 'Use bare true or false (no quotes)',
            }
    return None


def _is_string_or_null_spec(spec: Any) -> bool:
    """True for both object-form {type: 'string-or-null'} and bare-string 'string-or-null'."""
    return (isinstance(spec, dict) and spec.get('type') == 'string-or-null') or spec == 'string-or-null'


def _is_number_or_null_spec(spec: Any) -> bool:
    """True for both object-form {type: 'number-or-null'} and bare-string 'number-or-null'."""
    return (isinstance(spec, dict) and spec.get('type') == 'number-or-null') or spec == 'number-or-null'


def _validate_legacy_field(field: str, value: Any, spec: Any) -> list[ErrorDict]:
    """Validate one field value against its legacy-YAML-dialect field spec.

    Port of schema.js validateField (line 1059). Spec shapes accepted:
      - bare string (legacy):  "string" | "iso-date" | "number" | "boolean"
      - object spec:           {type: "enum", values: [...]}
                                {type: "string-or-null"} / {type: "number-or-null"}
                                {type: "list-of-string"}
                                {type: "object", fields: {sub: spec, ...}}

    Recurses into `type: object` specs that declare a `fields:` sub-spec
    block; sub-field nulls/missing are tolerated (sub-fields are implicitly
    optional) — the error `field` path is dotted (e.g. `loe.tshirt`).
    """
    errors: list[ErrorDict] = []

    if isinstance(spec, str):
        err = _check_type(field, value, spec)
        if err:
            errors.append(err)
        return errors

    if not isinstance(spec, dict):
        return errors

    type_ = spec.get('type')

    if type_ == 'enum':
        allowed = spec.get('values') or []
        if str(value) not in [str(a) for a in allowed]:
            errors.append({
                'field': field,
                'error': f'invalid enum value "{value}"',
                'hint': f'Allowed values: {", ".join(str(a) for a in allowed)}.{_suggest_near_miss(value, allowed)}',
            })
    elif type_ == 'string-or-null':
        if value is not None and not isinstance(value, str):
            errors.append({
                'field': field,
                'error': f'expected string or null, got {_js_typeof(value)}',
                'hint': 'Set to a string or null',
            })
    elif type_ == 'number-or-null':
        if value is not None and not (isinstance(value, (int, float)) and not isinstance(value, bool)):
            errors.append({
                'field': field,
                'error': f'expected number or null, got {_js_typeof(value)}',
                'hint': 'Set to a number or null',
            })
    elif type_ == 'list-of-string':
        if not isinstance(value, list):
            errors.append({
                'field': field,
                'error': 'expected a list',
                'hint': 'Use YAML list syntax, e.g. ["name"]',
            })
        else:
            bad = [v for v in value if not isinstance(v, str)]
            if bad:
                errors.append({
                    'field': field,
                    'error': 'list contains non-string items',
                    'hint': 'All list items must be strings',
                })
    elif type_ == 'object':
        if not isinstance(value, dict):
            actual = 'array' if isinstance(value, list) else ('null' if value is None else _js_typeof(value))
            errors.append({
                'field': field,
                'error': f'expected object, got {actual}',
                'hint': 'Use YAML nested mapping syntax',
            })
        elif isinstance(spec.get('fields'), dict):
            for sub_field, sub_spec in spec['fields'].items():
                sub_value = value.get(sub_field)
                if sub_value is None:
                    continue
                errors.extend(_validate_legacy_field(f'{field}.{sub_field}', sub_value, sub_spec))
    else:
        # Unknown type tag — fall through to _check_type (silently passes unknowns).
        err = _check_type(field, value, type_)
        if err:
            errors.append(err)

    return errors


def _validate_legacy_yaml_frontmatter(frontmatter: dict | None, schema: dict) -> dict:
    """Validate a frontmatter dict against a legacy-YAML-dialect schema.

    Port of schema.js validateFrontmatter's YAML-dialect branch (lines
    1134-1186) — the JSON-Schema-backed branch is validate_frontmatter()/
    _validate_json_schema_node() above; kept independent by design (ccos-1
    W2.5 constraint), not merged.

    Returns {"ok": True} or {"ok": False, "errors": [ErrorDict, ...]}.

    Negative-spec: a schema with no `required:` block is treated as
    unconditionally valid (mirrors schema.js `if (!schema || !schema.required)
    return {ok: true}`) — a bare applies_to-only schema validates everything.
    """
    if not schema or not schema.get('required'):
        return {'ok': True}
    if not frontmatter:
        return {
            'ok': False,
            'errors': [{
                'field': '(frontmatter)',
                'error': 'missing frontmatter block',
                'hint': 'Add --- delimited YAML frontmatter at the top of the file',
            }],
        }

    errors: list[ErrorDict] = []
    required_spec = schema.get('required') or {}
    for field, spec in required_spec.items():
        present = field in frontmatter
        value = frontmatter.get(field)
        is_missing = (not present) or (
            value is None and not (_is_string_or_null_spec(spec) or _is_number_or_null_spec(spec))
        )
        if is_missing:
            errors.append({
                'field': field,
                'error': 'required field missing',
                'hint': f'Add "{field}:" to frontmatter',
            })
            continue
        errors.extend(_validate_legacy_field(field, value, spec))

    # Optional fields: only shape (type/enum/list) is checked; presence is not
    # required. Required-field shape was already checked above.
    optional_spec = schema.get('optional')
    if isinstance(optional_spec, dict):
        for field, spec in optional_spec.items():
            if field not in frontmatter:
                continue
            value = frontmatter.get(field)
            if value is None:
                continue
            errors.extend(_validate_legacy_field(field, value, spec))

    # Cross-field rules — keyed by the schema's OWN declared name
    # (schema['schema'], the YAML-dialect self-name field) — schema.js
    # applyCrossFieldRules keys off schema.schema, not the caller's registry
    # lookup key (schema.js:2398). _CROSS_FIELD_RULES_BY_SCHEMA today only
    # registers "handoff"/"handoff-archived"/"cross-repo-memo" (all
    # JSON-Schema-backed in claude-klabauter's vendored set); a YAML-dialect schema name
    # looks up to [] (no rules) — parity with schema.js's own CROSS_FIELD_RULES,
    # which likewise has no entries for any currently-vendored YAML-dialect
    # (queue) schema.
    schema_own_name = schema.get('schema')
    if schema_own_name:
        errors.extend(_apply_cross_field_rules(frontmatter, schema_own_name))

    return {'ok': True} if not errors else {'ok': False, 'errors': errors}


# ---------------------------------------------------------------------------
# describe() — port of schema-cli.js --describe (describeYamlSchema +
# describeJsonSchema, lines 97-151).
#
# Reads from claude-klabauter's vendored schemas directory (coordinator_core/frontmatter/
# schemas/) rather than an arbitrary schemas dict, mirroring schema-cli.js's own
# fixed COORDINATOR_SCHEMAS_DIR resolution model (env-override not needed here —
# claude-klabauter vendors a single fixed schema set, no consumer-test fixture redirection).
# ---------------------------------------------------------------------------

_SCHEMAS_DIR: Path = Path(__file__).parent / "schemas"


def _extract_enum_values(spec: Any) -> list | None:
    """Extract the enum value list from a YAML-dialect field spec.

    Port of schema.js extractEnumValues (line 97). Returns the values array
    when spec is {type: 'enum', values: [...]}, None otherwise.
    """
    if isinstance(spec, dict) and spec.get('type') == 'enum' and isinstance(spec.get('values'), list):
        return spec['values']
    return None


def _describe_yaml_schema(schema: dict) -> dict:
    """Describe a YAML-dialect schema. Port of schema.js describeYamlSchema (line 109).

    required/optional preserve schema YAML key insertion order (dict iteration
    order in both Python 3.7+ and the parse_yaml/parseYaml loaders is insertion
    order, so no explicit re-sort is needed).
    """
    required_spec = schema.get('required')
    optional_spec = schema.get('optional')
    required = list(required_spec.keys()) if isinstance(required_spec, dict) else []
    optional = list(optional_spec.keys()) if isinstance(optional_spec, dict) else []
    enums: dict[str, list] = {}

    if isinstance(required_spec, dict):
        for field, spec in required_spec.items():
            vals = _extract_enum_values(spec)
            if vals is not None:
                enums[field] = vals
    if isinstance(optional_spec, dict):
        for field, spec in optional_spec.items():
            vals = _extract_enum_values(spec)
            if vals is not None:
                enums[field] = vals

    return {
        'required': required,
        'optional': optional,
        'enums': enums,
        'applies_to': schema.get('applies_to'),
    }


def _describe_json_schema(schema: dict) -> dict:
    """Describe a JSON-Schema-backed schema. Port of schema.js describeJsonSchema (line 136).

    required preserves the order declared in the JSON Schema `properties` object,
    filtered to those listed in `required`; optional is the remainder — NOT the
    order of the `required[]` array itself. dict iteration order over `properties`
    (both here and in the json.load()-produced dict) is source-declaration order.
    """
    properties = schema.get('properties')
    properties = properties if isinstance(properties, dict) else {}
    required_field = schema.get('required')
    required_set = set(required_field) if isinstance(required_field, list) else set()
    all_props = list(properties.keys())
    required = [k for k in all_props if k in required_set]
    optional = [k for k in all_props if k not in required_set]
    enums: dict[str, list] = {}

    for field, prop_schema in properties.items():
        if isinstance(prop_schema, dict) and isinstance(prop_schema.get('enum'), list):
            enums[field] = prop_schema['enum']

    return {
        'required': required,
        'optional': optional,
        'enums': enums,
        'applies_to': schema.get('applies_to'),
    }


def describe(schema_name: str) -> dict:
    """Describe a vendored schema — port of schema-cli.js --describe mode (lines 191-197).

    Loads claude-klabauter's vendored schema set (coordinator_core/frontmatter/schemas/)
    via load_schemas() and returns:
      {"required": [...], "optional": [...], "enums": {...}, "applies_to": str | None}

    required/optional are ORDERED field-name arrays in schema declaration order
    (properties-declaration order for JSON-Schema-backed schemas — NOT the order
    of the schema's required[] array). applies_to is always present in the
    returned dict, with value None when the source schema declares none.

    Dispatch mirrors schema.js's `_isJsonSchemaBacked(schema) ? describeJsonSchema
    : describeYamlSchema` (line 192): a schema loaded from a .schema.json file is
    stamped `_isJsonSchema: True` by load_schemas(); all schemas currently
    vendored under coordinator_core/frontmatter/schemas/ are .schema.json, so the
    YAML-dialect branch is exercised only if/when a .yaml schema is vendored here.

    Raises:
        ValueError: schema_name is not a registered schema (or is one of the
            internal "_byGlob"/"_byKind" index keys).
    """
    schemas = load_schemas(_SCHEMAS_DIR)
    if schema_name not in schemas or schema_name in ('_byGlob', '_byKind'):
        available = ', '.join(k for k in schemas if k not in ('_byGlob', '_byKind'))
        raise ValueError(f'unknown schema "{schema_name}". Available: {available}')
    schema = schemas[schema_name]
    if schema.get('_isJsonSchema'):
        return _describe_json_schema(schema)
    return _describe_yaml_schema(schema)


# ---------------------------------------------------------------------------
# validate() — port of schema-cli.js --validate mode's dispatch + envelope
# (validateFrontmatterDispatch, schema.js:3100; envelope schema-cli.js:213-227).
#
# Native replacement surface for the `node schema-cli.js --validate
# <schema_name>` shell-out (e.g. queue_append.py's _schema_cli_validate).
# ---------------------------------------------------------------------------

def _classify_schema_dialect(schema: dict) -> str:
    """Resolve which validator dialect a schema object belongs to.

    Returns 'json' (JSON-Schema draft-2020-12 subset), 'yaml' (legacy
    YAML-dialect), or 'unknown' (matches neither dialect's structural tells).

    Resolution order, and why:

    1. **The `_isJsonSchema` stamp wins whenever present.** It is applied by
       load_schemas() to every .schema.json file, so every schema resolved
       through the normal registry path short-circuits here — this function
       changes nothing for them, and carries no parity risk against
       schema.js's own `_isJsonSchemaBacked` load-time property.
    2. **Absent stamp ⇒ INFER, do not assume.** The stamp is a load-time
       provenance mark, not an intrinsic property of the object. A schema
       that reached a caller by any route other than load_schemas() —
       hand-built, derived/filtered, JSON round-tripped, or lifted out of a
       foreign corpus — has no stamp while still being a JSON Schema. The
       two dialects are structurally disjoint, so a shape read answers the
       question the missing stamp cannot.
    3. **Undecidable ⇒ 'unknown', which the caller must fail CLOSED on.**

    Negative-spec: `applies_to`, `kinds`/`kind`, and `match_mode` are NOT
    discriminators — load_schemas() reads all three off BOTH dialects
    (see the .schema.json loader branch), so they carry no dialect signal.
    """
    if schema.get('_isJsonSchema'):
        return 'json'

    # JSON-Schema tells. None of these can appear in the YAML dialect, whose
    # top-level vocabulary is schema/applies_to/kinds/required/optional.
    # `required` is the sharpest discriminator: an ARRAY of field names in
    # JSON Schema, a field-name -> spec MAPPING in the YAML dialect.
    if (
        '$schema' in schema
        or 'x-schema-name' in schema
        or isinstance(schema.get('properties'), dict)
        or isinstance(schema.get('required'), list)
        or schema.get('type') == 'object'
        or any(k in schema for k in ('allOf', 'anyOf', 'oneOf', '$defs'))
    ):
        return 'json'

    if (
        isinstance(schema.get('required'), dict)
        or isinstance(schema.get('optional'), dict)
        or isinstance(schema.get('schema'), str)
    ):
        return 'yaml'

    return 'unknown'


def _dispatch_validate(schema: dict, fields: dict) -> dict:
    """Shared dispatch body behind validate() and validate_frontmatter_obj().

    Given an already-resolved schema object (caller owns provenance — this
    helper never touches _SCHEMAS_DIR or load_schemas()) and raw field values,
    coerces dates, then dispatches on the dialect resolved by
    _classify_schema_dialect(): JSON-Schema-backed schemas run
    _validate_json_schema_node + _apply_cross_field_rules (keyed on
    x-schema-name), exactly like validateFrontmatterDispatch (schema.js:3100);
    legacy .yaml-dialect schemas run _validate_legacy_yaml_frontmatter.
    Returns {"ok": True} or {"ok": False, "errors": [ErrorDict, ...]}.

    Dispatch reads the `_isJsonSchema` stamp FIRST and falls back to a shape
    read only when it is absent (see _classify_schema_dialect) — so the
    registry-resolved path this shares with validate() is bit-for-bit
    unchanged. The fallback exists because a MISSING stamp means "provenance
    unknown", not "YAML dialect", and routing an unstamped JSON Schema into
    the legacy branch is a fail-OPEN: that branch's own negative-spec treats
    a schema with no `required:` MAPPING as unconditionally valid, so a JSON
    Schema (whose `required` is an ARRAY, or absent at the top level) would
    silently validate every document it was handed.

    Fail-closed on an undecidable schema object: a dict matching neither
    dialect's tells returns an `_schema` error result rather than falling
    through to the permissive branch. This is the one behaviour change
    against the pre-2026-07-30 dispatch, and it is deliberate — the previous
    default returned {"ok": True}.
    """
    record = _coerce_dates_to_strings(fields) if fields is not None else {}

    dialect = _classify_schema_dialect(schema)

    if dialect == 'json':
        shape_errors = _validate_json_schema_node(record, schema, schema, '')
        cf_schema_name = schema.get('x-schema-name')
        cross_errors = _apply_cross_field_rules(record, cf_schema_name) if cf_schema_name else []
        errors = shape_errors + cross_errors
        return {'ok': True} if not errors else {'ok': False, 'errors': errors}

    if dialect == 'yaml':
        return _validate_legacy_yaml_frontmatter(record, schema)

    return {
        'ok': False,
        'errors': [{
            'field': '_schema',
            'error': (
                'schema object matches neither the JSON-Schema nor the legacy '
                'YAML dialect — cannot determine how to validate against it'
            ),
            'hint': (
                'Pass a schema resolved by load_schemas()/match_schema(). A '
                'hand-built JSON Schema needs at least one of $schema, '
                'properties, type: object, or a required[] array; a legacy '
                'YAML-dialect schema needs a required:/optional: mapping.'
            ),
        }],
    }


def validate(schema_name: str, fields: dict) -> dict:
    """Validate fields against a vendored schema by name.

    Dispatches on schema type exactly like schema.js validateFrontmatterDispatch
    (schema.js:3100): JSON-Schema-backed schemas (stamped `_isJsonSchema` by
    load_schemas — every schema currently vendored under
    coordinator_core/frontmatter/schemas/ is this kind, so _dispatch_validate's
    unstamped-schema shape fallback is unreachable from here) run shape validation
    (_validate_json_schema_node) plus cross-field rules (_apply_cross_field_rules,
    keyed by the schema's x-schema-name); legacy .yaml-dialect schemas run
    _validate_legacy_yaml_frontmatter.

    Returns {"ok": bool, "errors"?: [ErrorDict, ...]} — the same {field, error,
    hint}-shaped errors validate_frontmatter() returns, i.e. the RICHER pre-
    flattening shape validateFrontmatterDispatch itself produces, before
    schema-cli.js's own CLI layer further string-flattens each error to
    "field: error" for its stdout contract (schema-cli.js:220-224). Callers
    that need schema-cli.js's exact flattened string form (e.g. parity
    assertions against `node schema-cli.js --validate`) must do that
    flattening themselves — this function does not do it, so structured
    consumers (e.g. a future native queue_append caller) are not forced to
    re-parse a string it was never natively given.

    schema_name is the schema-cli.js/vendored schema NAME (e.g.
    "lesson-entry"), not a caller-side CLI alias (e.g. queue.append's
    "lessons") — callers resolve aliases (see queue_append._SCHEMA_CLI_NAME)
    before calling.

    Negative-spec: unlike validate_frontmatter(), this does NOT raise
    SchemaVersionError on a schema_version major-version mismatch — parity
    with schema-cli.js --validate mode, which runs validateRecord in
    warn-on-newer-read ('read') mode by default and never fails a --validate
    call on a version warning alone (schema.js:3145-3146).

    Raises:
        ValueError: schema_name is not a registered vendored schema (or is
            one of the internal "_byGlob"/"_byKind" index keys).
    """
    schemas = load_schemas(_SCHEMAS_DIR)
    if schema_name not in schemas or schema_name in ('_byGlob', '_byKind'):
        available = ', '.join(k for k in schemas if k not in ('_byGlob', '_byKind'))
        raise ValueError(f'unknown schema "{schema_name}". Available: {available}')
    schema = schemas[schema_name]
    return _dispatch_validate(schema, fields)


def validate_frontmatter_obj(fm_dict: dict, schema_obj: dict) -> dict:
    """Validate a frontmatter dict against a caller-supplied schema OBJECT.

    Seam requested by DoE's validate-frontmatter-schema.js node-hook port
    (cross-repo memo 2026-07-22, claude-central-em): the caller (a
    validate-frontmatter-schema.py port of that hook) already resolved the
    schema object itself — via its own load_schemas()/match_schema() over
    DoE's ~40-schema corpus, NOT claude-klabauter's ~10-schema vendored copy — and
    passes it straight in. Unlike validate() and validate_frontmatter(), this
    function:

    1. Takes schema_obj as a fully-formed dict. It never resolves a schema
       name, never reads _SCHEMAS_DIR, and never touches disk — schema
       provenance is entirely the caller's, by design, so a foreign schema
       corpus can be validated without claude-klabauter's vendored set shadowing it.
    2. Dispatches exactly like validate() — see _dispatch_validate() — on the
       `_isJsonSchema` stamp when present, else on the schema object's own
       structural shape (_classify_schema_dialect): JSON-Schema-backed shape
       + cross-field rules, or the legacy YAML-dialect branch.

       The shape fallback matters MOST here, and this is the seam it was
       added for. validate() only ever sees registry-resolved (hence
       stamped) schemas; this function's whole contract is that the caller
       owns provenance, so an unstamped-but-genuine JSON Schema — derived,
       hand-built, or JSON round-tripped out of a foreign corpus — is a
       routine input, not a malformed one.
    3. NEVER raises. Every failure mode — a malformed/non-dict schema_obj, a
       None/non-dict fm_dict, a SchemaVersionError, or any unexpected
       internal exception — comes back as {"ok": False, "errors": [...]}
       with the same {field, error, hint} ErrorDict shape, never as a raised
       exception. This mirrors the JS validateFrontmatter contract this
       function replaces: it feeds a PreToolUse hook contractually bound to
       exit 0, so a validator that raises would crash the hook rather than
       report a validation failure.

    Negative-spec: this is not a name-keyed lookup. A schema_obj that matches
    NEITHER dialect's structural tells is rejected as an `_schema` error
    result — it is not dispatched into the legacy branch and does not inherit
    that branch's permissiveness. CHANGED 2026-07-30: until then, an
    unstamped schema_obj went unconditionally to the legacy YAML branch,
    whose own JS-parity negative-spec ("no `required` MAPPING" ==
    "everything passes") made every unrecognized schema — including a real
    but unstamped JSON Schema — return {"ok": True} against any document.
    That is the fail-open this seam could least afford: it feeds PreToolUse
    write guards, where a silently-passing validator is indistinguishable
    from a clean document. `_validate_legacy_yaml_frontmatter`'s own
    permissiveness is UNCHANGED and still JS-parity-correct — what changed is
    which schemas reach it.

    Still true: within a dialect, the dispatched branch's own permissiveness
    governs, and this function adds no second-guessing on top of it. A
    schema classified 'yaml' with no `required` block still passes
    everything; a schema classified 'json' with no constraints still passes
    everything, exactly as JSON Schema specifies. Non-dict schema_obj (and
    non-dict/non-None fm_dict) are still caught up front by the two
    isinstance guards below.

    Returns {"ok": True} or {"ok": False, "errors": [ErrorDict, ...]}.
    """
    if not isinstance(schema_obj, dict):
        return {
            'ok': False,
            'errors': [{
                'field': '_schema',
                'error': f'schema_obj must be a dict, got {type(schema_obj).__name__}',
                'hint': 'Pass the schema object returned by load_schemas()/match_schema()',
            }],
        }
    if fm_dict is not None and not isinstance(fm_dict, dict):
        return {
            'ok': False,
            'errors': [{
                'field': '_schema',
                'error': f'fm_dict must be a dict or None, got {type(fm_dict).__name__}',
                'hint': 'Pass the parsed frontmatter dict',
            }],
        }
    try:
        return _dispatch_validate(schema_obj, fm_dict)
    except Exception as exc:  # noqa: BLE001 - contractually never raises
        return {
            'ok': False,
            'errors': [{
                'field': '_internal',
                'error': str(exc),
                'hint': 'validate_frontmatter_obj() caught an unexpected internal error',
            }],
        }


# =============================================================================
# CLI trampoline body — port of DoE-claude coordinator/bin/lint-frontmatter.js
# (deleted at claude-klabauter commit c79e66cd; retrieved for this port via
# `git show c79e66cd~1:coordinator/bin/lint-frontmatter.js`).
#
# Scoped port: services --root/--file/--json/--strict-refs, the three flag
# shapes DoE's three live callers consume (workweek-complete.md Step 2.5,
# update-docs.md Phase 11d, handoff/SKILL.md's write-time gate). The oracle's
# --schema/--list-schemas/--lint-existing modes are NOT re-ported here (no
# live caller of this port uses them) — see _lint_parse_args' negative-spec.
# This module owns the logic per the porter brief ("do NOT duplicate logic in
# the trampoline"); coordinator/bin/lint-frontmatter.py is a thin argv/exit-code
# shim over main().
#
# Referential-integrity check — RECONCILED 2026-07-24 (flagged by code review
# 2026-07-24, reconciled same day) into a superset of the oracle's coverage:
#
#   Oracle (checkReferentialIntegrity, lib/schema.js, git show
#   c79e66cd~1:coordinator/bin/lib/schema.js): existence-checked ONLY
#   predecessor_id / origin_handoff_id (the ID-companion fields) against a
#   handoff_id -> path index built by scanning state/handoffs/*.md (live) and
#   archive/handoffs/**/*.md (archived, indexed at their pre-archival logical
#   path — buildHandoffIdIndex, lint-frontmatter.js). It ALSO enforced a
#   never-silently-disagree invariant: when an ID field resolves AND its path
#   companion (predecessor/origin_handoff) is also set, the two must name the
#   SAME artifact — an ALWAYS-ERROR check, independent of --strict-refs (only
#   the bare-dangling-ID case is strict-gated). Gated to schemaName ===
#   'handoff' only (never handoff-archived).
#
#   This port keeps BOTH mechanisms as a superset — neither replaces the
#   other, because they check disjoint field sets:
#     1. coordinator_core.dag.check_lineage_reachability — PATH-field
#        reachability (predecessor / forked_from / additional_predecessors[] /
#        origin_handoff) via live ∪ archive-on-disk ∪ git-history resolution.
#        This is a claude-klabauter-local addition beyond the oracle's original scope
#        (the oracle's lint-frontmatter.js never checked path fields at all —
#        confirmed by grep of the retrieved pre-deletion source, which has no
#        resolveTarget/dag-walk call in checkReferentialIntegrity). Kept
#        because it is real coverage the corpus benefits from, not because
#        the oracle had it.
#     2. _build_handoff_id_index / _check_referential_integrity_id_refs (below)
#        — restores the oracle's own ID-companion coverage: existence-checks
#        predecessor_id / origin_handoff_id against a local handoff_id index,
#        PLUS the never-silently-disagree invariant. Ported byte-faithfully
#        from buildHandoffIdIndex / checkReferentialIntegrity (collision
#        rules, live-outranks-archived precedence, forward-slash path
#        normalisation, and the always-error-on-disagree /
#        strict-gated-on-dangling split all carry over unchanged).
#
#   Asymmetry (Review: code-reviewer R1 P3): mechanism (1) covers all four
#   path fields (predecessor / forked_from / additional_predecessors[] /
#   origin_handoff); mechanism (2) covers only the two fields that today have
#   an ID-companion (predecessor_id / origin_handoff_id) — forked_from and
#   additional_predecessors[] have no ID-companion equivalent in the schema,
#   so the "superset" framing above is a superset only over the fields that
#   currently exist. Not a live gap (the schema has no forked_from_id or
#   per-entry additional_predecessors IDs today); would need extending if
#   either is ever added.
#
#   Empirical justification for restoring (2): predecessor_id/origin_handoff_id
#   ARE populated and load-bearing in the live corpus as of 2026-07-24 (14
#   files with non-null predecessor_id, 10 with non-null origin_handoff_id,
#   always paired with their path companion) — dropping this check silently
#   regressed real coverage, not dead-field cleanup. See the C3
#   never-silently-disagree cross-field rule the handoff.schema.json
#   descriptions for predecessor_id/origin_handoff_id point at
#   ("owned by C3, not this schema file") — C3 was never actually landed
#   anywhere in this repo prior to this reconciliation (grep confirmed zero
#   hits for predecessor_id outside this file and the schema); this
#   reconciliation IS that C3 restoration, implemented here rather than as a
#   schema-only cross-field rule because the oracle's own design intentionally
#   kept it OUT of CROSS_FIELD_RULES (resolver-seam, not a pure fm-dict check
#   — see checkReferentialIntegrity's own docstring rationale, ported verbatim
#   above _check_referential_integrity_id_refs below).
# =============================================================================

_LINT_SIDECAR_RE = re.compile(
    r'\.(prior-art-check|docs-check|coverage-check|plan-coverage-check|'
    r'plan-review-check|schema-migration-audit|review-[^./]+|[^./]*-review|review|'
    r'phase0|node-map)\b'
)


def _lint_is_sidecar_file(repo_rel: str) -> bool:
    """Port of lint-frontmatter.js isSidecarFile — review-worker sidecars are
    exempt from parent-directory schema validation (see module-level SIDECAR_RE
    comment in the deleted oracle for the full rationale).

    `phase0`/`node-map` extend the deleted oracle's set: a plan's phase-0
    inventory and node-map are companion deliverables carrying only
    `plan_id`/`deliverable_id`, not plans in their own right, and routing them
    to `plan` reports every required plan field as missing.
    """
    return bool(_LINT_SIDECAR_RE.search(repo_rel))


def _lint_find_repo_root(hint: str | None) -> str:
    """Port of lint-frontmatter.js findRepoRoot: explicit --root, else
    `git rev-parse --show-toplevel`, else cwd."""
    if hint:
        return str(Path(hint).resolve())
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=15,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return os.getcwd()


def _lint_collect_files_for_glob(repo_root: str, glob: str) -> list[tuple[str, str]]:
    """Port of lint-frontmatter.js collectFilesForGlob/walkDir.

    Returns [(abs_path, repo_rel_path), ...]. Walks only as deep as the glob's
    fixed (non-wildcard) prefix, recursing further only when the glob contains
    '**' — mirrors the oracle's walk depth exactly (a bare '*.md' glob like
    handoff.schema.json's applies_to is single-level, not recursive).
    """
    parts = glob.split('/')
    fixed_parts: list[str] = []
    for p in parts:
        if '*' in p or '?' in p:
            break
        fixed_parts.append(p)
    base_dir = os.path.join(repo_root, *fixed_parts) if fixed_parts else repo_root
    if not os.path.isdir(base_dir):
        return []
    recursive = '**' in glob
    results: list[tuple[str, str]] = []

    def walk(dir_path: str) -> None:
        try:
            entries = sorted(os.scandir(dir_path), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            full_path = entry.path
            repo_rel = os.path.relpath(full_path, repo_root).replace('\\', '/')
            if entry.is_dir(follow_symlinks=False):
                if recursive:
                    walk(full_path)
            elif entry.is_file(follow_symlinks=False):
                if match_glob(glob, repo_rel):
                    results.append((full_path, repo_rel))

    walk(base_dir)
    return results


def _build_handoff_id_index(repo_root: str) -> dict[str, str]:
    """Build a handoff_id -> logical repo-relative path index.

    Byte-faithful port of buildHandoffIdIndex (DoE-claude
    coordinator/bin/lint-frontmatter.js, deleted at claude-klabauter commit c79e66cd;
    retrieved via `git show c79e66cd~1:coordinator/bin/lint-frontmatter.js`).

    Scope: LOCAL handoff-DAG ID refs only. Scans state/handoffs/*.md (flat,
    live) and archive/handoffs/**/*.md (recursive, archived — flat AND
    YYYY-MM-foldered) for a frontmatter `handoff_id:` value.

    Logical-key indexing (load-bearing): a handoff found under
    archive/handoffs/** is indexed at its STABLE LOGICAL key —
    `state/handoffs/<basename>` — not its current on-disk archive path.
    Archival is a pure file move that preserves basename, and the handoff-DAG
    treats the pre-archival state/handoffs/ path as the artifact's permanent
    logical identity. predecessor:/origin_handoff: fields are authored
    against that logical path and deliberately never get rewritten when their
    target is later archived — indexing archived files at their on-disk path
    instead would make a live referrer whose predecessor: correctly still
    names state/handoffs/<basename> trip the never-silently-disagree check as
    a false divergence purely because its target moved.

    Collision rule: a handoff_id should never legitimately exist on both
    shelves (archival is a move, not a copy) or twice on the same shelf, but
    resolves deterministically rather than silently favoring whichever
    directory happens to be scanned last — live always outranks archived;
    otherwise first-seen wins. Either case is a data anomaly, surfaced via a
    one-line stderr warning rather than swallowed.

    Does NOT existence-check deliverable_id/initiative — those resolve on the
    central/claude-klabauter state plane; a local scan would false-positive every ref
    forever (mirrors the oracle's own scope note).

    Built ONCE per run (whole-tree mode) or once per --file invocation; never
    rebuilt per record — callers must build once and thread the result
    through, mirroring git_history_cache's threading pattern.

    Spec backlink: DoE-claude coordinator/bin/lint-frontmatter.js
      buildHandoffIdIndex (pre-deletion; git show
      c79e66cd~1:coordinator/bin/lint-frontmatter.js)
    """
    index: dict[str, str] = {}
    origin: dict[str, str] = {}  # handoff_id -> 'live' | 'archived'

    def claim(hid: str, key: str, is_archive_entry: bool) -> None:
        existing_key = index.get(hid)
        if existing_key is None:
            index[hid] = key
            origin[hid] = 'archived' if is_archive_entry else 'live'
            return
        existing_is_live = origin.get(hid) == 'live'
        if existing_is_live and not is_archive_entry:
            print(
                f'lint-frontmatter: handoff_id "{hid}" claimed by more than one live '
                f'handoff; keeping {existing_key}, ignoring {key}',
                file=sys.stderr,
            )
            return
        if existing_is_live and is_archive_entry:
            print(
                f'lint-frontmatter: handoff_id "{hid}" claimed by both a live and an '
                f'archived handoff; keeping the live entry ({existing_key}), ignoring '
                f'archived {key}',
                file=sys.stderr,
            )
            return
        if not existing_is_live and not is_archive_entry:
            print(
                f'lint-frontmatter: handoff_id "{hid}" claimed by both a live and an '
                f'archived handoff; keeping the live entry ({key}), ignoring archived '
                f'{existing_key}',
                file=sys.stderr,
            )
            index[hid] = key
            origin[hid] = 'live'
            return
        # both archived — first-seen wins
        print(
            f'lint-frontmatter: handoff_id "{hid}" claimed by more than one archived '
            f'handoff; keeping {existing_key}, ignoring {key}',
            file=sys.stderr,
        )

    def index_dir(abs_dir: str, recursive: bool, is_archive_entry: bool) -> None:
        try:
            entries = sorted(os.scandir(abs_dir), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            full_path = entry.path
            if entry.is_dir(follow_symlinks=False):
                if recursive:
                    index_dir(full_path, recursive, is_archive_entry)
                continue
            if not entry.is_file(follow_symlinks=False) or not entry.name.endswith('.md'):
                continue
            try:
                content = Path(full_path).read_text(encoding='utf-8')
            except OSError:
                continue
            parsed = parse_frontmatter(content)
            frontmatter = parsed['frontmatter']
            hid = frontmatter.get('handoff_id') if frontmatter else None
            if not hid or not isinstance(hid, str):
                continue
            key = f'state/handoffs/{entry.name}' if is_archive_entry else (
                os.path.relpath(full_path, repo_root).replace('\\', '/')
            )
            claim(hid, key, is_archive_entry)

    index_dir(os.path.join(repo_root, 'state', 'handoffs'), False, False)
    index_dir(os.path.join(repo_root, 'archive', 'handoffs'), True, True)
    return index


def _check_referential_integrity_id_refs(
    frontmatter: dict,
    handoff_id_index: dict[str, str],
) -> tuple[list[ErrorDict], list[ErrorDict]]:
    """Resolver-seam existence + never-silently-disagree validation for the
    local handoff-DAG ID refs (predecessor_id, origin_handoff_id).

    Byte-faithful port of checkReferentialIntegrity (DoE-claude
    coordinator/bin/lib/schema.js, deleted at claude-klabauter commit c79e66cd;
    retrieved via `git show c79e66cd~1:coordinator/bin/lib/schema.js`), with
    the JS resolver-callback seam collapsed into a direct dict lookup against
    handoff_id_index (this port's callers always build a full in-memory index
    up front — see _build_handoff_id_index — so there is no resolver
    indirection to preserve).

    Path/ID companion pairs (C2, add-not-swap): each pair is the human-legible
    path field and its machine-walked ID companion.
      - predecessor / predecessor_id
      - origin_handoff / origin_handoff_id

    Returns (errors, warnings):
      - warnings: dangling ID refs (ID set but not found in handoff_id_index).
        This is the normal state for in-flight work whose target has not been
        authored yet — soft by default.
      - errors: never-silently-disagree divergences (ID resolves to a
        DIFFERENT artifact than its path companion names) — ALWAYS an error,
        regardless of --strict-refs. A resolvable-but-wrong ref is a
        correctness bug, not an in-flight-authoring gap. Dangling refs never
        appear here; the caller escalates dangling warnings to errors itself
        when --strict-refs is set (matching this port's existing path-field
        reachability strict/non-strict split in _check_handoff_refs).

    Negative-spec: does NOT existence-check deliverable_id/initiative
    (central/claude-klabauter state plane, out of scope — mirrors the oracle). Does NOT
    do its own filesystem I/O — handoff_id_index must be pre-built by the
    caller (mirrors the oracle's resolver-seam design, which the oracle's own
    docstring notes was deliberately kept OUT of CROSS_FIELD_RULES because
    those closures run on every validate call with no place to receive a
    resolver).

    Spec backlink: DoE-claude coordinator/bin/lib/schema.js
      checkReferentialIntegrity (pre-deletion; git show
      c79e66cd~1:coordinator/bin/lib/schema.js)
    """
    errors: list[ErrorDict] = []
    warnings: list[ErrorDict] = []

    pairs = (('predecessor', 'predecessor_id'), ('origin_handoff', 'origin_handoff_id'))

    def is_set(v: Any) -> bool:
        return v is not None and str(v).strip() != '' and v != 'none'

    def norm_path(v: Any) -> str:
        # Basename-only, not a bare forward-slash normalisation: the path-field
        # authoring convention this corpus actually uses is directory-tolerant —
        # `predecessor:`/`origin_handoff:` are written as either a bare basename
        # ("2026-07-22_173931_de5a7f11.md") or a qualified logical path
        # ("state/handoffs/2026-07-22_173931_de5a7f11.md"), and resolve_target's
        # own candidate list (coordinator_core/dag.py) treats both forms as
        # equivalent. _build_handoff_id_index deliberately indexes an ARCHIVED
        # handoff at its stable LOGICAL key (state/handoffs/<basename>, never its
        # on-disk archive/handoffs/<...> path — see that function's docstring),
        # so id_field's resolved_path is always qualified even when path_field is
        # a bare basename referring to the same artifact. A bare
        # forward-slash-only compare (the original port, byte-faithful to the
        # since-deleted JS oracle's normPath) flags that legitimate pairing as a
        # never-silently-disagree divergence — a false positive discovered
        # 2026-07-29 auditing claude-klabauter's own corpus (3 of 5 flagged pairs were this
        # exact shape, all naming the correct artifact under the tolerant
        # convention). Comparing basenames matches what resolve_target already
        # treats as "the same artifact" and still catches a genuine mismatch
        # (different basename = different artifact, unaffected by this change).
        return os.path.basename(str(v).replace('\\', '/'))

    for path_field, id_field in pairs:
        id_value = frontmatter.get(id_field)
        if not is_set(id_value):
            continue  # no ID ref to check

        resolved_path = handoff_id_index.get(str(id_value))
        if resolved_path is None:
            warnings.append({
                'field': id_field,
                'error': f'{id_field} "{id_value}" does not resolve to a known artifact',
                'hint': (
                    f'No artifact was found for id "{id_value}" in the local handoff_id '
                    'index. This is the normal state for in-flight work whose target has '
                    'not been authored yet. Outside a cadence gate this is a warning only; '
                    'pass --strict-refs at a cadence gate to escalate.'
                ),
            })
            continue  # resolved artifact unknown — nothing to compare the path field against

        path_value = frontmatter.get(path_field)
        if not is_set(path_value):
            continue  # ID present, path absent — nothing to compare

        if norm_path(resolved_path) != norm_path(path_value):
            errors.append({
                'field': id_field,
                'error': (
                    f'{id_field} "{id_value}" resolves to "{resolved_path}", which does '
                    f'not match {path_field} "{path_value}" — never-silently-disagree invariant'
                ),
                'hint': (
                    f'{path_field} and {id_field} are two representations of the same edge '
                    'and must name the same artifact when both are set. Either '
                    f'{path_field} was hand-edited without updating {id_field}, or '
                    f'{id_field} is stale.'
                ),
            })

    return errors, warnings


def _check_handoff_refs(
    frontmatter: dict | None,
    repo_root: str,
    record_abs_path: str,
    record_repo_rel: str,
    git_history_cache: set | None,
    handoff_id_index: dict[str, str] | None,
    strict: bool,
) -> tuple[list[ErrorDict], list[ErrorDict]]:
    """Referential-integrity check for schema_name == 'handoff' records only.

    Superset of two DISJOINT-field mechanisms — see the CLI-trampoline section
    docstring above for the full reconciliation rationale:
      1. PATH-field reachability (predecessor / forked_from /
         additional_predecessors[] / origin_handoff) via
         coordinator_core.dag.check_lineage_reachability — a claude-klabauter-local
         addition beyond the deleted oracle's original scope.
      2. ID-field existence + never-silently-disagree (predecessor_id /
         origin_handoff_id) via _check_referential_integrity_id_refs — restores
         the deleted oracle's own checkReferentialIntegrity coverage.

    Negative-spec: deliberately excludes handoff-archived records — mirrors
    the deleted oracle (lint-frontmatter.js), whose own checkReferentialIntegrity
    call sites gated on `schemaName === 'handoff'` (never 'handoff-archived')
    in both --file and whole-tree modes. Verified 2026-07-24 by review
    against the retrievable pre-deletion .js (`git show
    c79e66cd~1:coordinator/bin/lint-frontmatter.js`) — this is inherited
    scope, not a porting regression, despite dag.py's own
    WAIVED_DANGLING_PREDECESSORS being keyed on archive/handoffs/ paths (that
    waiver belongs to check_lineage_reachability's OTHER caller, the batch
    corpus sweep, which does walk archive/handoffs/ — a different consumer
    with a different scope than this CLI).

    Args:
        handoff_id_index: pre-built via _build_handoff_id_index, or None to
            skip ID-field checking entirely (e.g. a caller that has not built
            one). Threaded once per run, like git_history_cache.
        strict: when True, dangling refs (both path-field and ID-field) are
            hard errors; when False, they are soft warnings. Never-silently-
            disagree ID/path divergences are ALWAYS hard errors regardless of
            this flag (mirrors the oracle exactly).

    Returns (errors, warnings) — both empty when frontmatter is None/absent.
    """
    if not frontmatter:
        return [], []
    from coordinator_core.dag import check_lineage_reachability  # local: avoid import at module load for non-CLI callers

    handoff_dir = os.path.dirname(record_abs_path)
    raw_path_violations = check_lineage_reachability(
        frontmatter,
        repo_root,
        handoff_dir=handoff_dir,
        record_repo_rel_path=record_repo_rel,
        git_history_cache=git_history_cache,
    )
    path_dangling: list[ErrorDict] = [
        {
            'field': v['field'],
            'error': f"dangling reference \"{v['value']}\": {v['reason']}",
            'hint': 'Point this field at an existing handoff, or clear it if the target was never authored.',
        }
        for v in raw_path_violations
    ]

    if handoff_id_index is not None:
        id_disagree, id_dangling = _check_referential_integrity_id_refs(frontmatter, handoff_id_index)
    else:
        id_disagree, id_dangling = [], []

    errors: list[ErrorDict] = list(id_disagree)  # always-error, regardless of strict
    warnings: list[ErrorDict] = []
    dangling = path_dangling + id_dangling
    if strict:
        errors.extend(dangling)
    else:
        warnings.extend(dangling)

    return errors, warnings


def _lint_parse_args(argv: list[str]) -> dict | None:
    """Port of lint-frontmatter.js parseArgs, scoped to the ported flag set.

    Returns a parsed-args dict, or None (having already written to stderr) on
    a usage error — caller returns exit code 2.

    Negative-spec (fail-loud, not silent no-op — mirrors A2's unported-flag
    contract): --schema, --list-schemas, and --lint-existing are recognized
    by the deleted oracle but not ported here (no live caller of this port
    uses them — see this module's CLI-trampoline section docstring). Passing
    any of them is a usage error, not a silent ignore.
    """
    args = {'root': None, 'file': None, 'json': False, 'strict_refs': False}
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok == '--root':
            if i + 1 >= n:
                print('lint-frontmatter: --root requires a path argument', file=sys.stderr)
                return None
            args['root'] = argv[i + 1]
            i += 2
        elif tok == '--file':
            if i + 1 >= n:
                print('lint-frontmatter: --file requires a path argument', file=sys.stderr)
                return None
            args['file'] = argv[i + 1]
            i += 2
        elif tok == '--json':
            args['json'] = True
            i += 1
        elif tok == '--strict-refs':
            args['strict_refs'] = True
            i += 1
        elif tok in ('--schema', '--list-schemas', '--lint-existing'):
            print(
                f'lint-frontmatter: {tok}: not ported — claude-klabauter BIG_PORT. '
                'Open a port request if you need this flag.',
                file=sys.stderr,
            )
            return None
        else:
            print(f'lint-frontmatter: unknown argument: {tok}', file=sys.stderr)
            return None
    return args


def _run_single_file_check(repo_root: str, file_path: str, as_json: bool) -> int:
    """Port of lint-frontmatter.js runSingleFileCheck. Returns the process exit code."""
    if not file_path:
        print('lint-frontmatter --file: --file requires a path argument', file=sys.stderr)
        return 2

    resolved = os.path.abspath(file_path)
    try:
        repo_rel = os.path.relpath(resolved, repo_root).replace('\\', '/')
    except ValueError:
        # Windows-only: os.path.relpath raises when the two paths sit on
        # different mounts ("path is on mount 'C:', start on mount 'X:'"). That
        # is routine here, not exotic — a repo on X: linting a scratch file
        # under the default TEMP on C: hits it every time, and the raise
        # escaped as a bare traceback rather than a diagnostic.
        #
        # There is no honest repo-relative rendering of an off-mount path, and
        # inventing one would feed match_schema a path shape that never
        # existed. Fall back to the absolute path, forward-slashed to match
        # what the downstream consumers (_lint_is_sidecar_file, the extension
        # regex, match_schema) expect. Path-keyed schema matching legitimately
        # misses on such a path, but match_schema also discriminates on the
        # record's own `kind`, so a real off-mount handoff still resolves its
        # schema and validates normally; only a record identifiable by path
        # alone degrades to "no schema matches" (exit 0). Either way it is a
        # diagnostic, never a traceback.
        repo_rel = resolved.replace('\\', '/')

    if not os.path.isfile(resolved):
        print(f'lint-frontmatter --file: file not found: {resolved}', file=sys.stderr)
        return 2

    if _lint_is_sidecar_file(repo_rel) or re.search(r'\.(ya?ml|json)$', repo_rel):
        if as_json:
            print(json.dumps({'ok': True, 'violations': [], 'note': 'not a lintable frontmatter file'}, indent=2))
        else:
            print(f'lint-frontmatter --file: {repo_rel} is not a lintable frontmatter file — nothing to validate')
        return 0

    schemas = load_schemas(_SCHEMAS_DIR)
    content = Path(resolved).read_text(encoding='utf-8')
    parsed = parse_frontmatter(content)
    frontmatter = parsed['frontmatter']

    resolved_schema = match_schema(repo_rel, frontmatter, schemas)
    if resolved_schema is None:
        if as_json:
            print(json.dumps({'ok': True, 'violations': [], 'note': 'no schema matches this path'}, indent=2))
        else:
            print(f'lint-frontmatter --file: no schema matches {repo_rel} — nothing to validate')
        return 0

    schema_name = resolved_schema['schemaName']
    schema = resolved_schema['schema']
    if schema.get('match_mode') == 'inline-tag-per-entry':
        # Lesson-entry inline-tag validation has no Python port (no live
        # --file caller targets a lesson entry) — treat as "nothing to
        # validate" rather than crash. Flagged, not silently misreported.
        if as_json:
            print(json.dumps({'ok': True, 'violations': [], 'note': 'inline-tag-per-entry schemas are unported for --file'}, indent=2))
        else:
            print(f'lint-frontmatter --file: {repo_rel} matched an inline-tag-per-entry schema — unported for --file, nothing checked')
        return 0

    result = _tolerate_handoff_kind_aliases_in_result(
        validate_frontmatter_obj(frontmatter, schema), schema_name, schema, frontmatter,
    )
    # --file mode always checks DANGLING refs NON-strict (write-time is not a
    # cadence gate; a dangling ref at author time is the normal in-flight
    # state) — mirrors the deleted oracle's
    # `checkReferentialIntegrity(..., { strict: false })` call in
    # runSingleFileCheck. Dangling refs land as warnings, never errors,
    # regardless of --strict-refs (that flag only escalates the whole-tree
    # walk). Never-silently-disagree ID/path divergences are ALWAYS errors
    # here too (strict=False only gates dangling-ness, not disagreement) —
    # mirrors the oracle's `refResult.errors` being unconditionally spread
    # into runSingleFileCheck's combinedErrors.
    ref_errors: list[ErrorDict] = []
    ref_warnings: list[ErrorDict] = []
    if schema_name == 'handoff':
        handoff_id_index = _build_handoff_id_index(repo_root)
        ref_errors, ref_warnings = _check_handoff_refs(
            frontmatter, repo_root, resolved, repo_rel, None, handoff_id_index, False,
        )
    combined_errors = (list(result.get('errors') or []) if not result.get('ok') else []) + ref_errors

    if not combined_errors:
        # 'warnings' (not 'refWarnings') here is intentional, not an
        # inconsistency — verified 2026-07-24 against the deleted oracle:
        # runSingleFileCheck's --json payload used the key 'warnings' while
        # main()'s whole-tree --json payload (see _run_tree_walk below) used
        # 'refWarnings'. The two modes genuinely had separate code paths in
        # the original too; this port mirrors that asymmetry byte-for-byte.
        if as_json:
            print(json.dumps({'ok': True, 'violations': [], 'warnings': ref_warnings}, indent=2))
        else:
            print(f'lint-frontmatter --file: {repo_rel} valid [{schema_name}]')
            for w in ref_warnings:
                print(f"  warning: {w['field']}: {w['error']}")
                if w.get('hint'):
                    print(f"    hint: {w['hint']}")
        return 0

    violation = {'file': repo_rel, 'schema': schema_name, 'errors': combined_errors}
    if as_json:
        print(json.dumps({'ok': False, 'violations': [violation], 'warnings': ref_warnings}, indent=2))
    else:
        print('lint-frontmatter --file: 1 violation(s)\n', file=sys.stderr)
        print(f"  {violation['file']}  [{violation['schema']}]", file=sys.stderr)
        for e in violation['errors']:
            print(f"    - {e['field']}: {e['error']}", file=sys.stderr)
            if e.get('hint'):
                print(f"      hint: {e['hint']}", file=sys.stderr)
        for w in ref_warnings:
            print(f"    warning: {w['field']}: {w['error']}", file=sys.stderr)
            if w.get('hint'):
                print(f"      hint: {w['hint']}", file=sys.stderr)
    return 1


def _run_tree_walk(repo_root: str, as_json: bool, strict_refs: bool) -> int:
    """Port of lint-frontmatter.js main()'s whole-tree walk branch (--schema/
    --list-schemas/--lint-existing modes excluded — see _lint_parse_args)."""
    from coordinator_core.dag import as_history_membership_set, build_git_history_cache

    schemas = load_schemas(_SCHEMAS_DIR)
    # Stripped via `dag.as_history_membership_set`: this cache is built ONCE
    # here and reused for the REST of this whole-tree walk (potentially the
    # entire repo's handoff corpus), so a target pruned/committed mid-walk
    # must still resolve correctly for a later file in the same walk; trusting
    # `.complete` across that reuse window would fast-reject such a miss
    # instead of falling through to the real per-call git check. A HIT is
    # unaffected either way — only a MISS changes, and only in the
    # falls-through direction. See `dag.as_history_membership_set`'s docstring
    # for the full rationale.
    #
    # Perf note: stripping `.complete` means a miss on a whole-tree lint now
    # pays a real per-call `git log --all -- <path>` subprocess spawn (tier-3
    # fallback inside `check_lineage_reachability`/`resolve_target`) instead
    # of an O(1) fast-reject, for every dangling ref this lint encounters
    # across the ENTIRE corpus in one run — this is the widest-blast-radius
    # of the three call sites this fix touches, since a whole-tree lint is
    # exactly the many-files/one-cache shape that made the fast-reject wrong
    # in the first place. Correctness must win over speed here (a dangling
    # ref silently misclassified as "never tracked" is exactly the bug this
    # fix closes), but a corpus with many dangling/pruned refs could see this
    # lint run meaningfully slower than before.
    git_history_cache = as_history_membership_set(build_git_history_cache(repo_root))
    handoff_id_index = _build_handoff_id_index(repo_root)  # built once — see _build_handoff_id_index docstring

    violations: list[dict] = []
    ref_warnings: list[dict] = []
    seen_files: set[str] = set()

    schema_names = [k for k in schemas if k not in ('_byGlob', '_byKind')]
    for name in schema_names:
        schema = schemas[name]
        # Collection-glob override, scoped to handoff-archived ONLY (2026-07-26,
        # PM-ratified): the vendored applies_to on disk is 'archive/handoffs/*.md'
        # (single star — DoE-owned SSOT, claude-klabauter only vendors it, see
        # check_schema_drift's tamper-check; editing the vendored copy directly
        # here would diverge from DoE HEAD and requires an upstream DoE-claude
        # schema edit + re-vendor, out of scope for this fix). _GLOB_OVERRIDES
        # already carries the recursive 'archive/handoffs/**/*.md' the registry
        # needs (build_type_to_glob above uses the SAME dict for the same
        # reason) — reused here rather than re-derived, so nested
        # archive/handoffs/<month>/*.md records are walked instead of silently
        # skipped by the whole-tree collector.
        # Review: code-reviewer — F1: this loop previously indexed
        # _GLOB_OVERRIDES by `name` (every loaded schema, not just
        # handoff-archived), so a future schema literally named
        # 'cross-repo-memo' would silently pick up the memo-inbox glob here
        # with no negative-spec covering the switch. Scoped by literal
        # equality to match match_schema()'s own archive-path-check
        # discipline (which reads _GLOB_OVERRIDES by fixed key, not by
        # iterating schema names).
        glob = _GLOB_OVERRIDES.get('handoff-archived') if name == 'handoff-archived' else schema.get('applies_to')
        if not glob:
            continue
        if schema.get('match_mode') == 'inline-tag-per-entry':
            continue  # unported — see _run_single_file_check note

        for full_path, repo_rel in _lint_collect_files_for_glob(repo_root, glob):
            if _lint_is_sidecar_file(repo_rel):
                continue
            if re.search(r'\.(ya?ml|json)$', repo_rel):
                continue
            if repo_rel in seen_files:
                continue

            try:
                content = Path(full_path).read_text(encoding='utf-8')
            except OSError as exc:
                seen_files.add(repo_rel)
                violations.append({
                    'file': repo_rel, 'schema': name,
                    'errors': [{'field': '(read)', 'error': 'could not read file', 'hint': str(exc)}],
                })
                continue

            parsed = parse_frontmatter(content)
            frontmatter = parsed['frontmatter']

            declares_unregistered_kind = (
                frontmatter is not None
                and frontmatter.get('kind') is not None
                and str(frontmatter['kind']) not in schemas['_byKind']
            )
            seen_files.add(repo_rel)
            if declares_unregistered_kind:
                continue

            # docs/plans/INDEX.md and docs/plans/README.md route to no
            # schema (match_schema returns None below for the same reason)
            # — but the fallback two lines down ("resolved is None -> use
            # the CURRENT glob-iteration schema/name") exists for other
            # legitimate no-better-match cases and would silently re-adopt
            # this glob's schema (`plan`) for these two files, defeating
            # the exclusion for exactly the whole-tree lint this fix is
            # for. Skip explicitly rather than falling through.
            if _is_plan_dir_index_routing_excluded(repo_rel.replace('\\', '/'), frontmatter):
                continue

            resolved = match_schema(repo_rel, frontmatter, schemas)
            effective_name = resolved['schemaName'] if resolved else name
            effective_schema = resolved['schema'] if resolved else schema

            result = _tolerate_handoff_kind_aliases_in_result(
                validate_frontmatter_obj(frontmatter, effective_schema),
                effective_name, effective_schema, frontmatter,
            )
            base_errors = (result.get('errors') or []) if not result.get('ok') else []

            ref_errors: list[ErrorDict] = []
            ref_field_warnings: list[ErrorDict] = []
            if effective_name == 'handoff':
                ref_errors, ref_field_warnings = _check_handoff_refs(
                    frontmatter, repo_root, full_path, repo_rel, git_history_cache,
                    handoff_id_index, strict_refs,
                )

            # _check_handoff_refs already applied the strict_refs split
            # internally (dangling refs land in errors when strict, warnings
            # otherwise; never-silently-disagree divergences always land in
            # errors) — the caller just combines and reports, it does not
            # re-derive the split.
            combined_errors = list(base_errors) + ref_errors
            for e in ref_field_warnings:
                ref_warnings.append({'file': repo_rel, 'schema': effective_name, 'warning': e})

            if combined_errors:
                violations.append({'file': repo_rel, 'schema': effective_name, 'errors': combined_errors})

    if as_json:
        print(json.dumps({'ok': len(violations) == 0, 'violations': violations, 'refWarnings': ref_warnings}, indent=2))
    else:
        if not violations:
            print(f'lint-frontmatter: all files valid (root: {repo_root})')
        else:
            print(f'lint-frontmatter: {len(violations)} violation(s) (root: {repo_root})\n')
            for v in violations:
                print(f"  {v['file']}  [{v['schema']}]")
                for e in v['errors']:
                    print(f"    - {e['field']}: {e['error']}")
                    if e.get('hint'):
                        print(f"      hint: {e['hint']}")
        if ref_warnings:
            print(f"\nlint-frontmatter: {len(ref_warnings)} dangling handoff-DAG ref warning(s) "
                  "(non-blocking; pass --strict-refs at a cadence gate to escalate)\n")
            for rw in ref_warnings:
                print(f"  {rw['file']}  [{rw['schema']}]")
                print(f"    - {rw['warning']['field']}: {rw['warning']['error']}")
                if rw['warning'].get('hint'):
                    print(f"      hint: {rw['warning']['hint']}")

    return 1 if violations else 0


def main(argv: list[str]) -> int:
    """Thin argv-only entrypoint dispatching to validate_frontmatter_obj + dag.

    coordinator/bin/lint-frontmatter.py imports and calls this directly — all
    CLI logic lives here (porter-brief rule: do not duplicate logic in the
    trampoline). See the CLI-trampoline section docstring above for scope.

    Exit codes (mirrors the deleted oracle):
      0 — no violations (whole-tree or --file), or --file finding no schema
          match / a non-lintable file.
      1 — one or more violations found.
      2 — usage/configuration error (unknown flag, missing --file/--root
          argument, --file target not found).
    """
    args = _lint_parse_args(argv)
    if args is None:
        return 2

    repo_root = _lint_find_repo_root(args['root'])

    # Fail loud (rc 2) on an explicit --root that doesn't resolve to a real
    # directory, rather than silently walking zero files and reporting "all
    # files valid" — a false-green result for the cadence gates (--strict-refs
    # whole-tree walk) this tool exists to protect. This is a defensive
    # addition beyond the deleted oracle (which also silently no-oped on a
    # bad --root — verified against the pre-deletion .js); it never fires for
    # a --root that resolves correctly, so it does not affect AC1 parity for
    # any currently-passing invocation.
    if args['root'] and not os.path.isdir(repo_root):
        print(f'lint-frontmatter: --root does not resolve to a directory: {repo_root}', file=sys.stderr)
        return 2

    if args['file'] is not None:
        return _run_single_file_check(repo_root, args['file'], args['json'])

    return _run_tree_walk(repo_root, args['json'], args['strict_refs'])


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
