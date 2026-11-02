"""File-surface discovery for percolation: which files a percolation run touches.

Answers exactly one question -- "given a tree, which files does the engine consider part
of the percolation surface" -- and answers it generically, with the include/exclude
*content* supplied entirely by the caller (ultimately store data, § Store schema). This
module contains no consumer-specific extension lists, exclude-prefixes, or exclude-
basenames: those are runtime arguments, never literals in this file.

Discovery rules (§ Transform-kind taxonomy `file-surface` row, A7/A8; ADMISSION-INVERTED
2026-08-05, state/audits/2026-08-05-first-full-payload-identity-findings.md Group A):
  - **Admission default: PUBLISHED-BY-DEFAULT-MEANS-SCANNED.** A file is in-surface unless
    something EXCLUDES it. Extension and shebang are NOT the admission test -- an
    extensionless file with no `#!` first line (a CLI trampoline authored as a bare
    comment/docstring, say) is in-surface exactly like a `.py` file is, because it was
    published and nothing said otherwise. This closes the exact gap Group A named: a
    file the publish allowlist ships and the OLD extension/shebang gate silently never
    scanned -- two independently-maintained tables (what ships vs. what gets checked)
    that this inversion collapses into one (what ships IS what gets checked, by default).
  - **Binary exclusion is CONTENT-DETECTED, never extension-guessed.** `is_probably_binary()`
    sniffs for a NUL byte in the first chunk (the standard git/file(1) heuristic) -- an
    unknown-extension TEXT file is scanned; an unknown-extension BINARY file is not. This
    was already the mechanism (gotcha 14, below); it is now load-bearing rather than a
    belt-and-braces check behind an extension allowlist that did most of the real
    filtering. An empty file has no NUL byte in its (empty) read -- `is_probably_binary`
    returns False, so an empty file is treated as text and IS in-surface (a transform
    that finds nothing to substitute in an empty file is a correct no-op, not a risk).
    An unreadable file (`OSError`) also returns False (fail-open to "treat as text") --
    matching the pre-existing behavior; a missing/unreadable file was never going to be in
    the surface anyway since `iter_surface_files` only calls this on paths its own
    `os.walk` actually found.
  - **Exclude (higher precedence than admission-by-default):** a path is out-of-surface
    if any path segment/prefix matches a caller-supplied exclude-prefix (e.g. `.git`,
    `node_modules`), or its basename matches a caller-supplied exact-basename set or
    glob-pattern set (e.g. `*.bak`, `CHANGELOG.md`, or `check-persona-names.py` itself --
    a mirror's own release-CI identity checker must never scrub itself, per docs/plans/
    2026-08-03-mirror-native-content-homed-and-injected.md AC4/AC5). This is the
    DECLARED-EXCLUSION path Group A's fix must keep expressible and visible -- a
    deliberate exclusion stays a real, named decision in store data, never a silent
    side effect of an extension list simply not mentioning a file.
  - **`include_extensions`/`shebang_prefixes` are now OPTIONAL NARROWING, not admission.**
    Pass `narrow_to_include_extensions=True` for a row that genuinely wants to scan ONLY
    files matching `include_extensions` (or an extensionless file with a recognized
    `#!` first line) and nothing else -- the pre-inversion behavior, opt-in. Default
    `False`: `include_extensions`/`shebang_prefixes`, if supplied, are simply unused for
    admission (a store's existing `include_extensions` list is inert unless that row also
    opts into narrowing) -- so no store row needs to change for the inversion to take
    effect, and every row's own file_surface config keeps validating and resolving
    exactly as before.

One discovery routine, one exclude source (§ Anti-scope): every consumer's include/exclude
lists flow through this single `iter_surface_files` walk. There is deliberately no second
copy of the walk logic for a "special case" consumer -- that duplication is exactly the
drift smell (triplicated plugin-alternation regexes) this plan eliminates.

Deliberate design decisions the shell inherited by accident (gotchas 14/17):

  - **Binary-file handling (gotcha 14):** the shell has no explicit binary guard -- it
    happily lets content-transform tools attempt in-place substitution on any matched
    file, which is silently safe only because its include-extension list never matches a
    real binary format. This engine makes the guard EXPLICIT rather than relying on
    extension-list luck: `is_probably_binary()` sniffs for a NUL byte in the first chunk
    of file content (the standard git/file(1) heuristic) and `iter_surface_files` treats
    a binary-sniffed file as OUT of the surface unconditionally, regardless of extension
    or shebang match. Rationale: a content-transform kind (substitute/stem-rewrite/
    depersonalize) operating on binary content would corrupt it byte-for-byte with no
    recovery path, and no current consumer needs binary-format transforms (§ Out of
    scope) -- the safe default is exclusion, not silent attempted mutation. This is a
    stricter-than-shell behavior, taken deliberately (§ Design principle -- improve on
    the shell, don't transliterate it).
  - **Symlink handling (gotcha 17):** the shell's `find`-based walk follows symlinks by
    default (no `-P`/`-type l` guard), so a symlink pointing outside the source tree (or
    forming a cycle) can silently pull unintended content into the percolation surface or
    infinite-loop the walk. This engine makes the opposite choice EXPLICITLY: a symlink is
    never followed and never itself included as a percolation-surface entry --
    `iter_surface_files` uses `os.walk(..., followlinks=False)` and additionally skips any
    directory entry whose resolved type is a symlink (`Path.is_symlink()`) before applying
    include/exclude logic. Rationale: a percolation surface is defined by the *tree's own*
    content; a symlink is a reference to content that (if in-scope) will already be walked
    at its real location, and following it risks tree-escape (symlink to a path outside
    the source root) and unbounded cycles the shell never guarded against. This is a
    correctness fix over the shell's accidental behavior, not a feature regression --
    no current consumer store relies on symlink-following (§ Anti-scope).

Negative-spec: this module contains zero repo-specific literals -- no persona names, no
codenames, no DoE path prefixes, and no hardcoded consumer-home path (`~/.claude`,
`$HOME/.claude`, or any machine-local prefix). Every include/exclude/shebang value is a
caller-supplied argument sourced from store data (§ Store schema); this module supplies
only the generic walk/matching mechanism.

Spec backlink: pln-percolation-engine-for-claude-klabauter--30c51f § Transform-kind
taxonomy (`file-surface` row, A7/A8, gotchas 14/17).
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
from pathlib import Path

from coordinator_core.percolate import guard_cache

_BINARY_SNIFF_CHUNK_SIZE = 8192
_SHEBANG_PREFIX = '#!'

#: SSOT for "is this directory name a publish-staging leftover" across
#: `coordinator_core/percolate/{surface,engine,store}.py` (§ C1 brief,
#: `docs/plans/2026-08-23-a-percolate-round-costs-417s-for-25s-of-work.md`).
#: The mint shape is `.{dest_dir.name}.publish-staging-<random>` -- after the
#: optional leading dot comes the DESTINATION NAME, never the literal
#: `publish-staging-` -- so this is deliberately UNANCHORED (`.search`, not
#: `.match`): an anchored `^\.?publish-staging-` variant never fires on a real
#: staging directory and is a live defect, not a stylistic choice. `_repo.py`
#: (ships inside the publish mirror, stdlib-only, no `coordinator_core` on its
#: path there) mirrors this pattern independently rather than importing it; a
#: parity test asserts the two stay equal.
PUBLISH_STAGING_DIR_RE = re.compile(r'publish-staging-')


def has_include_extension(path: Path, include_extensions: list[str]) -> bool:
    name = path.name
    return any(fnmatch.fnmatch(name, pattern) for pattern in include_extensions)


def sniff_shebang(path: Path, shebang_prefixes: list[str]) -> bool:
    if not shebang_prefixes:
        return False
    try:
        with path.open('r', encoding='utf-8', errors='ignore') as f:
            first_line = f.readline()
    except OSError:
        return False

    if not first_line.startswith(_SHEBANG_PREFIX):
        return False
    return any(prefix in first_line for prefix in shebang_prefixes)


def is_probably_binary(path: Path) -> bool:
    return guard_cache.cached_binary_verdict(path, _sniff_binary)


def _sniff_binary(path: Path) -> bool:
    try:
        with path.open('rb') as f:
            chunk = f.read(_BINARY_SNIFF_CHUNK_SIZE)
    except OSError:
        return False
    return b'\x00' in chunk


#: Path-segment names that are STRUCTURALLY never publish-owned content at ANY
#: destination tree, regardless of which row or guard is asking. `.git` is the
#: original member (destination-repo plumbing -- no row's sync ever copies into or
#: deletes from it, so nothing there is ever a genuine finding, only a false one:
#: a leak guard scanning `.git/hooks/pre-commit` reports a leak in a file that was
#: never published; a syntax guard parsing `.git`-internal blobs as utf-8 reports a
#: parse failure in a file that is not Python at all). A DEST-LOCAL virtualenv/
#: environment directory (`.fleet-env` -- observed live: 2.7GB, 26,405 files,
#: gitignored at dest, absent from every row's source tree, zero bytes written to it
#: by any round) is the SAME structural class: its third-party site-packages source
#: is never publish-owned content either, so a `python-syntax-valid` guard or a
#: parse sweep asking "does the published payload parse" gets a false failure from
#: vendored scipy/sklearn/mpmath/joblib source this repo never shipped and never
#: will. Scoped by exact segment name (§ `matches_exclude_prefix`), never a broad
#: "skip dot-directories" rule and never derived from a `.gitignore` read -- either
#: would silently widen what a leak guard stops seeing, the exact blind-spot trade
#: this list's own rationale (a true finding under a name NOT listed here must
#: still surface) depends on staying narrow. Add a new name here, deliberately and
#: individually, the same way `.fleet-env` was added -- never a pattern that could
#: also match genuinely-published content. `.fleet-env.prior` (added 2026-08-18) is a
#: SIBLING directory, not a sub-path of `.fleet-env` -- segment-exact matching means the
#: `.fleet-env` entry does not cover it, so it needs its own individual name here. It is
#: the rollback copy left by a fleet-env migration in flight: the same dest-local,
#: never-source, never-published class as `.fleet-env` itself, just parked one name over.
#: `.fleet-env.gen-*` (added 2026-08-20) is the third member of that family and the only
#: one that cannot be spelled as an individual name: `fleet_env._gen_root` mints it as
#: `<env_root name>.gen-<pid>-<hex>`, so the segment is unbounded by construction and a
#: literal entry could never match it. It is the half-provisioned generation a fleet-env
#: build populates before cutover, and an abandoned build leaves it behind until
#: `fleet_env._sweep_orphan_generations` reclaims it. This is the ONE glob this list
#: carries, and it is anchored to the `.fleet-env.` family prefix precisely so it cannot
#: reach published content -- the rule against patterns still governs every other name
#: here. Measured 2026-08-20: unexcluded, it put 4.8GB of a vendored site-packages tree
#: through `run_content_transform_sweep` on the toplevel row and reproduced verbatim the
#: non-utf-8 joblib read failure the `.fleet-env` entry was itself added to stop.
#: `.percolate` (added 2026-08-25) is round bookkeeping written INTO the destination as
#: `<dest_repo_root>/.percolate/round-manifest.json`. The path comes from
#: `round.py::default_manifest_path`, but that module's own smack-path round writes no
#: manifest itself -- the actual writers are `coordinator/bin/percolate-round.py` and
#: `coordinator/bin/publish.py`, which import that helper and call it. No row's sync
#: copies it -- no source tree contains a `.percolate/` path at all (`git ls-files`,
#: zero hits) -- and it is written AFTER the content-transform sweep has finished, so
#: "the sweep never visited it" is the only outcome its own ordering permits. Same
#: dest-side generated class as `__pycache__`, one tier up. Until this entry existed,
#: the end-of-run unscanned-published check fail-closed on the FIRST round to write a
#: manifest into any given mirror, and would have done so for every mirror in turn: the
#: manifest is written unconditionally, so this was fleet-wide breakage latent since
#: `a34b04c69` and surfaced the first time a destination received one. Segment-exact
#: matching keeps this clear of the sibling `.percolate-ignore` FILE, exactly as `.git`
#: stays clear of `.gitignore`.
#: `state` (added 2026-09-02, PM ruling) REPLACES the narrower
#: `engine-provenance-counts.jsonl` basename entry that stood here from 2026-08-26.
#: That entry named one file because a stamped mirror IS the live engine root the fleet
#: is served from, so every op served out of it called
#: `engine_provenance_counter.record_engine_provenance`, which appends to
#: `<root>/state/engine-provenance-counts.jsonl`. Measured then: 1332 -> 2664 bytes DURING
#: one investigation -- it grows faster than a round can clear it, so the end-of-run
#: unscanned-published check fail-closed on every round from every session, and that FATAL
#: aborted the round's remaining per-row commits, leaving residue that blocked
#: `percolate-push`.
#: One file was the wrong unit. The sink is keyed to the CWD's git root, not to an engine
#: root -- `record_engine_provenance` resolves `resolve_git_root_cheap(cwd)` -- and
#: `guard_advisory_counter` (two recorders) and `registry_fallback_counter` share the shape
#: verbatim. So does every other cwd-rooted writer: sweep-boot's `orientation_cache.md`,
#: `group-em-watch.json`, `housekeeping-liveness.json`, `session-hierarchy.*.json`,
#: `test-red/*.yaml`, `shim-usage-census.jsonl`, `raw-cmdline-transport-ledger.jsonl`,
#: `review-trail/`, `improvement-queue/`, `initiatives/`. ANY session that runs with cwd
#: inside a mirror leaves state behind, so the one-basename entry was the first member of a
#: family, not a special case. Swept from `claude-klabauter` on 2026-09-02: 19 files across
#: ~13 distinct writers, archived to claude-klabauter
#: `state/audits/2026-09-02-claude-klabauter-mirror-state-residue/`.
#: HISTORICAL NOTE (post-C7): several of the basenames named just above --
#: `orientation_cache.md`, `shim-usage-census.jsonl`, `review-trail/` among them -- no
#: longer land under `state/` at all. C7 relocated them to `.coordinator-local/`
#: (`machinery_paths.machinery_root`), which is why that dotdir has its own entry below:
#: without it, a mirror's `.coordinator-local/` residue would no longer be caught by the
#: `state` entry that used to cover it.
#: REVERSES this entry's own prior reasoning, deliberately and by PM ruling. The 2026-08-26
#: comment argued: "Named as the exact basename rather than the `state` directory: `state/`
#: may legitimately carry published payload in future, and a directory entry would silence
#: a real routing gap there." The ruling (PM, 2026-09-02) is that `state/` is working-repo
#: space and a publish repo has none, so that future is not one to hold the door open for.
#: The cost it named is real and is ACCEPTED, not refuted: with `state` excluded, a row that
#: genuinely tried to publish under `state/` is skipped silently rather than caught. A row
#: that ever needs to must narrow this entry back, not work around it.
#: EXCLUSION ALONE STRIPS NOTHING -- the half that makes this file's list actually clear a
#: mirror lives in `sweep_never_published_state` below, called by the driver. This tuple
#: only makes the walks blind: `engine.py`'s `_iter_files`, `guards.py`'s shared walk, and
#: through them the content-transform sweep, the residual-leak guards, and the end-of-run
#: unscanned-published check. The removal side cannot substitute for it either -- that rule
#: is `(head_tree INTERSECT row_scope) - declared_payload` over the destination's TRACKED
#: paths, and `state/` is untracked and gitignored in the mirror, so it is not in
#: `head_tree` at all.
STRUCTURAL_NEVER_PUBLISHED_PREFIXES: tuple[str, ...] = (
    '.git',
    '.fleet-env',
    '.fleet-env.prior',
    '.fleet-env.gen-*',
    '.percolate',
    'state',
    '.coordinator-local',
    # Session scratch (benchmark clones, probes) is gitignored at every dest and never
    # published; walked, a peer's clone lands in declared_payload and fails scan-secrets.
    'scratch',
)


def stranded_swap_priors(dest_dir: Path) -> "list[Path]":
    """Sorted top-level entries of `dest_dir` left behind by an incomplete
    root-dest swap -- the shared predicate for both the swap side
    (`publish.py :: _refuse_stranded_root_swap_prior`) and the removal side
    (`percolate-round.py :: _pathspec_from_manifest`).

    `dest_dir.glob("*.prior")` never yields a dotted basename -- stdlib glob
    hides `.`-prefixed names from a pattern that does not itself start with
    `.`, and `_swap_publish_staging_into_dest_root` iterates
    `staging_dir.iterdir()` (no dotfile filter), so it can legally mint a
    dotted strand like `.github.prior`. Scan `iterdir()` directly instead.

    `STRUCTURAL_NEVER_PUBLISHED_PREFIXES` is subtracted because it NAMES
    `.fleet-env.prior` -- destination-repo build plumbing minted by something
    else entirely, which an unfiltered check would read as a strand.

    No subprocess.
    """
    return sorted(
        entry
        for entry in dest_dir.iterdir()
        if entry.name.endswith(".prior")
        and not any(
            fnmatch.fnmatch(entry.name, pattern)
            for pattern in STRUCTURAL_NEVER_PUBLISHED_PREFIXES
        )
    )


#: The one swept member of `STRUCTURAL_NEVER_PUBLISHED_PREFIXES`. The rest of that tuple
#: is excluded but deliberately NOT swept: `.git` is the repository, `.fleet-env*` is a
#: multi-GB provisioned environment a live symlink points at, and `.percolate` is the
#: round's own in-flight bookkeeping. Only `state` is residue with no owner in a mirror.
SWEPT_NEVER_PUBLISHED_DIR = 'state'


def sweep_never_published_state(
    dest_root: Path,
    published_dest_dirs: "set[Path] | None" = None,
) -> "list[str]":
    """Remove `<dest_root>/state/` from a publish destination. Returns the
    repo-root-relative posix ids removed, or `[]` if there was nothing to remove.

    WHY A SWEEP EXISTS AT ALL, when `state` is already in
    `STRUCTURAL_NEVER_PUBLISHED_PREFIXES`: that tuple is a WALK exclusion and deletes
    nothing -- it only makes the content sweep, the residual-leak guards, and the
    end-of-run unscanned-published check blind to the path. The removal side cannot do
    it either: its rule is `(head_tree INTERSECT row_scope) - declared_payload` over the
    destination's TRACKED paths, and `state/` in a mirror is untracked and gitignored,
    so it never appears in `head_tree`. Excluding it and removing it are two different
    jobs, and until 2026-09-02 only the first was done -- which is why the mirror
    accumulated 19 files across ~13 writers while every list said it was handled.

    UNCONDITIONAL, once per destination repo root per round, never opt-in. A sweep that
    defaults off leaves the mirror exactly as it was; the residue regenerates between
    rounds by construction (any session running with cwd in the mirror writes here), so
    "sweep when someone remembers" is the state this closes, not an improvement on it.

    REFUSES rather than sweeps when this round published INTO `state/` -- i.e. when any
    `published_dest_dirs` entry IS `<dest_root>/state` or sits beneath it. That is the
    routing gap the 2026-08-26 comment on the excluded-prefix entry warned about, kept
    as a live check rather than a comment: with `state` excluded from every walk, a row
    that genuinely published there would otherwise have its payload silently deleted by
    this function on the same round that wrote it. Costs one set comparison, no spawn,
    no stat beyond the ones below. `None` (no caller knowledge) sweeps.

    Deliberately NOT guarded on git-tracked-ness. Asking git costs a process per
    destination per round, and the check it would buy is already covered: a tracked
    `state/` path can only exist if a row published it, which is exactly what
    `published_dest_dirs` answers for free.
    """
    target = dest_root / SWEPT_NEVER_PUBLISHED_DIR
    if not target.is_dir():
        return []

    if published_dest_dirs:
        for published in published_dest_dirs:
            try:
                published.relative_to(target)
            except ValueError:
                pass
            else:
                return []
            if published == target:
                return []

    removed = [
        f.relative_to(dest_root).as_posix()
        for f in sorted(target.rglob('*'))
        if f.is_file()
    ]
    shutil.rmtree(target, ignore_errors=True)
    return removed


def matches_exclude_prefix(relative_path: str, exclude_prefixes: list[str]) -> bool:
    """Return True if any path segment of `relative_path` matches an exclude-prefix.

    `relative_path` is POSIX-style (forward-slash-separated), relative to the walk root.
    A prefix matches if it equals any full path segment (e.g. exclude-prefix `.git`
    matches `.git/config` and `sub/.git/HEAD`, but not `.gitignore` -- segment-exact, not
    a raw string-prefix test, so `.gitignore` is never accidentally excluded by a `.git`
    prefix entry).

    A prefix containing `*` or `?` is matched against each segment by `fnmatch` instead.
    This is additive, never a loosening of the segment-exact rule above: a prefix with
    neither character still takes the equality path unchanged. As of this writing, no
    `exclude_prefixes` entry in `setup/percolate-hooks/percolate-store.yaml` -- the only
    percolate store checked -- contains `*` or `?`, so no entry that store declares today
    changes meaning. That is an empirical fact about that one file, not a schema-enforced
    invariant: the JSON schema for `file_surface.exclude_prefixes`
    (`coordinator_core/frontmatter/schemas/percolate-store.schema.json`) carries no pattern
    constraint, unlike the sibling `exclude_basenames` field, so a row-declared
    `exclude_prefixes` reaching this function via `engine.py` or `guards.py` could legally
    contain a literal `*`/`?` and would be reinterpreted as a wildcard here. This glob leg
    exists for a family whose segment is unbounded by construction and therefore cannot be
    spelled as a literal -- today only `.fleet-env.gen-<pid>-<hex>` (§
    `STRUCTURAL_NEVER_PUBLISHED_PREFIXES`). Reach for it only for that shape; an ordinary
    exclusion still gets its own individual name.
    """
    segments = relative_path.split('/')
    literals = [p for p in exclude_prefixes if '*' not in p and '?' not in p]
    globs = [p for p in exclude_prefixes if '*' in p or '?' in p]
    for segment in segments:
        if segment in literals:
            return True
        if any(fnmatch.fnmatch(segment, pattern) for pattern in globs):
            return True
    return False


def matches_exclude_basename(path: Path, exclude_basenames: list[str], exclude_globs: list[str]) -> bool:
    """Return True if `path`'s basename is excluded by exact name or glob pattern.

    `exclude_basenames` is matched exactly (e.g. `'CHANGELOG.md'`); `exclude_globs` is
    matched via `fnmatch` against the basename (e.g. `'*.bak'`). Either match excludes.
    """
    name = path.name
    if name in exclude_basenames:
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in exclude_globs)


def is_in_surface(
    root: Path,
    path: Path,
    *,
    include_extensions: list[str] | None = None,
    shebang_prefixes: list[str] | None = None,
    exclude_prefixes: list[str] | None = None,
    exclude_basenames: list[str] | None = None,
    exclude_globs: list[str] | None = None,
    narrow_to_include_extensions: bool = False,
    include_basenames: list[str] | None = None,
) -> bool:
    """Decide whether a single file `path` (under `root`) is in the percolation surface.

    ADMISSION-INVERTED default (§ module docstring): a file is in-surface unless
    excluded (symlink, exclude_prefixes, exclude_basenames/exclude_globs, or content-
    detected binary) -- `include_extensions`/`shebang_prefixes` do not gate admission
    unless `narrow_to_include_extensions=True` is passed explicitly, in which case this
    reverts to the pre-inversion behavior (must match an extension, or be extensionless
    with a recognized `#!` first line). Exclusion always wins over admission either way --
    there is no "narrow overrides exclude" escape hatch. All list arguments default to
    empty (no excludes fire, and -- when narrowing is on -- no includes match) so callers
    only need to supply the lists relevant to their store.

    `include_basenames` (default empty -- 100% behavior-preserving for every existing
    caller) is a third, narrowing-only admission path alongside extension-match and
    shebang-sniff: an EXACT basename in this list is in-surface even when it is
    extensionless AND carries no recognized `#!` first line (an empty file, or one whose
    first line the store's `shebang_prefixes` does not name). Unlike `shebang_prefixes`,
    which admits by CONTENT sniff, this is a per-file opt-in the store declares by name --
    for the extensionless-but-not-a-script case `shebang_prefixes` cannot reach. Only
    consulted when `narrow_to_include_extensions=True`; ignored otherwise, same as the
    other two admission paths.
    """
    include_extensions = include_extensions or []
    shebang_prefixes = shebang_prefixes or []
    exclude_prefixes = exclude_prefixes or []
    exclude_basenames = exclude_basenames or []
    exclude_globs = exclude_globs or []
    include_basenames = include_basenames or []

    if path.is_symlink():
        return False

    relative_path = path.relative_to(root).as_posix()

    if matches_exclude_prefix(relative_path, exclude_prefixes):
        return False
    if matches_exclude_basename(path, exclude_basenames, exclude_globs):
        return False

    if is_probably_binary(path):
        return False

    if not narrow_to_include_extensions:
        return True

    if has_include_extension(path, include_extensions):
        return True
    if path.suffix == '' and sniff_shebang(path, shebang_prefixes):
        return True
    if path.name in include_basenames:
        return True

    return False


def iter_surface_files(
    root: str | Path,
    *,
    include_extensions: list[str] | None = None,
    shebang_prefixes: list[str] | None = None,
    exclude_prefixes: list[str] | None = None,
    exclude_basenames: list[str] | None = None,
    exclude_globs: list[str] | None = None,
    narrow_to_include_extensions: bool = False,
    include_basenames: list[str] | None = None,
    top_level_only: bool = False,
):
    """Walk `root` and yield every file `Path` that is in the percolation surface.

    `top_level_only` yields `root`'s own files and descends nowhere: the surface of
    a flat-mirror row, whose dest root holds every sibling row's subtree. Without it
    a root row scans subtrees it never writes, so it fails on a sibling's stale
    residual its own run cannot fix.

    Single discovery routine (§ Anti-scope) -- every consumer's include/exclude lists
    flow through this one walk; there is no per-consumer copy of the traversal logic.
    `narrow_to_include_extensions` is forwarded unchanged to `is_in_surface` -- see that
    function's docstring and the module docstring's admission-inversion note.
    `include_basenames` is forwarded unchanged too -- see `is_in_surface`'s own
    docstring for the extensionless-basename opt-in it adds.

    Symlinked directories are never descended into (`os.walk(followlinks=False)`), and a
    symlinked file entry is always excluded regardless of admission (gotcha 17,
    deliberate -- see module docstring). Directories matching an exclude-prefix are pruned
    from the walk (not merely filtered post-hoc) so excluded subtrees are never descended.

    A publish-staging leftover directory (§ `PUBLISH_STAGING_DIR_RE`) is pruned the same
    way, unconditionally -- this is the one `iter_surface_files` caller of the three
    `publish-staging-` sniff sites (§ C1 brief) with no `exclude_prefixes`-based staging
    exclusion, because a random-`mkdtemp`-suffixed staging directory name can never match
    a caller-supplied EXACT `exclude_prefixes` entry.
    """
    root = Path(root)
    include_extensions = include_extensions or []
    shebang_prefixes = shebang_prefixes or []
    exclude_prefixes = exclude_prefixes or []
    exclude_basenames = exclude_basenames or []
    exclude_globs = exclude_globs or []
    include_basenames = include_basenames or []

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [] if top_level_only else [
            d for d in dirnames
            if d not in exclude_prefixes
            and not PUBLISH_STAGING_DIR_RE.search(d)
            and not (Path(dirpath) / d).is_symlink()
        ]

        for filename in filenames:
            candidate = Path(dirpath) / filename
            if is_in_surface(
                root,
                candidate,
                include_extensions=include_extensions,
                shebang_prefixes=shebang_prefixes,
                exclude_prefixes=exclude_prefixes,
                exclude_basenames=exclude_basenames,
                exclude_globs=exclude_globs,
                narrow_to_include_extensions=narrow_to_include_extensions,
                include_basenames=include_basenames,
            ):
                yield candidate
