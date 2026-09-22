"""
coordinator_core.docindex.entry_kinds

The entry-kind REGISTRY (AC4: "a pluggable reader, not a branch") — a dict keyed
by kind name, populated via the ``@register_reader(kind)`` decorator, matching the
``register_op`` pattern already established in ``coordinator_core/ipc.py``. A
fourth document family is a new reader function plus a frontmatter key; no edit to
this module's callers, the renderer, or the comparator.

Spec backlink:
  claude-klabauter: docs/plans/2026-08-14-registry-indexes-are-emitted-from-their-directory.md,
                  plan-spine id C1

Public surface:
  register_reader(kind: str) -> decorator
    Registers a reader function under `kind` in the module-level registry.

  get_reader(kind: str) -> EntryReader
    Looks up a registered reader by kind name. Raises UnknownEntryKindError,
    naming the kind and the registered kinds, if `kind` is not registered.

  read_entry(file_path, entry_fields, index_exclude_when) -> dict[str, str] | None
    (via get_reader(spec.entry_kind)) A reader's contract: given an entry file and
    the index's entry_fields (ordered EntryField list), return an ORDERED FIELD
    MAPPING covering exactly those fields, in that order — never a fixed
    token+description pair. Returns None when the entry is excluded by AC16's
    index_exclude_when predicate (evaluated before the missing-field check, so
    exclusion is never itself a MissingEntryFieldError). Raises
    MissingEntryFieldError, naming the file and the field, when a declared field
    has no value and (for the identity field, wiki-entry only) no `## HEADING`
    fallback either.

  Two registered readers, ships-with (AC4):
    "wiki-entry": every declared field read from the entry file's own frontmatter;
      the identity field (entry_fields[0]) falls back to the entry file's first
      `## HEADING` token when the entry carries no frontmatter value for it.
    "plugin-manifest": every declared field read from frontmatter (name/description
      — the agents/commands shape). No heading fallback: a plugin manifest has no
      `## HEADING` convention to fall back to.

Negative-spec:
  - Both readers return the identical Entry shape (dict[str, str], insertion-ordered
    to match entry_fields) so the renderer never learns which kind produced a given
    entry (AC4).
  - Readers read entry FRONTMATTER only, never full bodies, beyond the minimal
    scan for the `## HEADING` line the wiki-entry identity fallback needs (AC10).
  - No subprocess is spawned by this module.
  - AC16 exclusion is a scalar, coerced-to-string equality check only — never
    list-membership, never a fresh date/type-aware comparison. It is read from the
    entry's own frontmatter like every other field: no inference, no hard-coded
    knowledge of any particular index.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Optional

from coordinator_core.docindex.spec import EntryField, coerce_to_string
from coordinator_core.frontmatter.primitives import split_frontmatter

import yaml

Entry = "dict[str, str]"
EntryReader = Callable[
    [Path, "tuple[EntryField, ...]", "Optional[Mapping[str, object]]"],
    "Optional[dict[str, str]]",
]

_READERS: "dict[str, EntryReader]" = {}


class UnknownEntryKindError(ValueError):
    """Raised when an index declares an `entry_kind` with no registered reader."""


class MissingEntryFieldError(ValueError):
    """Raised when an entry file lacks a value for a field its index declares.

    Always names the file and the missing field — never a placeholder, never a
    silent omission (AC2).
    """


def register_reader(kind: str) -> Callable[[EntryReader], EntryReader]:
    """Register `fn` as the reader for `kind` in the module-level registry."""

    def _decorator(fn: EntryReader) -> EntryReader:
        _READERS[kind] = fn
        return fn

    return _decorator


def get_reader(kind: str) -> EntryReader:
    reader = _READERS.get(kind)
    if reader is None:
        known = ", ".join(sorted(_READERS)) or "(none registered)"
        raise UnknownEntryKindError(
            f"unknown entry_kind: {kind!r}; registered kinds: {known}"
        )
    return reader


def read_entry(
    file_path: Path,
    entry_kind: str,
    entry_fields: "tuple[EntryField, ...]",
    index_exclude_when: Optional[Mapping[str, object]],
) -> Optional["dict[str, str]"]:
    """Resolve `entry_kind`'s reader and apply it to `file_path`."""
    reader = get_reader(entry_kind)
    return reader(file_path, entry_fields, index_exclude_when)


def _load_entry_frontmatter(file_path: Path) -> tuple[dict, str]:
    """Read `file_path` and return (parsed frontmatter dict, body text).

    Frontmatter parsing is delegated to the existing
    ``coordinator_core.frontmatter.primitives.split_frontmatter`` parser rather
    than a fresh regex, per the plan body.
    """
    text = file_path.read_text(encoding="utf-8")
    split = split_frontmatter(text)
    if split is None:
        raise MissingEntryFieldError(
            f"{file_path}: no frontmatter block found"
        )
    try:
        fm = yaml.safe_load(split.fm_text)
    except yaml.YAMLError as e:
        raise MissingEntryFieldError(f"{file_path}: frontmatter is not valid YAML: {e}") from e
    if not isinstance(fm, dict):
        fm = {}
    return fm, split.body_with_leading_newline


def _first_heading(body: str) -> Optional[str]:
    """Return the token following the first `## HEADING` line in `body`, if any."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            return stripped[3:].strip()
    return None


def _is_excluded(fm: dict, index_exclude_when: Optional[Mapping[str, object]]) -> bool:
    if not index_exclude_when:
        return False
    for key, expected in index_exclude_when.items():
        if key not in fm:
            return False
        if coerce_to_string(fm[key]) != coerce_to_string(expected):
            return False
    return True


@register_reader("wiki-entry")
def _read_wiki_entry(
    file_path: Path,
    entry_fields: "tuple[EntryField, ...]",
    index_exclude_when: Optional[Mapping[str, object]],
) -> Optional["dict[str, str]"]:
    fm, body = _load_entry_frontmatter(file_path)

    if _is_excluded(fm, index_exclude_when):
        return None

    identity_field = entry_fields[0].field if entry_fields else None
    result: "dict[str, str]" = {}
    for i, ef in enumerate(entry_fields):
        if ef.field in fm and fm[ef.field] is not None:
            result[ef.field] = coerce_to_string(fm[ef.field])
            continue
        if i == 0 and ef.field == identity_field:
            heading = _first_heading(body)
            if heading is not None:
                result[ef.field] = heading
                continue
        raise MissingEntryFieldError(
            f"{file_path}: missing declared field {ef.field!r} "
            f"(entry_kind=wiki-entry)"
        )
    return result


@register_reader("plugin-manifest")
def _read_plugin_manifest(
    file_path: Path,
    entry_fields: "tuple[EntryField, ...]",
    index_exclude_when: Optional[Mapping[str, object]],
) -> Optional["dict[str, str]"]:
    fm, _body = _load_entry_frontmatter(file_path)

    if _is_excluded(fm, index_exclude_when):
        return None

    result: "dict[str, str]" = {}
    for ef in entry_fields:
        if ef.field in fm and fm[ef.field] is not None:
            result[ef.field] = coerce_to_string(fm[ef.field])
            continue
        raise MissingEntryFieldError(
            f"{file_path}: missing declared field {ef.field!r} "
            f"(entry_kind=plugin-manifest)"
        )
    return result
