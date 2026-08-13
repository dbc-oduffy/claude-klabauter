"""coordinator_core.ops.docgen.render — (doc_type, resolved_field_values) -> str (C4).

Purpose: the internal module function where the consume-as-data contract is
actually proven — a PURE render, mirroring how ``coordinator-doc-new`` resolves
every value (minting, git, path resolution) in ``main()`` *before* calling a
builder. This module is NOT an IPC op — no ``@register_op``, no ``OpClass``
entry, no ``_OP_KEY_SCOPE`` entry, no ``OP_MODULE_MAP`` entry (see the package
docstring). It performs NO filesystem write and NO subprocess: the only I/O is
reading the already-validated template JSON files under ``templates/`` via
``template_format.load_template`` — read-before-write is structural here, not
conventional (AC6; C7 asserts this formally as a standing test).

Consumes C2's frozen ``docgen-template/v1`` format (``template_format.py``) —
this module interprets the format, it does not define or validate it. Given a
``doc_type`` and a flat ``resolved_field_values`` mapping (already-computed
strings/lists — the SAME values a caller would have passed into a
``coordinator-doc-new`` builder), it walks the template's ``frontmatter.fields``
and ``body`` blocks in order and renders exactly what the shape prescribes:

  - ``literal`` / ``raw`` lines: emitted verbatim, with ``str.format``-style
    ``{field}`` substitution against ``resolved_field_values`` (a template
    author double-braces a literal brace, e.g. ``{{label, date, ...}}``, when
    the line is YAML-comment prose rather than a real placeholder — see
    ``strategic_self_description.json``).
  - ``value``: always emitted, quoted per the field spec's ``quote``. Missing
    from ``resolved_field_values`` is a caller defect (``RenderError``), not a
    presence branch — this kind has none.
  - ``present_as_null``: always emitted; the resolved value when truthy,
    else ``absent_literal`` (default ``"null"``, unquoted token).
  - ``optional_omit``: the whole line only when the resolved value is
    truthy; else the ``absent_comment`` line if the spec carries one, else
    nothing at all — matching the oracle's ``if x: lines.append(...)``
    shape line-for-line (e.g. ``completion``'s ``chain``).
  - ``list_emit_if_present``: a ``key:`` header plus one ``  - <item>`` line
    per element, but only when the resolved list is non-empty.
  - ``value_or_literal_fallback``: ``{key}: {value}{suffix}`` when the resolved
    value is truthy (identical shape to ``value``); else the fixed
    ``fallback_line`` verbatim, no substitution — a different key entirely, not
    this field's key with a placeholder value (contrast ``present_as_null``).

Quoting mirrors the oracle's ``_yaml_quote`` (``bin/lib/memo_compose.py``)
byte-for-byte: always double-quote, escaping ``\\``, ``"``, ``\\n``, ``\\r``,
``\\t`` — a template with ``quote: true`` calls this, never Python's own
``repr``/``json.dumps`` quoting, which escape differently.

Two frontmatter shapes bookend the field grammar (mirrors ``template_format``):
``frontmatter: null`` emits no fences and no fields at all (body-only
artifacts — no currently-extracted type uses this shape as of the 2026-07-24
review-findings schema unification, which made that type frontmatter-bearing;
the idiom remains structurally supported, see ``test_doc_render.py``'s
synthetic-template coverage); ``style: "whole_document"`` emits
fields with no ``---`` fences and requires ``body: null`` (structurally
guaranteed by C2's validator); ``style: "fenced"`` wraps the fields in a
``---``/``---`` pair followed by the body.

Negative-spec: this module does not mint IDs, does not call ``_current_branch``
or any subprocess, does not read env vars, and does not write to disk — every
value it touches arrives pre-resolved in ``resolved_field_values``. Determinism
normalization (the closed volatile-field set: ``deliverable_id``/``branch``
injection, ``created``/``dispatched_at`` redaction) is C5's surface, and
byte-identity conformance against the live oracle is C6's — this module is
tested here only for structural/idiom correctness (``test_doc_render.py``).

Spec backlink: pln-strang-12-document-generation--75a7eb § C4 (AC6)
Oracle: /coordinator/bin/coordinator-doc-new (coordinator-claude clone) — every ``_scaffold_*`` fn's
own value-resolution-then-builder-call shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from . import template_format

__all__ = ["RenderError", "render_document"]


class RenderError(ValueError):
    """Raised when a render cannot proceed: unknown doc_type, or a required
    ``value``-kind field missing from ``resolved_field_values``, or a
    ``literal``/``raw`` placeholder with no matching resolved value.
    """


def _yaml_quote(value: Any) -> str:
    """Double-quote a value for YAML, escaping backslashes/quotes/whitespace.

    Byte-for-byte mirror of the oracle's ``bin/lib/memo_compose._yaml_quote`` —
    always double-quotes, never bare. Kept local (not imported cross-repo) since
    this module must not depend on the coordinator-claude clone at render time; C6's harness is
    the only surface permitted to resolve into the coordinator-claude tree.
    """
    s = str(value)
    escaped = (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _format_line(line: str, values: Mapping[str, Any]) -> str:
    try:
        return line.format(**values)
    except KeyError as exc:
        raise RenderError(
            f"missing resolved field value for placeholder {exc} in line {line!r}"
        ) from exc


def _render_value(spec: dict, values: Mapping[str, Any]) -> str:
    field = spec["field"]
    if field not in values:
        raise RenderError(
            f"missing required resolved value for field {field!r} (key {spec['key']!r})"
        )
    rendered = _yaml_quote(values[field]) if spec["quote"] else str(values[field])
    return f"{spec['key']}: {rendered}{spec.get('suffix', '')}"


def _render_keyed_value(spec: dict, value: Any) -> str:
    """Shared truthy-branch shape: quote-or-str, then ``key: rendered+suffix``.

    Review: code-reviewer — this line was byte-identical across
    ``_render_present_as_null``, ``_render_optional_omit``, and
    ``_render_value_or_literal_fallback``; each idiom's distinct behavior lives
    entirely in its ABSENT branch, which stays separate per caller below.
    """
    rendered = _yaml_quote(value) if spec["quote"] else str(value)
    return f"{spec['key']}: {rendered}{spec.get('suffix', '')}"


def _render_present_as_null(spec: dict, values: Mapping[str, Any]) -> str:
    value = values.get(spec["field"])
    if value:
        return _render_keyed_value(spec, value)
    rendered = spec.get("absent_literal", "null")
    return f"{spec['key']}: {rendered}{spec.get('suffix', '')}"


def _render_optional_omit(spec: dict, values: Mapping[str, Any]) -> str | None:
    value = values.get(spec["field"])
    if value:
        return _render_keyed_value(spec, value)
    return spec.get("absent_comment")


def _render_value_or_literal_fallback(spec: dict, values: Mapping[str, Any]) -> str:
    value = values.get(spec["field"])
    if value:
        return _render_keyed_value(spec, value)
    return spec["fallback_line"]


def _render_list_emit_if_present(spec: dict, values: Mapping[str, Any]) -> list[str]:
    items = values.get(spec["field"])
    if not items:
        return []
    quote = spec["quote"]
    lines = [f"{spec['key']}:"]
    lines.extend(f"  - {_yaml_quote(item) if quote else str(item)}" for item in items)
    return lines


# Uniform dispatch over all 6 sanctioned field kinds (Review: code-reviewer —
# was a 2-kind lookup table plus 3 inline elif branches with no functional
# reason for the split; every kind now returns list[str] via one of these
# small wrappers, so the table is a complete, symmetric map).
def _dispatch_literal(spec: dict, values: Mapping[str, Any]) -> list[str]:
    return [_format_line(spec["line"], values)]


def _dispatch_value(spec: dict, values: Mapping[str, Any]) -> list[str]:
    return [_render_value(spec, values)]


def _dispatch_present_as_null(spec: dict, values: Mapping[str, Any]) -> list[str]:
    return [_render_present_as_null(spec, values)]


def _dispatch_optional_omit(spec: dict, values: Mapping[str, Any]) -> list[str]:
    line = _render_optional_omit(spec, values)
    return [line] if line is not None else []


def _dispatch_list_emit_if_present(spec: dict, values: Mapping[str, Any]) -> list[str]:
    return _render_list_emit_if_present(spec, values)


def _dispatch_value_or_literal_fallback(spec: dict, values: Mapping[str, Any]) -> list[str]:
    return [_render_value_or_literal_fallback(spec, values)]


_FIELD_RENDERERS = {
    "literal": _dispatch_literal,
    "value": _dispatch_value,
    "present_as_null": _dispatch_present_as_null,
    "optional_omit": _dispatch_optional_omit,
    "list_emit_if_present": _dispatch_list_emit_if_present,
    "value_or_literal_fallback": _dispatch_value_or_literal_fallback,
}


def _render_fields(fields: list[dict], values: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for spec in fields:
        kind = spec["kind"]
        renderer = _FIELD_RENDERERS.get(kind)
        if renderer is None:  # pragma: no cover — template_format.validate_template already rejects this
            raise RenderError(f"unsupported field kind {kind!r}")
        out.extend(renderer(spec, values))
    return out


def _render_body(body: list[dict], values: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for block in body:
        out.extend(_format_line(line, values) for line in block["lines"])
    return out


def render_template(template: dict, resolved_field_values: Mapping[str, Any]) -> str:
    """Render an already-loaded (and thus already-validated) template dict.

    Split out from ``render_document`` so C6's conformance harness (and tests)
    can render against an in-memory template without a disk round-trip.
    """
    values = dict(resolved_field_values)
    lines: list[str] = []
    frontmatter = template.get("frontmatter")
    style = frontmatter.get("style") if frontmatter is not None else None

    if style == "fenced":
        lines.append("---")
    if frontmatter is not None:
        lines.extend(_render_fields(frontmatter["fields"], values))
    if style == "fenced":
        lines.append("---")

    body = template.get("body")
    if body is not None:
        lines.extend(_render_body(body, values))

    return "\n".join(lines)


def _template_index(directory: str | Path | None) -> dict[str, dict]:
    d = Path(directory) if directory is not None else template_format.templates_dir()
    index: dict[str, dict] = {}
    sources: dict[str, Path] = {}
    for f in sorted(d.glob("*.json")):
        data = template_format.load_template(f)
        doc_type = data["doc_type"]
        if doc_type in index:
            # Review: code-reviewer — was silent last-wins (whichever file sorts
            # last alphabetically won with zero signal); fail loud instead, since
            # this is the render path's own runtime surface, not just a
            # test-module cross-check.
            raise RenderError(
                f"duplicate doc_type {doc_type!r} declared by both "
                f"{sources[doc_type]!r} and {f!r}"
            )
        index[doc_type] = data
        sources[doc_type] = f
    return index


def render_document(
    doc_type: str,
    resolved_field_values: Mapping[str, Any],
    *,
    templates_directory: str | Path | None = None,
) -> str:
    """Render ``doc_type`` against ``resolved_field_values``. Pure; no I/O beyond
    reading the template JSON (no write, no subprocess — AC6).

    ``resolved_field_values`` is the SAME flat mapping of already-computed
    strings/lists a caller would hand a ``coordinator-doc-new`` builder — this
    function does no minting, no git, no env reads, no path resolution.
    """
    index = _template_index(templates_directory)
    template = index.get(doc_type)
    if template is None:
        raise RenderError(
            f"no template found for doc_type {doc_type!r}; known types: {sorted(index)}"
        )
    return render_template(template, resolved_field_values)
