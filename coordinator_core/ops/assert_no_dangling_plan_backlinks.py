r"""
coordinator_core.ops.assert_no_dangling_plan_backlinks — AC9 gate for
programmatic terminal-plan archival, extended (C5) to a full citation-surface
gate: id-form resolution + path-form-outside-grandfathering, alongside the
original moved-plan path-form heal.

Purpose: after the C4 backfill moves a terminal plan
docs/plans/<name>.md -> archive/specs/YYYY-MM/<name>.md, every spec_backlink
that still cites the old docs/plans/ path dangles. This gate asserts ZERO
such dangling backlinks in active doctrine surfaces, and --fix repoints them
to the archive location.

C5 adds two more axes to the SAME gate, both driven by C1's resolver
(`coordinator_core.ops.spec_backlink_resolve`) rather than a second
resolution path:

  - id-form axis: a `pln-*`/`dlv-*` citation on a spec_backlink line that
    resolves to nothing (MISS) or to more than one record (AMBIGUITY) is a
    gate failure. AMBIGUITY is treated exactly like an unresolved citation
    (AC6) -- never a best-guess pick.
  - path-form-outside-grandfathering axis: a `docs/plans/...` path-form
    citation on a spec_backlink line whose target RESOLVES locally (the
    C1 index has a real id for that record) should have been converted to
    id-form and is a gate failure. A path-form citation whose target does
    NOT resolve locally is grandfathered (it is either a C7 cross-repo
    citation of a peer-repo plan, or a C8 unresolvable/disposed citation --
    both cases where no local id exists to convert to) and passes. This
    axis only ever inspects `docs/plans/...` citations (`_PLAN_PATH_RE`) --
    an `archive/specs/...` citation is out of scope. Known gap
    (review-integration, deferred to debt-backlog): `--fix` heals a
    dangling citation path-to-path (`docs/plans/<base>.md` ->
    `archive/specs/YYYY-MM/<base>.md`), never path-to-id -- once healed,
    the resulting `archive/specs/...` citation is permanently invisible to
    this axis even though it "should be id-form" by the same rule. Path-to-id
    conversion is C3's rewriter's job, not this gate's; duplicating it inside
    `--fix` would fork that mechanism.
    Dedup: a citation still at `docs/plans/<base>` for a plan that has since
    moved is reported once, under DANGLING (via the moved-plan axis) -- it is
    filtered out of this axis's results in `main()` so the same citation
    never surfaces under both DANGLING and UNGRANDFATHERED-PATH with two
    conflicting remediations.

Scope (both detect and --fix):
  - spec_backlink CONVENTION lines only -- a line matching /spec.?backlink/i.
    Incidental prose mentions ("Phase 2d reverses docs/plans/X") are NOT
    rewritten; only the citation convention is healed.
  - A cited plan counts as "moved" iff it exists under archive/specs/ AND no
    longer exists at docs/plans/<name>.md.
  - Excluded trees: archive/ (terminal paper trail) and dist/ (generated
    publish artifacts) anywhere in the path, tasks/, state/, build/,
    scratch/, scratchpad/, and node_modules/ (ephemera / gitignored build
    and session-scratch artifacts) at the path root, and plan review
    sidecars (<plan-stem>.<tag>.md, detected by an extra dot in the stem).
    The candidate file set is now derived from `git ls-files` (tracked +
    untracked-but-not-.gitignore'd) when `root` sits inside a git worktree
    (C5-fix): this excludes any gitignored tree -- scratch/, scratchpad/,
    build/, or a future one nobody's hand-added a prefix for -- without a
    hand-maintained prefix list drifting from .gitignore. The
    `_EXCLUDED_ROOT_PREFIXES` tuple (extended by this same fix) is now only
    a BACKSTOP: it still applies post-hoc to whatever the file walk
    produces, so (a) a test fixture root that is not itself a git worktree
    (every `tmp_path` in this module's test suite) still gets correct
    exclusion via the full-filesystem-walk fallback, and (b) a file that IS
    git-tracked despite living under one of these trees (e.g. committed
    before a .gitignore rule existed) is still excluded.

Exit codes (parity-critical):
  0 -- no dangling backlinks, no unresolved/ambiguous id-form citations, and
       no ungrandfathered path-form citations (or nothing to check / --fix
       healed everything). The "nothing to check" exit-0 case is scoped to
       the moved-plan (mvpath/hits) axis ONLY: when archive/specs/ doesn't
       exist yet, id-form and path-form-ungrandfathered are still fully
       scanned (there is no "nothing to check" shortcut for those two axes).
  1 -- >=1 failure found in any axis, --fix not requested (--fix only heals
       the moved-plan path-form axis; a residual id-form/ungrandfathered
       failure after --fix still returns 1), OR the scan could not read
       every candidate file (fail-closed -- see _scan_dangling)

Port of: assert-no-dangling-plan-backlinks.sh (DoE b5a4192c, 2026-07-20).
Spec backlink: archive/specs/2026-06/2026-06-23-programmatic-terminal-plan-archival.md § AC9 / C6
               pln-spec-backlinks-cite-a-stable-d-451b3e § C5

Negative-spec:
    - stdout/stderr split is preserved from the bash oracle: the "OK: ..."
      short-circuit messages and the --fix "healed N ..." summary go to
      stdout; the per-file "DANGLING in: ..." detail lines and the final
      "FAIL: N dangling ..." summary go to stderr. Callers that only capture
      stdout (as the bash oracle's own callers do) rely on this split.
    - Known bash-oracle quirk NOT reproduced here: under `set -u`, a bash
      associative array (`declare -A MVPATH`) that is declared but never
      assigned an element is treated by some bash builds as "unbound" when
      `${#MVPATH[@]}` is referenced, which silently skips the intended
      "OK: no moved plans" early-return message (the script still reaches
      the correct final "OK: no dangling ..." / exit 0 outcome, just via a
      different code path and a different message). This is an accidental
      artifact of bash's empty-associative-array/`set -u` interaction, not
      designed behavior -- the `[[ "${#MVPATH[@]}" -eq 0 ]] && { echo "OK:
      no moved plans"; exit 0; }` line's own intent is unambiguous. This
      Python port implements the DESIGNED behavior (prints "OK: no moved
      plans" and returns 0) rather than the bash accident; the two paths are
      exit-code- and dangling-count-identical (both 0 / no dangling), only
      the human-facing message text differs in this one edge case.
    - The candidate-line filter is two-stage, matching the bash oracle's
      two-stage grep exactly: a line must match /spec.?backlink/i (any
      single character, including none, between "spec" and "backlink")
      AND contain the literal substring "docs/plans/". Only lines passing
      BOTH stages are scanned for the stricter dated-plan-path regex
      (`docs/plans/YYYY-MM-DD-slug.md`) that actually extracts candidates.
      The id-form axis and the path-form-ungrandfathered axis reuse this
      same two-stage BACKLINK_LINE_RE gate (the id-form axis drops the
      "docs/plans/" substring requirement, since an id-form citation never
      carries that substring).
    - Exclusion checks run on already-matched hits, never on the recursive
      file walk itself -- mirroring the bash oracle's own file-then-line
      order (broad grep first, exclusion filter second) so a symlinked or
      oddly-named file that WOULD match the walk is still excluded exactly
      the same way the bash version excludes it post-grep.
    - The `perl -i -pe` in-place edit the bash oracle used has been REMOVED
      (C5): `perl` is not in docs/reference/shell-out-carve-outs.md's closed
      list. `_fix_file` is now a naked-Python read/literal-replace/write.
      The literal (not regex) replace is the faithful port of the perl
      one-liner's `quotemeta`'d `\Q$ENV{SRC}\E` substitution.
    - The mint-at-creation check (`scan_missing_ids`) IS wired into
      `main()`'s default flow (C5-fix): a record under docs/plans/ or
      archive/specs/ carrying neither `plan_id` nor `deliverable_id` is now
      a default-flow gate failure (MISSING-ID), not just a standalone
      `--check-mint` opt-in. The 13 originally-failing fixtures were a
      fixture problem, not a reason to leave the check off: this module's
      shared `_make_moved_plan_tree` test helper now stamps its plans with
      real ids, and every fixture that deliberately exercises an id-less
      record (`scan_missing_ids`'s own direct tests, `--check-mint`'s own
      test) already asserts that record IS flagged, so nothing was silenced
      to make this pass. `scan_missing_ids` still exists as its own
      function (and `--check-mint` standalone CLI mode) for a caller that
      wants only that one axis.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.ops.backfill_deliverable_spine import (
    _DATE_PREFIX_RE as _BACKFILL_DATE_PREFIX_RE,
    _PLAN_ID_CLASSES,
    _REVIEW_SIDECAR_RE,
    classify_artifact,
    extract_deliverable_id,
    extract_plan_id,
    is_sidecar_plan as _backfill_is_sidecar_plan,
)
from coordinator_core.ops.spec_backlink_resolve import (
    _doe_root_path,
    _RECOGNIZED_PEER_REPO,
    build_index as _build_backlink_index,
    resolve_id as _resolve_id,
    resolve_path_with_index as _resolve_path_with_index,
)
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.win_portability import no_console_creationflags

# Generator-provenance declaration (generator_provenance.py). The --fix path
# rewrites whichever tracked .md file anywhere in the repo still carries a
# dangling spec_backlink citation after a plan archival move (repo-wide .md
# scan, see `_iter_all_md_files`/git-ls-files-scoped candidate gathering
# above) -- the file set is data-dependent, so declared MUTATES rather than
# a fixed GENERATES artifact.
MUTATES = ["**/*.md"]

_LOG = logging.getLogger(__name__)

_PLAN_STEM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9.-]+$")
_BACKLINK_LINE_RE = re.compile(r"spec.?backlink", re.IGNORECASE)
_PLAN_PATH_RE = re.compile(r"docs/plans/[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9.-]+\.md")
# `pln-<slug>-<hash>` / `dlv-<slug>-<hash>`, optionally `<repo>:`-qualified.
_ID_TOKEN_RE = re.compile(
    r"(?:[A-Za-z0-9_.-]+:)?(?:pln|dlv)-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?",
    re.IGNORECASE,
)

_EXCLUDED_ROOT_PREFIXES = (
    "tasks/",
    "state/",
    "build/",
    "scratch/",
    "scratchpad/",
    "node_modules/",
    # Received cross-repo memos are a peer's authored words delivered into this
    # tree, not citations this repo writes. Same rationale as `archive/`: the
    # artifact records what a sender said, so "fixing" an unqualified id inside
    # one would falsify the record rather than repair a citation. A memo body is
    # also un-actionable here by construction — the sender owns the text, and
    # this repo has no authority to rewrite it.
    #
    # NEGATIVE-SPEC: this scopes the gate to text this repo authors; it does NOT
    # relax the citation rule. A bare foreign id in claude-klabauter-authored prose (a
    # plan body, a wiki page, source) still fails, and must be repo-qualified
    # per `docs/wiki/spec-backlink-convention.md`.
    "cross-repo/",
)


def _is_sidecar(basename_md: str) -> bool:
    """A plan-shaped basename with an extra dot in its stem is a sidecar
    (<plan-stem>.<tag>.md), not a plan itself."""
    stem = basename_md[:-3] if basename_md.endswith(".md") else basename_md
    return "." in stem


def _is_excluded_path(rel_path: str) -> bool:
    """Mirrors the bash oracle's two exclusion checks:
    `case "/$file" in */archive/*|*/dist/*) continue;; esac` (anywhere in
    path) and `case "$file" in tasks/*|state/*) continue;; esac` (root only)."""
    slashed = "/" + rel_path
    if "/archive/" in slashed or "/dist/" in slashed:
        return True
    if rel_path.startswith(_EXCLUDED_ROOT_PREFIXES):
        return True
    return False


def _git_tracked_md_files(root: str) -> Optional[List[str]]:
    """Root-relative, forward-slash, sorted .md paths under `root` sourced
    from `git ls-files` (tracked + untracked-but-not-.gitignore'd), or
    `None` if `root` is not inside a git worktree / git is unavailable
    (the caller falls back to a full filesystem walk in that case -- every
    `tmp_path` fixture in this module's test suite hits that fallback,
    since a pytest tmp dir is not itself a git worktree).

    C5-fix: this is the PRIMARY exclusion mechanism for gitignored trees
    (scratch/, scratchpad/, build/, ...) -- see the module docstring's
    "Excluded trees" paragraph for why this beats a hand-maintained prefix
    tuple. `_EXCLUDED_ROOT_PREFIXES` / `_is_excluded_path` still runs on
    top of this as a backstop."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.md"],
            cwd=root, capture_output=True, timeout=30,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.decode("utf-8", errors="replace")
    paths = [p for p in raw.split("\0") if p]
    # `--cached` lists index entries even when a concurrent session's
    # uncommitted edit has staged-added-then-working-tree-deleted a path
    # (git status "AD") -- filter to files that actually exist on disk so
    # this candidate set matches the os.walk fallback's own semantics (it
    # can only ever see files that are actually there).
    existing = [p for p in paths if os.path.isfile(os.path.join(root, p))]
    return sorted(p.replace(os.sep, "/") for p in existing)


def _find_all_md_files(root: str) -> List[str]:
    """Root-relative, forward-slash, sorted .md paths under `root` (all
    trees included -- exclusion is applied by the caller per-hit, matching
    the bash oracle's grep-then-filter order). Sourced from git when
    possible (see `_git_tracked_md_files`); falls back to a full
    filesystem walk otherwise."""
    tracked = _git_tracked_md_files(root)
    if tracked is not None:
        return tracked
    out: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out.append(rel)
    out.sort()
    return out


def _build_moved_plan_map(root: str) -> Dict[str, str]:
    """basename -> root-relative archive/specs/... path, for every plan that
    now lives under archive/specs/ AND no longer lives at docs/plans/<base>."""
    archive_specs = os.path.join(root, "archive", "specs")
    mvpath: Dict[str, str] = {}
    for rel in _find_all_md_files(archive_specs):
        full_rel = "archive/specs/" + rel
        base = os.path.basename(rel)
        if _is_sidecar(base):
            continue
        if os.path.exists(os.path.join(root, "docs", "plans", base)):
            continue  # still live -> not moved
        mvpath[base] = full_rel
    return mvpath


def _resolve_root(explicit_root: Optional[str]) -> Optional[str]:
    if explicit_root:
        return explicit_root
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        print(f"skip: _resolve_root: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def _scan_dangling(
    root: str, mvpath: Dict[str, str], unreadable: List[str]
) -> List[Tuple[str, str, str, str]]:
    """Returns a list of (rel_file, matched_ref, base, dest) tuples, ONE per
    unique (file, base) pair -- mirrors the bash oracle's SEEN_PAIRS dedup
    ("a plan cited on several spec_backlink lines in one file is one fix,
    not N").

    `unreadable` is appended to (not returned) with any candidate file this
    scan could not open — see BEHAVIOUR CHANGE note below.

    BEHAVIOUR CHANGE (2026-07-22, break-class fix): an unopenable file was
    previously silently excluded from the scan via a bare `continue`, so this
    AC9 commit gate could return 0 ("no dangling backlinks") while a dangling
    backlink existed in the unreadable file. `main()` now treats any
    unreadable file as scan-incomplete and fails loud.
    """
    results: List[Tuple[str, str, str, str]] = []
    seen_pairs = set()
    for rel_file in _find_all_md_files(root):
        if _is_excluded_path(rel_file):
            continue
        base_name = os.path.basename(rel_file)
        if _is_sidecar(base_name):
            continue
        full_path = os.path.join(root, rel_file)
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError as exc:
            print(
                f"WARNING: unreadable file excluded from dangling-backlink scan: {full_path}: {exc}",
                file=sys.stderr,
            )
            unreadable.append(rel_file)
            continue
        for line in lines:
            if not _BACKLINK_LINE_RE.search(line):
                continue
            if "docs/plans/" not in line:
                continue
            for match in _PLAN_PATH_RE.findall(line):
                base = os.path.basename(match)
                dest = mvpath.get(base)
                if not dest:
                    continue  # cited plan not moved
                key = (rel_file, base)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                results.append((rel_file, match, base, dest))
    return results


# ---------------------------------------------------------------------------
# C5: id-form axis + path-form-outside-grandfathering axis.
# Both driven by C1's resolver; the index is built exactly ONCE per
# invocation (never per-citation) -- see spec_backlink_resolve's own
# "does not glob per-citation" negative-spec, mirrored here.
# ---------------------------------------------------------------------------


def _iter_backlink_lines(root: str):
    """Yield (rel_file, line) for every spec_backlink CONVENTION line in
    every non-excluded, non-sidecar .md file under `root`. Files that cannot
    be opened are silently skipped here -- `_scan_dangling`'s own pass is
    the fail-closed unreadable-file detector; this generator is used only
    for the two axes that ADD to that scan, never in place of it."""
    for rel_file in _find_all_md_files(root):
        if _is_excluded_path(rel_file):
            continue
        base_name = os.path.basename(rel_file)
        if _is_sidecar(base_name):
            continue
        full_path = os.path.join(root, rel_file)
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in lines:
            if _BACKLINK_LINE_RE.search(line):
                yield rel_file, line


def scan_id_form_citations(
    root: str, worktree_root: Path, local_index
) -> List[Tuple[str, str, str]]:
    """Return [(rel_file, id, outcome), ...] for every id-form citation on a
    spec_backlink line that resolves to MISS or AMBIGUITY. `local_index` is
    the already-built (once per invocation) C1 index for `worktree_root`.

    A `<repo>:`-qualified id lazily builds (and caches, once) the peer-repo
    index -- mirrors `spec_backlink_resolve.resolve()`'s own laziness, just
    batched across every citation in this invocation instead of rebuilt per
    call.
    """
    results: List[Tuple[str, str, str]] = []
    seen = set()
    peer_index_cache: Dict[str, object] = {}
    for rel_file, line in _iter_backlink_lines(root):
        for m in _ID_TOKEN_RE.finditer(line):
            token = m.group(0)
            key = (rel_file, token)
            if key in seen:
                continue
            seen.add(key)
            if ":" in token:
                _repo, _sep, bare_id = token.partition(":")
                # Mirrors spec_backlink_resolve.resolve()'s own qualifier
                # validation (Review: code-reviewer P3) -- an unrecognized
                # `<repo>:` qualifier is a typed miss, never silently routed
                # to the DoE-claude peer index.
                if _repo != _RECOGNIZED_PEER_REPO:
                    outcome = {"outcome": "miss"}
                else:
                    peer_root = _doe_root_path()
                    if peer_root is None or not peer_root.is_dir():
                        outcome = {"outcome": "miss"}
                    else:
                        if "index" not in peer_index_cache:
                            peer_index_cache["index"] = _build_backlink_index(peer_root)
                        outcome = _resolve_id(peer_index_cache["index"], bare_id)
            else:
                outcome = _resolve_id(local_index, token)
            if outcome["outcome"] in ("miss", "ambiguity"):
                results.append((rel_file, token, outcome["outcome"]))
    return results


def scan_path_form_ungrandfathered(
    root: str, worktree_root: Path, local_index
) -> List[Tuple[str, str, str]]:
    """Return [(rel_file, cited_path, outcome), ...] for every path-form
    (`docs/plans/...`) citation on a spec_backlink line whose target
    RESOLVES locally (HIT -- should have been id-form) or is AMBIGUOUS
    (multiple local candidates share the basename). A citation resolving to
    MISS is grandfathered (C7 cross-repo or C8 unresolvable/disposed -- no
    local id exists to convert to) and is NOT returned.
    """
    results: List[Tuple[str, str, str]] = []
    seen = set()
    for rel_file, line in _iter_backlink_lines(root):
        if "docs/plans/" not in line:
            continue
        for match in _PLAN_PATH_RE.findall(line):
            key = (rel_file, match)
            if key in seen:
                continue
            seen.add(key)
            outcome = _resolve_path_with_index(local_index, worktree_root, match)
            if outcome["outcome"] in ("hit", "ambiguity"):
                results.append((rel_file, match, outcome["outcome"]))
    return results


# ---------------------------------------------------------------------------
# C5: mint-at-creation assertion (standalone -- see module docstring
# "Negative-spec" for why this is NOT wired into main()'s default flow).
# ---------------------------------------------------------------------------


def scan_missing_ids(root: str) -> List[str]:
    """Return root-relative paths of every `docs/plans/`/`archive/specs/`
    record carrying NEITHER `plan_id` NOR `deliverable_id`.

    Reuses `backfill_deliverable_spine`'s own exclusion predicates
    (`is_sidecar_plan`, the review-sidecar regex, the dated-prefix check for
    `docs/plans/`) rather than re-deriving rules that would drift from the
    backfill's own carry/skip logic -- an undated `docs/plans/` document
    (INDEX.md, README.md, config-slash-*.md, ...) and a review sidecar are
    never real plan records and must never trip this check. A record
    carrying the one authored `plan_id: null  # stamped by a real /plan run
    — do NOT hand-fabricate` placeholder also does not trip it:
    `extract_plan_id` reads that comment-bearing line as a non-empty string
    (not a bare YAML null), the same "already has an id" read the backfill's
    own `run_plan_id_leg` relies on to skip it -- this function reuses that
    exact reader rather than re-deriving null detection.
    """
    missing: List[str] = []
    for base_dir, recursive in (
        (os.path.join(root, "docs", "plans"), False),
        (os.path.join(root, "archive", "specs"), True),
    ):
        if not os.path.isdir(base_dir):
            continue
        if recursive:
            candidates = []
            for dirpath, _dirnames, filenames in os.walk(base_dir):
                for fn in filenames:
                    if fn.endswith(".md"):
                        candidates.append(os.path.join(dirpath, fn))
        else:
            candidates = [
                os.path.join(base_dir, fn)
                for fn in sorted(os.listdir(base_dir))
                if fn.endswith(".md") and os.path.isfile(os.path.join(base_dir, fn))
            ]
        for full_path in sorted(candidates):
            norm = full_path.replace(os.sep, "/")
            artifact_class = classify_artifact(norm)
            if artifact_class not in _PLAN_ID_CLASSES:
                continue
            if _backfill_is_sidecar_plan(full_path):
                continue
            base_name = os.path.basename(full_path)
            if _REVIEW_SIDECAR_RE.search(base_name):
                continue
            if artifact_class == "plan" and not _BACKFILL_DATE_PREFIX_RE.match(base_name):
                continue  # undated docs/plans/ document -- not a real plan record
            plan_id = extract_plan_id(full_path, artifact_class)
            deliverable_id = extract_deliverable_id(full_path, artifact_class)
            if not plan_id and not deliverable_id:
                missing.append(os.path.relpath(full_path, root).replace(os.sep, "/"))
    return missing


# ---------------------------------------------------------------------------
# --fix: naked-Python literal replace (C5 -- perl -i -pe removed, not in the
# closed shell-out carve-out list).
# ---------------------------------------------------------------------------


def _fix_file(full_path: str, src: str, dst: str) -> bool:
    r"""Literal-replace docs/plans/<base> -> <dest>, ONLY on spec_backlink
    convention lines. Faithful naked-Python port of the bash oracle's perl
    one-liner (`s/\Q$ENV{SRC}\E/$ENV{DST}/g if /spec.?backlink/i`) -- the
    perl form used `quotemeta` (literal match), so this is a literal
    `str.replace`, NOT `re.sub`.

    `newline=""` on both the read and the write preserves the file's
    original line endings byte-for-byte (Windows CRLF included) rather than
    normalizing to `\n` -- important since this function may touch files
    authored on either platform.
    """
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            content = fh.read()
    except OSError as exc:
        print(f"ERROR: could not read {full_path} for --fix: {exc}", file=sys.stderr)
        return False

    lines = content.splitlines(keepends=True)
    changed = False
    out_lines: List[str] = []
    for line in lines:
        if _BACKLINK_LINE_RE.search(line) and src in line:
            line = line.replace(src, dst)
            changed = True
        out_lines.append(line)

    if not changed:
        return True  # nothing on a spec_backlink line to heal in this file

    try:
        with open(full_path, "w", encoding="utf-8", newline="") as fh:
            fh.write("".join(out_lines))
    except OSError as exc:
        print(f"ERROR: could not write {full_path} for --fix: {exc}", file=sys.stderr)
        return False

    # DR-276: declared AFTER the in-place edit lands, never before.
    declare_write(full_path)
    return True


def main(argv: List[str]) -> int:
    root_arg: Optional[str] = None
    fix = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--root":
            root_arg = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        if arg == "--fix":
            fix = True
            i += 1
            continue
        if arg == "--check-mint":
            root = _resolve_root(root_arg)
            if not root:
                return 0
            missing = scan_missing_ids(root)
            if missing:
                _report_missing_ids(missing)
                return 1
            print("OK: every plan/archived-spec record carries an id")
            return 0
        i += 1

    root = _resolve_root(root_arg)
    if not root:
        return 0

    # C5-fix: mint-at-creation is now part of the default gate, run before
    # the archive/specs early-return so an id-less docs/plans/ record still
    # fails even in a corpus with no archive/specs/ tree yet.
    missing_ids = scan_missing_ids(root)

    # C5-fix (P1, review-integration): the id-form and path-form-ungrandfathered
    # axes must ALSO run before the archive/specs early-return below — a corpus
    # that has never archived a plan (no archive/specs/ tree yet) can still
    # carry a dangling pln-/dlv- id-form citation or an ungrandfathered
    # path-form citation, and this gate must never report those axes as "OK"
    # when it in fact never evaluated them. Only the moved-plan (mvpath/hits)
    # axis genuinely has nothing to check without archive/specs/.
    worktree_root = Path(root)
    local_index = _build_backlink_index(worktree_root)
    id_failures = scan_id_form_citations(root, worktree_root, local_index)
    path_ungrandfathered = scan_path_form_ungrandfathered(root, worktree_root, local_index)

    if not os.path.isdir(os.path.join(root, "archive", "specs")):
        id_and_path_failed = _report_id_and_path_failures(id_failures, path_ungrandfathered)
        if missing_ids:
            _report_missing_ids(missing_ids)
        if id_and_path_failed or missing_ids:
            return 1
        print("OK: no archive/specs/ — nothing to heal (moved-plan axis only; id-form/path-form-ungrandfathered checked)")
        return 0

    mvpath = _build_moved_plan_map(root)

    unreadable: List[str] = []
    hits = _scan_dangling(root, mvpath, unreadable)

    # BEHAVIOUR CHANGE (2026-07-22, break-class fix): restores this AC9 gate's
    # intended assertion — an incomplete scan can never be reported as "no
    # dangling backlinks" (see _scan_dangling docstring).
    if unreadable:
        for rel_file in unreadable:
            print(f"UNSCANNABLE: {rel_file}", file=sys.stderr)
        print(
            f"FAIL: {len(unreadable)} file(s) could not be scanned for dangling plan "
            "backlinks — assertion cannot be made (fail-closed)",
            file=sys.stderr,
        )
        return 1

    # Dedup (review-integration P2): a citation still at docs/plans/<base> for
    # a plan that has since moved is DANGLING (via `hits`, remediation:
    # --fix). Without this filter the SAME (rel_file, cited_path) pair would
    # also surface under UNGRANDFATHERED-PATH (path_ungrandfathered's
    # basename-fallback HIT), with a conflicting "should be id-form"
    # remediation -- one citation, one failure header.
    dangling_pairs = {(rel_file, match) for rel_file, match, _base, _dest in hits}
    path_ungrandfathered = [
        entry for entry in path_ungrandfathered
        if (entry[0], entry[1]) not in dangling_pairs
    ]

    if not mvpath and not hits and not id_failures and not path_ungrandfathered and not missing_ids:
        print("OK: no moved plans")
        return 0

    if fix:
        fixed = 0
        for rel_file, _match, base, dest in hits:
            full_path = os.path.join(root, rel_file)
            src = "docs/plans/" + base
            if _fix_file(full_path, src, dest):
                fixed += 1
        print(f"healed {fixed} dangling backlink citation(s)")
        # Latent-bug fix (C5-fix, same function): id_failures/
        # path_ungrandfathered were computed on PRE-fix file content. A
        # citation this loop just healed (docs/plans/... -> archive/specs/
        # ...) no longer matches the docs/plans/ path-form pattern, so it
        # must not be reported as a failure from a scan that predates the
        # write. Rescan post-fix rather than trusting the stale snapshot.
        post_fix_index = _build_backlink_index(worktree_root)
        post_fix_id_failures = scan_id_form_citations(root, worktree_root, post_fix_index)
        post_fix_path_ungrandfathered = scan_path_form_ungrandfathered(root, worktree_root, post_fix_index)
        post_fix_missing_ids = scan_missing_ids(root)
        if post_fix_id_failures or post_fix_path_ungrandfathered or post_fix_missing_ids:
            _report_id_and_path_failures(post_fix_id_failures, post_fix_path_ungrandfathered)
            if post_fix_missing_ids:
                _report_missing_ids(post_fix_missing_ids)
            return 1
        return 0

    dangling = len(hits)
    if dangling > 0:
        seen_files: List[str] = []
        for rel_file, match, _base, dest in hits:
            if rel_file not in seen_files:
                print(f"DANGLING in: {rel_file}", file=sys.stderr)
                seen_files.append(rel_file)
            print(f"  {match} → {dest}", file=sys.stderr)
        print(
            f"FAIL: {dangling} dangling plan backlink(s) to moved docs/plans/ paths "
            "— run with --fix",
            file=sys.stderr,
        )

    id_and_path_failed = _report_id_and_path_failures(id_failures, path_ungrandfathered)
    if missing_ids:
        _report_missing_ids(missing_ids)

    if dangling > 0 or id_and_path_failed or missing_ids:
        return 1

    print("OK: no dangling plan backlinks to moved docs/plans/ paths")
    return 0


def _report_missing_ids(missing: List[str]) -> None:
    """Print the mint-at-creation (MISSING-ID) failure block to stderr --
    shared by `--check-mint` standalone mode and the C5-fix default-flow
    wiring in `main()`."""
    for rel in missing:
        print(f"MISSING-ID: {rel}", file=sys.stderr)
    print(
        f"FAIL: {len(missing)} record(s) with neither plan_id nor deliverable_id",
        file=sys.stderr,
    )


def _report_id_and_path_failures(
    id_failures: List[Tuple[str, str, str]],
    path_ungrandfathered: List[Tuple[str, str, str]],
) -> bool:
    """Print the id-form and path-form-ungrandfathered failure blocks to
    stderr. Returns True iff either axis has >=1 failure."""
    if id_failures:
        for rel_file, token, outcome in id_failures:
            print(f"UNRESOLVED-ID in: {rel_file}", file=sys.stderr)
            print(f"  {token} → {outcome}", file=sys.stderr)
        print(
            f"FAIL: {len(id_failures)} id-form spec_backlink citation(s) resolve to "
            "MISS/AMBIGUITY",
            file=sys.stderr,
        )
    if path_ungrandfathered:
        for rel_file, cited_path, outcome in path_ungrandfathered:
            print(f"UNGRANDFATHERED-PATH in: {rel_file}", file=sys.stderr)
            print(f"  {cited_path} → {outcome} (resolves locally — should be id-form)", file=sys.stderr)
        print(
            f"FAIL: {len(path_ungrandfathered)} path-form spec_backlink citation(s) "
            "resolve locally and are outside the grandfathered set",
            file=sys.stderr,
        )
    return bool(id_failures or path_ungrandfathered)


def run_gate(root: str) -> int:
    """Programmatic (non-CLI) entry point for a caller that wants the full
    gate (moved-plan path-form + id-form + path-form-ungrandfathered),
    read-only, without going through `sys.argv`. Used by
    `fleet.archive_completed_plans`'s act phase (C5 AC7) as POST-HOC AUDIT
    VISIBILITY -- the citation surface is checked (and a failure logged
    loudly) on every archive act call that lands a move, not only when the
    CLI is invoked by hand, but the check runs strictly AFTER the git-mv +
    commit and never blocks or reverses it. See archive_plans._handle_act's
    own comment for the enforcement-vs-audit rationale."""
    return main(["--root", root])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
