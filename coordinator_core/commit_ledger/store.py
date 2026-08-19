"""
coordinator_core.commit_ledger.store — handoff-keyed, append-only commit ledger.

Spec backlink: state/dispatch-briefs/2026-08-19-the-baton-carries-its-commits/C1.md

One entry per commit, keyed on the owning handoff baton's ``handoff_id`` —
NOT a basename or path (both move when the boot sweep archives a file), and
NOT stored in handoff markdown frontmatter or in ``session_baton/store.py``
(session-keyed, wrong unit for this ledger, and a sibling collision).

Store location: ``<git-common-dir>/coordinator-sessions/.commit-ledger/
<handoff_id>.jsonl`` — one JSON object per line, append-only. Dot-prefixed
(``.commit-ledger``) deliberately: ``ops/session/reap.py ::
_reap_stale_sessions`` archives any non-dot child of the session hub after
24h of inactivity and explicitly skips dot children — a bare
``<hub>/<handoff_id>.json`` file, or a non-dot subdirectory, would be swept
by the reaper or misread as a stale/malformed session directory.

Entry shape (one JSON object per line)::

    {
      "sha": str,
      "kind": str,
      "weight_basis": <any JSON-serialisable value>,
      "reviewed_by": [str, ...],
    }

Append-only, NO read-modify-write: locked append only needs the ``write()``
itself under lock, not a parse-mutate-serialise cycle — cheaper than the
``locked_rmw`` shape ``session_baton/store.py`` uses, and avoids serialising
every agent commit under one EM's baton through a single RMW at this box's
load norm (see ``docs/wiki/machine-load-norm.md``). Mirrors
``coordinator_core.session.claims::atomic_dedup_append``'s locking shape
(``held_lock`` wrapping a scan/append region), not ``session_baton``'s
``locked_rmw`` shape — this store never reads-to-rewrite.

Dedup is a READ-time concern, not a write-time one: appending the same
``sha`` twice is tolerated (a duplicate line is filtered on read, never
rewritten) so a caller retrying a commit that landed despite a reported
failure can safely re-append without a pre-check. "Idempotent on sha" holds
for that retry case and is explicitly NOT claimed for amend/rebase/
cherry-pick, which mint new SHAs for the same content — those are accepted
as new, separate entries on purpose (documented for C10, not discovered
later: this module has no concept of "same logical commit, new SHA").

``reviewed_by`` is the one field that is UNIONED, not first-line-wins, at
read time (see :func:`read_entries`) — this is what lets
:func:`mark_reviewed` (C9) append a reviewer onto an already-ledgered
``sha`` without a read-modify-write: a mechanical "reviewer concluded over
this range" convenience, not a correctness mechanism, fails safe.

Negative-spec:
    - Do NOT write into handoff markdown frontmatter — that is explicitly
      out of scope (see the brief's Anti-scope).
    - Do NOT write into ``session_baton/store.py`` — session-keyed, wrong
      unit, a sibling namespace, not this ledger.
    - Do NOT use a non-dot-prefixed child of the session hub — the reaper
      sweeps those; see the module docstring above.
    - Do NOT do a read-modify-write on append — dedup happens on READ,
      never by rewriting the file to drop a duplicate line.
    - Do NOT treat amend/rebase/cherry-pick SHAs as continuations of a prior
      entry — each SHA is its own, separate, permanent entry.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.locked_write import LockTimeout, held_lock
from coordinator_core.ops.resolve_swept_baton import _find_first_match
from coordinator_core.session import core

_LOG = logging.getLogger(__name__)

LEDGER_DIRNAME = ".commit-ledger"

#: Suffix for the write-time-resolved predecessor-pointer sidecar (AC18):
#: one small JSON object recording where THIS baton's predecessor lives,
#: written ONCE at successor-mint time by :func:`record_predecessor_pointer`
#: so :func:`read_chain` never re-derives it by walking the archive.
_PREDECESSOR_POINTER_SUFFIX = ".predecessor.json"

#: Hard bound on ancestor hops :func:`read_chain` will follow, pointer or
#: legacy fallback alike -- a cycle (corrupt/hand-edited pointer data) must
#: terminate the walk rather than loop forever.
_MAX_CHAIN_HOPS = 50

#: Hard bound on LEGACY (pointer-absent) hops specifically -- a baton minted
#: before this pointer existed still needs *a* predecessor lookup, but it is
#: allowed to cost ONE bounded on-disk search, never a walk repeated at every
#: hop of a growing chain (AC17/AC18's whole point). Stated here rather than
#: left implicit: at most this many consecutive pointer-less ancestors are
#: resolved per `read_chain` call before the walk gives up on any further
#: legacy ancestor.
_MAX_LEGACY_FALLBACK_HOPS = 3

#: Bounded wait for the cross-process flock — matches the sibling
#: ``touched.txt`` writer's timeout
#: (``coordinator_core.session.claims._ATOMIC_DEDUP_APPEND_LOCK_TIMEOUT_SECS``):
#: a per-commit append must never itself become the slow op at this box's
#: load norm.
_LOCK_TIMEOUT_SECS = 2.0


def ledger_dir(cwd: Optional[str] = None) -> Optional[Path]:
    """The commit-ledger directory (``<git-common-dir>/coordinator-sessions/
    .commit-ledger``), or ``None`` when the session hub is unresolvable (not
    a git repo)."""
    base = core.sessions_dir(cwd)
    if not base:
        return None
    return Path(base) / LEDGER_DIRNAME


def ledger_path(handoff_id: str, cwd: Optional[str] = None) -> Optional[Path]:
    """The absolute path to ``<handoff_id>``'s ledger file, or ``None`` when
    ``handoff_id`` is empty or the session hub is unresolvable."""
    if not handoff_id:
        return None
    ldir = ledger_dir(cwd)
    if ldir is None:
        return None
    return ldir / f"{handoff_id}.jsonl"


def _lock_anchor(path: Path) -> Optional[Path]:
    """Derive the ``held_lock`` ``anchor_root`` from ``path``'s own on-disk
    position, mirroring ``coordinator_core.session.claims::
    _atomic_dedup_append_lock_anchor`` for the sibling ``touched.txt``
    writer under the same session hub. ``path`` is always
    ``<git-common-dir>/coordinator-sessions/.commit-ledger/<handoff_id>
    .jsonl``, so its THIRD parent is the git common dir itself
    (``parents[0]`` = ``.commit-ledger``, ``parents[1]`` =
    ``coordinator-sessions``, ``parents[2]`` = the git common dir, named
    ``.git`` for every non-bare, non-worktree-private layout this hub
    uses).

    Returns ``None`` when ``path`` does not have the expected shape (a test
    fixture writing to an ad-hoc temp path, for instance) so the caller can
    fall back to an unlocked append rather than passing a nonsense anchor
    into ``held_lock``.
    """
    p = Path(os.path.abspath(str(path)))
    parents = p.parents
    if len(parents) < 3:
        return None
    common_dir = parents[2]
    if common_dir.name != ".git":
        return None
    return common_dir.parent


def _entry_line(sha: str, kind: str, weight_basis: Any, reviewed_by: Optional[List[str]]) -> str:
    entry = {
        "sha": sha,
        "kind": kind,
        "weight_basis": weight_basis,
        "reviewed_by": list(reviewed_by) if reviewed_by else [],
    }
    return json.dumps(entry, sort_keys=True) + "\n"


def append_entry(
    handoff_id: str,
    sha: str,
    kind: str,
    weight_basis: Any = None,
    reviewed_by: Optional[List[str]] = None,
    cwd: Optional[str] = None,
) -> bool:
    """Append one commit entry to ``handoff_id``'s ledger. Pure append — NO
    read-modify-write: a duplicate ``sha`` (a retry of a commit that landed
    despite a reported failure) is safe to re-append and is deduplicated at
    READ time (see :func:`read_entries`), never by rewriting this file.

    Creates the ``.commit-ledger`` directory (and any missing ancestor)
    on first use — unlike ``session_baton/store.py``'s per-session
    directory, this namespace is owned entirely by this module, not a
    pre-existing session lifecycle object, so there is no "require it to
    already exist" constraint here.

    The append itself is wrapped in ``held_lock``, with the same bounded
    timeout the sibling ``touched.txt`` writer uses. Fails CLOSED, never
    open: no derivable anchor (see :func:`_lock_anchor`), ``LockTimeout``,
    ``RuntimeError`` (no lock backend), ``ValueError`` (``held_lock``'s own
    precondition), or ``OSError`` all SKIP the append and log a warning
    naming the unrecorded ``sha`` — an unlocked multi-writer append is not
    proven atomic on Windows (this repo's CLAUDE.md: "Windows is
    first-class"), so a concurrent writer racing the lock timeout must not
    be allowed to interleave a partial line into the file. This function
    itself still never raises for a lock failure: the plan's hard
    constraint is that a ledger write never fails the commit it
    accompanies, so an unrecorded sha is a silent-but-logged
    under-count, the same accepted-degradation shape AC3b/AC15 use for
    other failure modes — not a raise.

    Returns True on success, False when ``handoff_id``/``sha``/``kind`` is
    empty, the session hub is unresolvable, the lock could not be acquired
    (logged), or the locked append itself fails (``OSError``).
    """
    if not handoff_id:
        raise ValueError("handoff_id required")
    if not sha:
        raise ValueError("sha required")
    if not kind:
        raise ValueError("kind required")

    path = ledger_path(handoff_id, cwd)
    if path is None:
        return False

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    line = _entry_line(sha, kind, weight_basis, reviewed_by)

    def _append() -> None:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)

    anchor = _lock_anchor(path)
    if anchor is None:
        _LOG.warning("commit-ledger append skipped (no lock anchor): sha=%s handoff_id=%s", sha, handoff_id)
        return False

    try:
        with held_lock(
            Path(os.path.abspath(str(path))),
            anchor_root=anchor,
            timeout=_LOCK_TIMEOUT_SECS,
        ):
            _append()
    except (LockTimeout, RuntimeError, ValueError):
        _LOG.warning("commit-ledger append skipped (lock unavailable): sha=%s handoff_id=%s", sha, handoff_id)
        return False
    except OSError:
        return False
    return True


def read_entries(handoff_id: str, cwd: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read ``handoff_id``'s ledger, deduplicated on ``sha`` — the FIRST
    line for a given ``sha`` wins for ``kind``/``weight_basis``, later
    duplicate lines (a tolerated retry re-append, see :func:`append_entry`,
    OR a later :func:`mark_reviewed` mark-only append for a ``sha`` already
    ledgered) are dropped for those two fields. ``reviewed_by`` is the ONE
    exception: it is UNIONED (order-preserving, deduplicated) across every
    line sharing a ``sha`` — this is what lets :func:`mark_reviewed` append
    a reviewer onto an already-ledgered commit without a read-modify-write
    (see that function's docstring). Malformed lines (corrupt JSON, a JSON
    value that isn't an object, or an object missing ``sha``) are skipped,
    never raised. Returns ``[]`` when ``handoff_id`` is empty, the session
    hub is unresolvable, or the ledger file does not exist.

    Unlocked — a read racing a concurrent appender may miss the
    in-flight line or (never, given append-only single-line writes) see a
    torn one; either degrades gracefully rather than raising.
    """
    path = ledger_path(handoff_id, cwd)
    if path is None or not path.is_file():
        return []

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    seen: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    reviewers: Dict[str, List[str]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        sha = data.get("sha")
        if not sha or not isinstance(sha, str):
            continue
        if sha not in seen:
            order.append(sha)
            seen[sha] = data
        bucket = reviewers.setdefault(sha, [])
        for reviewer in data.get("reviewed_by") or []:
            if isinstance(reviewer, str) and reviewer and reviewer not in bucket:
                bucket.append(reviewer)

    result = []
    for sha in order:
        entry = dict(seen[sha])
        entry["reviewed_by"] = list(reviewers.get(sha, []))
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Reviewed-marking (C9) — EM ergonomics, not a correctness mechanism
# ---------------------------------------------------------------------------

#: ``kind`` stamped on a mark-only append (see :func:`mark_reviewed`) —
#: distinguishes a reviewed-marking line from a real commit-kind append at
#: a glance on-disk. Never read back as an entry's "real" ``kind`` for a
#: ``sha`` that already has a prior, real entry: :func:`read_entries`
#: keeps the FIRST line's ``kind`` for a ``sha``, so this sentinel only
#: surfaces as the visible ``kind`` when the mark lands before any real
#: entry for that ``sha`` (fails safe — see module docstring's dedup rule).
_REVIEW_MARK_KIND = "review-mark"


def mark_reviewed(
    handoff_id: str,
    shas: List[str],
    reviewer: str,
    cwd: Optional[str] = None,
) -> bool:
    """Mark each of ``shas`` as reviewed by ``reviewer`` in ``handoff_id``'s
    ledger — EM ergonomics for "a dispatched reviewer concluded over this
    range", NOT a correctness mechanism (Anti-scope: no verification that
    ``shas`` are real, ledgered, or even hex-shaped; fails safe, worst case
    a redundant review).

    Mechanically an :func:`append_entry` call per sha with
    ``reviewed_by=[reviewer]`` and ``kind=_REVIEW_MARK_KIND`` — this store
    is append-only (see module docstring), so "marking" an already-ledgered
    commit reviewed can never rewrite that commit's existing line. Instead
    it appends a NEW line for the same ``sha``, and :func:`read_entries`'s
    read-time union of ``reviewed_by`` across every line sharing a ``sha``
    (see that function's docstring) is what makes the mark visible without
    a read-modify-write.

    Returns True only when EVERY sha's append succeeds (mirrors
    :func:`append_entry`'s own True/False contract); a partial failure
    still leaves whichever appends succeeded on disk (append-only, no
    rollback) since the failure mode here is "worst case a redundant
    review", not data loss. Returns False immediately, with no appends
    attempted, when ``handoff_id``, ``reviewer``, or ``shas`` is empty.
    """
    if not handoff_id or not reviewer or not shas:
        return False

    ok = True
    for sha in shas:
        if not sha:
            ok = False
            continue
        if not append_entry(
            handoff_id,
            sha,
            _REVIEW_MARK_KIND,
            weight_basis=None,
            reviewed_by=[reviewer],
            cwd=cwd,
        ):
            ok = False
    return ok


# ---------------------------------------------------------------------------
# Ancestor chain (AC17/AC18) — write-time predecessor pointer + read_chain
# ---------------------------------------------------------------------------


def _predecessor_pointer_path(handoff_id: str, cwd: Optional[str] = None) -> Optional[Path]:
    """The sidecar path holding ``handoff_id``'s resolved predecessor
    pointer, or ``None`` when ``handoff_id`` is empty or the session hub is
    unresolvable. Same directory as the per-baton ledger file, distinguished
    by suffix — both are dot-prefixed-parent children of the session hub, so
    both are equally exempt from the 24h reaper sweep (see module
    docstring).
    """
    if not handoff_id:
        return None
    ldir = ledger_dir(cwd)
    if ldir is None:
        return None
    return ldir / f"{handoff_id}{_PREDECESSOR_POINTER_SUFFIX}"


def _worktree_root(cwd: Optional[str] = None) -> Optional[Path]:
    """The repo worktree root for ``cwd``/current dir, or ``None``.

    Delegates to ``coordinator_core.git.repo_root.show_toplevel``, which
    WALKS the filesystem for a ``.git`` entry and never spawns a subprocess
    (see that module's docstring) -- required so neither this resolution nor
    anything built on it can violate AC9's zero-subprocess constraint.
    """
    top = show_toplevel(cwd)
    return Path(top) if top else None


def _read_handoff_frontmatter(fpath: Path) -> Dict[str, str]:
    """Best-effort raw frontmatter field read for ``handoff_id`` /
    ``predecessor`` / ``predecessor_id`` off a handoff markdown file.

    Never raises: a missing file, an unreadable file, or a file with no
    frontmatter fence all degrade to ``{}``. Pure local file I/O and YAML/
    regex parsing via ``coordinator_core.frontmatter.primitives`` -- no
    subprocess, no nested interpreter (AC9).
    """
    try:
        raw = fpath.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        return {}
    split = split_frontmatter(raw)
    if split is None:
        return {}
    fields: Dict[str, str] = {}
    for key in ("handoff_id", "predecessor", "predecessor_id", "continued_into"):
        val = read_fm_field_unquoted(split.fm_text, key)
        if val:
            fields[key] = val
    return fields


def _frontmatter_id_search(root: Path, target_id: str) -> Optional[Path]:
    """Locate a handoff file whose ``handoff_id:`` frontmatter field equals
    ``target_id`` by walking ``state/handoffs/`` then ``archive/handoffs/``.

    This is the ONE case ``_find_first_match``'s basename search cannot
    cover: a bare ``handoff_id`` (e.g. ``hnd-<slug>-<hex6>``) generally
    does NOT match the on-disk filename (``<date>-<slug>.md``), so a
    filename-shaped search never finds it. Mirrors the id-shaped branch of
    ``ops.handoff_stamp._resolve_continued_into`` (same corpus, same
    shape); NOT a second version of ``_find_first_match`` -- that function
    covers path/basename shapes and stays the only resolver for those.

    Deliberately full-cost (an ``rglob`` over every archived handoff,
    reading each file's frontmatter): called at most ONCE per
    :func:`record_predecessor_pointer` invocation (a one-time write-time
    cost at successor-mint) or, on the read side, at most
    ``_MAX_LEGACY_FALLBACK_HOPS`` times per :func:`read_chain` call for a
    pointer-less legacy ancestor -- never per-ancestor on a pointer-backed
    walk (AC18's growth term). No subprocess, no nested interpreter (AC9).
    """
    state_dir = root / "state" / "handoffs"
    archive_dir = root / "archive" / "handoffs"
    for rootdir, pattern in ((state_dir, "*.md"), (archive_dir, "*.md")):
        if not rootdir.is_dir():
            continue
        for fpath in rootdir.rglob(pattern):
            if not fpath.is_file():
                continue
            fm = _read_handoff_frontmatter(fpath)
            if fm.get("handoff_id") == target_id:
                return fpath
    return None


def record_predecessor_pointer(
    handoff_id: str,
    predecessor_ref: str,
    repo_root: Optional[str] = None,
    cwd: Optional[str] = None,
) -> bool:
    """Resolve ``predecessor_ref`` (a ``continued_into``-shaped value --
    either a bare ``handoff_id`` or a repo-relative path, see module-level
    plan citation) to a concrete on-disk location and store that
    resolution, ONCE, in ``handoff_id``'s predecessor-pointer sidecar
    (AC18).

    Intended caller: successor-mint time, by whichever mechanism mints
    ``handoff_id`` as a continuation of ``predecessor_ref`` -- OUT OF SCOPE
    for this chunk to wire (surface is this store module only; the mint
    call site belongs to the sibling one-baton-at-a-time plan, see this
    module's own docstring / C2 dispatch brief). This function is the API
    that caller uses; :func:`read_chain` is the API that consumes it.

    Reuses ``ops.resolve_swept_baton._find_first_match`` for path/basename
    resolution and :func:`_frontmatter_id_search` for the bare-``handoff_id``
    shape ``_find_first_match`` cannot cover (see that function's
    docstring) -- both are ONE-TIME write-time costs, and NEITHER is called
    from :func:`read_chain` on the pointer-backed path (that is the whole
    point of storing the result here instead of re-deriving it at read
    time).

    Returns True on a successful write, False on any unresolvable input
    (empty ``handoff_id``/``predecessor_ref``, unresolvable worktree root,
    predecessor file not found, or a write failure) -- never raises.
    """
    if not handoff_id or not predecessor_ref:
        return False

    root = Path(repo_root) if repo_root else _worktree_root(cwd)
    if root is None:
        return False

    ref = predecessor_ref.strip()
    if not ref:
        return False

    candidate: Optional[Path] = None
    if "/" in ref or ref.endswith(".md"):
        p = Path(ref)
        direct = p if p.is_absolute() else root / p
        if direct.is_file():
            candidate = direct
        else:
            candidate = _find_first_match(root, Path(ref).name)
    else:
        # id-shaped: no separator, no .md suffix -- basename search first
        # (cheap; catches the rare case a filename happens to equal the
        # handoff_id), falling back to the frontmatter id-match search
        # (see _frontmatter_id_search's docstring for why this is the ONE
        # write-time exception to "_find_first_match only").
        candidate = _find_first_match(root, ref) or _frontmatter_id_search(root, ref)

    if candidate is None or not candidate.is_file():
        return False

    fm = _read_handoff_frontmatter(candidate)
    try:
        rel = str(candidate.relative_to(root))
    except ValueError:
        rel = str(candidate)

    pointer = {
        "predecessor_id": fm.get("handoff_id") or (ref if not ("/" in ref or ref.endswith(".md")) else None),
        "predecessor_path": rel,
    }

    path = _predecessor_pointer_path(handoff_id, cwd)
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(pointer, sort_keys=True), encoding="utf-8")
    except OSError:
        return False
    return True


def _read_predecessor_pointer(handoff_id: str, cwd: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Read back ``handoff_id``'s predecessor-pointer sidecar, or ``None``
    when absent/unreadable/malformed. Pure local file read -- no
    subprocess, no archive walk (AC9/AC18)."""
    path = _predecessor_pointer_path(handoff_id, cwd)
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _legacy_resolve_predecessor_id(
    handoff_id: str, root: Path, cwd: Optional[str] = None
) -> Optional[str]:
    """Bounded, pointer-absent fallback: locate ``handoff_id``'s own handoff
    file and read its ``predecessor_id`` (preferred) or ``predecessor``
    frontmatter field.

    Stated bound (AC18's "bound it and state the bound"): this does AT MOST
    one basename search via ``_find_first_match`` (itself one bounded rglob
    per archive subdir, ~2ms measured on this tree at 505 files -- see the
    plan's ruling section), falling back to one
    :func:`_frontmatter_id_search` (a full walk, same shape as
    ``record_predecessor_pointer``'s write-time id-match) ONLY when the
    basename search misses, PLUS one direct file read -- and this whole
    sequence is invoked at most ``_MAX_LEGACY_FALLBACK_HOPS`` times per
    :func:`read_chain` call. It is NOT invoked at all once a caller has
    back-filled the pointer via :func:`record_predecessor_pointer` for the
    batons on this walk.
    """
    direct = root / "state" / "handoffs" / f"{handoff_id}.md"
    fpath: Optional[Path] = (
        direct
        if direct.is_file()
        else (_find_first_match(root, handoff_id) or _frontmatter_id_search(root, handoff_id))
    )
    if fpath is None:
        return None
    fm = _read_handoff_frontmatter(fpath)
    pred_id = fm.get("predecessor_id")
    if pred_id:
        return pred_id
    pred = fm.get("predecessor")
    if not pred:
        return None
    # `predecessor` on this schema is path-shaped; resolve its own
    # handoff_id from ITS frontmatter (one more direct file read, not a
    # walk) so the caller always keys the next hop on handoff_id.
    p = Path(pred)
    pred_path = p if p.is_absolute() else root / p
    if pred_path.is_file():
        pred_fm = _read_handoff_frontmatter(pred_path)
        return pred_fm.get("handoff_id") or pred
    return pred


def read_chain(handoff_id: str, cwd: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return ``handoff_id``'s own ledger entries plus every ancestor's,
    walking the ``continued_into`` edge BACKWARD (AC17/AC18).

    Archive-aware by construction: ancestor resolution never inspects
    ``state/handoffs/`` vs ``archive/handoffs/*/`` as a precondition --
    the predecessor pointer (or its bounded legacy fallback) stores/reads a
    location under either root indifferently, per the corrected substrate
    measurement (153/154 ``continued_into``-bearing files are archived; see
    this plan's § Problem).

    Cost shape: each ancestor hop is either (a) ONE small local file read
    of a predecessor-pointer sidecar (the common case once
    :func:`record_predecessor_pointer` has back-filled it at successor-mint
    time) -- O(1), no archive walk -- or (b) the bounded legacy fallback
    (see :func:`_legacy_resolve_predecessor_id`), capped at
    ``_MAX_LEGACY_FALLBACK_HOPS`` uses total per call. The walk overall is
    capped at ``_MAX_CHAIN_HOPS`` to guarantee termination against a
    corrupt/cyclic pointer. No git subprocess, no nested interpreter on any
    branch (AC9).

    Returns ``[]`` when ``handoff_id`` is empty; a partial chain (never a
    raise) when an ancestor cannot be resolved -- resolution failure ends
    the walk at that ancestor, it does not drop entries already collected.
    """
    if not handoff_id:
        return []

    chain: List[Dict[str, Any]] = list(read_entries(handoff_id, cwd))

    root: Optional[Path] = None
    visited = {handoff_id}
    current = handoff_id
    legacy_hops_used = 0

    for _ in range(_MAX_CHAIN_HOPS):
        pointer = _read_predecessor_pointer(current, cwd)
        predecessor_id: Optional[str] = None
        if pointer is not None:
            predecessor_id = pointer.get("predecessor_id")
            if not predecessor_id:
                predecessor_path = pointer.get("predecessor_path")
                if predecessor_path:
                    if root is None:
                        root = _worktree_root(cwd)
                    if root is not None:
                        p = root / predecessor_path
                        if p.is_file():
                            predecessor_id = _read_handoff_frontmatter(p).get("handoff_id")
        elif legacy_hops_used < _MAX_LEGACY_FALLBACK_HOPS:
            if root is None:
                root = _worktree_root(cwd)
            if root is not None:
                legacy_hops_used += 1
                try:
                    predecessor_id = _legacy_resolve_predecessor_id(current, root, cwd)
                except OSError:
                    predecessor_id = None

        if not predecessor_id or predecessor_id in visited:
            break

        visited.add(predecessor_id)
        chain.extend(read_entries(predecessor_id, cwd))
        current = predecessor_id

    return chain
