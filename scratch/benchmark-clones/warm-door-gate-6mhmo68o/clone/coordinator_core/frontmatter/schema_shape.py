"""coordinator_core.frontmatter.schema_shape -- claude-klabauter's own semantic
shape-hash over a vendored JSON Schema, so a change to what a schema
VALIDATES can be detected independently of what it SAYS.

Why this exists: claude-klabauter vendors DoE's schemas under
`coordinator_core/frontmatter/schemas/`, and a local edit that widens one of
them (an `enum` entry, a new `$defs` sub-schema, a changed `required` set) can
land while `x-schema-version` stays put. It has happened on
`percolate-store.schema.json` more than once. Both repos then read the same
version string while validating differently, and nothing notices. This module
supplies the primitive the detecting test needs: a hash that moves when
validation behaviour moves and stays put when only prose moves.

NEGATIVE SPEC -- what this is NOT:

  - NOT a byte hash of the file. A byte hash trips on reindentation, key
    reordering, and a typo fix in a `description`, so a gate built on one
    would demand an `x-schema-version` bump for edits that change no
    validator's behaviour -- and on a schema DoE and claude-klabauter hold
    byte-identically, a spurious bump costs a whole re-vendor round trip.
    Hence the annotation strip and the sorted-key canonical serialization.

  - NOT a cross-repo comparison. Three gates already compare claude-klabauter's copy to
    DoE's (`schema_drift_watch.scan_vendored_schema_drift`,
    `schema_validate.check_schema_drift`, and schema_validate's consumer-ahead
    version gate) and all three read GREEN at the instant of the bad edit,
    because at that instant the two repos still agree on the version and the
    shape edit has not reached DoE either. The axis this module serves is a
    file against its OWN last-committed blob.

  - NOT an `x-*` prefix glob. The authoring annotations stripped here are a
    NAMED allowlist (`x-bump-class`, `x-bump-note`). Several `x-` keys are
    read by code across the repo boundary -- `x-baton-class` on
    `handoff.schema.json` is consumed by claude-klabauter's cockpit-contract summaries
    seam -- so a prefix glob would silently absorb a code-consumed key and go
    blind to a real change in it.

  - NOT a version stamper. The deliverable of the gate above this module is a
    red test naming both hashes, never a machine-chosen bump: major-vs-minor
    is an authoring judgement.

Spec backlink: pln-a-vendored-schema-cannot-chang-42124c
(AC1, AC2; chunk C1). The annotation-stripping RULE is borrowed from DoE's
`coordinator/tests/_schema_shape.py`; the code deliberately is not -- an
unpoliced second copy of a must-not-drift file is the same defect class this
plan closes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = [
    "semantic_shape_hash",
    "strip_annotations",
    "PROSE_ANNOTATION_KEYWORDS",
    "AUTHORING_ANNOTATION_KEYWORDS",
]


PROSE_ANNOTATION_KEYWORDS = frozenset({"description", "$comment"})
"""JSON-Schema keywords the spec defines as having no effect on validation.
Stripped at schema positions only -- never as a flat key-name match, see
`strip_annotations`' negative spec."""


AUTHORING_ANNOTATION_KEYWORDS = frozenset({"x-bump-class", "x-bump-note"})
"""Root-level annotations ABOUT a version bump rather than about document
shape. A validator behaves identically with or without them. A NAMED
allowlist, never an `x-*` glob (see module negative spec)."""


_SINGLE_SCHEMA_KEYWORDS = frozenset(
    {
        "items",
        "additionalProperties",
        "additionalItems",
        "contains",
        "propertyNames",
        "unevaluatedItems",
        "unevaluatedProperties",
        "not",
        "if",
        "then",
        "else",
        "contentSchema",
    }
)
"""Keywords whose VALUE is itself a schema -- descend directly. `items` is
polymorphic: draft-07 allows a LIST of per-position schemas (2020-12 split
that out as `prefixItems`), handled as an array-of-schemas position."""

_SCHEMA_ARRAY_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
"""Keywords whose value is a list, each ITEM a schema."""

_NAME_KEYED_SCHEMA_MAP_KEYWORDS = frozenset(
    {
        "properties",
        "patternProperties",
        "$defs",
        "definitions",
        "dependentSchemas",
        "dependencies",
    }
)
"""Keywords whose value is a dict of USER-CHOSEN names to schemas. The keys
are data, not keywords: a property literally named `description` keeps its
key, while the schema underneath it still has its own annotations stripped."""

_VERSION_KEYWORD = "x-schema-version"


def strip_annotations(node: Any) -> Any:
    """Return `node` with pure annotations removed at schema positions,
    walked as DEFAULT-DENY descent: strip only where the node is known to be
    in schema position, descend only into positions known to hold schemas.

    NEGATIVE SPEC -- this must NOT be a flat recursive strip of every key
    named `description` (or `$comment`) wherever it appears. Live schemas in
    this corpus declare a *property* literally named `description`
    (`handoff.schema.json` at
    `properties.carried_items.items.properties.description`, and others). A
    flat strip would delete those property DECLARATIONS from the hash input,
    making the gate blind to adding or removing a `description` property --
    a real shape change it would then silently pass.

    Polarity rule this walk depends on: forgetting to list a schema-position
    keyword above is SAFE (a spurious trip -- noisy, not blind). Forgetting a
    name-keyed-map or instance-data carve-out is NOT safe (silent blindness).
    So the default is to neither descend nor strip, and every schema-bearing
    position is named explicitly in the keyword sets above rather than
    inferred from "it happens to be a nested dict".

    Instance-data positions (`default`, `const`, `examples`, `enum`) hold
    DATA, not schemas: they are hashed verbatim, even when they contain a key
    named `description`. That falls out of the allowlists automatically, and
    is stated here so a later extender does not add them by pattern-matching
    on "it's a nested object". `$ref`'s value is a URI string -- nothing to
    descend into.
    """
    if not isinstance(node, dict):
        return node

    result: dict[str, Any] = {}
    for key, value in node.items():
        if key in PROSE_ANNOTATION_KEYWORDS:
            continue
        if key == "items" and isinstance(value, list):
            result[key] = [strip_annotations(item) for item in value]
        elif key in _SINGLE_SCHEMA_KEYWORDS:
            result[key] = strip_annotations(value)
        elif key in _SCHEMA_ARRAY_KEYWORDS:
            result[key] = (
                [strip_annotations(item) for item in value]
                if isinstance(value, list)
                else value
            )
        elif key in _NAME_KEYED_SCHEMA_MAP_KEYWORDS:
            result[key] = (
                {subkey: strip_annotations(subvalue) for subkey, subvalue in value.items()}
                if isinstance(value, dict)
                else value
            )
        else:
            result[key] = value
    return result


def semantic_shape_hash(schema: dict) -> str:
    """Stable `sha256:<hex>` over a schema's VALIDATION shape.

    Insensitive to: key order (canonicalized with `sort_keys=True`), prose
    annotations (`description`, `$comment`) at schema positions, root-level
    authoring annotations (`x-bump-class`, `x-bump-note`), and the
    `x-schema-version` value itself -- the version is the thing the gate above
    this compares SEPARATELY, so folding it into the hash would make every
    shape comparison trivially "changed" whenever the version moved and hide
    the one case that matters (shape moved, version did not).

    Sensitive to everything else, including a widened `enum`, an added or
    removed `required` entry, an added or removed `properties` key, and a
    changed `type`.

    Does not mutate `schema`: the two `.pop`s below act on a shallow copy, and
    `strip_annotations` builds a fresh container at every node it descends
    into.
    """
    stripped = dict(schema)
    stripped.pop(_VERSION_KEYWORD, None)
    for key in AUTHORING_ANNOTATION_KEYWORDS:
        stripped.pop(key, None)
    shape = strip_annotations(stripped)
    canonical = json.dumps(shape, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
