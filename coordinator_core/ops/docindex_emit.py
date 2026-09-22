"""
coordinator_core.ops.docindex_emit — JSON-RPC "docindex.emit" operation, plus
the naked-Python operator command that calls it (coordinator/bin/doc-index.py).

Purpose: the op and CLI seam over coordinator_core.docindex (spec/entry_kinds/
render/compare, C1/C2a/C2b) — the thin registration layer that makes a
self-declaring generated index (index_source_dir: + entry_kind: +
entry_fields: in its own frontmatter, AC3/AC4) resolvable and emittable by
name or discovered under a root, mirroring
coordinator_core/ops/cartography_file_index.py's registration pattern
(same op_scopes.py "none" row class, same docstring discipline).

Spec backlink:
  claude-klabauter: docs/plans/2026-08-14-registry-indexes-are-emitted-from-their-directory.md,
                  plan-spine id C3

Wire params ("docindex.emit"):
    target_root (str, required) — root of the tree to resolve/discover indexes
                                   under. Must resolve inside a git worktree
                                   (mirrors cartography.file_index's contract).
    index_path  (str, optional) — a single index document, repo-relative to
                                   target_root. When given, only that document
                                   is resolved (no discovery walk). When
                                   absent, every self-declared index under
                                   target_root is discovered (AC7: zero
                                   discovered indexes is a pass, not an error).
    write       (bool, optional, default False) — when False (the default),
                                   the op REPORTS what it would change and
                                   writes nothing (AC9's "reports by default
                                   and WRITES only when asked" — a dry-run
                                   report is first-class output, not a debug
                                   affordance; AC11 depends on it). When True,
                                   every resolved index whose comparison is
                                   ordinary drift (never a hand-edit) is
                                   rewritten on disk.

Reply fields:
    target_root (str)  — resolved target_root, echoed.
    results     (list) — one entry per resolved index:
        {index_path, index_source_dir, entry_kind, hand_edit (bool),
         added (list[str] identities), removed (list[str] identities),
         changed (list[{identity, field}]), written (bool)}
    refused     (list) — one entry per index refused outright, never compared:
        {index_path, reason} — AC13 (duplicate (index_source_dir, entry_kind)
        pair, naming both documents) or AC14 (index_source_dir resolves under
        an excluded root).

DISCOVERY (AC6's discovery clause, F4/F5/F7): enumerates tracked markdown via
ONE `git ls-files '*.md'` spawn (AC10/AC15 — bounded, never per-index or
per-entry), pruned through coordinator_core.cartography._skip_dirs.SKIP_DIR_NAMES
(imported, never respelled) plus state/, archive/, tasks/ before any
frontmatter read. Each survivor's LEADING FRONTMATTER BLOCK ONLY is parsed
(coordinator_core.frontmatter.primitives.split_frontmatter) for
`index_source_dir:` — never a text/grep scan of file contents, which would
self-poison on this plan's own output (docs/reference/generated-doc-indexes.md's
fenced example, and this plan file itself, both contain that token outside any
frontmatter block). A document whose parsed frontmatter carries
`index_source_dir:` is treated as a declared index and its full frontmatter is
handed to `docindex.spec.parse_index_spec`, which raises IndexSpecError (named,
loud) on any further malformed field — never a silent skip once a document has
declared itself an index.

Entry-file discovery, per resolved index, lists `*.md` children of
`index_source_dir` directly via `pathlib.Path.glob` — a single in-process
directory listing, no subprocess — matching the "docs/architecture/systems/
(23 pages)" shape this plan's worked example (C5) converts.

UNIQUENESS (AC13): before comparing/writing anything, every resolved index's
(index_source_dir, entry_kind) pair is checked for collision; a collision
refuses BOTH documents by name (see `refused` reply field) rather than
picking one arbitrarily. Two indexes over the SAME directory with a
DIFFERENT entry_kind stay legal.

EXCLUDED ROOTS (AC14, enforced invariant): an index whose index_source_dir
resolves under state/, archive/, tasks/, or a directory named in
SKIP_DIR_NAMES is refused by name — never emitted — closing the
archived-copy-inherits-index_source_dir hazard and (combined with AC13) the
template-copy-collides-with-its-source hazard.

RAW-VALUE RECONCILIATION (render.py's own "ENTRY TYPE CONTRACT" docstring,
explicitly left to the caller): `docindex.entry_kinds` readers return
coerced-to-string values (their own string-only contract, AC16's exclusion
comparison). `docindex.render`'s CELL-RENDERING CONTRACT needs the entry's
RAW, pre-coercion frontmatter value to apply thousands-separator / list-join /
empty-list-sentinel formatting correctly. This module is the caller render.py
names as owning that reconciliation: `_raw_entry_values` re-reads each entry
file's frontmatter directly (reusing the same `split_frontmatter` primitive
`entry_kinds.py` does) to recover the raw values for rendering, while
`entry_kinds.read_entry` is still called first and is still the sole
authority on AC2 missing-field detection, AC16 exclusion, and the
wiki-entry `## HEADING` identity fallback — `_raw_entry_values` trusts that
call's field set and only supplies raw typed values for fields it already
confirmed present (the identity-fallback case is exempted: its value is
already a plain string token, never a type render.py's cell contract needs
to reformat).

GENERATOR-PROVENANCE RATCHET (F3): this module writes to a path resolved
from each discovered index document at runtime — one artifact per
discovered index, never a fixed set knowable ahead of time — which is
exactly the shape `coordinator_core.ops.generator_provenance`'s own module
docstring names as the case `GENERATES` cannot express ("a corpus MUTATOR
that rewrites however many tracked files currently match a data-dependent
predicate ... rather than emitting a fixed set of artifacts"). A single
static `GENERATES` entry cannot name "the emitted region of whichever
tracked `*.md` file currently declares `index_source_dir:` in its own
frontmatter" as a fixed artifact/sources pair, so this module declares
`MUTATES` instead of `GENERATES` (module-level `MUTATES` below) — the
declared, healthy `Verdict.MUTATES_DECLARED` state, never `GENERATES = []`
(which would misstate this module as a non-emitter) and never a
`state/generator-provenance/unresolved-writers.json` baseline row (this
chunk's `writes:` deliberately does not include that file — see its
plan-spine row).

SPAWN DISCIPLINE (AC15): enumeration is the ONE `git ls-files '*.md'` call
named above. No other subprocess is spawned anywhere in this module — every
per-index and per-entry read is an in-process file read
(`Path.read_text`/`Path.write_text`), never `git show HEAD:<path>` in a loop.

Negative-spec:
  - Does not branch on entry_kind — dispatch is entirely
    coordinator_core.docindex.entry_kinds's registered-reader table (AC4).
  - Does not extend coordinator_core.cartography.churn's comparator
    (Anti-scope) — drift vocabulary is docindex.compare's own CompareResult.
  - Does not decide E1/E2 — this module has no whole-repo, HEAD-truth,
    git-spawning drift leg; that is C4b's, gated on the PM's E1 answer.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.cartography._guard import path_guard
from coordinator_core.cartography._skip_dirs import SKIP_DIR_NAMES
from coordinator_core.docindex.compare import compare
from coordinator_core.docindex.entry_kinds import read_entry
from coordinator_core.docindex.render import render
from coordinator_core.docindex.spec import IndexSpec, IndexSpecError, parse_index_spec
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ipc import register_op

#: Declared per DR-084-class provenance discipline (see module docstring
#: "GENERATOR-PROVENANCE RATCHET"): this module rewrites the emitted region
#: of whichever tracked markdown file currently self-declares
#: `index_source_dir:` in its own frontmatter — a data-dependent set, never
#: a fixed artifact GENERATES could name. `**/*.md` carries a literal file
#: extension (never the catch-all `*`/`**`/`**/*`/`*/*` shape) and a
#: wildcard metacharacter, matching the corpus-mutator convention this
#: repo's other MUTATES declarations already use.
MUTATES = ["**/*.md"]

_EXCLUDED_TOP_SEGMENTS = ("state", "archive", "tasks")


class DocindexEmitError(ValueError):
    """Raised when target_root is missing/invalid or `git ls-files` fails."""


def _list_tracked_markdown(repo_root: Path) -> list[str]:
    """One `git ls-files '*.md'` spawn (AC10/AC15) — the sole subprocess call
    anywhere in this module."""
    from coordinator_core.git.run import run_git

    result = run_git(["-C", str(repo_root), "ls-files", "*.md"])
    if result.returncode == 127:
        raise DocindexEmitError("git ls-files failed to spawn: git not found")
    if result.returncode != 0:
        raise DocindexEmitError(
            f"git ls-files exited {result.returncode}: {result.stderr.strip()}"
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _is_pruned(rel_path: str) -> bool:
    parts = Path(rel_path).parts[:-1]
    if any(part in SKIP_DIR_NAMES for part in parts):
        return True
    return bool(parts) and parts[0] in _EXCLUDED_TOP_SEGMENTS


def _read_leading_frontmatter(abs_path: Path) -> Optional[dict]:
    try:
        text = abs_path.read_text(encoding="utf-8")
    except OSError:
        return None
    split = split_frontmatter(text)
    if split is None:
        return None
    try:
        fm = yaml.safe_load(split.fm_text)
    except yaml.YAMLError:
        return None
    return fm if isinstance(fm, dict) else None


def _resolves_under_excluded_root(index_source_dir: str, repo_root: Path) -> bool:
    parts = Path(index_source_dir).parts
    if parts and parts[0] in _EXCLUDED_TOP_SEGMENTS:
        return True
    return any(part in SKIP_DIR_NAMES for part in parts)


def _discover_index_paths(repo_root: Path) -> list[str]:
    """Every tracked, pruned `*.md` path whose leading frontmatter carries
    `index_source_dir:` — the AC6 discovery clause. Text/grep scanning is
    never used (F7's self-poisoning hazard)."""
    candidates: list[str] = []
    for rel_path in _list_tracked_markdown(repo_root):
        if _is_pruned(rel_path):
            continue
        fm = _read_leading_frontmatter(repo_root / rel_path)
        if fm is not None and "index_source_dir" in fm:
            candidates.append(rel_path)
    return sorted(candidates)


def _parse_index(repo_root: Path, rel_path: str) -> IndexSpec:
    text = (repo_root / rel_path).read_text(encoding="utf-8")
    split = split_frontmatter(text)
    if split is None:
        raise IndexSpecError(f"{rel_path}: no frontmatter block found")
    return parse_index_spec(split.fm_text)


def _raw_entry_values(entry_path: Path, fields: set) -> dict:
    """Raw, pre-coercion frontmatter values for the given field names — the
    render.py "ENTRY TYPE CONTRACT" reconciliation this module owns (see
    module docstring)."""
    fm = _read_leading_frontmatter(entry_path) or {}
    return {k: v for k, v in fm.items() if k in fields}


def _resolve_one(repo_root: Path, rel_path: str) -> tuple[IndexSpec, dict]:
    """Return (spec, {"document_text", "entries"}) for one index document,
    with per-entry AC2/AC16 reads applied via entry_kinds.read_entry and raw
    values recovered for rendering (see "RAW-VALUE RECONCILIATION")."""
    spec = _parse_index(repo_root, rel_path)
    source_dir = repo_root / spec.index_source_dir
    entry_paths = sorted(source_dir.glob("*.md")) if source_dir.is_dir() else []

    entries = []
    for entry_path in entry_paths:
        coerced = read_entry(
            entry_path, spec.entry_kind, spec.entry_fields, spec.index_exclude_when
        )
        if coerced is None:  # AC16-excluded
            continue
        raw = _raw_entry_values(entry_path, set(coerced.keys()))
        merged = {k: raw.get(k, coerced[k]) for k in coerced}
        entries.append(merged)

    document_text = (repo_root / rel_path).read_text(encoding="utf-8")
    return spec, {"document_text": document_text, "entries": entries}


@register_op("docindex.emit")
def _docindex_emit(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "docindex.emit" handler — see module docstring for wire
    params and reply fields."""
    target_root = params.get("target_root")
    if not target_root:
        raise ValueError("docindex.emit requires param: target_root")

    write = bool(params.get("write", False))
    guarded_root = path_guard(target_root, ".")

    index_path = params.get("index_path")
    if index_path:
        rel_paths = [index_path]
    else:
        rel_paths = _discover_index_paths(guarded_root)

    # AC13: uniqueness on (index_source_dir, entry_kind), refusing both
    # documents by name on collision. Parse first so a malformed spec still
    # surfaces its own IndexSpecError rather than being swallowed here.
    specs: dict = {}
    refused: list[dict] = []
    for rel_path in rel_paths:
        spec = _parse_index(guarded_root, rel_path)
        specs[rel_path] = spec

    by_pair: dict = {}
    for rel_path, spec in specs.items():
        by_pair.setdefault((spec.index_source_dir, spec.entry_kind), []).append(rel_path)

    collided_paths: set = set()
    for pair, paths in by_pair.items():
        if len(paths) > 1:
            collided_paths.update(paths)
            for rel_path in paths:
                refused.append(
                    {
                        "index_path": rel_path,
                        "reason": (
                            f"duplicate (index_source_dir, entry_kind) pair {pair!r} "
                            f"also declared by: {sorted(set(paths) - {rel_path})}"
                        ),
                    }
                )

    results: list[dict] = []
    for rel_path, spec in specs.items():
        if rel_path in collided_paths:
            continue
        if _resolves_under_excluded_root(spec.index_source_dir, guarded_root):
            refused.append(
                {
                    "index_path": rel_path,
                    "reason": (
                        f"index_source_dir {spec.index_source_dir!r} resolves under "
                        "an excluded root (state/, archive/, tasks/, or a "
                        "SKIP_DIR_NAMES directory)"
                    ),
                }
            )
            continue

        _, resolved = _resolve_one(guarded_root, rel_path)
        document_text = resolved["document_text"]
        entries = resolved["entries"]

        cmp_result = compare(document_text, spec, entries)

        written = False
        if write and not cmp_result.hand_edit and cmp_result.has_drift:
            new_text = render(document_text, spec, entries)
            (guarded_root / rel_path).write_text(new_text, encoding="utf-8", newline="\n")
            written = True

        results.append(
            {
                "index_path": rel_path,
                "index_source_dir": spec.index_source_dir,
                "entry_kind": spec.entry_kind,
                "hand_edit": cmp_result.hand_edit,
                "added": [e.identity for e in cmp_result.added],
                "removed": [e.identity for e in cmp_result.removed],
                "changed": [
                    {"identity": c.identity, "field": c.field} for c in cmp_result.changed
                ],
                "written": written,
            }
        )

    return {"target_root": str(guarded_root), "results": results, "refused": refused}
