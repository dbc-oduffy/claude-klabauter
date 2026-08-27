"""
coordinator_core.cartography.chunk_table — tree -> caller-bucketed chunk table.

Purpose: the reduction DoE-claude's ``/coordinator:architecture-survey`` Phase-0.5
gate actually needs (cross-repo/inbox/2026-08-06-doe-claude-em-cartography-
chunk-table-producer-seam.md): repo tree -> filter to source files -> exclude
build/vendor/test artifacts -> bucket by CALLER-SUPPLIED system boundaries ->
slice into fixed-size chunks. On the 3831-tracked-file repo that prompted the
memo, this should reduce to ~201 source files across 9 caller-named systems —
not 384 sub-chunks bucketed by top-level directory name (the defect their
memo describes; that shape is ``cartography.file_index.system_for_path``'s
first-path-component rule, which this module deliberately does NOT reuse or
alter — see its own docstring's "deliberately coarser" framing).

The op owns the deterministic substrate (tracked-file enumeration, source
filtering, artifact exclusion, chunk slicing, ordering); the caller keeps the
semantic system boundaries (a caller-supplied ``{system_name: [path_prefix,
...]}`` map) — the split their memo explicitly asks for ("the op should own
the deterministic substrate; the caller should keep the semantic
boundaries").

Composition, not reinvention: tracked-file enumeration reuses
``coordinator_core.cartography.tree.list_tracked_files``; language labelling
reuses ``coordinator_core.cartography.tree._lang_for``; directory-name
exclusion reuses ``coordinator_core.cartography._skip_dirs.SKIP_DIR_NAMES``
(hoisted here from its former duplicated-literal homes). Only two things are
genuinely new: the source/build-test-artifact filter predicates below, and
the caller-boundary bucketing function (deliberately NOT
``cartography.file_index.system_for_path``, whose first-path-component rule
is documented as intentional and is not touched by this module).

Ordering discipline: copied verbatim from
``coordinator_core.ops.distill_scope`` (``compute_scope`` /
``write_scope_manifest``) — every list here is deterministically sorted
(never filesystem mtime), so two runs over an unmodified tree produce
byte-identical chunk tables.

Negative-spec:
  - Does NOT decide system boundaries — a caller who passes an empty/absent
    ``systems`` map gets every source file bucketed as "unbucketed"; this
    module never infers a boundary from directory structure (that would
    reproduce the exact 384-sub-chunks-by-top-level-directory defect the
    memo names).
  - Does NOT alter ``cartography.file_index.system_for_path`` or its
    first-path-component behavior — that module's coarseness is documented
    as deliberate and out of scope for this change.
  - Does NOT walk untracked files — inherits ``list_tracked_files``'s
    tracked-file boundary (mirrors a fresh clone/checkout).
  - Does NOT write to disk — pure computation; the disk-write leg lives in
    ``coordinator_core.ops.cartography_chunk_table`` (the DR-228 § D6
    scratch-tier writer).

Spec backlink: cross-repo/inbox/2026-08-06-doe-claude-em-cartography-chunk-table-producer-seam.md
Governing DR: docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D6
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coordinator_core.cartography._skip_dirs import SKIP_DIR_NAMES
from coordinator_core.cartography.tree import _lang_for, list_tracked_files

__all__ = [
    "SOURCE_LANGS",
    "TEST_DIR_NAMES",
    "is_source_file",
    "is_build_or_test_artifact",
    "bucket_by_boundaries",
    "chunk_list",
    "ChunkTableResult",
    "compute_chunk_table",
]

#: Languages counted as "source" for this reduction — deliberately narrower
#: than tree.py's full _EXTENSION_LANG map: prose/config/data languages
#: (markdown, yaml, json, toml, ini, html, css, text) and "unknown" carry no
#: architectural-survey signal and are excluded. The resulting reduction
#: ratio is a property of the repo, not a tuning target: this tree reduces
#: ~12490 -> ~1254, the memo's reduces ~3831 -> ~201. A caller needing a
#: different source definition should say so rather than have this set
#: widened toward a particular repo's number.
SOURCE_LANGS = frozenset(
    {
        "python",
        "javascript",
        "typescript",
        "shell",
        "rust",
        "go",
        "java",
        "c",
        "cpp",
        "csharp",
        "ruby",
        "php",
        "sql",
    }
)

#: Top-level-or-nested directory basenames that mark a path as a test
#: artifact regardless of language — distinct from SKIP_DIR_NAMES (vendor/
#: build/VCS noise) because test directories are legitimate tracked source
#: in many repos; excluding them is a chunk-table-specific reduction choice,
#: not a general walk-pruning rule (see this module's own docstring negative-
#: spec, and _skip_dirs.py's negative-spec, for why the two sets stay apart).
TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__", "spec"})

#: Filename patterns that mark a single file as a test artifact even when it
#: is not under a TEST_DIR_NAMES directory (e.g. a same-directory
#: ``test_foo.py`` colocated with its module, or a ``*.test.ts`` sibling
#: file — both real-world conventions this repo and DoE's own tree use).
_TEST_FILENAME_RE = re.compile(
    r"^(test_.*\.py|.*_test\.py|conftest\.py|.*\.(test|spec)\.(ts|tsx|js|jsx))$"
)


def is_source_file(relpath: str) -> bool:
    """Return True iff `relpath`'s extension maps to a language in SOURCE_LANGS.

    Delegates language labelling to ``cartography.tree._lang_for`` (single
    extension->language source of truth); this function only narrows that
    label set down to "counts as source for the chunk table."
    """
    return _lang_for(Path(relpath)) in SOURCE_LANGS


def is_build_or_test_artifact(relpath: str) -> bool:
    """Return True iff `relpath` is a build/vendor artifact or a test file.

    Two independent checks, either sufficient:
      (a) any path component matches SKIP_DIR_NAMES (vendor/build/VCS) or
          TEST_DIR_NAMES (test-framework directories) — a directory-name
          match anywhere in the path, not just at the top level, since a
          tracked file may be nested arbitrarily deep under e.g.
          ``coordinator_core/ops/tests/``.
      (b) the bare filename matches the test-file naming convention
          (``test_*.py``, ``*_test.py``, ``conftest.py``, ``*.test.*``,
          ``*.spec.*``) even when not under a TEST_DIR_NAMES directory.
    """
    normalized = relpath.replace("\\", "/")
    parts = normalized.split("/")
    excluded_dirs = SKIP_DIR_NAMES | TEST_DIR_NAMES
    if any(part in excluded_dirs for part in parts[:-1]):
        return True
    filename = parts[-1]
    return bool(_TEST_FILENAME_RE.match(filename))


def bucket_by_boundaries(relpath: str, systems: dict[str, list[str]]) -> str | None:
    """Return the system name whose path-prefix list contains a prefix of
    `relpath`, or None when no boundary matches ("unbucketed").

    `systems` is the caller-supplied ``{system_name: [path_prefix, ...]}``
    map (the load-bearing param DoE's memo asks for) — NOT
    ``cartography.file_index.system_for_path``'s first-path-component rule,
    which this function deliberately does not reuse (that rule is documented
    as intentionally coarse and is the exact shape the memo's defect
    describes: 384 sub-chunks bucketed by top-level directory name instead of
    caller-named systems).

    Longest-matching-prefix wins when a path matches more than one system's
    prefix list (deterministic tie-break: prefix length, then system name),
    so a caller may supply overlapping/nested boundaries (e.g. "ops" and
    "ops/fleet") without an undefined outcome.
    """
    normalized = relpath.replace("\\", "/")
    best_system: str | None = None
    best_len = -1
    for system, prefixes in systems.items():
        for prefix in prefixes:
            norm_prefix = prefix.replace("\\", "/").rstrip("/")
            if not norm_prefix:
                continue
            if normalized == norm_prefix or normalized.startswith(norm_prefix + "/"):
                plen = len(norm_prefix)
                if plen > best_len or (plen == best_len and (best_system is None or system < best_system)):
                    best_len = plen
                    best_system = system
    return best_system


def chunk_list(items: list[str], chunk_size: int) -> list[list[str]]:
    """Slice `items` (already deterministically ordered by the caller) into
    fixed-size chunks. Mirrors distill_scope.compute_scope's batching idiom
    verbatim (``items[i:i+chunk_size]`` over a `max(1, chunk_size)` step, so
    a caller-supplied 0/negative chunk_size never divides-by-zero or infinite-
    loops)."""
    step = max(1, chunk_size)
    return [items[i : i + step] for i in range(0, len(items), step)]


@dataclass(frozen=True)
class ChunkTableResult:
    """Result of compute_chunk_table: the fully-assembled chunk-table body
    (sans schema_version/run_id envelope, added by the op-tier writer) plus
    summary counts."""

    buckets: dict[str, dict[str, Any]]
    unbucketed: list[str]
    counts: dict[str, int]


def compute_chunk_table(
    target_root: str | Path,
    systems: dict[str, list[str]] | None,
    chunk_size: int = 25,
) -> ChunkTableResult:
    """Compute the full chunk-table reduction over `target_root`.

    Pipeline: list_tracked_files -> is_source_file -> not
    is_build_or_test_artifact -> bucket_by_boundaries(systems) ->
    chunk_list(chunk_size), each stage deterministically ordered (sorted
    tracked-file list in, sorted-by-construction out — no set() reordering
    at any stage after the initial sort).
    """
    systems = systems or {}
    tracked = list_tracked_files(target_root)

    source_files = [p for p in tracked if is_source_file(p) and not is_build_or_test_artifact(p)]

    bucketed: dict[str, list[str]] = {name: [] for name in systems}
    unbucketed: list[str] = []
    for relpath in source_files:
        system = bucket_by_boundaries(relpath, systems)
        if system is None:
            unbucketed.append(relpath)
        else:
            bucketed[system].append(relpath)

    buckets: dict[str, dict[str, Any]] = {}
    for system in sorted(bucketed):
        files = sorted(bucketed[system])
        buckets[system] = {
            "files": files,
            "chunks": chunk_list(files, chunk_size),
        }

    counts = {
        "tracked_total": len(tracked),
        "source_total": len(source_files),
        "bucketed_total": len(source_files) - len(unbucketed),
        "unbucketed_total": len(unbucketed),
        "system_count": len(systems),
    }

    return ChunkTableResult(buckets=buckets, unbucketed=sorted(unbucketed), counts=counts)
