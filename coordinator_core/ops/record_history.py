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
from typing import Sequence

from coordinator_core.ipc import register_op
from coordinator_core.ops import records_query
from coordinator_core.win_portability import (
    leaf_spawn_creationflags,
    no_console_creationflags,
)


class UnsupportedRecordTypeError(ValueError):

    def __init__(self, record_type: str, supported: list[str]):
        self.record_type = record_type
        self.supported = supported
        super().__init__(
            f'record_history: unsupported record type {record_type!r}; '
            f'supported types: {", ".join(supported)}'
        )


# `_SYNTHETIC_TYPES`'s own docstring); there is no one-file-one-record glob
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


def _type_directory_pathspec_one(record_type: str) -> str:
    _require_supported(record_type)
    glob_pat = records_query._TYPE_TO_GLOB[record_type]
    parts = glob_pat.split('/')
    fixed: list[str] = []
    for part in parts:
        if '*' in part:
            break
        fixed.append(part)
    if len(fixed) == len(parts):
        fixed = fixed[:-1]
    return '/'.join(fixed)


def type_directory_pathspec(record_type: str | Sequence[str]) -> str | list[str]:
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

    A bare string returns the single pathspec (unchanged contract). A
    sequence returns the DEDUPLICATED UNION of each member's pathspec, order
    preserved — the multi-type caller's one ``git log`` pass takes this list
    directly as its pathspec argv (P083-C4).
    """
    if isinstance(record_type, str):
        return _type_directory_pathspec_one(record_type)
    pathspecs: list[str] = []
    for one in record_type:
        spec = _type_directory_pathspec_one(one)
        if spec not in pathspecs:
            pathspecs.append(spec)
    return pathspecs


def _resolve_record_files_one(worktree_root: Path, record_type: str) -> frozenset[str]:
    _require_supported(record_type)
    files = records_query._collect_files(worktree_root, record_type)
    return frozenset(p.relative_to(worktree_root).as_posix() for p in files)


def resolve_record_files(worktree_root: Path, record_type: str | Sequence[str]) -> frozenset[str]:
    if isinstance(record_type, str):
        return _resolve_record_files_one(worktree_root, record_type)
    union: frozenset[str] = frozenset()
    for one in record_type:
        union = union | _resolve_record_files_one(worktree_root, one)
    return union


def _resolve_record_files_by_type(worktree_root: Path, types: list[str]) -> dict[str, str]:
    """Path -> owning record type, across ``types`` (P083-C4 R3).

    `_FIELD_POLICY` selection and per-type `untracked` grouping both need
    per-file attribution that the plain union in `resolve_record_files`
    discards; this is the one place that attribution is computed, first-type
    wins on any path collision (types' globs are not expected to overlap).
    """
    owner: dict[str, str] = {}
    for one in types:
        for path in _resolve_record_files_one(worktree_root, one):
            owner.setdefault(path, one)
    return owner


def partition_known_files(
    candidate_paths: list[str], known_files: frozenset[str],
) -> tuple[list[str], list[str]]:
    known: list[str] = []
    unknown: list[str] = []
    for path in candidate_paths:
        (known if path in known_files else unknown).append(path)
    return known, unknown


def untracked_record_paths(
    known_files: frozenset[str], tracked_paths: frozenset[str],
) -> frozenset[str]:
    return frozenset(known_files) - frozenset(tracked_paths)


# Data only, selecting which frontmatter fields are of INTEREST for a given
# surface reports the CURRENT-RECORDS-ONLY subset, so the sizing figure it
# CURRENT-RECORDS-ONLY SCOPE note in the module docstring. `decision` is 14
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


# `_COMMIT_SEP`/`_FIELD_SEP` are the actual bytes git EMITS for `%x00`/`%x01`
_COMMIT_SEP = "\x00"
_FIELD_SEP = "\x01"
_LOG_FORMAT = "%x00%H%x01%an%x01%aI"

_FIELD_LINE_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$')
_HUNK_HEADER_RE = re.compile(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@')
_DIFF_GIT_RE = re.compile(r'^diff --git a/(.*) b/(.*)$')

_FRONTMATTER_LINE_BOUND = 60


def _run_git_log_pass(worktree_root: Path, pathspec: str | list[str]) -> str:
    pathspecs = [pathspec] if isinstance(pathspec, str) else list(pathspec)
    result = subprocess.run(
        ["git", "log", f"--format={_LOG_FORMAT}", "-p", "-U0", "--", *pathspecs],
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
            raise ValueError(
                'record_history: unparseable commit header in the git log frame '
                f'({header[:120]!r}) -- refusing to silently drop the commit, '
                'because a dropped commit is indistinguishable from a quiet record'
            )
        yield sha, author, committed_at, diff_text


def _split_file_sections(diff_text: str) -> list[list[str]]:
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
    stripped = raw_value.strip()
    quote = stripped[0] if stripped[:1] in ("'", '"') else None
    if quote:
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


def derive_type_history(
    worktree_root: Path,
    record_type: str | Sequence[str],
    since: str | None = None,
) -> list[dict]:
    """Derive one event stream per current file of ``record_type`` (AC1).

    Exactly one ``git log`` invocation PER CALL, not per type (AC2,
    widened by P083-C4) — the pathspec union and known file set come from
    C1a's `type_directory_pathspec`/`resolve_record_files`; every event
    comes from parsing that single pass's patch text, never a second git
    call and never a blob read.

    A bare ``record_type`` string keeps the EXISTING single-type shape
    byte-for-byte: a list of ``{"path", "created_at", "created_by",
    "events"}``, no per-record ``record_type`` key (P083-C4 R3 backward
    compatibility). A sequence of types adds a ``record_type`` key to each
    record (per-record attribution, chosen over per-type grouping of the
    whole envelope so the record array shape stays intact for an existing
    consumer). `_FIELD_POLICY` is looked up PER RESOLVED FILE from that
    file's own owning type, never once for the whole call, so a
    multi-type request never applies one type's field policy to another
    type's files.

    ``since`` bounds the EVENTS only, never the walk or `created_at`/adds
    (P083-C4 R2): a bounded walk would null `created_at` for every
    pre-window record and misreport it as `untracked`, which is a
    different, wrong, answer — see the module docstring's "Wipe/restore
    safety" and this chunk's plan body. ``since`` is compared against each
    event's ``committed_at`` (ISO 8601, lexicographically comparable)
    AFTER field-policy filtering, so a since-excluded event never
    surfaces regardless of which field it touched.

    Each event is ``{"sha", "author", "committed_at", "changes"}`` where
    ``changes`` maps field name to ``{"from", "to"}`` — a record with no
    real transitions (or none inside the ``since`` window) reports an
    empty ``events`` list by construction.
    """
    is_multi = not isinstance(record_type, str)
    types = list(record_type) if is_multi else [record_type]
    for one in types:
        _require_supported(one)

    owner = _resolve_record_files_by_type(worktree_root, types)
    known_files = frozenset(owner)
    pathspecs = type_directory_pathspec(types)
    raw = _run_git_log_pass(worktree_root, pathspecs)

    parsed: list[tuple[str, str, str, dict]] = []
    for sha, author, committed_at, diff_text in _iter_commit_blocks(raw):
        for lines in _split_file_sections(diff_text):
            info = _parse_file_section(lines)
            if info["path"] is None:
                continue
            parsed.append((sha, author, committed_at, info))

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
        owning_type = owner[canonical]
        if owning_type in _FIELD_POLICY:
            tracked = frozenset(fields_of_interest(owning_type))
            changes = {f: c for f, c in changes.items() if f in tracked}
        if not changes:
            continue
        if since is not None and committed_at < since:
            continue
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
        record = {
            "path": path,
            "created_at": created_at,
            "created_by": created_by,
            "events": events,
        }
        if is_multi:
            record["record_type"] = owner[path]
        results.append(record)
    return results


def derive(
    record_type: str | Sequence[str],
    worktree_root: Path | None = None,
    since: str | None = None,
) -> list[dict]:
    root = Path(worktree_root) if worktree_root is not None else Path.cwd()
    return derive_type_history(root, record_type, since=since)


def _is_git_worktree(root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(root),
            capture_output=True,
            check=False,
            **leaf_spawn_creationflags(),
        )
    except OSError:
        return False
    return result.returncode == 0


def derive_across_roots(roots: list[Path], record_type: str) -> dict:
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
    """COMPUTE_ONLY: git-derived transition history for one or several
    record types in a single call (P083-C4), with an optional derivation-side
    ``since`` window and a silent-success stdout contract (no output on this
    seam besides the envelope below -- the warm-client stderr noise a
    consumer flagged is on the `cc_invoke` path, not this op's, per this
    chunk's plan body).

    Reads git history and writes nothing, anywhere. The `_registry_map`
    entry alone does not register a handler -- this decorator is the live
    registration `ipc._REGISTRY` is populated from (AC6).

    ``record_type`` accepts a bare string (existing single-type shape,
    unchanged byte-for-byte: singular top-level ``record_type``, flat
    ``untracked`` list, no per-record ``record_type`` key) or a sequence of
    strings (P083-C4 widening: ``record_type`` echoes the requested list,
    each record carries its own ``record_type``, and ``untracked`` is
    grouped per type -- ``{"handoff": [...], "sizing-object": [...]}``).
    """
    record_type = params.get("record_type")
    if not record_type:
        raise ValueError(
            "records.history requires 'record_type'; supported: "
            + ", ".join(sorted(supported_record_types()))
        )
    since = params.get("since")
    root_arg = params.get("root") or repo_root
    if not root_arg:
        raise ValueError(
            "records.history requires an explicit 'root' (or a dispatched repo_root); "
            "refusing to fall back to the process cwd, which under a warm engine is "
            "the server's directory rather than the caller's worktree"
        )
    root = Path(root_arg)
    is_multi = not isinstance(record_type, str)
    records = derive(record_type=record_type, worktree_root=root, since=since)

    if not is_multi:
        on_disk = resolve_record_files(root, record_type)
        with_history = frozenset(
            r["path"] for r in records if r.get("created_at") is not None
        )
        untracked = sorted(untracked_record_paths(on_disk, with_history))
        return {
            "record_type": record_type,
            "root": root.as_posix(),
            "records": records,
            "untracked": untracked,
        }

    types = list(record_type)
    with_history_by_type: dict[str, set[str]] = {t: set() for t in types}
    for r in records:
        if r.get("created_at") is not None:
            with_history_by_type[r["record_type"]].add(r["path"])
    untracked_by_type = {
        t: sorted(untracked_record_paths(
            resolve_record_files(root, t), frozenset(with_history_by_type[t]),
        ))
        for t in types
    }
    return {
        "record_type": types,
        "root": root.as_posix(),
        "records": records,
        "untracked": untracked_by_type,
    }
