"""
coordinator_core.ops.record_history — file-set resolution and single-pass git
log derivation for record history.

Purpose: given a record type, resolve (a) a SHORT directory pathspec safe to
hand to ``git log`` (never the raw ``_TYPE_TO_GLOB`` value — see Negative-spec
below), and (b) the EXACT set of on-disk files that type's own glob matches,
reusing ``records_query``'s own collection walker rather than re-deriving glob
semantics a second time. Callers post-filter a git-reported path list against
this set in pure Python. A single ``git log -p -U0`` pass per type then
recovers per-file lifecycle events (creation, rename chains, and frontmatter
field transitions) directly from the patch text — never a blob read, never a
second invocation per record.

Spec backlink: docs/plans/2026-08-20-a-time-axis-for-any-record-type.md
  § C1a (AC5, AC5a, AC5b), § C1b (AC1, AC2, AC3, AC4)

Negative-spec (why the glob string itself is never handed to git as a
pathspec): git pathspec ``*`` crosses ``/``, Python ``glob``/``fnmatch`` ``*``
does not. Probed 2026-08-20: ``git ls-files -- 'docs/*.md'`` returns 898 paths
BELOW ``docs/`` — a subdirectory created under any governance dir would
silently widen a naive pathspec's match set with no visible symptom. This
module instead derives the FIXED (non-wildcard) directory prefix of the glob
as the git pathspec, and lets callers post-filter git's output against the
exact set collected here in Python — over-collection is removed by the
filter, never avoided by hoping the pathspec was narrow. See
``records_query.py``'s own ``_collect_files``/``_walk_glob_segments`` for the
glob-walking implementation this module reuses rather than reimplements.

**Sentinel framing.** The git log format string opens with a NUL byte
(``%x00``) rather than a printable delimiter, because this corpus's own
records quote frontmatter and diff text in their bodies (decisions and
handoffs both do) — a printable sentinel is a live collision hazard; a NUL
byte cannot occur in tracked text, closing it by construction. A second NUL
(``\x01``... no — see ``_FIELD_SEP``) separates the per-commit sha/author/date
header fields.

**Wipe/restore safety.** A transition is derived ONLY from a matched
``-field:``/``+field:`` pair inside the SAME commit's patch for the SAME
file — never by comparing field values across separate commits. Whole-file
add (``new file mode``) and delete (``deleted file mode``) events are
excluded from transition parsing entirely; a record's ``created_at`` is the
oldest add event in its rename chain (a restore-after-wipe add is never the
oldest, because a delete necessarily precedes it in that same chain).

**Hunk-position bound.** ``-U0`` emits no context lines, so nothing but the
``@@ -a,b +c,d @@`` hunk header distinguishes a real frontmatter field from
an identically-shaped line deep in a record's body (a fenced YAML example,
for instance). This module tracks the running line number from each hunk
header and accepts a field line only at column 0 (no leading whitespace —
this also excludes a nested ``status:`` under a YAML mapping) and at or
below line 60. That bound is a generous constant, not a proof: the historical
frontmatter-fence position is not recoverable from patch text alone without a
blob read.

**Known-absent (F7):** ``git log -p`` emits no patch for merge commits (a
commit whose diff is empty in this corpus's one measured case), and
``git log -- <path>`` applies history simplification by default, pruning
TREE-SAME parents — a side-branch change later reconciled by merge can
vanish from this derivation. ``-m``/``--cc`` is deliberately NOT added, since
it multiplies patch volume against this plan's cross-repo cost ceiling. This
is a known-absent class, not a "no change happened" claim.

Second known-absent class -- CURRENT-RECORDS-ONLY SCOPE. The event streams
cover only files still present in the record type's own glob, because that
is the set `resolve_record_files` collects. History belonging to a record
since archived out of that directory is NOT reported. Measured on this
corpus 2026-08-20: `state/sizings` carries 319 real `status` transitions
across all history, of which 114 belong to paths no longer under
`state/sizings/` -- so this surface reports ~202 rather than the ~315 an
all-history count gives. Neither number is wrong; they answer different
questions. A consumer computing a rate or a latency distribution over a
time window that predates an archival sweep will under-count unless it
says so, so this scope is stated here, in the op's output contract, and in
the handover memo rather than left for a reader to rediscover from a
surprising total.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from coordinator_core.ipc import register_op
from coordinator_core.ops import records_query
from coordinator_core.win_portability import no_console_creationflags


class UnsupportedRecordTypeError(ValueError):
    """Raised when a record type's glob cannot be expressed by this module.

    Carries the full supported-type list so callers can report it directly —
    per AC5, "a type whose glob cannot be expressed exactly raises a named
    error listing what IS supported."
    """

    def __init__(self, record_type: str, supported: list[str]):
        self.record_type = record_type
        self.supported = supported
        super().__init__(
            f'record_history: unsupported record type {record_type!r}; '
            f'supported types: {", ".join(supported)}'
        )


# Synthetic types (`handoff-ledger`, `research-claim`) yield N records per
# source FILE — `records_query._collect_files` is never called for them (see
# `_SYNTHETIC_TYPES`'s own docstring); there is no one-file-one-record glob
# for this module to walk, so they are excluded from the supported set rather
# than silently mishandled.
_UNSUPPORTED_HISTORY_TYPES: frozenset[str] = records_query._SYNTHETIC_TYPES


def supported_record_types() -> frozenset[str]:
    """The record types this module can resolve a file set + pathspec for.

    Every ``_TYPE_TO_GLOB`` entry except the synthetic (N-records-per-file)
    types — see `_UNSUPPORTED_HISTORY_TYPES`.
    """
    return frozenset(records_query._TYPE_TO_GLOB) - _UNSUPPORTED_HISTORY_TYPES


def _require_supported(record_type: str) -> None:
    if record_type not in supported_record_types():
        raise UnsupportedRecordTypeError(record_type, sorted(supported_record_types()))


def type_directory_pathspec(record_type: str) -> str:
    """The short, fixed (no-wildcard) directory pathspec for ``record_type``.

    Derived by taking the ``_TYPE_TO_GLOB`` value's path segments up to (not
    including) the first segment containing a ``*``. A glob with no wildcard
    segment at all (e.g. ``tracker``'s ``docs/project-tracker.md``) drops its
    trailing filename segment instead, since a pathspec must name a directory
    or a real path — never a bare filename glob it doesn't have.

    This is deliberately SHORTER than the full glob (never the glob itself —
    see module Negative-spec) so it is safe to hand to ``git log -- <pathspec>``
    as one directory-scoped argument; callers still MUST post-filter the
    result against `resolve_record_files`'s exact set, since this pathspec is
    intentionally permissive, not exact.
    """
    _require_supported(record_type)
    glob_pat = records_query._TYPE_TO_GLOB[record_type]
    parts = glob_pat.split('/')
    fixed: list[str] = []
    for part in parts:
        if '*' in part:
            break
        fixed.append(part)
    if len(fixed) == len(parts):
        # No wildcard segment anywhere (e.g. 'docs/project-tracker.md') — the
        # last segment is a literal filename, not a directory; a pathspec
        # names the containing directory instead.
        fixed = fixed[:-1]
    return '/'.join(fixed)


def resolve_record_files(worktree_root: Path, record_type: str) -> frozenset[str]:
    """The EXACT set of on-disk files ``record_type``'s glob matches.

    Reuses ``records_query._collect_files`` (the same walker
    ``records.query`` itself uses) rather than re-deriving glob-matching
    semantics — the single place where the AC5a git-vs-Python ``*`` hazard is
    avoided rather than re-encoded a second time to drift out of sync.

    Returns worktree-relative POSIX paths (``/``-separated on every
    platform, including Windows) so the set is directly comparable against
    git's own path output.
    """
    _require_supported(record_type)
    files = records_query._collect_files(worktree_root, record_type)
    return frozenset(p.relative_to(worktree_root).as_posix() for p in files)


def partition_known_files(
    candidate_paths: list[str], known_files: frozenset[str],
) -> tuple[list[str], list[str]]:
    """Split ``candidate_paths`` into (known, unknown) against ``known_files``.

    Pure post-filter — the AC5 "post-filter that pass's output in Python
    against the collected set exactly" step. ``candidate_paths`` order is
    preserved within each output list; no git call, no I/O. Applied by the
    caller (C1b) to a ``git log``-reported path list, which is why this
    over-collection removal happens AFTER the git pass rather than by
    trusting a narrower pathspec up front (AC5a).
    """
    known: list[str] = []
    unknown: list[str] = []
    for path in candidate_paths:
        (known if path in known_files else unknown).append(path)
    return known, unknown


def untracked_record_paths(
    known_files: frozenset[str], tracked_paths: frozenset[str],
) -> frozenset[str]:
    """Files present on disk (`known_files`) but absent from `tracked_paths`.

    AC5b: an on-disk record file git has never tracked (e.g. a fresh,
    uncommitted record) must be reported with an explicit
    empty/untracked marker rather than silently reading as "zero events" —
    the same outcome a genuinely-empty-but-tracked record would show. This
    function only computes the set; the marker itself is applied by the
    history-assembly caller (C1b), which is the only stage with both this
    set and the per-file event list in hand.
    """
    return frozenset(known_files) - frozenset(tracked_paths)


# --------------------------------------------------------------------------
# C1c — field policy per record type.
# --------------------------------------------------------------------------
#
# Data only, selecting which frontmatter fields are of INTEREST for a given
# type — never whether transitions are emitted at all, which stays uniform
# across every type per C1b's extractor. No branch here suppresses a real
# transition: a type absent from this table (or a field absent from its
# tuple) still has its transitions extracted by `derive_type_history`; this
# table exists for callers that want to narrow a report to the fields that
# matter for a type, not for the extractor itself.
#
# Measured on this corpus 2026-08-20 (pairing `-`/`+` within a commit,
# comment-stripped, wipe commits excluded, per C1b), ACROSS ALL HISTORY:
# `sizing-object` shows 319 real transitions and `decision` shows 14. This
# surface reports the CURRENT-RECORDS-ONLY subset, so the sizing figure it
# returns is ~202, not ~319 -- 114 belong to paths archived out of
# `state/sizings/`. Two scopes, two numbers, both correct; see the
# CURRENT-RECORDS-ONLY SCOPE note in the module docstring. `decision` is 14
# under both scopes. Earlier drafts cited 315 for the all-history sizing
# count with no scope qualifier; 319 is the re-measured value — `proposed->accepted` x9,
# `accepted->superseded` x3, `superseded->accepted` x1, `draft->proposed` x1.
# The asymmetry is a volume fact (14 vs 315), not a presence fact — a
# type-policy branch that suppressed `decision` `status` transitions would
# discard exactly the 3 supersession events the source memo asked for, so no
# such branch exists here or anywhere else in this module.
_FIELD_POLICY: dict[str, tuple[str, ...]] = {
    "sizing-object": ("status",),
    "decision": ("status", "supersedes", "superseded_by"),
}


def fields_of_interest(record_type: str) -> tuple[str, ...]:
    """The frontmatter fields of interest for ``record_type`` (C1c).

    Selection only — never a filter on whether transitions are emitted;
    see `_FIELD_POLICY`'s docstring. A type with no entry returns an empty
    tuple rather than raising, since this table is advisory metadata, not
    a gate on `derive_type_history`'s supported-type check.
    """
    return _FIELD_POLICY.get(record_type, ())


# --------------------------------------------------------------------------
# C1b — single-pass git log derivation.
# --------------------------------------------------------------------------

# `_COMMIT_SEP`/`_FIELD_SEP` are the actual bytes git EMITS for `%x00`/`%x01`
# in its own placeholder syntax — used to split the captured OUTPUT. The
# `--format=` argv string passed TO git must instead spell those placeholders
# out literally (`%x00`, not a raw NUL byte, which `CreateProcess` rejects as
# an embedded null in an argv element).
_COMMIT_SEP = "\x00"
_FIELD_SEP = "\x01"
_LOG_FORMAT = "%x00%H%x01%an%x01%aI"

# Frontmatter field line, column 0 only (no leading whitespace) — see module
# docstring's "Hunk-position bound" for why this also satisfies the
# YAML-whole-file "require column 0" requirement uniformly, with no
# type-specific branch.
_FIELD_LINE_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$')
_HUNK_HEADER_RE = re.compile(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@')
_DIFF_GIT_RE = re.compile(r'^diff --git a/(.*) b/(.*)$')

_FRONTMATTER_LINE_BOUND = 60


def _run_git_log_pass(worktree_root: Path, pathspec: str) -> str:
    """The one git spawn per type (AC2): ``git log -p -U0`` over a directory
    pathspec, sentinel-framed header, decoded permissively so a stray
    non-UTF8 byte in old history never aborts the whole pass."""
    result = subprocess.run(
        ["git", "log", f"--format={_LOG_FORMAT}", "-p", "-U0", "--", pathspec],
        cwd=str(worktree_root),
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )
    return result.stdout.decode("utf-8", errors="replace")


def _iter_commit_blocks(raw: str):
    """Yield ``(sha, author, committed_at, diff_text)`` per commit.

    ``raw`` is framed by ``_COMMIT_SEP`` (NUL) ahead of every commit,
    including the first — splitting on it and dropping the (empty) leading
    piece recovers exactly one block per commit.
    """
    for part in raw.split(_COMMIT_SEP):
        if not part:
            continue
        header, _, diff_text = part.partition("\n")
        try:
            sha, author, committed_at = header.split(_FIELD_SEP)
        except ValueError:
            # A header that does not split into exactly three fields means the
            # frame is not what we think it is (an author name carrying the
            # separator byte, a corrupted object). Dropping it silently would let
            # a record whose whole history was excluded report `events: []` --
            # indistinguishable from a record that genuinely never changed. A
            # wrong-but-plausible history is worse than a loud failure.
            raise ValueError(
                'record_history: unparseable commit header in the git log frame '
                f'({header[:120]!r}) -- refusing to silently drop the commit, '
                'because a dropped commit is indistinguishable from a quiet record'
            )
        yield sha, author, committed_at, diff_text


def _split_file_sections(diff_text: str) -> list[list[str]]:
    """Split one commit's patch text into per-file ``diff --git`` sections."""
    sections: list[list[str]] = []
    current: list[str] | None = None
    for line in diff_text.split("\n"):
        if line.startswith("diff --git "):
            if current is not None:
                sections.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        sections.append(current)
    return sections


def _normalize_field_value(raw_value: str) -> str:
    """Strip an inline YAML comment and surrounding quotes (F5).

    ``status: sized  # draft | sized | routed | shipped | declined``
    normalizes to ``sized`` so a comment-only edit compares equal to its
    prior value and is dropped as a non-transition by the caller.
    """
    stripped = raw_value.strip()
    quote = stripped[0] if stripped[:1] in ("'", '"') else None
    if quote:
        # Quoted: the comment can only start after the CLOSING quote, so a '#'
        # inside the quotes is data. Splitting on '#' first would truncate
        # `status: "url#fragment"` to `"url` and call it a value change.
        end = stripped.find(quote, 1)
        if end != -1:
            return stripped[1:end]
    return stripped.split("#", 1)[0].strip()


def _maybe_capture_field(content: str, line_no: int | None, out: dict[str, str]) -> None:
    if line_no is None or line_no > _FRONTMATTER_LINE_BOUND:
        return
    match = _FIELD_LINE_RE.match(content)
    if not match:
        return
    field, value = match.group(1), match.group(2)
    out[field] = value


def _parse_file_section(lines: list[str]) -> dict:
    """Parse one ``diff --git`` section: path identity, add/delete/rename
    markers, and the raw removed/added frontmatter field values (F2, F4,
    F6) — pairing and comment-stripping happen in the caller, uniformly,
    once every field from every hunk in this file's diff has been seen."""
    diff_match = _DIFF_GIT_RE.match(lines[0]) if lines else None
    old_path = diff_match.group(1) if diff_match else None
    new_path = diff_match.group(2) if diff_match else None
    is_new = False
    is_deleted = False
    rename_from: str | None = None
    rename_to: str | None = None
    removed: dict[str, str] = {}
    added: dict[str, str] = {}
    old_line: int | None = None
    new_line: int | None = None

    for line in lines[1:]:
        if line.startswith("new file mode"):
            is_new = True
        elif line.startswith("deleted file mode"):
            is_deleted = True
        elif line.startswith("rename from "):
            rename_from = line[len("rename from "):]
        elif line.startswith("rename to "):
            rename_to = line[len("rename to "):]
        elif line.startswith("@@"):
            hunk_match = _HUNK_HEADER_RE.match(line)
            if hunk_match:
                old_line = int(hunk_match.group(1))
                new_line = int(hunk_match.group(2))
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            pos = new_line
            if new_line is not None:
                new_line += 1
            _maybe_capture_field(line[1:], pos, added)
        elif line.startswith("-"):
            pos = old_line
            if old_line is not None:
                old_line += 1
            _maybe_capture_field(line[1:], pos, removed)

    return {
        "path": rename_to or new_path,
        "old_path": rename_from or old_path,
        "is_new": is_new,
        "is_deleted": is_deleted,
        "is_rename": bool(rename_from and rename_to),
        "removed": removed,
        "added": added,
    }


def _pair_field_transitions(removed: dict[str, str], added: dict[str, str]) -> dict[str, dict[str, str]]:
    """Pair same-commit ``-field:``/``+field:`` values into transitions
    (F2a), dropping any pair that compares equal after comment/quote
    stripping (F5)."""
    changes: dict[str, dict[str, str]] = {}
    for field in set(removed) | set(added):
        old_raw = removed.get(field)
        new_raw = added.get(field)
        if old_raw is None or new_raw is None:
            continue
        old_value = _normalize_field_value(old_raw)
        new_value = _normalize_field_value(new_raw)
        if old_value == new_value:
            continue
        changes[field] = {"from": old_value, "to": new_value}
    return changes


def derive_type_history(worktree_root: Path, record_type: str) -> list[dict]:
    """Derive one event stream per current file of ``record_type`` (AC1).

    Exactly one ``git log`` invocation (AC2) — the pathspec and known file
    set come from C1a's `type_directory_pathspec`/`resolve_record_files`;
    every event comes from parsing that single pass's patch text, never a
    second git call and never a blob read.

    Returns a list of ``{"path", "created_at", "created_by", "events"}``,
    one per file currently in `resolve_record_files`'s set, keyed by that
    file's rename chain (F6) rather than any one historical path. Each
    event is ``{"sha", "author", "committed_at", "changes"}`` where
    ``changes`` maps field name to ``{"from", "to"}`` — a record with no
    real transitions reports an empty ``events`` list by construction.
    """
    _require_supported(record_type)
    pathspec = type_directory_pathspec(record_type)
    known_files = resolve_record_files(worktree_root, record_type)
    # C1c's table is field SELECTION, not a gate on whether a type emits
    # transitions at all -- the extractor above stays uniform for every
    # type, and a type with no policy entry keeps every field it finds.
    # Membership, never truthiness: an absent entry means "keep every field",
    # while an entry mapping to () would mean "track none" -- an empty tuple is
    # falsy, so a truthiness test silently turns the second into the first.
    has_policy = record_type in _FIELD_POLICY
    tracked = frozenset(fields_of_interest(record_type))
    raw = _run_git_log_pass(worktree_root, pathspec)

    parsed: list[tuple[str, str, str, dict]] = []
    for sha, author, committed_at, diff_text in _iter_commit_blocks(raw):
        for lines in _split_file_sections(diff_text):
            info = _parse_file_section(lines)
            if info["path"] is None:
                continue
            parsed.append((sha, author, committed_at, info))

    # Pass A: build the full rename-chain alias map before grouping any
    # event, so an event recorded before a later rename still resolves to
    # the file's final canonical path (F6) rather than a stale one.
    alias: dict[str, str] = {}
    for _sha, _author, _committed_at, info in parsed:
        if info["is_rename"] and info["old_path"] and info["path"]:
            alias[info["old_path"]] = info["path"]

    def _canonical(path: str) -> str:
        seen: set[str] = set()
        while path in alias and path not in seen:
            seen.add(path)
            path = alias[path]
        return path

    groups: dict[str, dict] = {}
    for sha, author, committed_at, info in parsed:
        canonical = _canonical(info["path"])
        if canonical not in known_files:
            continue
        group = groups.setdefault(canonical, {"adds": [], "events": []})
        if info["is_new"]:
            group["adds"].append((committed_at, author))
            continue
        if info["is_deleted"]:
            continue
        changes = _pair_field_transitions(info["removed"], info["added"])
        if has_policy:
            changes = {f: c for f, c in changes.items() if f in tracked}
        if changes:
            group["events"].append({
                "sha": sha,
                "author": author,
                "committed_at": committed_at,
                "changes": changes,
            })

    results: list[dict] = []
    for path in known_files:
        group = groups.get(path, {"adds": [], "events": []})
        adds = sorted(group["adds"])
        created_at, created_by = adds[0] if adds else (None, None)
        events = sorted(group["events"], key=lambda e: e["committed_at"])
        results.append({
            "path": path,
            "created_at": created_at,
            "created_by": created_by,
            "events": events,
        })
    return results


def derive(record_type: str, worktree_root: Path | None = None) -> list[dict]:
    """AC1's named entry point: the record-history derivation for one type.

    Thin keyword-first alias over :func:`derive_type_history`. The name and
    signature are contract, not preference: `record_history.derive(record_type=...)`
    is what AC1 specifies and what example-cockpit-repo-em was handed as the
    consumer-facing shape ahead of this surface existing, so renaming it is a
    cross-repo break rather than a local refactor.
    """
    root = Path(worktree_root) if worktree_root is not None else Path.cwd()
    return derive_type_history(root, record_type)


# --------------------------------------------------------------------------
# C5 — cross-repo pass measurement + AC10 queried-root labelling.
# --------------------------------------------------------------------------
#
# Measured 2026-08-20 (evidence transcribed into this plan's body by C5b, per
# C5's own dispatch brief) across every root `fleet.work_state ::
# _resolve_active_sibling_paths` returned on this box: 16 candidate roots, 1
# SKIPPED (a registered `repos.*` entry that is `is_dir()` but not a git
# worktree — `_resolve_active_sibling_paths` does not itself filter on that,
# per staff-eng F11), 15 WALKED. Spawn count was exactly 1 per walked root
# regardless of that root's corpus size (AC9's root-scoped budget,
# reconciled against AC2's corpus-scoped one below). Peak patch bytes
# ranged 0..11_683_642 (this repo, `sizing-object`) — the same order as the
# 11.6MB this plan's Problem section already called "comfortable"; no
# walked root crossed a threshold that would make buffering the full patch
# text (as `_run_git_log_pass` does today via `capture_output=True`)
# unsafe. No streaming change is made in this chunk as a result — the
# module keeps buffering — but the ceiling this measurement names for a
# FUTURE corpus is ~50MB peak patch bytes per root (roughly 4x the largest
# measured value): a root whose `_run_git_log_pass` stdout crosses that
# should switch to `Popen` + incremental read, contained entirely inside
# C1b's parser (`_iter_commit_blocks` already consumes framed blocks
# sequentially, so streaming is an input-source change, not a parsing-model
# change) — a follow-up chunk's concern, not a code change this
# measurement's own numbers require yet.
#
# AC2 vs AC9 budget reconciliation (staff-eng F11): AC2's spawn budget is
# corpus-scoped — 1 spawn per pathspec, WITHIN one repo, independent of how
# many records that repo holds. AC9's is root-scoped — this section's own
# `derive_across_roots` issues exactly one `derive_type_history` call (and
# therefore exactly one git spawn, per AC2) per WALKED root, independent of
# how many roots are queried. Restated as one budget rather than two
# implicitly-equated ones: "≤1 spawn per walked root, and that 1 is itself
# independent of corpus size" — corpus-size-independence and root-count
# growth are orthogonal axes, and neither is allowed to make the other's
# spawn count grow.


def _is_git_worktree(root: Path) -> bool:
    """True iff `root` is a git worktree `git` itself will accept a `log`
    call against.

    `_resolve_active_sibling_paths` filters candidate roots on `is_dir()`
    only (staff-eng F11) -- a registered `repos.*` entry can be a real,
    existing, non-git directory. Handing such a root straight to
    `_run_git_log_pass` exits `git log` with rc=128 (`CalledProcessError`,
    since that call passes `check=True`); this predicate is the walk-only
    pre-check that routes such a root to SKIPPED before that spawn, mirroring
    `fleet.work_state`'s own `_walk_git_common_dir` pre-check discipline
    rather than treating a caught rc=128 as "empty" (which would make
    AC10's queried-root count over-report).
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(root),
            capture_output=True,
            check=False,
            **no_console_creationflags(),
        )
    except OSError:
        return False
    return result.returncode == 0


def derive_across_roots(roots: list[Path], record_type: str) -> dict:
    """AC9/AC10: derive `record_type` history across every root in `roots`,
    labelled by how many were actually queried.

    A non-git-worktree root (`_is_git_worktree` false) is SKIPPED rather
    than walked -- never silently reported as an empty-but-walked repo
    (staff-eng F11). `queried_root_count` is the count of roots actually
    walked, never the count of candidate roots handed in, so a caller
    cannot present this as a claim about every registered sibling when some
    were skipped (AC10's "never presented as a 31-repo fleet claim").

    Returns ``{"record_type", "queried_root_count", "roots_walked",
    "roots_skipped", "roots_failed", "repos"}`` where ``repos`` maps each
    walked root's POSIX path to that root's `derive_type_history` result, and
    ``roots_skipped``/``roots_failed`` are lists of ``{"root", "reason"}``.

    A root whose git pass RAISES is recorded in ``roots_failed`` and does not
    abort the pass: losing every already-walked root to one bad sibling would
    make a multi-root answer all-or-nothing. ``roots_failed`` is deliberately
    distinct from ``roots_skipped`` -- skipped means "not a worktree, never
    walked", failed means "walked and errored", and collapsing them would
    reintroduce the over-report AC10 exists to prevent.
    """
    walked: list[str] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    repos: dict[str, list[dict]] = {}
    for candidate in roots:
        root = Path(candidate)
        if not _is_git_worktree(root):
            skipped.append({"root": root.as_posix(), "reason": "not a git worktree"})
            continue
        try:
            repos[root.as_posix()] = derive_type_history(root, record_type)
        except Exception as exc:  # noqa: BLE001 -- one root's failure is not the pass's
            failed.append({"root": root.as_posix(), "reason": f"{type(exc).__name__}: {exc}"})
            continue
        walked.append(root.as_posix())

    return {
        "record_type": record_type,
        "queried_root_count": len(walked),
        "roots_walked": walked,
        "roots_skipped": skipped,
        "roots_failed": failed,
        "repos": repos,
    }


@register_op("records.history")
def _records_history(params: dict, repo_root: Path | None = None) -> dict:
    """COMPUTE_ONLY: git-derived transition history for one record type.

    Reads git history and writes nothing, anywhere. The `_registry_map`
    entry alone does not register a handler -- this decorator is the live
    registration `ipc._REGISTRY` is populated from (AC6).
    """
    record_type = params.get("record_type")
    if not record_type:
        raise ValueError(
            "records.history requires 'record_type'; supported: "
            + ", ".join(sorted(supported_record_types()))
        )
    # No Path.cwd() fallback: warm-served handlers run in a shared server
    # process, so cwd is the SERVER's, not the caller's worktree -- a missing
    # root would silently derive against the wrong repo and return a
    # plausible answer. Fail loudly instead.
    root_arg = params.get("root") or repo_root
    if not root_arg:
        raise ValueError(
            "records.history requires an explicit 'root' (or a dispatched repo_root); "
            "refusing to fall back to the process cwd, which under a warm engine is "
            "the server's directory rather than the caller's worktree"
        )
    root = Path(root_arg)
    records = derive(record_type=record_type, worktree_root=root)
    # AC5b needs both halves: what is on disk now, and what the git pass
    # actually reported history for. A record in the first and not the second
    # is untracked, not quiet.
    on_disk = resolve_record_files(root, record_type)
    with_history = frozenset(
        r["path"] for r in records if r.get("created_at") is not None
    )
    untracked = sorted(untracked_record_paths(on_disk, with_history))
    return {
        "record_type": record_type,
        "root": root.as_posix(),
        "records": records,
        # AC5b: a record present on disk but untracked has NO history, which is
        # not the same fact as a tracked record that never changed. Both would
        # otherwise read as `events: []`; this names the first explicitly.
        "untracked": untracked,
    }
