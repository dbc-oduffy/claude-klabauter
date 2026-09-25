"""
HEAD-truth whole-repo drift leg (C4b, AC6/AC7/AC10/AC15) for
coordinator_core.docindex — "the check nobody has to remember to run".

Spec backlink:
  claude-klabauter: docs/plans/2026-08-14-registry-indexes-are-emitted-from-their-directory.md,
                  plan-spine id C4b

TIER (E1, resolved as cadence): `spawns_process` + `cadence`, module-level —
reading HEAD needs git, so this leg runs at cadence gates; the zero-spawn,
unmarked fast-tier leg is AC6b.

WHAT THIS ASSERTS (AC6): of the self-declaring index documents discovered
among TRACKED markdown (same frontmatter-parsed discovery as C4a — never a
text/grep scan, per Review coordinator-staff-eng F-7), re-renders each from
its source directory's entry frontmatter and asserts the recorded region
matches — failing by naming the directory and the specific disagreeing
entry. Zero discovered indexes is a PASS (AC7), which is the state at HEAD
until C5 converts `docs/architecture/systems-index.md`.

ASSERT AGAINST COMMITTED CONTENT, NEVER THE WORKING TREE (F6): every byte
this test reasons about — which documents exist, what they declare, what
each entry file says — comes from `HEAD`, read via one batched
`git cat-file --batch` feed (AC15), never `Path.read_text()` on a live file.
When the index document itself, or ANY entry file its source directory
covers, is DIRTY in the working tree, this test REPORTS AND SKIPS that whole
index (never asserts against uncommitted content, never partially compares
around the dirty entry) — see PRIOR ART below for why per-index granularity,
not per-entry, is what actually closes DR-227's shape.

BOUNDED SPAWNS (AC15): exactly three `git` processes, independent of index
and entry count —
  1. one `git ls-files '*.md'` (enumeration),
  2. one `git status --porcelain` (the whole-tree dirty set), and
  3. one batched `git cat-file --batch` (every survivor's HEAD blob).
`git show HEAD:<path>` in a per-item loop is forbidden by
`test_no_unbatched_per_item_git_spawn.py`'s standing leg and by Anti-scope;
the fix is the batch above, never a `_KNOWN_SITES` entry.

PRIOR ART (DR-227,
`docs/decisions/DR-227-whole-tree-dirty-classifier-redundant-under-explicit-path-scoping.md`),
CORRECTED: an earlier draft argued this walk was safe because it is "a
static markdown frontmatter glob, not a git-status walk", mistaking
mechanism for cause. DR-227's actual root cause was reading uncommitted
concurrent-peer state and treating it as a fact about the repo — a markdown
glob over the LIVE working tree does exactly that: a peer mid-edit adding
frontmatter fields to a few system pages would turn every other concurrent
session's run red on files that session never touched, and index-vs-entry
disagreement mid-edit is *correct*, not drift. Reading HEAD and skipping
dirty index/entry sets by name — never the walk's static-glob genus — is
what actually closes the DR-227 shape under the 50-70-concurrent-LLM load
norm (`docs/wiki/machine-load-norm.md`).

Negative-spec:
  - No module-level spawn (Rule 1) — every subprocess call lives inside a
    `test_*` function body; the repo root is resolved via
    `Path(__file__).resolve().parents[N]`, never a spawned `git rev-parse`.
  - Discovery parses each survivor's LEADING FRONTMATTER BLOCK ONLY for
    `index_source_dir:` — never a text/grep scan of file contents.
  - Never `git show HEAD:<path>` per file — one batched `cat-file --batch`
    feed for every needed path (index docs AND entry files) together.
  - Duplicates (adapted to already-read HEAD text rather than a live
    filesystem path) the entry-reading shape `coordinator_core.docindex.
    entry_kinds`'s two registered readers and `coordinator_core.ops.
    docindex_emit._raw_entry_values` already establish for the working-tree
    case — this module cannot call those directly, since both take a
    `pathlib.Path` and unconditionally `Path.read_text()` it, which is
    exactly the live-filesystem read F6 forbids here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Sequence

import pytest
import yaml

from coordinator_core.cartography._skip_dirs import SKIP_DIR_NAMES
from coordinator_core.docindex.compare import compare
from coordinator_core.docindex.spec import EntryField, IndexSpec, IndexSpecError, parse_index_spec
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.git.run import run_git
from coordinator_core.ops.ceremony.git_native import cat_file_batch

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

# coordinator_core/docindex/tests/test_index_drift.py -> parents[3] is the
# repo root ([0]=tests, [1]=docindex, [2]=coordinator_core, [3]=repo root),
# matching C4a's own convention in this same directory.
_REPO_ROOT = Path(__file__).resolve().parents[3]

_EXCLUDED_TOP_SEGMENTS = ("state", "archive", "tasks")


class DriftCheckSpawnError(RuntimeError):
    """Raised when a required `git` spawn fails outright (never silently
    folded into an empty/false result, which would read a git failure as
    "nothing is dirty" or "nothing is tracked")."""


def _is_pruned(rel_path: str) -> bool:
    parts = Path(rel_path).parts[:-1]
    if any(part in SKIP_DIR_NAMES for part in parts):
        return True
    return bool(parts) and parts[0] in _EXCLUDED_TOP_SEGMENTS


def _list_tracked_markdown() -> list[str]:
    """Spawn 1/3: `git ls-files '*.md'`, pruned through SKIP_DIR_NAMES plus
    state/archive/tasks — matching C4a/C3's exclusion convention."""
    result = run_git(["ls-files", "*.md"], cwd=str(_REPO_ROOT))
    if not result.ok:
        raise DriftCheckSpawnError(
            f"git ls-files '*.md' failed (rc={result.returncode}): {result.stderr}"
        )
    survivors = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return sorted(p for p in survivors if not _is_pruned(p))


def _dirty_relpaths() -> set[str]:
    """Spawn 2/3: one whole-tree `git status --porcelain` call, parsed into
    the set of repo-relative paths carrying any uncommitted worktree or
    index change (staged or unstaged) — either side of a rename record
    counts as dirty for both paths, matching `dirty_relpaths_from_porcelain`'s
    own contract in `ops/ceremony/git_native.py`."""
    result = run_git(["--no-optional-locks", "status", "--porcelain"], cwd=str(_REPO_ROOT))
    if not result.ok:
        raise DriftCheckSpawnError(
            f"git status --porcelain failed (rc={result.returncode}): {result.stderr}"
        )
    dirty: set[str] = set()
    for line in result.stdout.splitlines():
        if len(line) < 4 or line[2] != " ":
            continue
        rest = line[3:]
        if " -> " in rest:
            old_path, new_path = rest.split(" -> ", 1)
            dirty.add(_unquote(old_path))
            dirty.add(_unquote(new_path))
        else:
            dirty.add(_unquote(rest))
    return dirty


def _unquote(path: str) -> str:
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        return path[1:-1].encode().decode("unicode_escape")
    return path


def _frontmatter(text: str) -> Optional[dict]:
    split = split_frontmatter(text)
    if split is None:
        return None
    try:
        fm = yaml.safe_load(split.fm_text)
    except yaml.YAMLError:
        return None
    return fm if isinstance(fm, dict) else None


def _first_heading(body: str) -> Optional[str]:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            return stripped[3:].strip()
    return None


def _is_excluded(fm: dict, index_exclude_when: Optional[Mapping[str, object]]) -> bool:
    from coordinator_core.docindex.spec import coerce_to_string

    if not index_exclude_when:
        return False
    for key, expected in index_exclude_when.items():
        if key not in fm:
            return False
        if coerce_to_string(fm[key]) != coerce_to_string(expected):
            return False
    return True


def _entry_from_head_text(
    entry_text: str,
    entry_kind: str,
    entry_fields: "Sequence[EntryField]",
    index_exclude_when: Optional[Mapping[str, object]],
) -> Optional[dict]:
    """Build one entry's RAW-value mapping from its already-read HEAD blob
    text — the same shape `coordinator_core.docindex.entry_kinds.read_entry`
    plus `coordinator_core.ops.docindex_emit._raw_entry_values` produce
    together for the working-tree case (see module docstring
    Negative-spec), adapted here to operate on text already resolved from
    `HEAD` rather than re-reading a live path. Returns None when AC16-
    excluded. Raises `IndexSpecError` naming the file's role via the
    caller's own exception context when a declared field cannot be
    resolved — mirrors `entry_kinds.MissingEntryFieldError`'s "never a
    silent omission" contract.
    """
    split = split_frontmatter(entry_text)
    if split is None:
        raise IndexSpecError("entry carries no frontmatter block")
    try:
        fm = yaml.safe_load(split.fm_text)
    except yaml.YAMLError as e:
        raise IndexSpecError(f"entry frontmatter is not valid YAML: {e}") from e
    if not isinstance(fm, dict):
        fm = {}

    if _is_excluded(fm, index_exclude_when):
        return None

    identity_field = entry_fields[0].field if entry_fields else None
    result: dict = {}
    for i, ef in enumerate(entry_fields):
        if ef.field in fm and fm[ef.field] is not None:
            result[ef.field] = fm[ef.field]  # RAW value, per render.py's contract
            continue
        if i == 0 and ef.field == identity_field and entry_kind == "wiki-entry":
            heading = _first_heading(split.body_with_leading_newline)
            if heading is not None:
                result[ef.field] = heading
                continue
        raise IndexSpecError(
            f"missing declared field {ef.field!r} (entry_kind={entry_kind!r})"
        )
    return result


def _discover_and_compare() -> list[str]:
    """Run the whole HEAD-truth drift check and return a list of failure
    messages, one per (index, disagreement) — empty means clean (AC7 covers
    the zero-discovered-indexes case trivially, since the loop below never
    runs)."""
    tracked = _list_tracked_markdown()
    dirty = _dirty_relpaths()

    blobs = cat_file_batch(_REPO_ROOT, "HEAD", tracked)

    failures: list[str] = []
    skipped: list[str] = []

    index_specs: dict[str, IndexSpec] = {}
    for rel_path in tracked:
        head_text = blobs.get(rel_path)
        if head_text is None:
            # Not resolvable at HEAD (e.g. newly-added, untracked-at-HEAD) —
            # cannot be a committed self-declaring index yet.
            continue
        fm = _frontmatter(head_text)
        if fm is None or "index_source_dir" not in fm:
            continue
        split = split_frontmatter(head_text)
        assert split is not None  # _frontmatter() already proved this parses
        try:
            index_specs[rel_path] = parse_index_spec(split.fm_text)
        except IndexSpecError as e:
            failures.append(f"{rel_path}: malformed index frontmatter at HEAD: {e}")

    for index_path, spec in index_specs.items():
        # `index_source_dir` is declared with a trailing slash; a
        # `Path.parent` never carries one.
        source_dir = spec.index_source_dir.rstrip("/")
        entry_candidates = [
            p
            for p in tracked
            if p != index_path and Path(p).parent.as_posix() == source_dir
        ]

        covered = [index_path, *entry_candidates]
        dirty_covered = [p for p in covered if p in dirty]
        if dirty_covered:
            skipped.append(
                f"{index_path}: skipped (dirty in working tree, not asserted "
                f"against): {sorted(dirty_covered)}"
            )
            continue

        index_head_text = blobs[index_path]
        entries: list[dict] = []
        entry_error = None
        for entry_path in sorted(entry_candidates):
            entry_text = blobs.get(entry_path)
            if entry_text is None:
                continue
            try:
                entry = _entry_from_head_text(
                    entry_text, spec.entry_kind, spec.entry_fields, spec.index_exclude_when
                )
            except IndexSpecError as e:
                entry_error = f"{index_path}: entry {entry_path} at HEAD: {e}"
                break
            if entry is not None:
                entries.append(entry)
        if entry_error is not None:
            failures.append(entry_error)
            continue

        result = compare(index_head_text, spec, entries)
        if result.hand_edit:
            failures.append(
                f"{index_path}: hand-edit inside delimited region detected at HEAD "
                f"(digest mismatch) — not diffed further"
            )
            continue
        if result.has_drift:
            names = [a.identity for a in result.added]
            names += [r.identity for r in result.removed]
            names += [f"{c.identity}.{c.field}" for c in result.changed]
            failures.append(
                f"{index_path}: drift against {spec.index_source_dir} at HEAD — "
                f"disagreeing entries: {sorted(names)}"
            )

    return failures


def test_head_truth_whole_repo_drift():
    """AC6/AC7: every self-declaring index's committed region matches a
    fresh HEAD render of its committed entries. Zero discovered indexes is
    a PASS."""
    failures = _discover_and_compare()
    assert failures == [], "HEAD-truth drift detected:\n" + "\n".join(failures)
