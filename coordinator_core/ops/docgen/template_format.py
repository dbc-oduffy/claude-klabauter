"""coordinator_core.ops.docgen.template_format — the template-as-data format (C2).

Purpose: pin the declarative shape every extracted ``coordinator-doc-new`` scaffolder
is expressed against, so C3 (type enumeration) and C4 (render) author against ONE
frozen contract rather than each inventing its own reading of the 22 JSON files
under ``templates/``. This module is validation-only — it does NOT render a
document (that pure ``(doc_type, resolved_field_values) -> str`` function is C4's
surface) and does NOT enumerate the canonical type set from the example-doctrine-repo manifest
(that internal function, unioning manifest + known supplements, is C3's surface).
Neither this module nor any sibling in this package registers an IPC op — see
the package docstring.

Substrate finding this format encodes (plan Problem section, verified against all
22 ``_scaffold_*`` bodies in example-doctrine-repo's ``coordinator-doc-new``): every scaffolder is a
PURE function of already-resolved strings — no subprocess, no git, no filesystem
access inside any builder — and the entire conditional surface reduces to exactly
THREE idioms, each reused independently across fields:

  1. **present-as-null** — the key is ALWAYS emitted; its value is the quoted
     resolved string when supplied, else a bare unquoted fallback token (``null``
     by default — see ``absent_literal`` below). Used for fields like
     ``deliverable_id``/``initiative`` that must round-trip through a typed-null
     read (DR-069) rather than ever going key-absent. The same always-emit /
     substitute-a-schema-legal-placeholder-when-absent shape also covers fields
     whose fallback is the literal word ``PLACEHOLDER`` rather than ``null``
     (e.g. ``goal-seed``'s ``gate_dependency``) — one idiom, a configurable
     ``absent_literal`` (default ``"null"``), not a fourth construct.
  2. **optional-key-omit** — the ENTIRE line is emitted only when the resolved
     value is present/truthy; otherwise the key does not appear at all. Used for
     fields the schema declares OPTIONAL with no cross-field rule forcing
     presence (e.g. ``handoff_id``, ``completion_id``). An optional
     ``absent_comment`` companion may supply a purely decorative ``#``-comment
     line to emit in the key's place when absent (e.g. ``completion``'s
     ``chain`` — "omit this line entirely for standalone entries") — inert to
     any YAML parse (the key itself is still omitted), so this remains the same
     idiom, not a fourth.
  3. **list-emit-if-present** — a list-valued field: emitted as a ``key:`` header
     line followed by one ``  - <item>`` line per element ONLY when the resolved
     list is non-empty; the whole block (header + items) is omitted when the
     list is empty or absent (e.g. ``origin_goal_id`` from the ``goals`` arg).
  4. **value-or-literal-fallback** — added 2026-08-05 (re-opens the "closed at 3"
     finding above; see ``archive/specs/2026-08/2026-08-03-gate-dependency-template-emission-spec.md``
     § C1, which introduced this idiom oracle-side and was never ported here).
     When the resolved value is truthy, emit ``{key}: {value}{suffix}`` exactly
     like ``value``; when falsy, emit a completely different FIXED line
     (``fallback_line``, verbatim — a different key, not this field's absence).
     Distinct from ``present_as_null`` (which always emits the SAME key with a
     substituted value) and from ``optional_omit`` (whose absent branch is
     silence or a comment on the SAME key, never a second key). Used for
     ``gate_dependency``, whose unfilled default now writes a `blocking_notes``
     placeholder instead of parking the deprecated field (C1 of the spec above).

A template additionally carries plain, non-conditional lines:

  - **literal** — a fixed line, no presence branch. MAY still embed
    ``str.format``-style ``{field}`` placeholders (e.g. ``goal``'s commented
    ``# goal_id: {goal_id}`` hint, which repeats the SAME computed value the
    ``id:`` field above it already emits) — "literal" means "not conditional",
    not "no substitution"; the placeholder substitution pass is C4's, same as
    for ``raw`` body lines (below), against the same resolved-value mapping.
    Convention: a ``literal``/``raw`` placeholder always names a mapping key
    holding the value in ITS FINAL EMITTED FORM (already YAML-quoted where the
    oracle quotes it) — ``literal``/``raw`` never apply quoting themselves, only
    ``value``/the 4 conditional kinds do. Where the same underlying value
    appears once quoted (inside a ``value``/conditional field) and once bare
    inside a ``literal``/``raw`` line, the resolved-value mapping carries BOTH
    forms under distinct keys (e.g. ``goal``'s ``goal_id`` for the quoting
    ``value`` field and ``goal_id_quoted`` for the literal hint line) — C4
    computes both from the one underlying string, never a template-side quote
    call.
  - **value** — a REQUIRED field, ALWAYS emitted as ``{key}: {value}{suffix}``
    (optionally YAML-quoted per ``quote``) — no presence branch; the caller is
    expected to always supply the value (e.g. ``title``, ``created``).

No FieldSpec kind beyond these six exists, and no kind reaches for arbitrary
Python (no eval, no callable payloads in the JSON) — AC3's "no idiom requires an
escape hatch to Python" is a structural property of this module's validator, not
a convention: ``validate_template`` rejects any ``kind`` outside ``FIELD_KINDS``.

The body is a flat, ORDER-PRESERVING list of ``{"kind": "raw", "lines": [...]}``
blocks — every extracted scaffolder's body is static section-skeleton prose (the
substrate finding above: zero conditional logic outside frontmatter fields), so
one ``raw`` kind suffices; there is no body-level idiom. A ``raw`` line MAY embed
``str.format``-style ``{field}`` placeholders (e.g. ``"# {title}"`` in the plan
template) — C4's render function substitutes these against the same resolved
field-value mapping used for frontmatter fields; this module does not perform
that substitution itself (validation-only, per the module docstring above).

Two frontmatter *styles* exist on top of the field grammar:

  - ``"fenced"``  — the common ``---\\n<fields>\\n---\\n<body>`` shape.
  - ``"whole_document"`` — the whole-file-IS-the-record shape (``goal``,
    ``strategic-self-description``): no ``---`` fences, no markdown body, and
    ``additionalProperties: false`` upstream means the field list is exhaustive.
    A ``whole_document`` template's ``body`` MUST be ``null``.

Spec backlink: docs/plans/2026-07-21-strang-12-doc-generation-strangle.md § C2 (AC3)
Oracle: /coordinator/bin/coordinator-doc-new (example-doctrine-repo clone) — every ``_scaffold_*`` fn.
Negative-spec: this module does not write files, does not shell out, and does not
import ``coordinator_core.ops.emit.doe_drift`` — the byte-identity conformance
harness against the live example-doctrine-repo oracle is C6's surface, not this one's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FORMAT_VERSION = "docgen-template/v1"

# The two non-conditional line kinds (always emitted; no presence branch).
_PLAIN_FIELD_KINDS = frozenset({"literal", "value"})

# The 3 idioms AC3 originally closed the set at, plus "value_or_literal_fallback"
# (2026-08-05, archive/specs/2026-08/2026-08-03-gate-dependency-template-emission-spec.md
# § C1) — a real 4th shape the oracle introduced after AC3 landed, not a casual
# re-opening. Do not add a 5th without the same substrate-verification rigor.
CONDITIONAL_FIELD_KINDS = frozenset(
    {
        "present_as_null",
        "optional_omit",
        "list_emit_if_present",
        "value_or_literal_fallback",
    }
)

FIELD_KINDS = _PLAIN_FIELD_KINDS | CONDITIONAL_FIELD_KINDS

# Kinds carrying a fixed "line" string (no key/field/quote shape).
_LINE_SHAPED_KINDS = frozenset({"literal"})

# Kinds carrying the key/field/quote(/suffix) shape — "value" plus the 4 conditionals.
_KEYED_SHAPED_KINDS = frozenset({"value"}) | CONDITIONAL_FIELD_KINDS

BODY_KINDS = frozenset({"raw"})

FRONTMATTER_STYLES = frozenset({"fenced", "whole_document"})

# Per-kind required keys (beyond the universal "kind" discriminator).
_FIELD_REQUIRED_KEYS: dict[str, frozenset] = {
    "literal": frozenset({"line"}),
    "value": frozenset({"key", "field", "quote"}),
    "present_as_null": frozenset({"key", "field", "quote"}),
    "optional_omit": frozenset({"key", "field", "quote"}),
    "list_emit_if_present": frozenset({"key", "field", "quote"}),
    "value_or_literal_fallback": frozenset({"key", "field", "quote", "fallback_line"}),
}

# Keys every field spec dict may carry beyond the required set (all optional).
_FIELD_OPTIONAL_KEYS: dict[str, frozenset] = {
    "literal": frozenset(),
    "value": frozenset({"suffix"}),
    "present_as_null": frozenset({"suffix", "absent_literal"}),
    "optional_omit": frozenset({"suffix", "absent_comment"}),
    "list_emit_if_present": frozenset(),
    "value_or_literal_fallback": frozenset({"suffix"}),
}


class TemplateFormatError(ValueError):
    """Raised when a template dict/file does not conform to FORMAT_VERSION's shape.

    Carries every violation found (not just the first) in ``.errors`` so a single
    load reports the complete defect list, matching this repo's fail-loud-with-
    detail convention rather than a first-error-wins scan.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors) if self.errors else "invalid template")


def _validate_field_spec(spec: Any, index: int) -> list[str]:
    errors: list[str] = []
    prefix = f"frontmatter.fields[{index}]"
    if not isinstance(spec, dict):
        return [f"{prefix}: expected an object, got {type(spec).__name__}"]
    kind = spec.get("kind")
    if kind not in FIELD_KINDS:
        errors.append(
            f"{prefix}: unknown field kind {kind!r} — must be one of "
            f"{sorted(FIELD_KINDS)} (no escape hatch to arbitrary Python; AC3)"
        )
        return errors
    required = _FIELD_REQUIRED_KEYS[kind]
    optional = _FIELD_OPTIONAL_KEYS[kind]
    allowed = required | optional | {"kind"}
    missing = required - spec.keys()
    if missing:
        errors.append(f"{prefix} (kind={kind}): missing required key(s) {sorted(missing)}")
    extra = spec.keys() - allowed
    if extra:
        errors.append(f"{prefix} (kind={kind}): unexpected key(s) {sorted(extra)}")
    if kind in _LINE_SHAPED_KINDS and not isinstance(spec.get("line"), str):
        errors.append(f"{prefix} (kind={kind}): 'line' must be a string")
    if kind in _KEYED_SHAPED_KINDS:
        for k in ("key", "field"):
            if k in spec and not isinstance(spec[k], str):
                errors.append(f"{prefix} (kind={kind}): '{k}' must be a string")
        if "quote" in spec and not isinstance(spec["quote"], bool):
            errors.append(f"{prefix} (kind={kind}): 'quote' must be a bool")
        if "suffix" in spec and not isinstance(spec["suffix"], str):
            errors.append(f"{prefix} (kind={kind}): 'suffix' must be a string")
        if "absent_literal" in spec and not isinstance(spec["absent_literal"], str):
            errors.append(f"{prefix} (kind={kind}): 'absent_literal' must be a string")
        if "absent_comment" in spec and not isinstance(spec["absent_comment"], str):
            errors.append(f"{prefix} (kind={kind}): 'absent_comment' must be a string")
        if "fallback_line" in spec and not isinstance(spec["fallback_line"], str):
            errors.append(f"{prefix} (kind={kind}): 'fallback_line' must be a string")
    return errors


def _validate_body_spec(spec: Any, index: int) -> list[str]:
    errors: list[str] = []
    prefix = f"body[{index}]"
    if not isinstance(spec, dict):
        return [f"{prefix}: expected an object, got {type(spec).__name__}"]
    kind = spec.get("kind")
    if kind not in BODY_KINDS:
        errors.append(f"{prefix}: unknown body kind {kind!r} — must be one of {sorted(BODY_KINDS)}")
        return errors
    lines = spec.get("lines")
    if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
        errors.append(f"{prefix} (kind=raw): 'lines' must be a list of strings")
    extra = spec.keys() - {"kind", "lines"}
    if extra:
        errors.append(f"{prefix} (kind=raw): unexpected key(s) {sorted(extra)}")
    return errors


def validate_template(data: Any) -> list[str]:
    """Return a list of validation error strings; empty list means conformant.

    Never raises — callers that want fail-loud behaviour use ``load_template``,
    which wraps this in a ``TemplateFormatError``.
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return [f"template root must be an object, got {type(data).__name__}"]

    if data.get("format_version") != FORMAT_VERSION:
        errors.append(
            f"format_version must be {FORMAT_VERSION!r}, got {data.get('format_version')!r}"
        )

    doc_type = data.get("doc_type")
    if not isinstance(doc_type, str) or not doc_type:
        errors.append("doc_type must be a non-empty string")

    top_allowed = {"format_version", "doc_type", "frontmatter", "body"}
    extra_top = data.keys() - top_allowed
    if extra_top:
        errors.append(f"unexpected top-level key(s) {sorted(extra_top)}")

    frontmatter = data.get("frontmatter")
    style = None
    if frontmatter is not None:
        if not isinstance(frontmatter, dict):
            errors.append("frontmatter must be an object or null")
        else:
            style = frontmatter.get("style")
            if style not in FRONTMATTER_STYLES:
                errors.append(
                    f"frontmatter.style must be one of {sorted(FRONTMATTER_STYLES)}, got {style!r}"
                )
            fields = frontmatter.get("fields")
            if not isinstance(fields, list):
                errors.append("frontmatter.fields must be a list")
            else:
                for i, spec in enumerate(fields):
                    errors.extend(_validate_field_spec(spec, i))
            fm_extra = frontmatter.keys() - {"style", "fields"}
            if fm_extra:
                errors.append(f"frontmatter: unexpected key(s) {sorted(fm_extra)}")

    body = data.get("body")
    if body is not None:
        if not isinstance(body, list):
            errors.append("body must be a list or null")
        else:
            for i, spec in enumerate(body):
                errors.extend(_validate_body_spec(spec, i))

    if style == "whole_document" and body is not None:
        errors.append("whole_document frontmatter style requires body: null (no markdown body)")

    return errors


def load_template(path: str | Path) -> dict:
    """Read + parse + validate a template JSON file. Raises TemplateFormatError on defect."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    errors = validate_template(data)
    if errors:
        raise TemplateFormatError([f"{p}: {e}" for e in errors])
    return data


def templates_dir() -> Path:
    """Directory holding the extracted per-type template JSON files."""
    return Path(__file__).resolve().parent / "templates"


def available_template_types(directory: str | Path | None = None) -> list[str]:
    """Return the sorted ``doc_type`` values for every template staged on disk.

    This is a disk-enumeration convenience for tests/tooling — it is NOT the
    canonical type-enumeration function C3 authors (which reads the example-doctrine-repo manifest
    and unions in known supplements, then asserts set-equality against it per
    AC4). Do not substitute this for that check.
    """
    d = Path(directory) if directory is not None else templates_dir()
    types = []
    for f in sorted(d.glob("*.json")):
        data = load_template(f)
        types.append(data["doc_type"])
    return sorted(types)
