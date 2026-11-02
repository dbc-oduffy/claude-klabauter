"""Physical basename-rename transform-kind (§ transform-kind `basename-rename`).

The engine's last content-transform step: physically renames files on disk according to
a consumer-supplied old->new basename map, run AFTER all other content substitution
(exact-substitute, stem-rewrite, path-rewrite, depersonalize, cosmetic post-pass) so that
by the time a file is renamed, every in-file reference to its *contents* already points at
the post-substitution names. Renaming baseNAMEs first would leave in-file references
dangling until the content pass caught up; ordering it last means there is never a window
where file content and file name disagree.

Renames are keyed on **basename** (a bare filename, no path component) -- the caller is
responsible for resolving which on-disk paths under the target tree carry a matching
basename; this module does not itself walk the tree (that is coordinator_core/percolate/
surface.py's job -- file-surface discovery). This module performs the rename mechanics and
the rename-manifest bookkeeping only.

Critical v3 seam -- the rename-manifest. A rename performed via plain `os.rename` (or
`Path.rename`) is invisible to any phase that captured a file-list *before* the rename ran:
a guard/audit routine (coordinator_core/percolate/guards.py, C11) that enumerated files
pre-rename will look up the OLD path post-rename, get ENOENT, and silently skip the file
entirely -- a coverage gap, not a loud failure. `rename_basenames` therefore returns (never
just performs) a `RenameManifest`: an ordered, consumable old->new record the guard/audit
phase can reconcile a stale pre-rename file-list against. The manifest is the mechanism
that closes the gap; a bare rename is not enough.

Ordering invariant (§ Transform-kind taxonomy, `basename-rename` row):
    basename-rename -> (emit manifest) -> guard/audit reconciles against the manifest.

Cross-PASS seam -- the rename generation ledger. The rename-manifest above closes the
gap WITHIN one pass. A second, independent gap opens ACROSS passes, and it is the reason
`reap_superseded_renames` exists: a `dst` basename is a name that exists in NO source
tree. Every destination-reaping sweep in the publish chain (the driver's mirror/
flat-mirror "delete what the source does not have" phase) is therefore structurally
blind to it -- it can only ever conclude "engine output is not in source, delete it",
which is why a delete-sync destination converges by ACCIDENT (the sweep removes last
pass's renamed file, this pass's rename recreates it). A destination synced WITHOUT that
reaping phase has nothing that can remove a superseded `dst`, and the store's own
`basename_rename` table cannot help: once the table's output form changes
(`x.py` -> `a-x.py` becomes `x.py` -> `b-a-x.py`), the superseded name `a-x.py` appears
in NO table -- neither as a `src` nor as a `dst` -- so nothing in the current pass's
inputs names it. The result is a destination that is not reproducible from a single fresh
publish: both generations' basenames accumulate, invisibly on a first publish and only
ever on a table flip.

The engine is the only component that knows a given destination name is engine-produced,
so it is the only component that can reap one. `reap_superseded_renames` persists each
pass's produced `dst` set to a per-target ledger under the engine's own out-of-tree state
root (the same `consumer_state_home()` convention `inject.py`'s preserve-destination-
native sentinel already uses -- deliberately NOT a sidecar inside `target_root`, which
would land an engine artifact in every published tree and be counted by `file-count-delta`
/ walked by every content guard) and, on the next pass, deletes every previously-produced
name the current pass no longer produces. Structural, not advisory: no store author has to
remember to enumerate a superseded name in a `prune-stale-paths` entry, which is the
"relocate the transcription" shape this repo's north star rules out.

Door-facing emissions (`emit_published_name_map` / `emit_published_at`, § docs/plans/
2026-08-28-the-currency-signal-stops-one-hop-short-of-the-door.md chunk C2) -- TWO
best-effort, sibling-to-the-rename artifacts written INTO the published tree, for a
consumer (`coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py`) that is stdlib-only,
installed into a bare `bin/` with no package context, and forbidden from importing
`coordinator_core` (~75ms). Neither may ever fail a publish round -- every failure
(OSError, serialisation problem) is swallowed, same discipline as `coordinator_core/
ops/shim_usage_census.py::record_invocation`.

WIRED into `coordinator_core/percolate/round.py` (see the `WIRING` note below for
both call sites) -- see each function's own docstring for the `ROW-SCOPED` path
contract that wiring depends on.

  - `emit_published_name_map`: a flat `{src_basename: dst_basename}` JSON object,
    scoped to FILE renames only -- readable by `json.loads(path.read_text())` and
    NOTHING else (no wrapper object, no version key, no schema/engine helper to
    interpret it). An absent or corrupt map degrades to "no mapping known" at the
    door -- today's behaviour, never a new failure mode.

  - `emit_published_at`: a single ISO-8601 timestamp line, a SIBLING of
    `_engine_stamp` -- deliberately NOT folded into it. `skew.read_engine_stamp_sha`
    reads the whole stamp file, strips it, and splits on `+`; appending a
    `published_at:` field or line to that file returns a corrupt sha with no
    exception raised, which reaches `git rev-list` and fails -- the currency signal
    goes dark rather than wrong, but a sibling file avoids walking into that
    landmine at all and leaves the stamp's byte contract with `compute_client_token`
    untouched.

    ABSENT-MEANS-UNKNOWN, NEVER ZERO. Every mirror published before this emission
    existed carries an `_engine_stamp` and no timestamp file, so "absent" is the
    COMMON case at first, not an edge one. A reader MUST return `None` (or otherwise
    structurally refuse to produce a value) for an absent/corrupt file, never an
    epoch-zero or an empty string coerced to a date: an age computed from a
    zero/sentinel timestamp reads as "just published" -- a false green in exactly the
    direction this signal exists to prevent, for the mirror it is least true of.

ROW-SCOPED PATH CONTRACT, established by reading `coordinator_core/percolate/
round.py` rather than assumed. `rename_basenames`' own `target_root` is never the
published REPO root at its one real call site (`engine.run_content_transform_sweep`,
via `round.py::step_transform`) -- it is `staging_dir`, ONE ROW's own staging
subtree, which `step_smack` later copies verbatim onto `dest_repo_root / row.
dest_prefix`. `round.py::stamp_engine_row` -- the existing sibling emission this
mechanism was modelled on -- proves the convention: it writes `_engine_stamp` at
`Path(staging_dir) / skew.ENGINE_STAMP_FILENAME`, a BARE basename with no directory
prefix, because `staging_dir` for the engine-package row already IS what lands at
`dest_repo_root/coordinator_core/`. Both emitters here follow the identical
convention -- write directly at `target_root / <basename>`, never at a
repo-root-relative nested path -- so a caller passing the BIN row's own `staging_dir`
(for the map) or the ENGINE-PACKAGE row's own `staging_dir` (for the vintage,
literally alongside `stamp_engine_row`'s own write) produces the door-expected
`coordinator/bin/published-name-map.json` / `coordinator_core/_engine_published_at`
paths in the FINAL published tree, without this module ever needing to know a
repo root exists.

WIRING (`coordinator_core/percolate/round.py`, in this chunk's writes scope as of
the plan's amendment):

  1. `emit_published_at(staging_dir)` is called immediately after `round.py::
     stamp_engine_row`'s own `_engine_stamp` write, under the SAME `_is_engine_
     package_row(row)` guard, on the SAME `staging_dir`, inside the SAME
     `run_round` step (`with timings.step("stamp_engine_row", name): stamp_engine_
     row(row, staging_dir)`).

  2. `round.py::step_transform` now returns `run_content_transform_sweep`'s
     `RenameManifest` (previously discarded). `round.py::run_round` captures it and,
     for a row whose `dest_prefix`'s LAST TWO path segments are `('coordinator',
     'bin')` -- `round.py::_is_bin_row`, a STRUCTURAL predicate on `dest_prefix`,
     deliberately never on `row.target_name` (§ `rename_ledger_path`'s own
     docstring: "consumer-target identity supplied by the caller at call time,
     never a literal embedded here") -- calls `emit_published_name_map(staging_dir,
     rename_manifest or RenameManifest())`.

RESOLVED BLOCKER, kept here so a future edit does not reintroduce it (found while
proving reachability end to end -- reported rather than silently worked around, and
FIXED by EM decision rather than by this module carving an exemption into shared
mechanics): `round.py::_smack_copy_in` unconditionally skips every TOP-LEVEL
DOTFILE when copying a row's `staging_dir` into its destination row root ("a
dotfile sitting at staging's root is publish machinery ... must not ship as a
destination file") -- correct in general (`.percolate-ignore` itself IS such
machinery), and NOT to be exempted for this file, which is payload. The map's
basename was originally `.published-name-map.json`, chosen for tidiness with no
other reason -- a dotfile, so it reached `staging_dir` correctly and was then
silently, categorically discarded by `step_smack` on every single round, with no
error and no log line, before the orphan-sweep hazard this chunk separately
flagged was ever reached. THE FIX IS THE NAME: `PUBLISHED_NAME_MAP_BASENAME` is
`published-name-map.json`, no leading dot, never `_smack_copy_in` gaining an
exemption. `emit_published_at`'s basename (`_engine_published_at`) never carried
this risk. Regression-pinned by `coordinator_core/percolate/tests/
test_rename_map_emission.py::test_map_basename_is_never_dot_prefixed`, and proven
end to end by `test_run_round_map_survives_step_smack_and_lands_where_the_door_
expects`.

GENERALISES PAST THIS CHUNK: the dotfile skip means ANY future generated payload
emitted at a row's staging root is silently dropped by `step_smack`, with no error
and no log line, regardless of how correctly it is wired upstream. This was found
only because a `strict` `xfail` forced a real end-to-end `run_round` call rather
than stopping at a staging-only assertion -- a future emitter at this same seam
should budget for the same end-to-end proof, not a direct-write check alone.

Spec backlink: pln-percolation-engine-for-claude-klabauter--30c51f § Transform-kind
taxonomy (`basename-rename` row), § Store schema (old->new basename pairs, `pathPair`
schema shape), chunk C5c (v3-amend c8366f2e / 7a88cf92: rename-manifest seam).

Negative-spec: this module contains zero repo-specific scrub content -- no persona
names, no codenames, no consumer path literals. Rename pairs are supplied entirely by
the caller (ultimately store data resolved via coordinator_core/percolate/store.py).
This module also does NOT walk the filesystem to discover rename candidates and does NOT
decide guard/audit reconciliation policy -- it only performs renames and emits the record
that makes reconciliation possible.

Directory basename renames (state/audits/2026-08-05-first-full-payload-identity-
findings.md Group E) -- a fleet codename carried by a DIRECTORY's own name (e.g.
`lib/resolve-claude-klabauter/`) is a leak identical in kind to a file's basename carrying one,
but no mechanism reached it before this: `path-rewrite` (rewrite_path.py) only rewrites
path-shaped TEXT inside file content, never the physical tree, and the file-rename loop
below explicitly skips every non-file path. `rename_directories` (below) closes that gap
by REUSING `basename_rename`'s existing src/dst vocabulary rather than inventing a
parallel one, opted into per-entry via a TRAILING SLASH on both `src` and `dst`
(`{"src": "resolve-claude-klabauter/", "dst": "resolve-claude-klabauter/"}`) -- deliberately not a
new schema field: `pathPair`'s shape (this module's own store contract,
coordinator_core/frontmatter/schemas/percolate-store.schema.json) is a cross-repo
VENDORED, tamper-gated file (coordinator_core/percolate/tests/test_store.py
`TestDriftGate`, checked byte-for-byte against coordinator-content-repo's own pinned copy) -- widening
it requires bilateral coordination with that repo, out of scope here, whereas a
convention encoded entirely within the existing `src`/`dst` STRING values needs no schema
change at all. An entry with a trailing slash on exactly one side (not both) is an
authoring mistake, refused loudly (§ `_partition_rename_pairs`) rather than silently
guessed at.

Directory-rename design (§ `rename_directories` for the full contract):
  - Deepest-first ordering: a nested match's own rename must complete before its
    ancestor's, so the ancestor rename never invalidates an already-discovered
    descendant path mid-sweep.
  - Collision is a hard, named error (`DirectoryRenameCollisionError`) -- never a
    silent merge, since this runs against a live destination tree a merge could
    destroy files in.
  - Idempotent: a second sweep over an already-renamed tree finds no directory left
    matching any `src` basename, so it is a structural no-op, not a special-cased one.
  - Windows/case-insensitive-filesystem safe: a rename that differs ONLY in case
    (`Foo` -> `foo`) is detected via `Path.samefile` (never a name-string heuristic
    alone, which cannot distinguish "same directory, case-insensitive alias" from "a
    genuinely different directory that happens to collide once lowercased") and
    routed through a two-step temp-name hop, since a single-step case-only rename is
    unreliable across filesystem implementations.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from coordinator_core.percolate.inject import consumer_state_home
from coordinator_core.percolate.surface import STRUCTURAL_NEVER_PUBLISHED_PREFIXES, matches_exclude_prefix
from coordinator_core.win_portability import split_path
from coordinator_core.wire_paths import rel_id

_LOG = logging.getLogger(__name__)

# Generator-provenance declaration (coordinator_core/ops/generator_census). This module's writes
# (physical renames under a caller-supplied `target_root` publish-destination tree,
# and a rename-generation ledger under `consumer_state_home()`, an out-of-tree state
# root) never land inside claude-klabauter's own tracked repo tree.
GENERATES = []

# `.git`/`.fleet-env` are destination-repo plumbing / a dest-local virtualenv, never
# publish-owned content -- the SAME structural fact `surface.
# STRUCTURAL_NEVER_PUBLISHED_PREFIXES` names as the single shared source of truth (see
# that constant's own docstring for the full worked rationale). `engine.
# _STRUCTURAL_NEVER_PUBLISHED_PREFIXES` and `coordinator/bin/publish.py`'s
# `_is_structurally_never_published` are the other enforcement points reading the same
# structural fact.
#
# Applied to this module's own tree walk because a toplevel row's `target_root` IS the
# destination repo root, so an unfiltered `rglob` reaches inside `.git` and any
# dest-local `.fleet-env` -- and a `basename_rename` `src` colliding with an
# internal filename (`.git/config`, `.git/HEAD`, or any of the 26,405 vendored
# `.fleet-env` filenames) would physically rename a file the destination repo/venv
# needs, corrupting it. No store row makes that reachable today; the exclusion closes
# it before one does, not after.
_STRUCTURAL_NEVER_PUBLISHED_PREFIXES = STRUCTURAL_NEVER_PUBLISHED_PREFIXES


def _iter_tree_paths(root: Path) -> list[Path]:
    """Every path under `root` this module is allowed to rename or record, sorted.

    The SINGLE point at which rename candidates are produced -- the file-rename loop,
    the directory-rename candidate scan, and the already-current pass all consume this
    rather than calling `rglob` themselves, so the `.git`/`.fleet-env` exclusion (§
    `_STRUCTURAL_NEVER_PUBLISHED_PREFIXES`) cannot be forgotten by a later edit that
    adds a fourth walk. A per-use-site check would have to be re-added at every such
    site; a filtered producer is re-used by construction.

    Exclusion semantics are `surface.matches_exclude_prefix`'s, called directly rather
    than reimplemented: segment-exact, so a directory literally named `.git` or
    `.fleet-env` at ANY depth is excluded (a vendored checkout as much as the
    destination root's own), while `.gitignore`, `.github/`, or a file named
    `foo.git`/`foo.fleet-env` -- all legitimately publishable -- are untouched.
    """
    return [
        path
        for path in sorted(root.rglob('*'))
        if not matches_exclude_prefix(
            path.relative_to(root).as_posix(), list(_STRUCTURAL_NEVER_PUBLISHED_PREFIXES)
        )
    ]


class DirectoryRenameCollisionError(RuntimeError):
    """Raised when a directory-rename target already exists as a DISTINCT filesystem
    object (never a case-insensitive alias of the source itself -- see
    `_is_case_only_rename`). Never silently merged or overwritten: this runs against a
    live destination tree, where merging two directories could destroy files silently.
    """


class FileRenameCollisionError(RuntimeError):
    """Raised when a FILE-rename target already exists AND is current-source-derived --
    a file the source tree itself supplies at that same relative path, which this pass
    would otherwise clobber silently. The store's `basename_rename` table (or the source
    tree) is what has to change; the file leg refuses to pick a winner.

    Distinct from the benign case this module DOES resolve: a rename target that exists
    but is absent from the source is this row's OWN prior-pass output, superseded by the
    rename now being performed, so it is removed first and the rename proceeds. Removing
    it explicitly is not a nicety -- `Path.rename` overwrites an existing destination on
    POSIX but raises `FileExistsError` on Windows, so a leg that relied on the implicit
    overwrite was a platform-dependent abort (see `_prepare_file_rename_target`).
    """


class RenameCycleError(RuntimeError):
    """Raised by `RenameManifest.resolve()` when a chain of renames loops back
    on a path it has already produced (e.g. an A->B / B->A pair) instead of
    converging.

    A cycle in a rename manifest is always an upstream bug in rename
    generation (§ `RenameManifest.resolve` docstring) -- the manifest is
    only ever store-authored `src`/`dst` pairs a caller expects to compose
    into a single forward-progressing chain, never a genuine loop. Raising
    here, instead of falling through to `resolved`, means a caller that
    would otherwise silently build a commit pathspec (or any other
    downstream artifact) on the pre-resolution input path gets a loud,
    named failure instead of a value that looks legitimate but is wrong.
    """


class DirectoryRenamePairShapeError(ValueError):
    """Raised when a `basename_rename` entry's `src`/`dst` trailing-slash convention is
    inconsistent -- exactly one of the two ends with `/`, so it is undecidable whether
    the author intended a file or a directory rename. Refused loudly rather than
    guessed (§ module docstring, directory basename renames)."""


@dataclass(frozen=True)
class RenameRecord:
    """One resolved old->new rename, both paths relative to the target tree root.

    `kind` distinguishes a physical FILE rename (`'file'`, this module's original and
    still-default behavior) from a physical DIRECTORY rename (`'directory', § module
    docstring) -- `RenameManifest.resolve()` needs it to know whether to treat
    `old_path`/`new_path` as an exact match (file) or a path-PREFIX to substitute
    (directory, since every file living under a renamed directory has a stale prefix,
    not a stale exact path).
    """

    old_path: str
    new_path: str
    kind: str = 'file'


@dataclass
class RenameManifest:
    """Ordered, consumable record of every rename performed in one basename-rename pass.

    Consumed by the guard/audit phase (coordinator_core/percolate/guards.py) to reconcile
    a file-list captured before the rename ran: for each entry, a guard that still holds
    `old_path` in its enumerated set must resolve it to `new_path` before checking
    existence, instead of getting a false-ENOENT on the vanished `old_path`.
    """

    records: list[RenameRecord] = field(default_factory=list)

    def resolve(self, path: str) -> str:
        """Map a possibly-stale path to its post-rename path.

        Returns `path` unchanged if it was not touched by this manifest's renames --
        callers can apply this unconditionally to every entry in a pre-rename file-list
        without needing to know in advance which entries were actually renamed.

        A `'file'`-kind record matches only an EXACT `path` equal to its `old_path`
        (unchanged from this method's original behavior). A `'directory'`-kind record
        additionally matches `path` as a directory itself (`path == old_path`) OR as
        anything living UNDER the renamed directory (`path` starts with
        `old_path + '/'`) -- every file a renamed directory contains has a stale path
        PREFIX, not a stale exact path, so an exact-match-only resolve would silently
        fail to reconcile any of them.

        CHAINED, not single-shot: a file whose CONTAINING directory is renamed AND
        whose own basename is renamed needs BOTH records applied in sequence -- the
        file-kind record's `old_path` is captured post-directory-rename (§
        `rename_basenames`, directory renames run first and the file-rename walk sees
        their output), so it only matches a path that has ALREADY had the directory
        substitution applied. A single pass that returns on its first match (the
        directory-prefix hit) would stop there and hand back a path with the new
        directory but the stale basename -- a path matching nothing on disk. Looping
        until a pass produces no further change converges on the fully-resolved path
        regardless of how many renames compose, and is a no-op (one iteration) for the
        common single-rename or untouched case.

        Raises `RenameCycleError` if the chain loops back onto an already-visited
        path (e.g. an A->B / B->A manifest) instead of converging -- a cycle can
        only mean an upstream bug in rename generation (§ `RenameCycleError`), and
        returning the unresolved input in that case would hand a caller a value
        that looks like a legitimate resolution but is not.
        """
        original = path
        seen = {path}
        while True:
            resolved = path
            for record in self.records:
                if record.kind != 'directory':
                    if record.old_path == path:
                        resolved = record.new_path
                        break
                    continue
                if path == record.old_path:
                    resolved = record.new_path
                    break
                prefix = record.old_path + '/'
                if path.startswith(prefix):
                    resolved = record.new_path + '/' + path[len(prefix):]
                    break
            if resolved == path:
                return resolved
            if resolved in seen:
                raise RenameCycleError(
                    f'RenameManifest.resolve({original!r}) hit a rename cycle at '
                    f'{resolved!r} -- the manifest\'s rename chain loops back onto a '
                    'path it already produced instead of converging (e.g. an A->B / '
                    'B->A pair). This indicates an upstream bug in rename generation, '
                    'not a resolvable path.'
                )
            seen.add(resolved)
            path = resolved

    def old_paths(self) -> list[str]:
        """All old (pre-rename) paths recorded in this manifest, in rename order."""
        return [record.old_path for record in self.records]

    def new_paths(self) -> list[str]:
        """All new (post-rename) paths recorded in this manifest, in rename order."""
        return [record.new_path for record in self.records]

    def as_dict(self) -> dict:
        """Plain-data form (old->new mapping) for callers that want a serializable shape.

        LOSSY: flattens away `RenameRecord.kind`, so a directory record and a file
        record round-trip identically through this shape -- a wire caller that needs
        `kind` to survive (e.g. to later call `resolve()` against a directory-prefix
        match) MUST use `as_records()` instead. No production caller remains as of
        this diff (`_phase_result_to_wire` now uses `as_records()`); kept, unchanged,
        for existing tests that only ever want the plain old->new mapping and never
        re-hydrate a `RenameManifest` from it. Left lossy/unchanged rather than
        widened or removed, since a test-only convenience form has no wire seam to
        keep in sync -- see `as_records()` for the wire-safe replacement.

        Prior wording claimed `reap_superseded_renames`
        diagnostics still used this form; no such call was found anywhere in this
        module or elsewhere in production. Corrected.
        """
        return {record.old_path: record.new_path for record in self.records}

    def as_records(self) -> list[dict]:
        """Plain-data form that preserves `kind` -- one `{old_path, new_path, kind}`
        dict per record, in rename order. The wire-safe round-trip shape: a caller
        that reconstructs a `RenameManifest` from this (one `RenameRecord` per dict)
        gets a manifest whose `resolve()` behaves identically to the original,
        including directory-prefix resolution -- `as_dict()` cannot support that
        because it drops `kind` (§ that method's docstring).
        """
        return [
            {"old_path": r.old_path, "new_path": r.new_path, "kind": r.kind}
            for r in self.records
        ]


def _partition_rename_pairs(basename_pairs: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split a `basename_rename` section's pairs into (file_pairs, directory_pairs).

    Convention (§ module docstring): a pair is a DIRECTORY rename iff BOTH `src` and
    `dst` end with `/`; a pair is a FILE rename iff NEITHER does (the original,
    still-default shape, unchanged). Exactly one side ending with `/` is an authoring
    mistake -- undecidable which kind was meant -- and is refused loudly rather than
    guessed at, via `DirectoryRenamePairShapeError`.
    """
    file_pairs: list[dict] = []
    directory_pairs: list[dict] = []
    for pair in basename_pairs:
        src, dst = pair['src'], pair['dst']
        src_is_dir, dst_is_dir = src.endswith('/'), dst.endswith('/')
        if src_is_dir != dst_is_dir:
            raise DirectoryRenamePairShapeError(
                f'basename_rename entry src={src!r} dst={dst!r} has a trailing slash '
                'on only one side -- undecidable whether a file or directory rename '
                'was intended. Add a trailing slash to BOTH src and dst for a '
                'directory rename, or remove it from both for a file rename.'
            )
        if src_is_dir:
            directory_pairs.append({'src': src.rstrip('/'), 'dst': dst.rstrip('/')})
        else:
            file_pairs.append(pair)
    return file_pairs, directory_pairs


def _resolve_relocate_context(root: Path) -> tuple[str, object]:
    """Best-effort `(session_id, relocate_touched_path)` pair for routing a
    physical rename through `coordinator_core.session.scope.relocate_touched_
    path` (docs/plans/2026-08-06-relocation-re-declares-the-touch-claim.md,
    C5c) instead of a bare `Path.rename`.

    Resolved ONCE per rename pass by the caller (`rename_directories` /
    `rename_basenames`), never per-file: the session id and the helper's own
    importability do not change mid-walk, so a per-file re-resolve would
    re-run the same lazy import and 4-tier env/sentinel lookup for no
    benefit.

    Lazy AND fail-open, the same pattern `coordinator/bin/cross-repo-memo.py`
    and `coordinator/bin/workweek-complete-close.py` already use for this
    exact helper: this module sits on the percolate/publish path and must
    not gain a hard import coupling to the session engine, nor let an
    unrelated session-engine failure (missing module, no live session,
    resolver exception) turn an otherwise-successful physical rename into a
    failure. Returns `("", None)` on ANY failure -- import failure,
    unresolvable session id, or the resolver itself raising -- which is
    exactly the signal `_do_rename` reads as "take the plain rename path".
    """
    try:
        from coordinator_core.session.core import resolve_session_id

        session_id = resolve_session_id(str(root)) or ""
    except Exception:
        return "", None
    if not session_id:
        return "", None
    try:
        from coordinator_core.session.scope import relocate_touched_path
    except Exception:
        return "", None
    return session_id, relocate_touched_path


def _do_rename(
    old_path: Path,
    new_path: Path,
    *,
    root: Path,
    session_id: str,
    relocate_fn: object,
) -> None:
    """Physically move `old_path` -> `new_path`.

    Routed through `relocate_fn` (`relocate_touched_path`) when both a
    resolved `session_id` and the helper itself are available -- which
    re-declares a claim already held on `old_path` onto `new_path` before
    moving, and is a safe no-op re: claims when `old_path` was never
    claimed. Falls back to a plain `Path.rename` when either is missing, or
    if the helper call itself raises for any reason: a rename this module
    is already committed to performing must never fail, or behave
    differently, because of a claim-bookkeeping nicety. The catch here is
    deliberately broad (not narrowed to `OSError`), same discipline as
    `migrate_cross_repo_layout._move_one`'s non-OSError leg and
    `priority_drain._move`'s own broad-catch fallback -- silence is not
    correct here, only breadth is (§ `_LOG.debug` below): this sits on the
    publish path and is designed to fail open forever, so a systemic
    claim-bookkeeping failure logged nowhere would be permanently invisible.

    Behavioral drift on the happy path: when
    `relocate_fn`/`session_id` are both resolved, the physical move goes
    through `relocate_touched_path`'s internal `shutil.move`, which falls
    back to copy+delete across filesystems, whereas the fallback leg just
    below (`old_path.rename(new_path)`, taken when session context is
    unavailable) is a bare rename that raises `OSError` (EXDEV) on a
    cross-filesystem move instead of copying. So the SAME call can succeed
    or fail depending on whether a session happens to be resolvable --
    riding along with the claim-bookkeeping migration rather than being a
    documented, intentional broadening of move semantics. Judged inert in
    practice (percolate's `target_root` is always a same-filesystem
    destination tree) and left as two legs with genuinely different
    semantics rather than unified, since forcing the `Path.rename` leg
    through a copy+delete fallback would be a real behavior change to the
    long-standing no-session path for no percolate-observable benefit.
    """
    if relocate_fn is not None and session_id:
        try:
            relocate_fn(session_id, str(old_path), str(new_path), cwd=str(root))
            return
        except Exception:
            _LOG.debug(
                "rewrite_basename._do_rename: relocate_touched_path claim-bookkeeping "
                "failed for %s -> %s; falling back to a plain rename (fail-open)",
                old_path, new_path,
                exc_info=True,
            )
    old_path.rename(new_path)


def _is_case_only_rename(old_path: Path, new_path: Path) -> bool:
    """True when `old_path` -> `new_path` differs ONLY in case, on a filesystem where
    that distinction may be invisible to a naive existence check (Windows NTFS, macOS
    default case-insensitive APFS/HFS+).

    Detected via `Path.samefile` (same underlying filesystem object), never via a
    name-string heuristic alone: two GENUINELY DIFFERENT directories that happen to
    collide once lowercased (`Foo` and `foo` both real, distinct entries -- possible on
    a case-SENSITIVE filesystem like Linux) must classify as a real collision, not a
    benign case-only rename; `samefile` is what tells the two apart, since it is
    True only when `old_path` and `new_path` name the SAME inode.
    """
    if old_path.name == new_path.name or old_path.name.lower() != new_path.name.lower():
        return False
    if not new_path.exists():
        return False
    try:
        return old_path.samefile(new_path)
    except OSError:
        return False


def _ledger_recognises_case_variant(
    rel: str,
    new_path: Path,
    *,
    root: Path,
    known_prior_produced_dirs: "set[str]",
) -> bool:
    """True when some entry in `known_prior_produced_dirs` names the SAME on-disk
    directory as `new_path`, differing from `rel` only in case.

    `rel_id` (`coordinator_core/wire_paths.py`) performs no case-folding, so a ledger
    entry recorded under one case spelling and this pass's freshly-computed `rel` fail
    a bare set-membership test even when, on a case-insensitive volume, they name the
    identical directory. A blind casefold comparison would close that gap but, on a
    case-SENSITIVE filesystem, would also conflate two genuinely distinct directories
    that merely collide once lowercased -- exactly the ambiguity `_is_case_only_rename`
    exists to resolve via `samefile` rather than a name-string heuristic alone. This
    mirrors that approach: a casefold match is only trusted once `samefile` confirms
    the ledger entry's path and `new_path` are the same filesystem object, so a
    case-sensitive filesystem's genuinely-distinct directories are never conflated.
    """
    folded = rel.casefold()
    for entry in known_prior_produced_dirs:
        if entry == rel or entry.casefold() != folded:
            continue
        candidate = root / entry
        try:
            if new_path.samefile(candidate):
                return True
        except OSError:
            continue
    return False


def _prepare_file_rename_target(
    new_path: Path,
    *,
    old_path: Path,
    root: Path,
    source_root: "str | Path | None",
) -> None:
    """Make `new_path` free for the file rename about to be performed onto it, or
    refuse loudly.

    Three outcomes, in the order checked:

    1. Nothing there, or `old_path` -> `new_path` is a case-only rename of the SAME
       filesystem object (§ `_is_case_only_rename`) -- nothing to do; unlinking on the
       case-only path would delete the very file being renamed.
    2. An occupant the SOURCE tree supplies at the same relative path -- current-source-
       derived content, never this row's own output. `FileRenameCollisionError`: the
       previous behaviour clobbered it silently on POSIX, which is a store defect the
       file leg must not absorb. Same oracle the directory leg already applies
       (§ `_rename_one_directory`, oracle 2), file-granular.
    3. An occupant absent from the source -- this row's own prior-pass `dst`, superseded
       by the rename now being performed. Removed, then the rename proceeds.

    Why (3) is explicit rather than left to the rename call's own overwrite: the
    destination-sync sweep used to delete last pass's `dst` before this ran, so the
    occupant was never there (§ module docstring, "converges by ACCIDENT"). Once that
    sweep grew a rename exemption (`publish_sync._sweep_mirror_top_level_orphans`,
    `sync_mirror`'s per-plugin phase), the occupant survives by design and this leg is
    what meets it. `Path.rename` overwrites it on POSIX and raises `FileExistsError` on
    Windows -- a publish that succeeds on one platform and aborts on the other, which
    this repo does not ship (CLAUDE.md § Runtime conventions, Windows is first-class).

    `source_root` unavailable is NOT treated as "collision": with no source tree to ask,
    oracle 2 cannot fire, and the leg keeps its long-standing overwrite semantics rather
    than inventing a refusal a caller has never seen.
    """
    if not new_path.exists():
        return
    if _is_case_only_rename(old_path, new_path):
        return

    rel = rel_id(new_path, root)
    if source_root is not None and (Path(source_root) / rel).exists():
        raise FileRenameCollisionError(
            f'file rename collision: cannot rename {old_path} -> {new_path} -- '
            f'{new_path} already exists and the source tree supplies {rel!r} itself, so '
            'it is current content, not this row\'s superseded prior output. Refusing to '
            "overwrite; resolve the collision in the store's basename_rename table, or in "
            'the source tree, before publishing.'
        )
    if new_path.is_dir():
        raise FileRenameCollisionError(
            f'file rename collision: cannot rename {old_path} -> {new_path} -- '
            f'{new_path} already exists as a DIRECTORY. Refusing to remove a directory to '
            "make room for a file rename; resolve it in the store's basename_rename table."
        )
    new_path.unlink()


def _rename_one_directory(
    old_path: Path,
    new_path: Path,
    *,
    root: Path,
    known_prior_produced_dirs: "set[str] | None" = None,
    source_root: "str | Path | None" = None,
    session_id: str = "",
    relocate_fn: object = None,
) -> None:
    """Physically rename one directory, handling the case-only hazard and collisions.

    Raises `DirectoryRenameCollisionError` (naming both paths) if `new_path` is already
    occupied by something other than `old_path` itself -- UNLESS ONE OF TWO independent
    oracles recognises the occupant as this same mechanism's own prior output:

      1. `new_path`'s rel-id is a member of `known_prior_produced_dirs` (the
         rename-generation ledger's own prior directory output, § `rewrite_basename.py`
         module docstring "Cross-PASS collision" and `engine.run_content_transform_sweep`'s
         pre-rename ledger read). Machine-local: keyed on the target's own name in a
         per-machine state file, so it recognises nothing on a target's first-ever run on
         a given machine. `rel_id` (`coordinator_core/wire_paths.py`) performs no
         case-folding, so a bare set-membership test alone misses a ledger entry that
         names the same on-disk directory under a different case spelling; this oracle
         also recognises that case variant, gated on `samefile` confirming genuine
         filesystem identity rather than a blind casefold, so a case-sensitive host's
         genuinely-distinct case-variant directories are never conflated (§
         `_ledger_recognises_case_variant`).

      2. `source_root` is supplied AND `new_path`'s rel-id (relative to `root`) does NOT
         exist AT ALL under `source_root` at that same relative path -- checked via
         `Path.exists()`, deliberately not `Path.is_dir()`: a FILE (or any other
         filesystem object) sitting at that relative path in source is still
         current-source-derived content and must NOT be recognised by this oracle, only
         a fully-absent path may be. A rename's `dst` name is, by construction, never
         itself a name this pass's restricted source tree carries -- the source only
         ever has the PRE-rename (`src`) name, since the rename is what produces the
         `dst` name in the first place -- so an occupant sitting at the `dst` path that
         the current source tree does not itself contain, in any form, cannot be
         current-source-derived content; it can only be residue this same mechanism
         produced on some earlier pass (possibly on a different machine, or before the
         target had a stable ledger key at all -- a fresh clone, a renamed store row, or
         any ledger-key change all present with an empty ledger but a populated
         destination). This oracle needs no persisted state, so it recognises a target's
         own prior output even on its very first run on this machine.

         Interaction with `rel_id`'s lack of case-folding (`coordinator_core/wire_paths.py`):
         oracle 1 now resolves a case-only ledger mismatch itself (§ its own
         paragraph above), so this oracle no longer needs to be the thing that
         silently absorbs it via `recognised_by_source_absence`.

    Why either is safe to clear rather than raise: a two-publish cycle recreates
    `old_path` fresh from source every pass (sync never renames source-side), while
    `new_path` -- if a prior pass's rename produced it and the destination sync exempted
    it from orphan-deletion (§ `sync_mirror`'s `renamed_dir_names` parameter) -- SURVIVES
    the sync that just recreated `old_path`, so by the time this rename runs, both names
    exist simultaneously: a genuine collision by the OLD (pre-fix) rule, but not a
    genuine one in fact, since `new_path` is OUR OWN prior output about to be
    immediately superseded by the fresh rename this call is about to perform. Reaping it
    here -- known to be ours via the ledger or via source-absence, never via a bare
    name-string guess -- converts that into "delete then recreate," which converges to
    the identical final content a normal single-pass rename would produce. A collision
    against anything neither oracle recognises (a genuinely unrelated directory, or
    content the store's own table never produced) still raises unconditionally -- this
    is deliberately narrow, not a general merge-on-collision escape hatch. The two
    oracles are a UNION, not a replacement of one by the other: either recognising the
    occupant is sufficient to reap; `source_root` omitted (`None`, the default) simply
    leaves oracle 2 inert, unchanged prior behavior for any caller with no restricted
    source tree to supply.

    `session_id`/`relocate_fn` (resolved ONCE by the caller, § `_resolve_relocate_context`)
    route the case-only two-step's hops through `_do_rename` so a touch-claim already held
    on `old_path` is re-declared onto its new name rather than stranded -- see
    `_do_rename`'s own docstring for the fail-open contract. Defaulted to `""`/`None` (the
    plain-`Path.rename` path) for any caller that has not resolved a relocate context.

    A directory relocation performed here restates the claim on every currently-claimed
    file NESTED UNDER the moved tree, not merely on the directory path itself:
    `relocate_touched_path` takes a tree-walking branch whenever its source is a real
    directory, so this call site needed no change to gain that. The narrower single-path
    behaviour this docstring previously described was the state before that widening landed
    (`state/bug-backlog/2026-08-06-relocating-a-directory-strands-touch-cla-3878d0fc0ca0.yaml`,
    since closed).
    """
    known_prior_produced_dirs = known_prior_produced_dirs or set()

    if _is_case_only_rename(old_path, new_path):
        # Two-step through a temp sibling name: a single Path.rename() straight from
        # "Foo" to "foo" is unreliable across filesystem implementations on a
        # case-insensitive volume (a case-insensitive existence check reports the
        # destination as "already there" even though it names the same directory,
        # which is exactly the ambiguity a direct rename call risks stumbling on) --
        # the temp hop forces two genuinely distinct names in sequence.
        tmp_path = old_path.with_name(f'{new_path.name}.rename-tmp-{os.getpid()}')
        while tmp_path.exists():
            tmp_path = tmp_path.with_name(tmp_path.name + '~')
        _do_rename(old_path, tmp_path, root=root, session_id=session_id, relocate_fn=relocate_fn)
        _do_rename(tmp_path, new_path, root=root, session_id=session_id, relocate_fn=relocate_fn)
        return

    if new_path.exists():
        rel = rel_id(new_path, root)
        recognised_by_ledger = rel in known_prior_produced_dirs or _ledger_recognises_case_variant(
            rel, new_path, root=root, known_prior_produced_dirs=known_prior_produced_dirs,
        )
        # `.exists()`, not `.is_dir()`: the negative
        # branch must mean "genuinely absent from source", not "absent from
        # source AS A DIRECTORY". A FILE sitting at the same relative path in
        # `source_root` is current-source-derived content -- oracle 2 must
        # not recognise it, or a same-name file-vs-directory collision would
        # get silently reaped instead of raised. `.exists()` follows symlinks
        # the same way `.is_dir()` did, so a symlinked occupant is unaffected.
        recognised_by_source_absence = (
            source_root is not None and not (Path(source_root) / rel).exists()
        )
        if recognised_by_ledger or recognised_by_source_absence:
            # Our own prior-pass output, about to be superseded by the rename
            # this call is already committed to performing -- reap it, then
            # fall through to the ordinary rename below.
            shutil.rmtree(new_path)
        else:
            raise DirectoryRenameCollisionError(
                f'directory rename collision: cannot rename {old_path} -> {new_path} -- '
                f'{new_path} already exists as a distinct filesystem object. Refusing to '
                'merge or overwrite; resolve the collision in the store\'s basename_rename '
                'table, or in the source tree, before publishing.'
            )

    _do_rename(old_path, new_path, root=root, session_id=session_id, relocate_fn=relocate_fn)


def _record_already_current(
    root: Path,
    manifest: RenameManifest,
    *,
    dst_basenames: set[str],
    produced_this_pass: set[Path],
    is_dir: bool,
) -> None:
    """Append a SELF-referential manifest record (old==new) for every on-disk
    file/directory already carrying one of `dst_basenames` that this pass's own
    rename loop did NOT itself just produce.

    Closes a cross-pass correctness gap in `reap_superseded_renames` (§ that
    function's docstring) discovered via the exact "run the sweep twice, assert
    byte-identical trees" property this whole mechanism is required to hold --
    PRE-EXISTING in the file-rename leg before this fix, not introduced by the
    directory-rename addition (reproduced directly against the unmodified file leg
    in isolation): reaping decides "no longer wanted" from "not produced or
    consumed in THIS PASS's manifest" -- which cannot distinguish, from the
    manifest alone, between "the table stopped producing this name" (genuinely
    superseded, should reap) and "nothing needed renaming this pass because the
    name is ALREADY correct" (still current, must NOT reap). A second call to
    `rename_basenames`/`rename_directories` over an already-renamed tree produces
    an EMPTY manifest for that reason -- correct as "no rename action needed" but,
    without this pass, indistinguishable to the reaper from "this row stopped
    renaming entirely."

    The rename table's own `dst` set is still a live, current fact regardless of
    whether an actual rename ran this pass, so scanning the tree for `dst`-named
    entries not already accounted for by an actual rename this pass, and recording
    them too, gives `reap_superseded_renames` the same "still current" signal an
    active rename would have produced.
    """
    for path in _iter_tree_paths(root):
        if path.is_dir() != is_dir:
            continue
        if path.name not in dst_basenames:
            continue
        if path in produced_this_pass:
            continue
        rel = rel_id(path, root)
        manifest.records.append(
            RenameRecord(old_path=rel, new_path=rel, kind='directory' if is_dir else 'file')
        )


def rename_directories(
    target_root: str | Path,
    directory_pairs: list[dict],
    *,
    known_prior_produced_dirs: "set[str] | None" = None,
    source_root: "str | Path | None" = None,
    _relocate_ctx: "tuple[str, object] | None" = None,
) -> RenameManifest:
    """Physically rename every directory under `target_root` matching a directory_pairs entry.

    `directory_pairs` is already trailing-slash-stripped (§ `_partition_rename_pairs`) --
    plain `{"src": <old-basename>, "dst": <new-basename>}` dicts, same shape as the file
    leg. Must run BEFORE the file-rename loop in the same pass (§ `rename_basenames`):
    the file loop does a FRESH `rglob` walk from `target_root`, so it naturally sees
    whatever tree shape directory renames already produced, with no coordination needed
    beyond ordering.

    `known_prior_produced_dirs` (rel-paths, POSIX, relative to `target_root` -- the
    rename-generation ledger's own prior directory output, read by the caller BEFORE
    this call) is forwarded to `_rename_one_directory`'s own collision check: a
    same-named directory surviving from a prior pass (e.g. because the destination
    sync's own orphan-deletion exempted it, § `sync_mirror`'s `renamed_dir_names`) is
    reaped rather than raised as a collision, since it is this SAME mechanism's own
    prior output about to be superseded by the rename this call is already committed
    to performing -- see that function's docstring for the full rationale. `None`
    (the default) preserves the original always-raise-on-collision behavior for any
    caller that has no ledger context to supply (e.g. a caller with no stable
    `target_name`, which never enables the ledger contract in the first place).

    `source_root` (§ `_rename_one_directory`'s docstring, oracle 2) is forwarded
    unchanged to that function as a second, independent, machine-state-free collision
    discriminator, unioned with `known_prior_produced_dirs` rather than replacing it:
    an occupant recognised by EITHER oracle is reaped. `None` (the default) leaves
    this second oracle inert, identical to every call site predating it.

    Deepest-first (§ module docstring): every currently-matching directory is discovered
    in ONE walk before any rename starts, then processed in path-depth-DESCENDING order
    (ties broken by path string, for determinism). This guarantees every descendant
    match is renamed before its ancestor -- an ancestor rename moves its entire subtree
    at once, which would otherwise invalidate a shallower discovery pass's cached path
    for anything nested under it.

    Idempotent by construction, not by special-casing: a second call over an
    already-renamed tree finds nothing left matching any `src` basename (the directories
    now carry their `dst` names), so `candidates` is empty and no ACTUAL rename runs --
    but see the already-current pass below for why the returned manifest still records
    something in that case.

    Raises `DirectoryRenameCollisionError` on a genuine rename-target collision (§
    `_rename_one_directory`) -- never merges or overwrites.

    `_relocate_ctx` is an internal seam: `rename_basenames` resolves the
    (session_id, relocate_fn) pair ONCE (§ `_resolve_relocate_context`) and passes it
    down here so a call made via `rename_basenames` does not re-resolve it a second
    time for the directory leg. `None` (every direct caller of this function, and its
    own default) resolves it locally instead -- still exactly once per call, just
    scoped to this function rather than shared with a sibling file-leg call.
    """
    root = Path(target_root)
    rename_map = {pair['src']: pair['dst'] for pair in directory_pairs}

    manifest = RenameManifest()
    if not rename_map:
        return manifest

    session_id, relocate_fn = (
        _relocate_ctx if _relocate_ctx is not None else _resolve_relocate_context(root)
    )

    candidates = [p for p in _iter_tree_paths(root) if p.is_dir() and p.name in rename_map]
    candidates.sort(key=lambda p: (-len(p.parts), str(p)))

    renamed_new_paths: set[Path] = set()
    for old_path in candidates:
        # Guaranteed unreachable under correct deepest-first ordering (a strict
        # ancestor always sorts AFTER its descendants, so no candidate's ancestor can
        # have moved it away before its own turn) -- kept as a defensive skip, not a
        # silent-corruption risk, rather than crashing on an ENOENT this ordering
        # should make impossible.
        if not old_path.exists():
            continue

        new_basename = rename_map[old_path.name]
        new_path = old_path.with_name(new_basename)
        # String-form, case-SENSITIVE comparison -- not `old_path == new_path`:
        # `pathlib.PureWindowsPath.__eq__` folds case (Windows/macOS-default
        # filesystems), so a genuine case-only rename (`Foo` -> `foo`) compares
        # EQUAL and this guard would skip it as a no-op, silently dropping the
        # exact rename `_is_case_only_rename`'s temp-hop below exists to perform.
        # `new_basename` is compared directly against `old_path.name` (both plain
        # strings) so the only renames skipped here are ones where the table
        # itself declares no change at all.
        if old_path.name == new_basename:
            continue

        _rename_one_directory(
            old_path,
            new_path,
            root=root,
            known_prior_produced_dirs=known_prior_produced_dirs,
            source_root=source_root,
            session_id=session_id,
            relocate_fn=relocate_fn,
        )

        manifest.records.append(
            RenameRecord(
                old_path=rel_id(old_path, root),
                new_path=rel_id(new_path, root),
                kind='directory',
            )
        )
        renamed_new_paths.add(new_path)

    _record_already_current(root, manifest, dst_basenames=set(rename_map.values()),
                             produced_this_pass=renamed_new_paths, is_dir=True)

    return manifest


def rename_basenames(
    target_root: str | Path,
    basename_pairs: list[dict],
    *,
    known_prior_produced_dirs: "set[str] | None" = None,
    source_root: "str | Path | None" = None,
) -> RenameManifest:
    """Physically rename every file (and, opted-in, directory) under `target_root`
    matching a basename_pairs entry.

    `known_prior_produced_dirs` is forwarded unchanged to `rename_directories` --
    see that function's docstring. It has no file-leg equivalent because the file leg
    does not need a ledger to recognise its own prior output: `source_root` alone
    answers it (§ `_prepare_file_rename_target`).

    `source_root` (this pass's restricted source tree, § `_rename_one_directory`'s
    docstring oracle 2) is forwarded to `rename_directories` AND consulted by the file
    leg. It stopped being directory-only on 2026-08-26, when the destination sync's
    orphan sweep grew a FILE-granular rename exemption (`publish_sync.
    _sweep_mirror_top_level_orphans`, then `sync_mirror`'s per-plugin phase): a
    renamed file's prior-pass `dst` now survives that sweep, so the file leg meets an
    occupant where it previously never did -- see `_prepare_file_rename_target` for
    what it does with one, and why leaving it to `Path.rename`'s implicit overwrite
    was a Windows-only publish abort.

    `basename_pairs` is the composed+expanded `basename_rename` store section: a list of
    `{"src": <old-basename>, "dst": <new-basename>}` dicts (§ Store schema `pathPair`
    shape) -- bare basenames, no path components, EXCEPT for the directory-rename
    trailing-slash opt-in (§ module docstring, `_partition_rename_pairs`). Must be run
    LAST, after every other content-transform kind (exact-substitute, stem-rewrite,
    path-rewrite, depersonalize, cosmetic post-pass) has already completed, so renamed
    files'/directories' *contents* already match their *new* names by the time this runs.

    Directory renames (if any) run FIRST, via `rename_directories` -- the file-rename
    loop below does a fresh `rglob` walk afterward, so it transparently sees whatever
    tree shape the directory renames already produced. Then walks `target_root` once
    more, renaming every FILE whose current basename matches a file-kind `src` entry to
    the corresponding `dst` basename (directory component unchanged -- this is a
    basename rename, not a move). Returns a single `RenameManifest` recording every
    rename actually performed (both kinds, directory renames first), in the order
    performed, so the guard/audit phase can reconcile a pre-rename file-list against it
    (see module docstring, ordering invariant).

    `.git`/`.fleet-env` are excluded from every walk this module performs (§
    `_STRUCTURAL_NEVER_PUBLISHED_PREFIXES`, `_iter_tree_paths`): a `src` colliding with a
    git-internal or dest-local-venv filename would otherwise rename a file the
    destination repo/venv needs.

    A `src` basename with no on-disk match performs no rename and produces no manifest
    entry -- this is not an error (the store may declare renames for files/directories
    that don't exist in every consumer's tree).

    Does NOT itself reap a previous pass's superseded output -- that is the separate
    cross-pass concern `reap_superseded_renames` owns (see module docstring, rename
    generation ledger), because it requires per-target persisted state this pure
    mechanics function has no business reading or writing.
    """
    root = Path(target_root)
    file_pairs, directory_pairs = _partition_rename_pairs(basename_pairs)

    # Resolved ONCE for this whole pass (§ `_resolve_relocate_context`) and forwarded
    # into `rename_directories` so the directory leg does not re-resolve it a second
    # time; the file leg below reuses the same pair directly.
    relocate_ctx = _resolve_relocate_context(root)
    session_id, relocate_fn = relocate_ctx

    manifest = rename_directories(
        root,
        directory_pairs,
        known_prior_produced_dirs=known_prior_produced_dirs,
        source_root=source_root,
        _relocate_ctx=relocate_ctx,
    )

    rename_map = {pair['src']: pair['dst'] for pair in file_pairs}
    if not rename_map:
        return manifest

    renamed_new_paths: set[Path] = set()
    for old_path in _iter_tree_paths(root):
        if not old_path.is_file():
            continue
        new_basename = rename_map.get(old_path.name)
        if new_basename is None:
            continue

        new_path = old_path.with_name(new_basename)
        _prepare_file_rename_target(
            new_path, old_path=old_path, root=root, source_root=source_root
        )
        _do_rename(old_path, new_path, root=root, session_id=session_id, relocate_fn=relocate_fn)

        manifest.records.append(
            RenameRecord(
                # Forward-slash: RenameManifest.resolve() matches these strings
                # against guard `params['paths']` entries, which are store-authored
                # POSIX paths. A native-separator record would never match, so the
                # rename would go un-resolved and dirs-present would false-ENOENT.
                old_path=rel_id(old_path, root),
                new_path=rel_id(new_path, root),
                kind='file',
            )
        )
        renamed_new_paths.add(new_path)

    _record_already_current(root, manifest, dst_basenames=set(rename_map.values()),
                             produced_this_pass=renamed_new_paths, is_dir=False)

    return manifest


def declared_directory_dst_names(basename_pairs: list[dict]) -> "frozenset[str]":
    """The `dst` basenames of every DIRECTORY-kind pair declared in a target's
    `basename_rename` store section -- static, in-repo, and present with no prior
    publish having ever run (unlike the rename-generation ledger, § module
    docstring, which is empty on a fresh clone/machine and therefore cannot be
    the SOLE source for `sync_mirror`'s `renamed_dir_names` orphan-sweep
    exemption -- see `coordinator/bin/publish.py`'s `renamed_dir_names` block).

    `basename_pairs` is the composed+expanded `basename_rename` section, same
    shape `rename_basenames` consumes. Delegates to `_partition_rename_pairs`
    for the file/directory split (§ that function's trailing-slash convention)
    so a file-kind pair -- never a top-level directory -- cannot enter the
    returned set. Raises `DirectoryRenamePairShapeError` for the same
    malformed-pair case `_partition_rename_pairs` itself refuses loudly on;
    this function does not additionally degrade that away.
    """
    _, directory_pairs = _partition_rename_pairs(basename_pairs)
    return frozenset(pair['dst'] for pair in directory_pairs)


def declared_file_dst_names(basename_pairs: list[dict]) -> "frozenset[str]":
    """The `dst` basenames of every FILE-kind pair declared in a target's
    `basename_rename` store section -- the file-granular twin of
    `declared_directory_dst_names` above, and read for the same reason at the same
    place: `sync_mirror`'s orphan sweep cannot tell "a name the engine itself
    renamed last pass" apart from "a name the source dropped", because in both
    cases the destination holds a name the source does not.

    Needed once the sweep reached top-level FILES (2026-08-26, § `publish_sync.
    _sweep_mirror_top_level_orphans`). Measured on the `claude-klabauter-
    coordinator-bin` row the moment that sweep was first previewed: 10 of its 12
    proposed deletions were engine-renamed published files, not orphans -- they
    would have been deleted and re-created under the same names by the very next
    transform pass, exactly the oscillation `renamed_dir_names` exists to prevent
    one granularity up.

    Same static/ledger split as the directory case: this section is in-repo and
    present with no prior publish having run, so it covers the fresh-clone case the
    rename-generation ledger (empty until a pass has succeeded) cannot. Callers
    union the two rather than choosing. Delegates to `_partition_rename_pairs`, so
    a directory-kind pair cannot enter the returned set, and raises
    `DirectoryRenamePairShapeError` on the same malformed-pair case that function
    refuses loudly on rather than degrading it away.
    """
    file_pairs, _ = _partition_rename_pairs(basename_pairs)
    return frozenset(pair['dst'] for pair in file_pairs)


_LEDGER_STEM = 'rename-generation'


def rename_ledger_path(target_name: str, *, state_home: str | Path | None = None) -> Path:
    """Resolve the rename-generation ledger file for one consumer target.

    Root: `state_home` if the caller supplied one, else the engine's generic
    `consumer_state_home()` -- the SAME out-of-tree state convention `inject.py`'s
    preserve-destination-native sentinel uses, and deliberately not a sidecar inside
    `target_root` (§ module docstring, cross-PASS seam). Filename is keyed on
    `target_name`, which is consumer-target identity supplied by the caller at call time,
    never a literal embedded here.
    """
    root = Path(state_home) if state_home else consumer_state_home()
    return root / f'{_LEDGER_STEM}.{target_name}.json'


def read_rename_ledger(ledger: str | Path) -> list[str]:
    """Read a rename-generation ledger: the FILE `dst` rel-paths the PREVIOUS pass
    produced (`produced` key).

    Returns `[]` for an absent ledger (first pass for this target) and for a ledger whose
    content is unreadable or not the expected `{"produced": [...]}` shape. A corrupt
    ledger degrades to "no prior generation known" rather than raising: the ledger is a
    reaping optimisation over a destination that is otherwise merely stale, so failing the
    whole publish over it would trade a cosmetic defect for an outage. The next successful
    pass rewrites it, so the degradation is self-healing rather than sticky.
    """
    path = Path(ledger)
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    produced = payload.get('produced') if isinstance(payload, dict) else None
    if not isinstance(produced, list):
        return []
    return [entry for entry in produced if isinstance(entry, str)]


def read_directory_rename_ledger(ledger: str | Path) -> list[str]:
    """Read a rename-generation ledger: the DIRECTORY `dst` rel-paths the PREVIOUS pass
    produced (`produced_directories` key, § `rename_directories`).

    Returns `[]` for every degrade case `read_rename_ledger` also returns `[]` for,
    PLUS an older ledger written before directory-rename support existed (no
    `produced_directories` key at all) -- that predates-this-feature case is exactly as
    benign as "no prior directory generation known", not a corruption.
    """
    path = Path(ledger)
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    produced = payload.get('produced_directories') if isinstance(payload, dict) else None
    if not isinstance(produced, list):
        return []
    return [entry for entry in produced if isinstance(entry, str)]


def write_rename_ledger(
    ledger: str | Path,
    produced: list[str],
    produced_directories: list[str] | None = None,
) -> None:
    """Record the `dst` rel-paths THIS pass produced (files and, opted-in, directories),
    replacing any prior generation.

    `produced_directories` defaults to `None` (treated as `[]`) -- every EXISTING caller
    that only ever renamed files keeps its exact call shape and, since the key is
    omitted entirely when empty (see below), its exact ledger byte shape too.

    Written atomically (temp sibling + `os.replace`) so a pass interrupted mid-write
    leaves the previous generation's ledger intact rather than a truncated file that
    `read_rename_ledger`/`read_directory_rename_ledger` would degrade to "no prior
    generation" -- which would strand exactly the superseded names this ledger exists to
    reap. Both `produced` empty AND `produced_directories` empty removes the ledger
    entirely: "this target produces no engine-renamed names" is the same state as "this
    target has never been renamed", and leaving a husk behind for every non-renaming
    target litters the state root for no gain. The `produced_directories` KEY itself is
    omitted (not written as `[]`) whenever there is nothing to put in it, so a
    directory-rename-naive target's ledger stays byte-identical to what this function
    wrote before directory-rename support existed.
    """
    path = Path(ledger)
    produced_directories = produced_directories or []
    if not produced and not produced_directories:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'{path.name}.tmp')
    payload: dict = {'produced': sorted(produced)}
    if produced_directories:
        payload['produced_directories'] = sorted(produced_directories)
    tmp.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8', newline="\n")
    os.replace(tmp, path)


def _is_safe_ledger_entry(rel: str) -> bool:
    """Reject a ledger entry that could resolve outside `target_root` before deleting it.

    Ledger entries are engine-written POSIX rel-paths, so in the ordinary case this always
    passes. It is checked anyway because this is the one code path in this module that
    DELETES a caller-independent path read back off disk: a hand-edited or otherwise
    tampered ledger must not be able to steer an unlink outside the target tree.

    Segmentation goes through `win_portability.split_path`, the repo's one sanctioned
    fold-then-split primitive, so a backslash counts as a separator on every host --
    the entry's author platform is unknown, and `os.sep` would read a Windows-authored
    entry as a single opaque filename on POSIX. The rooted test runs on the SPLIT form
    for the same reason: `\\escapee.py` and `\\\\host\\share` carry no `/` and no `:`,
    but `Path(target_root) / entry` on Windows discards `target_root` for both -- folded,
    each yields a leading empty segment, which is the one shape that catches all of
    `/x`, `\\x`, and UNC together.
    """
    if not rel or ':' in rel:
        return False
    parts = split_path(rel)
    return parts[0] != '' and '..' not in parts


def reap_superseded_renames(
    target_root: str | Path,
    manifest: RenameManifest,
    *,
    target_name: str,
    state_home: str | Path | None = None,
) -> list[str]:
    """Delete every previously-produced `dst` this pass no longer produces, then re-ledger.

    The cross-pass half of the rename contract (§ module docstring, cross-PASS seam).
    `manifest` is THIS pass's `rename_basenames` result -- pass an empty `RenameManifest`
    when `basename-rename` is inactive for the target, which is itself a meaningful state:
    a target that stops renaming must have its last generation's synthesized names reaped,
    not left behind forever.

    Reaps `prior_produced - this_pass_produced - this_pass_consumed`. Subtracting this
    pass's `old_paths()` as well as its `new_paths()` is belt-and-braces: a name the
    previous pass produced and this pass consumed as a rename INPUT has already been
    renamed away by the time this runs, so it would fail the existence check regardless --
    but stating the exclusion keeps the reap set legible as "names no longer accounted for
    by the current table" rather than depending on an ordering coincidence.

    Returns the rel-paths actually deleted, in ledger order. Two kinds of reap, one per
    ledger list: the FILE leg is narrow by construction -- only regular files and
    symlinks are unlinked, since a directory sitting at a recorded FILE path is
    something this pass did not create and must not remove. The DIRECTORY leg (§
    `rename_directories`) is the mirror image -- only a directory (never a symlink) is
    removed, via `shutil.rmtree` since a superseded renamed directory may still contain
    many files this engine itself produced last pass; provenance for BOTH legs is the
    same argument (the ledger only ever names paths THIS ENGINE previously produced via
    THIS mechanism), just applied to the two different filesystem-object kinds the two
    mechanisms produce.

    KNOWN, ACCEPTED narrowing: a name is reaped when the current table stops producing it,
    including when the reason it stopped is that the SOURCE file/directory backing it
    went missing for this pass. That is the same conclusion the driver's own delete-sync
    reaches for every non-renamed path (source-absent implies destination-absent), so
    the two agree rather than diverge; a source path that returns is re-copied and
    re-renamed on the next pass.
    """
    root = Path(target_root)
    ledger = rename_ledger_path(target_name, state_home=state_home)

    file_records = [r for r in manifest.records if r.kind != 'directory']
    dir_records = [r for r in manifest.records if r.kind == 'directory']

    produced_now = {r.new_path for r in file_records}
    consumed_now = {r.old_path for r in file_records}
    dirs_produced_now = {r.new_path for r in dir_records}
    dirs_consumed_now = {r.old_path for r in dir_records}

    reaped: list[str] = []
    for rel in read_rename_ledger(ledger):
        if rel in produced_now or rel in consumed_now:
            continue
        if not _is_safe_ledger_entry(rel):
            continue
        candidate = root / rel
        if candidate.is_symlink() or (candidate.exists() and candidate.is_file()):
            candidate.unlink()
            reaped.append(rel)

    for rel in read_directory_rename_ledger(ledger):
        if rel in dirs_produced_now or rel in dirs_consumed_now:
            continue
        if not _is_safe_ledger_entry(rel):
            continue
        candidate = root / rel
        if candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate)
            reaped.append(rel)

    write_rename_ledger(ledger, sorted(produced_now), sorted(dirs_produced_now))
    return reaped


#: Basename (no directory component) of the src->dst basename map (§ module
#: docstring, door-facing emissions, ROW-SCOPED PATH CONTRACT). Written
#: directly at a caller-supplied `target_root`'s own root -- `target_root` is
#: expected to be the BIN ROW's own `staging_dir`
#: (`coordinator_core/percolate/round.py`'s per-row staging subtree, which
#: `step_smack` copies onto `dest_repo_root / row.dest_prefix`), matching
#: `stamp_engine_row`'s bare-basename write convention exactly. A caller
#: passing the bin row's `staging_dir` here produces
#: `coordinator/bin/published-name-map.json` in the FINAL published tree
#: without this module ever resolving a repo root itself.
#:
#: NOT dot-prefixed, and that is load-bearing (§ module docstring, RESOLVED
#: BLOCKER): `round.py::_smack_copy_in` unconditionally skips every
#: top-level dotfile when copying a row's staging_dir into its destination,
#: so a dot-prefixed name here would be written into staging and then
#: silently, permanently discarded on every single publish round -- exactly
#: what happened before this was caught and fixed.
PUBLISHED_NAME_MAP_BASENAME = 'published-name-map.json'


def published_name_map_key(src_basename: str) -> str:
    """The map key a door computes for *src_basename* -- lowercase sha256 hex
    of its UTF-8 bytes.

    WHY THE MAP IS NOT KEYED BY THE NAME ITSELF. The map's keys are SOURCE
    basenames, and a source basename is exactly what the mirror's identity
    scrub exists to remove: `probe-cwd-example-retrieval-repo-relevance.py` carries a
    scrub-list codename verbatim. A map keyed by literal source names
    therefore ships every renamed target's internal spelling into the public
    mirror in plain text -- which the `no-residual-pattern` leak check refuses,
    fail-closing the whole publish route and leaving the mirror holding nine
    passing rows' certified bytes uncommitted. Keying by digest keeps the
    binding the door cannot infer while putting no codename in the published
    file (PM ruling, 2026-09-02: redesign the door lookup rather than exempt
    the map from the leak check).

    HONEST LIMIT, stated rather than implied: a digest of a short guessable
    basename is not secrecy. Anyone holding a candidate name can confirm it by
    hashing it. What this buys is the property the scrub actually enforces --
    no scrub-list string is PRESENT in a published byte -- not
    unrecoverability. Do not reach for this helper as though it were the
    latter.

    DUPLICATED BY NECESSITY in `coordinator/lib/resolve-claude-klabauter/
    _resolve_claude_klabauter.py::_published_name_map_key`. That module is installed
    standalone into a bare `bin/` with no package context and is stdlib-only
    by contract (an engine import on the exec path costs ~75ms before any work
    happens), so it cannot import this one -- the same reason it hardcodes
    `PUBLISHED_NAME_MAP_BASENAME` rather than importing it. The two definitions
    are pinned equal by `coordinator_core/percolate/tests/
    test_rename_map_emission.py::test_door_key_helper_matches_engine_helper`;
    change one and that test fails rather than the door silently missing every
    lookup.
    """
    return hashlib.sha256(src_basename.encode('utf-8')).hexdigest()

#: Basename of the published-at vintage file. A SIBLING of `skew.
#: ENGINE_STAMP_FILENAME` ('_engine_stamp') in whatever directory a caller
#: writes both to -- never folded into `_engine_stamp` itself (§ module
#: docstring, the `read_engine_stamp_sha` corrupt-sha landmine this avoids).
#: Same ROW-SCOPED PATH CONTRACT as `PUBLISHED_NAME_MAP_BASENAME`: a caller
#: passing the ENGINE-PACKAGE row's own `staging_dir` -- the SAME `staging_dir`
#: `round.py::stamp_engine_row` already writes `_engine_stamp` into --
#: produces `coordinator_core/_engine_published_at` in the final published
#: tree.
PUBLISHED_AT_BASENAME = '_engine_published_at'


def _atomic_write_text(path: Path, content: str) -> None:
    """Temp-sibling + `os.replace`, same atomicity convention `write_rename_ledger`
    already uses -- a publish round interrupted mid-write must never leave a
    truncated file behind for a reader that only ever does `json.loads`/a bare
    strip-and-parse with no partial-content handling of its own."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'{path.name}.tmp')
    tmp.write_text(content, encoding='utf-8', newline='\n')
    os.replace(tmp, path)


@dataclass(frozen=True)
class DoorEmission:
    """The outcome of ONE door-facing emission, returned so a publish route can
    SAY what happened instead of leaving a silence behind.

    Exists because the two halves of this mechanism are each individually
    defensible and jointly blind: the emitters swallow every exception by
    contract (they must never fail the round they ride along with), and the
    door reader treats an absent map as "no mapping known" rather than an
    error. A mirror could therefore go indefinitely with no map while nothing
    anywhere said so -- and did, until the four installed forwarders naming a
    renamed target were found failing at a 127. This value is what makes the
    non-event observable; the un-failable contract is unchanged.

    `emitted` is False both for a genuine failure and for a deliberate skip (a
    row this artifact does not belong to). `detail` carries which, in one line
    fit for a route's own stdout/stderr.
    """

    kind: str
    emitted: bool
    path: "str | None" = None
    entries: int = 0
    detail: str = ""

    def render(self) -> str:
        """One line, agent-facing register: one fact, once (docs/wiki/
        guard-messaging.md § Register)."""
        if self.emitted:
            return f"{self.kind}: emitted {self.path} ({self.entries} entry/entries)"
        return f"{self.kind}: NOT emitted -- {self.detail}"


def is_door_bin_destination(dest_prefix: "str | None") -> bool:
    """True iff `dest_prefix` names the destination subtree the door
    (`coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py`) reads an exec
    target's published name from -- i.e. the row `PUBLISHED_NAME_MAP_BASENAME`
    belongs to.

    STRUCTURAL, on `dest_prefix` only, never on a row/target NAME: a literal
    target name here is exactly what `rename_ledger_path`'s docstring refuses
    ("consumer-target identity supplied by the caller at call time, never a
    literal embedded here"), and a name match would stop matching the moment
    the row is renamed -- a poor choice to make twice inside a mechanism whose
    whole premise is that renames break name-keyed lookups.

    Matches the LAST TWO path segments, so a row nested under a deeper
    destination root (`vendor/coordinator/bin`) is still recognised while a row
    merely CONTAINING the substring in a longer unrelated segment is not.

    Lives here rather than in either publish route because BOTH routes need the
    identical answer (`round.py::run_round` and `coordinator/bin/publish.py::
    process_target`) -- two copies of this predicate is the drift this function
    exists to make unrepresentable.
    """
    if not dest_prefix:
        return False
    return PurePosixPath(dest_prefix).parts[-2:] == ('coordinator', 'bin')


def _coerce_rename_manifest(manifest: "RenameManifest | list | tuple | None") -> RenameManifest:
    """Accept either a `RenameManifest` (the in-process shape `round.py::
    step_transform` returns) or its `as_records()` wire shape (the `list[dict]`
    `coordinator/bin/publish.py` receives back from the `post_rsync` phase),
    and hand back a `RenameManifest` either way.

    `None` becomes an EMPTY manifest, never a refusal: `basename-rename`
    inactive for a row is "this pass renamed nothing", which must publish `{}`
    and thereby retire any stale prior map -- not leave the previous pass's
    bindings standing (§ module docstring, cross-PASS seam).
    """
    if manifest is None:
        return RenameManifest()
    if isinstance(manifest, RenameManifest):
        return manifest
    records = []
    for entry in manifest:
        records.append(
            RenameRecord(
                old_path=entry['old_path'],
                new_path=entry['new_path'],
                kind=entry.get('kind', 'file'),
            )
        )
    return RenameManifest(records)


def emit_door_name_map_for_row(
    target_root: "str | Path",
    dest_prefix: "str | None",
    manifest: "RenameManifest | list | tuple | None",
    *,
    scope_prefix: str = '',
) -> DoorEmission:
    """THE one call path both sanctioned publish routes share for the
    door-facing name map -- `coordinator_core/percolate/round.py::run_round`
    (`percolate-round`) and `coordinator/bin/publish.py::process_target`
    (`coordinator-publish`).

    It exists because the two routes previously disagreed about whether a
    door-facing artifact exists AT ALL: the emission was wired into the round
    driver only, so which command an operator happened to type decided whether
    the published mirror could serve a renamed forwarder's target. The percolate
    skill routinely instructs `coordinator-publish` for several registered rows
    against one mirror, so that was the normal shape, not an edge case.

    Skips silently-but-REPORTEDLY (a `DoorEmission` with `emitted=False` and a
    stated reason) for any row that is not the door's bin destination -- a map
    under a `lib/`/doc row would name a binding nothing reads.

    NEVER raises: delegates to `emit_published_name_map`, whose swallow-everything
    contract is unchanged.

    Negative-spec -- A GENERATED DOOR ARTIFACT IS NOT DERIVABLE FROM TRACKED
    SOURCES AND MUST NEVER BE RE-DERIVED THAT WAY. The map is written by the
    process that performs the rename, and by nothing else. It cannot be
    reconstructed by inferring a token rewrite from the source basename:
    `check-claude-klabauter-doctor-sentinel.sh` -> `check-claude-klabauter-doctor-
    sentinel.py` looks inferable and three of the four live renames are, but
    `probe-cwd-example-retrieval-repo-relevance.py` publishes as
    `probe-cwd-example-retrieval-repo-relevance.py`, derivable from nothing in
    the source name. It equally must NOT be re-derived by regenerating it from
    a tracked file list: `coordinator/bin/publish-allowlist-generate.py`
    regenerates the row's field-7 inclusion CSV from `git ls-files`, so a
    generated artifact that exists only downstream of the copy can never be a
    member of that CSV, and hand-adding one would be reverted by the next
    generator run. Anything that "looks up" a published name outside this
    emission is guessing.
    """
    emission_kind = 'published-name-map'
    if not is_door_bin_destination(dest_prefix):
        return DoorEmission(
            kind=emission_kind,
            emitted=False,
            detail=f"row destination {dest_prefix!r} is not the door's bin destination; nothing to emit",
        )
    return emit_published_name_map(
        target_root,
        _coerce_rename_manifest(manifest),
        scope_prefix=scope_prefix,
    )


def emit_published_name_map(
    target_root: str | Path,
    manifest: RenameManifest,
    *,
    scope_prefix: str = '',
) -> DoorEmission:
    """Best-effort: emit THIS pass's src->dst basename map directly at
    `target_root / PUBLISHED_NAME_MAP_BASENAME` (§ module docstring, ROW-SCOPED
    PATH CONTRACT -- NOT nested under a `coordinator/bin/` path component;
    `target_root` is expected to already BE that row's own root).

    KEYS ARE DIGESTS, NOT NAMES (§ `published_name_map_key`, and the PM ruling
    it cites). Each key is `published_name_map_key(src_basename)`; each value
    is the published basename verbatim, because a published name is the scrub's
    own output and carries nothing to hide. A reader wanting the source
    spelling back cannot get it from this file, which is the point -- the
    rename ledger (`write_rename_ledger`) stays the source-side record, and it
    does not ship.

    CROSS-VINTAGE DEGRADE, both directions, deliberately unguarded by any
    version envelope: a door of one vintage reading a map of the other simply
    misses every lookup and falls back to the asked-for name, which is the
    "absent map -> no mapping known" path every caller already handles (§
    `resolve_target_path`'s negative spec). That is the pre-map behaviour, not
    a new failure mode, so an envelope would buy a clearer error for a state
    that already degrades correctly. The two legs land together from source
    and no map of the literal-keyed vintage ever reached a mirror -- the leak
    check fail-closed every round that would have shipped one -- so there is
    no deployed population to strand.

    `scope_prefix` (default `''`, meaning "every file record in `manifest`
    belongs to the bin row and is admitted") exists for a caller whose
    `manifest` was NOT already produced by a bin-row-scoped `rename_basenames`
    call -- e.g. a manifest spanning a wider tree -- and needs to narrow to
    entries whose `old_path` starts with a literal prefix before computing
    basenames. The intended production caller (§ module docstring, WIRING)
    calls `rename_basenames` with the bin row's own `staging_dir` as
    `target_root`, whose manifest entries are ALREADY bin-scoped bare
    rel-paths with no prefix to strip, so the default `''` (no filtering) is
    the expected shape there.

    Scoped to FILE renames (never directory). Basenames are unique within a
    single directory by filesystem construction, so a `src` basename
    collision cannot arise from a manifest genuinely scoped to one row's own
    tree; the guard below is defensive (a hand-edited/malformed manifest, or a
    caller supplying `scope_prefix=''` against an unscoped multi-directory
    manifest, could produce two different `dst` values for the same `src`
    basename) -- on that guard tripping, this function reports nothing rather
    than silently picking one binding: the map is left unwritten this pass,
    a strict subset of the ordinary "absent map -> no mapping known" degrade
    path already required of every caller.

    Always overwrites the map with THIS pass's set (including an empty `{}`
    when the pass renamed nothing in scope) -- a prior pass's map surviving
    unrefreshed after a table stops renaming a target would be a stale
    binding no different in kind from the rename ledger's own
    superseded-name problem (§ module docstring, cross-PASS seam).

    NEVER raises: any OSError or serialisation failure is swallowed here, same
    discipline as `shim_usage_census.record_invocation` -- this emission must
    never be able to fail the publish round it rides along with.
    """
    try:
        root = Path(target_root)
        rename_map: dict[str, str] = {}
        for record in manifest.records:
            if record.kind != 'file':
                continue
            if scope_prefix and not record.old_path.startswith(scope_prefix):
                continue
            src_basename = record.old_path.rsplit('/', 1)[-1]
            dst_basename = record.new_path.rsplit('/', 1)[-1]
            src_key = published_name_map_key(src_basename)
            existing = rename_map.get(src_key)
            if existing is not None and existing != dst_basename:
                _LOG.debug(
                    "rewrite_basename.emit_published_name_map: basename collision "
                    "for src=%r (dst=%r vs %r) -- refusing to pick one; map not "
                    "written this pass",
                    src_basename, existing, dst_basename,
                )
                return DoorEmission(
                    kind='published-name-map',
                    emitted=False,
                    detail=(
                        f"basename collision for src={src_basename!r} "
                        f"({dst_basename!r} vs {existing!r}); refusing to pick "
                        "one, map not written this pass"
                    ),
                )
            rename_map[src_key] = dst_basename

        path = root / PUBLISHED_NAME_MAP_BASENAME
        _atomic_write_text(path, json.dumps(rename_map, sort_keys=True) + '\n')
        return DoorEmission(
            kind='published-name-map',
            emitted=True,
            path=str(path),
            entries=len(rename_map),
        )
    except Exception as exc:  # noqa: BLE001 -- best-effort by contract, see docstring
        _LOG.debug(
            "rewrite_basename.emit_published_name_map: best-effort emission "
            "failed; publish round proceeds unchanged",
            exc_info=True,
        )
        return DoorEmission(
            kind='published-name-map',
            emitted=False,
            detail=f"{type(exc).__name__}: {exc}",
        )


def emit_published_at(target_root: str | Path, *, when: "str | None" = None) -> DoorEmission:
    """Best-effort: emit a single ISO-8601 `published_at` line directly at
    `target_root / PUBLISHED_AT_BASENAME` -- a SIBLING of `skew.
    ENGINE_STAMP_FILENAME` in that same directory, never folded into it (§
    module docstring, the `read_engine_stamp_sha` corrupt-sha landmine this
    avoids). § module docstring, ROW-SCOPED PATH CONTRACT: `target_root` is
    expected to be the ENGINE-PACKAGE row's own `staging_dir` -- the identical
    directory `round.py::stamp_engine_row` already writes `_engine_stamp`
    into -- not the published repo root.

    `when` defaults to the current UTC time in ISO-8601 form; a caller may
    supply the round's own pinned timestamp instead so every emission this
    pass produces agrees on one instant. This field NAMES ITS OWN AXIS --
    published-at, never "stale" and never "age" -- because the two are not the
    same claim and do not move together (§ module docstring).

    ABSENT-MEANS-UNKNOWN, NEVER ZERO (§ module docstring): this function only
    ever writes a real timestamp or nothing at all. Any reader consuming this
    file MUST treat a missing/corrupt file as `None` ("publish time unknown"),
    never as an epoch-zero or empty-string-coerced-to-a-date sentinel -- a
    zero-derived age reads as "just published", the false-green failure this
    signal exists to prevent, and is most likely to fire on exactly the
    population (mirrors published before this emission existed) this signal
    is least true for.

    NEVER raises: any OSError or serialisation failure is swallowed here, same
    discipline as `emit_published_name_map` / `shim_usage_census.
    record_invocation` -- this emission must never be able to fail the publish
    round it rides along with.
    """
    try:
        if when is not None:
            ts = when.strip()
        else:
            from datetime import datetime, timezone

            ts = datetime.now(timezone.utc).isoformat()
        if not ts:
            return
        root = Path(target_root)
        path = root / PUBLISHED_AT_BASENAME
        _atomic_write_text(path, ts + '\n')
        return DoorEmission(
            kind='published-at',
            emitted=True,
            path=str(path),
            entries=1,
        )
    except Exception as exc:  # noqa: BLE001 -- best-effort by contract, see docstring
        _LOG.debug(
            "rewrite_basename.emit_published_at: best-effort emission failed; "
            "publish round proceeds unchanged",
            exc_info=True,
        )
        return DoorEmission(
            kind='published-at',
            emitted=False,
            detail=f"{type(exc).__name__}: {exc}",
        )
