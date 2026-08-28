"""
coordinator_core.frontmatter.tests.test_schema_keyword_coverage

Regression guard for the "keyword present in schema but unimplemented"
defect class: `_validate_json_schema_node` silently ignores any JSON-Schema
keyword it doesn't recognize (the walker only checks the keys it knows
about), so a schema author can ship e.g. `maxLength` and have it be a
no-op — no error, no validation, just quiet non-enforcement. That shape
shipped TWICE before this test existed, most recently when `pattern` landed
as a fleet-shared MAJOR while silently unenforced.

This walks every vendored schema structurally (keyword position vs
user-defined PROPERTY NAME position — a property literally named `pattern`
or `items` under a `properties` block is data, not a keyword) and asserts
every keyword key it finds is in `_SUPPORTED_KEYWORDS`, a hardcoded mirror
of what `_validate_json_schema_node` actually implements (see that
function's docstring — the two lists must be kept in sync by hand; this
test does not introspect the implementation itself).

Spec backlink: review finding, coordinator:code-reviewer session
bda67cb8-d819-430b-81e3-45de924d3cd6 (Finding 1) — closes the "is the class
actually closed" gap with a guard instead of relying on periodic full sweeps.

Negative-spec:
  - Does NOT validate values against schemas (that's schema_validate.py's
    job) — this only validates that the SCHEMA FILES themselves use no
    keyword the validator can't see.
  - `unevaluatedProperties` is included even though no shipped schema
    currently uses it (see schema_validate.py's module docstring) — it IS
    implemented, so it belongs in the supported set regardless of use.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"

# Mirror of every keyword `_validate_json_schema_node`
# (coordinator_core/frontmatter/schema_validate.py) implements. Keep in sync
# by hand with that function's docstring "Supported keywords:" line whenever
# either changes — this test does not introspect the implementation.
_SUPPORTED_KEYWORDS = frozenset({
    "$ref",
    "anyOf",
    "allOf",
    "oneOf",
    "const",
    "not",
    "type",
    "enum",
    "format",
    "pattern",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "required",
    "properties",
    "if",
    "then",
    "propertyNames",
    "additionalProperties",
    "unevaluatedProperties",
    "minItems",
    # Implemented in _validate_json_schema_node's array-level block. Arrived
    # with plan.schema.json 2.5.0 (review_signals); implemented rather than
    # allowlisted-away, since a tolerated keyword is a silent no-op.
    "uniqueItems",
    "items",
    # Implemented in _validate_json_schema_node's object-level block. Arrived
    # with plan.schema.json 2.9.0, which makes `prime_exit_criterion.falsifier.
    # promotion_reason` depend on `promotion`. Implemented rather than
    # allowlisted-away for the reason this test exists: vendoring the bump
    # without it would have shipped that constraint as a silent no-op.
    "dependentRequired",
})

# Non-keyword structural/metadata keys that may appear on a schema node but
# are not JSON-Schema-keyword-position at all (schema identity/tooling
# metadata, not something `_validate_json_schema_node` needs to recognize).
# Review: code-reviewer — this test asserts KEYWORD NAMES are implemented,
# which is blind to the case where a keyword name is implemented but a
# specific VALUE of it is not (e.g. `format: "date"` is handled but
# `format: "uri"` was silently a no-op — same silent-non-enforcement class,
# invisible to the keyword-name walk above because "format" itself is a
# recognized/supported keyword). Mirror of every `format` VALUE
# `_validate_json_schema_node` actually implements (see that function's
# `format` branch) — keep in sync by hand.
_SUPPORTED_FORMAT_VALUES = frozenset({
    "date",
    "date-time",
    "uri",
})

_METADATA_KEYS = frozenset({
    "$schema",
    "$id",
    "$comment",
    "title",
    "description",
    "default",
    "x-schema-version",
    "x-schema-name",
    "x-bump-class",
    "x-bump-note",
    "x-baton-class",
    # The single-point declaration of the producer-axis typed-command
    # vocabulary (49 members: 46 coordinator command verbs plus
    # `other-command`, `hand-authored`, `unresolved`), each carrying a
    # `group` for consumer-side facet grouping. Authored and owned in
    # DoE-claude and vendored here with handoff.schema.json 7.1.0 — a
    # DECLARATION consumed by readers and a parity test on their side, never
    # a value-shape constraint this validator could enforce. Deliberately
    # single-point: re-enumerating the vocabulary on this side would create a
    # second source of truth that drifts silently, which is why
    # `session/producer_resolve.py` validates only the closed op-identity
    # axis and leaves membership of this open one to the declaring repo.
    "x-producer-typed-command",
    # Declares which sibling repos read this schema's records as an external
    # contract (e.g. research-claim.schema.json's `["example-market-data-repo"]`)
    # — annotation for humans/memos, not a value-shape constraint. See
    # DoE-claude coordinator/docs/wiki/coordinator-tripwires.md
    # "RESEARCH-CLAIMS-JSON-IS-AN-EXTERNAL-CONTRACT".
    "x-external-consumers",
    # Declares the markdown body sections expected BELOW the frontmatter
    # (run-report.schema.json's `## Observations` etc.). Metadata, not a
    # frontmatter value-shape constraint — this validator only ever sees the
    # parsed frontmatter mapping, so a body-section expectation is not
    # something it could enforce or silently no-op on. Unlike
    # `x-external-consumers` (which has real enforcement elsewhere — see the
    # comment above), `x-body-sections` currently has NO enforcement anywhere
    # in the tree: nothing in body_blocks.py/sentinel_blocks.py reads it, so
    # this classification is presently decorative-only, not merely
    # out-of-scope for this validator. Tracked as
    # state/improvement-queue/2026-08-06-run-report-s-x-body-sections-declares-ex-1e33c80a281a.yaml.
    "x-body-sections",
    "examples",
    # Legacy-YAML-dialect matcher metadata (schema_validate.py's
    # `match_schema_for_path`/`load_schemas` seam) — not JSON-Schema-keyword
    # position, describes which records this schema applies to and how it's
    # matched, not a value-shape constraint.
    "applies_to",
    "match_mode",
    # Free-form prose annotations / repo-local vocabulary tables that are
    # not JSON-Schema-keyword position — schema author commentary or a
    # closed-vocabulary lookup consumed elsewhere (e.g. handoff.schema.json's
    # `kinds` top-level enumeration, mirrored by `properties.kind.enum`
    # which IS walked normally).
    "_additionalProperties_note",
    "kinds",
})

# Keys whose IMMEDIATE children are schema-VALUED but not keyword-position
# themselves (e.g. `properties`'s children are user-defined property names,
# not keywords) are handled specially in `_walk_schema_node` below rather
# than listed here.


def _walk_schema_node(
    node,
    json_path: str,
    offenders: list[tuple[str, str]],
    unpaired: list[tuple[str, str]] | None = None,
) -> None:
    """Recursively walk a JSON-Schema-shaped node, collecting any KEYWORD key
    not in `_SUPPORTED_KEYWORDS` (or `_METADATA_KEYS`) into `offenders` as
    (keyword, json_path) pairs. Does not descend into non-schema data
    (enum lists, const literals, `required` string-array contents).

    If `unpaired` is given, also collects any node that carries exactly one
    of `if`/`then` — `_validate_json_schema_node` only applies conditional
    logic when BOTH are present on the same node, so a lone `if` or `then`
    is silently inert: individually a supported keyword, but unenforced in
    combination. See Finding 2, coordinator:code-reviewer session
    bda67cb8-d819-430b-81e3-45de924d3cd6 (43d0cdad sidecar).
    """
    if not isinstance(node, dict):
        return

    if unpaired is not None and ("if" in node) != ("then" in node):
        unpaired.append(("if" if "if" in node else "then", json_path))

    for key, val in node.items():
        if key in _METADATA_KEYS:
            continue
        if key == "$defs":
            if not isinstance(val, dict):
                raise TypeError(
                    f"malformed schema: {json_path}/$defs is not an object "
                    f"(got {type(val).__name__}) — $defs must map def-name to sub-schema"
                )
            # $defs's children are named sub-schemas (referenced via $ref),
            # not keywords themselves — same shape as `properties`, walk
            # each definition's schema body.
            for def_name, def_schema in val.items():
                _walk_schema_node(def_schema, f"{json_path}/$defs/{def_name}", offenders, unpaired)
            continue
        if key not in _SUPPORTED_KEYWORDS:
            offenders.append((key, json_path))
            continue

        # Recurse per-keyword into the schema-shaped sub-structure. Any key
        # not explicitly handled here (type, enum, format, pattern,
        # minLength, minimum, minItems, required, const, $ref,
        # additionalProperties-as-bool, unevaluatedProperties-as-bool) is a
        # leaf: its value is data (a literal, a list of literals, a bool),
        # not a nested schema, so nothing further to walk.
        if key == "properties" and isinstance(val, dict):
            # Children here are USER-DEFINED PROPERTY NAMES, not keywords —
            # walk each one's value (the nested schema) but never check the
            # property name itself against _SUPPORTED_KEYWORDS.
            for prop_name, prop_schema in val.items():
                _walk_schema_node(prop_schema, f"{json_path}/properties/{prop_name}", offenders, unpaired)
        elif key in ("items", "not", "propertyNames", "if", "then"):
            _walk_schema_node(val, f"{json_path}/{key}", offenders, unpaired)
        elif key in ("anyOf", "allOf", "oneOf") and isinstance(val, list):
            for i, sub in enumerate(val):
                _walk_schema_node(sub, f"{json_path}/{key}[{i}]", offenders, unpaired)
        elif key == "additionalProperties" and isinstance(val, dict):
            # Schema-valued additionalProperties IS supported (see
            # schema_validate.py's _validate_json_schema_node) — walk it like
            # any other nested schema so a keyword unsupported inside it is
            # still caught.
            _walk_schema_node(val, f"{json_path}/additionalProperties", offenders, unpaired)
        # $ref, type, enum, format, pattern, minLength, minimum, minItems,
        # required, const, additionalProperties (bool),
        # unevaluatedProperties (bool) — leaves, nothing to recurse into.


def _collect_format_values(node, json_path: str, found: list[tuple[str, str]]) -> None:
    """Recursively collect every `format` keyword's VALUE (not just its
    presence as a keyword name) into `found` as (value, json_path) pairs.

    Reuses the same keyword-position-vs-property-name distinction as
    `_walk_schema_node` (a property literally named `format` under
    `properties` is data, not the `format` keyword) by walking the same
    schema-shaped structure.
    """
    if not isinstance(node, dict):
        return
    for key, val in node.items():
        if key == "format" and isinstance(val, str):
            found.append((val, json_path))
            continue
        if key in _METADATA_KEYS or key == "$defs":
            if key == "$defs" and isinstance(val, dict):
                for def_name, def_schema in val.items():
                    _collect_format_values(def_schema, f"{json_path}/$defs/{def_name}", found)
            continue
        if key == "properties" and isinstance(val, dict):
            for prop_name, prop_schema in val.items():
                _collect_format_values(prop_schema, f"{json_path}/properties/{prop_name}", found)
        elif key in ("items", "not", "propertyNames", "if", "then"):
            _collect_format_values(val, f"{json_path}/{key}", found)
        elif key in ("anyOf", "allOf", "oneOf") and isinstance(val, list):
            for i, sub in enumerate(val):
                _collect_format_values(sub, f"{json_path}/{key}[{i}]", found)
        elif key == "additionalProperties" and isinstance(val, dict):
            _collect_format_values(val, f"{json_path}/additionalProperties", found)


def _schema_files() -> list[Path]:
    return sorted(SCHEMAS_DIR.glob("*.json"))


class TestSchemaKeywordCoverage:
    def test_every_shipped_schema_json_uses_only_implemented_keywords(self):
        assert _schema_files(), f"no schema files found under {SCHEMAS_DIR} — path likely wrong"

        offenders: list[tuple[str, str, str]] = []  # (keyword, json_path, filename)
        unpaired_offenders: list[tuple[str, str, str]] = []  # (keyword, json_path, filename)
        for schema_path in _schema_files():
            data = json.loads(schema_path.read_text(encoding="utf-8"))
            per_file_offenders: list[tuple[str, str]] = []
            per_file_unpaired: list[tuple[str, str]] = []
            _walk_schema_node(data, "$", per_file_offenders, per_file_unpaired)
            for keyword, json_path in per_file_offenders:
                offenders.append((keyword, json_path, schema_path.name))
            for keyword, json_path in per_file_unpaired:
                unpaired_offenders.append((keyword, json_path, schema_path.name))

        if offenders:
            lines = [
                f"  - keyword {keyword!r} at {json_path} in {filename}"
                for keyword, json_path, filename in offenders
            ]
            pytest.fail(
                "Unimplemented JSON-Schema keyword(s) found in shipped schema(s) — "
                "_validate_json_schema_node silently ignores keywords it doesn't "
                "implement, so this would validate as a no-op:\n"
                + "\n".join(lines)
                + "\n\nFix: either implement the keyword in "
                "coordinator_core/frontmatter/schema_validate.py's "
                "_validate_json_schema_node and add it to this test's "
                "_SUPPORTED_KEYWORDS, or stop using it in the schema. "
                "(This is the same defect class that shipped `pattern` as a "
                "fleet-shared MAJOR while silently unenforced.)"
            )

        if unpaired_offenders:
            lines = [
                f"  - lone {keyword!r} (no matching if/then partner) at {json_path} in {filename}"
                for keyword, json_path, filename in unpaired_offenders
            ]
            pytest.fail(
                "Unpaired if/then keyword found in shipped schema(s) — "
                "_validate_json_schema_node only applies conditional logic when BOTH "
                "'if' and 'then' are present on the same node, so a lone one is "
                "silently inert even though each is individually a supported keyword:\n"
                + "\n".join(lines)
                + "\n\nFix: add the missing partner keyword, or remove the orphaned one."
            )

    def test_every_shipped_schema_format_value_is_implemented(self):
        # Spec backlink: review finding, coordinator:code-reviewer session
        # 0155defd-6f43-4dc3-b1e7-c2f199fd2ef0 (vendored-corpus-twelve slice)
        # — `format: "uri"` shipped in strategic-self-description.schema.json
        # as a silent no-op (only date/date-time were implemented), invisible
        # to `test_every_shipped_schema_json_uses_only_implemented_keywords`
        # above because that test only checks keyword NAMES and `format`
        # itself IS a recognized keyword. This closes the value-level blind
        # spot generally rather than only the one instance.
        assert _schema_files(), f"no schema files found under {SCHEMAS_DIR} — path likely wrong"

        offenders: list[tuple[str, str, str]] = []
        for schema_path in _schema_files():
            data = json.loads(schema_path.read_text(encoding="utf-8"))
            per_file: list[tuple[str, str]] = []
            _collect_format_values(data, "$", per_file)
            for value, json_path in per_file:
                if value not in _SUPPORTED_FORMAT_VALUES:
                    offenders.append((value, json_path, schema_path.name))

        if offenders:
            lines = [
                f"  - format {value!r} at {json_path} in {filename}"
                for value, json_path, filename in offenders
            ]
            pytest.fail(
                "Unimplemented `format` VALUE(s) found in shipped schema(s) — "
                "_validate_json_schema_node's format branch silently no-ops on "
                "any format value it doesn't recognize, even though `format` "
                "itself is a supported keyword:\n"
                + "\n".join(lines)
                + "\n\nFix: either implement the format value in "
                "coordinator_core/frontmatter/schema_validate.py's `format` "
                "branch and add it to this test's _SUPPORTED_FORMAT_VALUES, "
                "or stop using it in the schema."
            )

    def test_guard_fires_on_synthetic_lone_if(self):
        # Proves the paired-keyword check (Finding 2, coordinator:code-reviewer
        # session bda67cb8-d819-430b-81e3-45de924d3cd6, 43d0cdad sidecar) is not
        # decorative — a synthetic schema with `if` and no `then` must be caught.
        offenders: list[tuple[str, str]] = []
        unpaired: list[tuple[str, str]] = []
        _walk_schema_node({"if": {"type": "string"}}, "$", offenders, unpaired)
        assert offenders == []
        assert unpaired == [("if", "$")]

    def test_guard_does_not_fire_on_paired_if_then(self):
        offenders: list[tuple[str, str]] = []
        unpaired: list[tuple[str, str]] = []
        _walk_schema_node({"if": {"type": "string"}, "then": {"minLength": 1}}, "$", offenders, unpaired)
        assert offenders == []
        assert unpaired == []

    def test_malformed_defs_raises_shape_error_not_keyword_offender(self):
        # Finding 1, coordinator:code-reviewer session bda67cb8-d819-430b-81e3-45de924d3cd6
        # (43d0cdad sidecar) — a non-dict $defs must not be misreported as an
        # "unsupported keyword"; it's a shape error.
        with pytest.raises(TypeError, match=r"\$defs is not an object"):
            _walk_schema_node({"$defs": ["not", "a", "dict"]}, "$", [], [])
