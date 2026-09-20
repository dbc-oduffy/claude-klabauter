"""
coordinator_core.docindex.render

Emits a generated index's table into a delimited region inside an existing
document — a pure function of (document_text, IndexSpec, entries) -> document_text
(C2a). No file reads, no writes, no subprocess, no env reads: every value it
needs arrives pre-resolved by its caller (C3's ``docindex_emit.py`` op, or
``emit.py`` if the read/write seam grows large enough to warrant its own
module) — never here.

Spec backlink:
  claude-klabauter: docs/plans/2026-08-14-registry-indexes-are-emitted-from-their-directory.md,
                  plan-spine id C2a

SENTINEL FORMAT (AC12): the delimited region is bounded by an HTML-comment
sentinel pair — chosen because it survives markdown rendering and greps
cleanly:

  <!-- GENERATED docindex -->
  | Label1 | Label2 | ... |
  |---|---|...|
  | ... one row per entry ... |
  <!-- /GENERATED docindex sha256:<64 hex digits> -->

The header row and its ``|---|`` separator are emitted INSIDE the delimited
region, built from ``entry_fields:``'s declared ``{field, label}`` pairs —
never hand-authored outside it (Review: coordinator-staff-eng — F-2: with the
header outside the region, a field silently dropped from ``entry_fields:``
would produce a malformed table rather than a named AC17 failure). The closing
sentinel carries a sha256 digest of the emitted region's bytes (the text
strictly between the two sentinel lines, newline-terminated) — computed
OUTSIDE the digested span itself, so stamping the digest never changes the
digest. This is the provenance mechanism C2b's ``compare.py`` reads to
discriminate a hand-edit from ordinary drift (AC12).

Everything outside the delimited region is left byte-identical (AC5) —
``render`` never touches document text before the opening sentinel or after
the closing sentinel.

ENTRY TYPE CONTRACT: entries are ``Mapping[str, object]`` carrying each
declared field's RAW, pre-coercion frontmatter value (str, int, float, bool,
list, ``datetime.date``, or ``None``) — never a string every caller has
already stringified. This is required by the CELL-RENDERING CONTRACT below,
whose numeric-formatting and list-joining rules need to see the real Python
type; a value already coerced to ``str`` (as ``entry_kinds.py``'s two shipped
readers currently do, via ``coerce_to_string``, for their OWN string-only
return contract) has already lost that information. Reconciling the two
contracts — whether ``entry_kinds.py`` grows a raw-value return mode, or C3's
caller re-reads raw values another way — is explicitly left to the caller;
this module states its own input contract and does not silently adapt to the
narrower one.

CELL-RENDERING CONTRACT (Review: coordinator-staff-eng — F-3):
  - a literal ``|`` or newline inside a string value is escaped (``\\|``, a
    space) before insertion into a table cell — verbatim emission corrupts
    the table on the first real entry (live fixture: ``guards.md``'s
    ``entry_points: "2 harness-hook seams (\\`PreToolUse:Bash\\`,
    \\`PreToolUse:Write|Edit\\`) + per-guard \\`check()\\`"``).
  - a list-valued field is comma-joined; an empty list renders as the
    sentinel ``—`` (matching the current hand-written table's own
    convention) rather than an invented phrase (live fixture:
    ``cartography.md``'s ``depends_on: []``).
  - a numeric (``int``/``float``) value is rendered with a thousands
    separator (``41403`` -> ``41,403``) — presentation only, never applied to
    the entry's own carried value.

SORT ORDER (AC1): rows are sorted by the entry's identity field (the field
named first in ``entry_fields:``), compared as strings, so emission is
deterministic run-to-run and machine-to-machine — never left to
directory-listing order. AC1 also names an index's own declared
``entry_order:`` as an override when an index carries one; ``IndexSpec`` (C1,
``coordinator_core/docindex/spec.py``) does not currently expose such a field,
so there is nothing for this module to read yet — this module sorts by
identity field only, and the moment ``IndexSpec`` grows an ``entry_order``
attribute this docstring and the sort below need a matching follow-up change.

PRIOR ART (DR-221): ``coordinator_core/ops/docgen/render.py`` is the same
"engine renders mechanism, definitions consumed as data" shape, and its
purity invariant (no filesystem write, no subprocess, no env reads; every
value pre-resolved) is inherited here unchanged. Its rendering/template
machinery does NOT fit this module's shape and is not reused: docgen renders
a WHOLE document from a field spec (frontmatter fences + body blocks, no
pre-existing text to preserve); this module replaces one delimited region
INSIDE an already-existing document and must leave everything outside that
region byte-identical (AC5), which docgen's shape has no concept of.

Negative-spec:
  - No file reads, no file writes, no subprocess, no env reads (F10). Document
    text, IndexSpec, and entries all arrive as already-resolved arguments.
  - Does not validate ``IndexSpec`` or entry shape beyond what rendering
    itself requires (a missing declared field raises here, but exclusion
    (AC16) and missing-field detection (AC2) are the entry-kinds reader's job,
    not this module's).
  - Does not interpret or compare the closing-sentinel digest against
    anything on disk — computing and stamping it is all this module does;
    reading it back and discriminating hand-edit vs. drift is C2b's
    ``compare.py``.
"""
from __future__ import annotations

import datetime
import hashlib
import re
from typing import Mapping, Sequence

from coordinator_core.docindex.spec import EntryField, IndexSpec

__all__ = ["RenderError", "render"]

OPEN_SENTINEL = "<!-- GENERATED docindex -->"
_CLOSE_PREFIX = "<!-- /GENERATED docindex sha256:"
_CLOSE_SUFFIX = " -->"
_CLOSE_RE = re.compile(
    re.escape(_CLOSE_PREFIX) + r"[0-9a-f]{64}" + re.escape(_CLOSE_SUFFIX)
)

_EMPTY_LIST_SENTINEL = "\u2014"  # em dash, matching the hand-written table's own convention


class RenderError(ValueError):
    """Raised when rendering cannot proceed: no delimited region found in the
    document text, a malformed sentinel pair, or an entry missing a value for
    a field its index declares.
    """


def _escape_cell(text: str) -> str:
    """Escape a literal ``|`` and any newline before insertion into a table cell."""
    return text.replace("|", "\\|").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _render_cell_value(value: object) -> str:
    """Render one raw frontmatter value per the CELL-RENDERING CONTRACT."""
    if isinstance(value, bool):
        return _escape_cell(str(value))
    if isinstance(value, (int, float)):
        return f"{value:,}"
    if isinstance(value, (list, tuple)):
        if not value:
            return _EMPTY_LIST_SENTINEL
        return _escape_cell(", ".join(_render_list_item(item) for item in value))
    if isinstance(value, (datetime.date, datetime.datetime)):
        return _escape_cell(value.isoformat())
    if value is None:
        return _EMPTY_LIST_SENTINEL
    return _escape_cell(str(value))


def _render_list_item(item: object) -> str:
    if isinstance(item, (datetime.date, datetime.datetime)):
        return item.isoformat()
    return str(item)


def _identity_key(entry: Mapping[str, object], identity_field: str) -> str:
    return _render_list_item(entry.get(identity_field))


def _render_row(entry: Mapping[str, object], entry_fields: Sequence[EntryField]) -> str:
    cells = []
    for ef in entry_fields:
        if ef.field not in entry:
            raise RenderError(
                f"entry missing declared field {ef.field!r} at render time"
            )
        cells.append(_render_cell_value(entry[ef.field]))
    return "| " + " | ".join(cells) + " |"


def _render_region(spec: IndexSpec, entries: Sequence[Mapping[str, object]]) -> str:
    entry_fields = spec.entry_fields
    header = "| " + " | ".join(ef.label for ef in entry_fields) + " |"
    separator = "|" + "|".join("---" for _ in entry_fields) + "|"

    identity_field = entry_fields[0].field if entry_fields else None
    ordered_entries = (
        sorted(entries, key=lambda e: _identity_key(e, identity_field))
        if identity_field is not None
        else list(entries)
    )

    lines = [header, separator]
    lines.extend(_render_row(entry, entry_fields) for entry in ordered_entries)
    return "\n".join(lines) + "\n"


def render(
    document_text: str,
    spec: IndexSpec,
    entries: Sequence[Mapping[str, object]],
) -> str:
    """Replace the delimited region in ``document_text`` with a freshly
    emitted table built from ``spec.entry_fields`` and ``entries``.

    Pure: a function of (document_text, spec, entries) -> document_text.
    Raises ``RenderError`` when the document carries no recognizable
    sentinel pair, or when an entry lacks a value for a declared field.
    """
    open_idx = document_text.find(OPEN_SENTINEL)
    if open_idx == -1:
        raise RenderError(
            f"no delimited region found: missing opening sentinel {OPEN_SENTINEL!r}"
        )
    region_start = open_idx + len(OPEN_SENTINEL)
    if document_text[region_start : region_start + 1] == "\n":
        region_start += 1

    close_match = _CLOSE_RE.search(document_text, region_start)
    if close_match is None:
        raise RenderError(
            "no delimited region found: missing or malformed closing sentinel "
            f"(expected {_CLOSE_PREFIX}<64 hex digits>{_CLOSE_SUFFIX})"
        )

    region_text = _render_region(spec, entries)
    digest = hashlib.sha256(region_text.encode("utf-8")).hexdigest()
    close_sentinel = f"{_CLOSE_PREFIX}{digest}{_CLOSE_SUFFIX}"

    prefix = document_text[:open_idx] + OPEN_SENTINEL + "\n"
    suffix = document_text[close_match.end() :]
    return prefix + region_text + close_sentinel + suffix
