"""
coordinator_core.docindex.spec

Parses a generated index document's own frontmatter into an ``IndexSpec`` — the
self-declaring contract (AC3) that lets `docindex.emit` regenerate an index without
any central registry-of-registries.

Spec backlink:
  claude-klabauter: docs/plans/2026-08-14-registry-indexes-are-emitted-from-their-directory.md,
                  plan-spine id C1

Public surface:
  IndexSpec(index_source_dir, entry_kind, entry_fields, index_exclude_when)
    entry_fields: an ORDERED tuple of EntryField(field, label) pairs — the emitted
      column order AND the emitted column header text, declared explicitly in the
      index document's own frontmatter rather than derived from the field name
      (AC4; a naive `field.replace("_", " ").title()` silently renames real columns,
      which is never attempted here).
    index_exclude_when: optional mapping of {field: value} naming entries this index
      does not cover (AC16). Comparison is coerced-to-string equality, evaluated by
      the entry_kinds reader, not here.

  parse_index_spec(fm_text: str) -> IndexSpec
    Parses an index document's raw frontmatter text (as split by
    coordinator_core.frontmatter.primitives.split_frontmatter) into an IndexSpec.
    Raises IndexSpecError naming the missing/malformed field on any of:
    unknown `entry_kind`, missing `index_source_dir`, missing or empty
    `entry_fields`, or a malformed `index_exclude_when` predicate.

  coerce_to_string(value: object) -> str
    The AC16 comparison primitive: coerces a YAML-parsed scalar (str, bool, int,
    float, datetime.date, None) to its string form for exclusion-predicate matching.
    Shared here so spec parsing and entry_kinds' exclusion check use one coercion,
    never two silently-different ones.

Negative-spec:
  - This module reads only frontmatter TEXT already split from the document body —
    it does not read files or call split_frontmatter itself. AC10's per-file I/O
    budget is the caller's (docindex.discovery / docindex.emit) to hold.
  - `entry_kind` validity against the entry_kinds registry is NOT checked here —
    spec.py has no dependency on entry_kinds.py, to keep the reader-registration
    seam one-directional (entry_kinds imports spec's IndexSpec, never the reverse).
    Unknown-kind is checked by the caller that resolves a reader for the kind.
  - `generated_from` is deliberately never read or written — that key already means
    a different fact (`coordinator_core/ops/staleness_git.py`,
    `check_generator_output_staleness.py`: the source commit an artifact was emitted
    at). This module's provenance vocabulary is `index_source_dir`/`entry_kind`.
"""
from __future__ import annotations

import datetime
from typing import Mapping, NamedTuple, Optional

import yaml


class IndexSpecError(ValueError):
    """Raised when an index document's frontmatter is missing or malformed.

    The message always names the offending field so a failure is loud and
    actionable, never a silent skip (per the plan body's Unknown kind / missing
    directory / missing-or-empty entry_fields / malformed exclusion predicate list).
    """


class EntryField(NamedTuple):
    """One declared column: the frontmatter key to read, and its emitted header."""

    field: str
    label: str


class IndexSpec(NamedTuple):
    """The self-declaring contract read from an index document's own frontmatter."""

    index_source_dir: str
    entry_kind: str
    entry_fields: tuple[EntryField, ...]
    index_exclude_when: Optional[Mapping[str, object]]


def coerce_to_string(value: object) -> str:
    """Coerce a YAML-parsed scalar to its string form for AC16 predicate matching.

    A scalar match only — never list-membership, never a fresh date/type-aware
    comparison. ``None`` coerces to the literal string ``"None"`` only to keep the
    function total; callers never rely on that case being meaningful.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value)


def _require_str(fm: dict, key: str) -> str:
    v = fm.get(key)
    if not isinstance(v, str) or not v.strip():
        raise IndexSpecError(f"index frontmatter missing required field: {key!r}")
    return v


def parse_index_spec(fm_text: str) -> IndexSpec:
    """Parse an index document's frontmatter text into an IndexSpec.

    ``fm_text`` is the raw text between the ``---`` delimiters, as produced by
    ``coordinator_core.frontmatter.primitives.split_frontmatter(...).fm_text`` —
    reusing that existing parser is what this module and its callers standardise
    on, per the plan body's "reuse the existing frontmatter parser" instruction.
    """
    try:
        fm = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        raise IndexSpecError(f"index frontmatter is not valid YAML: {e}") from e
    if not isinstance(fm, dict):
        raise IndexSpecError("index frontmatter did not parse to a mapping")

    index_source_dir = _require_str(fm, "index_source_dir")
    entry_kind = _require_str(fm, "entry_kind")

    raw_fields = fm.get("entry_fields")
    if not isinstance(raw_fields, list) or len(raw_fields) == 0:
        raise IndexSpecError(
            "index frontmatter missing or empty required field: 'entry_fields'"
        )
    entry_fields: list[EntryField] = []
    for i, item in enumerate(raw_fields):
        if not isinstance(item, dict) or "field" not in item or "label" not in item:
            raise IndexSpecError(
                f"entry_fields[{i}] must be a mapping with 'field' and 'label' keys, "
                f"got: {item!r}"
            )
        field = item["field"]
        label = item["label"]
        if not isinstance(field, str) or not field:
            raise IndexSpecError(f"entry_fields[{i}].field must be a non-empty string")
        if not isinstance(label, str) or not label:
            raise IndexSpecError(f"entry_fields[{i}].label must be a non-empty string")
        entry_fields.append(EntryField(field=field, label=label))

    index_exclude_when: Optional[Mapping[str, object]] = None
    if "index_exclude_when" in fm:
        raw_exclude = fm["index_exclude_when"]
        if not isinstance(raw_exclude, dict) or not raw_exclude:
            raise IndexSpecError(
                "index_exclude_when, when present, must be a non-empty mapping of "
                f"field to value, got: {raw_exclude!r}"
            )
        for k in raw_exclude:
            if not isinstance(k, str):
                raise IndexSpecError(
                    f"index_exclude_when key must be a string, got: {k!r}"
                )
        index_exclude_when = raw_exclude

    return IndexSpec(
        index_source_dir=index_source_dir,
        entry_kind=entry_kind,
        entry_fields=tuple(entry_fields),
        index_exclude_when=index_exclude_when,
    )
