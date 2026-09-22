"""
coordinator_core.docindex

Self-declaring generated index documents: an index document carries its own
`index_source_dir` / `entry_kind` / `entry_fields` frontmatter (AC3/AC4), so
`docindex.emit` regenerates it deterministically from its source directory's
entry frontmatter with no invented or silently dropped field (AC1/AC2/AC17).

Spec backlink:
  claude-klabauter: docs/plans/2026-08-14-registry-indexes-are-emitted-from-their-directory.md,
                  plan-spine id C1 (this module: spec.py + entry_kinds.py)

This C1 slice exposes the index-spec parser and the entry-kind reader registry.
Render/compare/emit (C2a/C2b/C3+) land in later plan-spine chunks and are not
part of this module's public surface yet.
"""
from coordinator_core.docindex.spec import (
    EntryField,
    IndexSpec,
    IndexSpecError,
    coerce_to_string,
    parse_index_spec,
)
from coordinator_core.docindex.entry_kinds import (
    MissingEntryFieldError,
    UnknownEntryKindError,
    get_reader,
    read_entry,
    register_reader,
)

__all__ = [
    "EntryField",
    "IndexSpec",
    "IndexSpecError",
    "coerce_to_string",
    "parse_index_spec",
    "MissingEntryFieldError",
    "UnknownEntryKindError",
    "get_reader",
    "read_entry",
    "register_reader",
]
