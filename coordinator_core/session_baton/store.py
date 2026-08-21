"""
coordinator_core.session_baton.store — the lazy, session-scoped baton record.

Spec backlink: docs/plans/2026-08-18-a-session-always-has-a-baton.md § C1 (D-A, D-B).

Store location: ``<git-common-dir>/coordinator-sessions/<sid>/baton.json`` —
the SAME per-session directory the existing claim ledger
(``coordinator_core.session.claims``), write-bump anchor, and other
per-session state already live in (see ``coordinator_core.session.core ::
session_dir``). This module adds a file to a directory the system already
creates and manages per session, rather than introducing a new lifecycle
object — so it never creates that directory itself (rule ``30cd75d80``):
``write_baton``/``merge_baton`` require the directory to already exist and
no-op, observably (a ``stderr`` advisory), when it does not.

HARD CONSTRAINT (C1's own scope bar): this module writes NOTHING outside
``.git/`` — no path under ``state/handoffs/`` or anywhere else in the tracked
tree is ever touched here. Promotion into a real handoff artifact is a LATER
chunk (C3, ``ops/session_baton_promote.py``); this module has no knowledge
of it and no promotion side effect fires from any function below.

Record shape (all fields optional on read — a fresh/corrupt file reads as
the all-defaults skeleton from :func:`default_record`):

    {
      "session_id": str,
      "created_at": str,          # ISO-8601 UTC, set once at first write
      "first_prompt": str | None,
      "title": str | None,        # EM-supplied
      "intent": str | None,       # EM-supplied
      "adopted_artifacts": [str, ...],
      "minted_artifacts": [str, ...],   # handed to the session, not picked up
      "commits": [str, ...],
      "promoted_to": str | None,  # nullable — set by C3's promotion op
    }

Known limitation, recorded rather than fixed here (Director review F6):
``ops/session/reap.py :: _reap_stale_sessions`` ARCHIVES a >24h-inactive
session dir to ``.archive/<sid>-<date>/``; nothing DELETES it afterward — the
only pruner, ``_prune_stale_agent_archive``, is scoped to
``.archive/_agents-*`` at 14 days. This tree currently holds 3,101 archived
session dirs against 43 live. Harmless for the baton itself (it is never
offered as pickup-able work either way — see D-A), but this module's own
docstrings and any caller-facing text must never say "collected" or imply
pruning happens. No pruning chunk is added here; this is a stated, accepted
limitation, not new scope.

Concurrency: every mutating entrypoint below (``write_baton``,
``merge_baton``) goes through :func:`coordinator_core.locked_write.locked_rmw`
— a cross-process flock keyed to the baton file itself, anchored at the
owning repo's root (derived from the ``.git`` ancestor of the target path,
mirroring ``coordinator_core.session.claims::_atomic_dedup_append_lock_
anchor``'s own derivation for the sibling ``touched.txt`` writer in the same
per-session directory). Two sessions (or a session and a hook subprocess)
racing a mint/merge on the SAME sid serialise correctly; a reader
(``read_baton``) is unlocked — a torn read degrades to the skeleton default,
never a crash, matching this store's overall degrade-to-default posture.

Negative-spec:
    - Do NOT write anything outside ``.git/coordinator-sessions/<sid>/``.
    - Do NOT treat this record as authoritative over commit trailers (D-B —
      out of C1's scope; the rebuild-fallback discipline is a C4/C5 concern,
      this module just stores whatever list a later caller gives it).
    - Do NOT prune or delete stale baton directories here — see the reaper
      note above; that is explicitly out of scope for this chunk.
    - Do NOT create the per-session directory — it is the system's own
      lifecycle object (see the module docstring above); require it to
      already exist and no-op, observably, otherwise.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.locked_write import LockTimeout, locked_rmw
from coordinator_core.session import core

BATON_FILENAME = "baton.json"

#: Sentinel distinguishing "caller did not pass this kwarg" from "caller
#: explicitly wants this field set to None" (load-bearing for `promoted_to`,
#: which is nullable-by-design and must be settable back to None).
_UNSET = object()

#: Bounded wait for the cross-process flock — the same order of magnitude as
#: the sibling `touched.txt` writers in this same per-session directory
#: (`coordinator_core.session.claims._ATOMIC_DEDUP_APPEND_LOCK_TIMEOUT_SECS`),
#: deliberately NOT the 10s default: a hook-invoked mint firing on every
#: first prompt of every session (39-92/day, see D-A) must never itself
#: become the slow op.
_LOCK_TIMEOUT_SECS = 2.0


def default_record(session_id: str) -> Dict[str, Any]:
    """The all-defaults skeleton a fresh, missing, or corrupt baton file
    reads as. Never carries a ``created_at`` — callers that mint a new
    record stamp that themselves, once, at first write."""
    return {
        "session_id": session_id,
        "created_at": None,
        "first_prompt": None,
        "title": None,
        "intent": None,
        "adopted_artifacts": [],
        # What this session was HANDED without asking, as opposed to
        # `adopted_artifacts`' what-this-session-picked-up. Exists so an
        # auto-mint is legible to the session accountable for it: DoE's
        # `runtime-tripwire-em-check.py` announces a baton off this record at
        # the next `UserPromptSubmit`, and could not announce a minted handoff
        # because nothing wrote its path anywhere (their reply to
        # `2026-08-20-claude-klabauter-em-handoff-mint-has-no-announce`; the
        # advisory leg is theirs, this signal is ours).
        #
        # A list field rather than a sibling marker file deliberately: their
        # hook already reads this whole record once per prompt for its own
        # field gate, so a key costs it zero extra I/O, while a marker costs a
        # stat and invents a second convention beside the one that exists.
        # Additive and `.get()`-safe, so a reader that predates it is
        # unaffected -- there is no JSON schema over this record, only this
        # function.
        "minted_artifacts": [],
        "commits": [],
        "promoted_to": None,
        # Closure. A journal is not born resolved and is never pickup-able —
        # it is live for as long as the session is the thing accruing work.
        # When a pickup adopts an artifact, the session's work belongs to THAT
        # baton from then on, and this record stops being the live one:
        # `closed_at` stamps when, `closed_into` names the artifact it is now
        # an ancestor of. Each field is independently first-wins, like
        # `created_at` — a later adoption in the same session never
        # overwrites an already-set `closed_at` or `closed_into`. The two
        # are set together by every current caller, but this primitive does
        # not enforce that pairing: a caller passing only one non-`None`
        # field can leave the other unset (see `merge_baton`'s docstring).
        #
        # Deliberately NOT `promoted_to`: that field means "this record was
        # scaffolded into a handoff under state/handoffs/", a corpus artifact
        # this closure never mints. A closed journal stays a JSON file in
        # `.git/`; closing it is a fact about the record, not a promotion of
        # it.
        "closed_at": None,
        "closed_into": None,
    }


def baton_dir(sid: str, cwd: Optional[str] = None) -> Optional[Path]:
    """The per-session directory the baton file lives in
    (``<git-common-dir>/coordinator-sessions/<sid>``), or ``None`` when
    ``sid`` is empty or the session hub is unresolvable (not a git repo)."""
    if not sid:
        return None
    sdir = core.session_dir(sid, cwd)
    if not sdir:
        return None
    return Path(sdir)


def baton_path(sid: str, cwd: Optional[str] = None) -> Optional[Path]:
    """The absolute path to ``<sid>``'s ``baton.json``, or ``None`` when
    ``sid`` is empty or the session hub is unresolvable."""
    sdir = baton_dir(sid, cwd)
    if sdir is None:
        return None
    return sdir / BATON_FILENAME


def _lock_anchor(path: Path) -> Optional[Path]:
    """Derive the ``locked_rmw`` ``repo_root`` anchor from ``path``'s own
    on-disk position, mirroring
    ``coordinator_core.session.claims::_atomic_dedup_append_lock_anchor``
    for the sibling ``touched.txt`` writer in the same per-session
    directory. ``path`` is always ``<git-common-dir>/coordinator-sessions/
    <sid>/baton.json``, so its third parent is the git common dir itself
    (``parents[0]`` = ``<sid>``, ``parents[1]`` = ``coordinator-sessions``,
    ``parents[2]`` = the git common dir, named ``.git`` for every non-bare,
    non-worktree-private layout this hub uses).

    Returns ``None`` when ``path`` does not have the expected shape (a test
    fixture writing to an ad-hoc temp path, for instance) so the caller can
    fall back to an unlocked write rather than passing a nonsense anchor
    into ``locked_rmw``.
    """
    p = Path(os.path.abspath(str(path)))
    parents = p.parents
    if len(parents) < 3:
        return None
    common_dir = parents[2]
    if common_dir.name != ".git":
        return None
    return common_dir.parent


def _parse_record(text: str, session_id: str) -> Dict[str, Any]:
    """Parse ``text`` as a baton record, degrading to the all-defaults
    skeleton on any read/parse failure — empty file, missing file (``""``
    from ``missing_ok=True``), corrupt JSON, or a JSON value that isn't an
    object. A torn or corrupt baton is never a crash; it is evidence-absent,
    same posture as the rest of this store."""
    if not text:
        return default_record(session_id)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return default_record(session_id)
    if not isinstance(data, dict):
        return default_record(session_id)
    record = default_record(session_id)
    record.update(data)
    # session_id is never taken from disk over the caller's own — the file
    # name (the directory this lives in) is the source of truth for identity.
    record["session_id"] = session_id
    return record


def read_baton(sid: str, cwd: Optional[str] = None) -> Dict[str, Any]:
    """Read ``sid``'s baton record. Unlocked — a read racing a concurrent
    writer may see a slightly stale or (on a non-atomic-write platform, not
    this store's own writer) torn file; either degrades to the all-defaults
    skeleton rather than raising. Never returns ``None`` — a missing session
    or missing file both read as :func:`default_record`."""
    path = baton_path(sid, cwd)
    if path is None or not path.is_file():
        return default_record(sid)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return default_record(sid)
    return _parse_record(text, sid)


def write_baton(
    sid: str, record: Dict[str, Any], cwd: Optional[str] = None
) -> bool:
    """Overwrite ``sid``'s baton record wholesale with ``record`` (its
    ``session_id`` is forced to ``sid`` regardless of what ``record``
    carries). Requires the per-session directory to already exist — this
    store never creates it (rule ``30cd75d80``; see module docstring); a
    missing directory no-ops with a ``stderr`` advisory rather than
    minting one. Locked against concurrent writers via
    :func:`coordinator_core.locked_write.locked_rmw` when an anchor can be
    derived (see :func:`_lock_anchor`); falls back to a direct write when
    it cannot (test fixtures at ad-hoc paths).

    Returns True on success, False when ``sid`` is empty, the session hub is
    unresolvable, the session directory does not yet exist, or the write
    itself fails (``OSError``, ``LockTimeout``).
    """
    if not sid:
        return False
    path = baton_path(sid, cwd)
    if path is None:
        return False

    if not path.parent.is_dir():
        print(
            f"session_baton.write_baton: no session directory for {sid}; write skipped",
            file=sys.stderr,
        )
        return False

    to_write = dict(record)
    to_write["session_id"] = sid
    new_text = json.dumps(to_write, indent=2, sort_keys=True) + "\n"

    anchor = _lock_anchor(path)
    if anchor is not None:
        try:
            locked_rmw(
                path,
                lambda _old, _new=new_text: _new,
                repo_root=anchor,
                timeout=_LOCK_TIMEOUT_SECS,
                missing_ok=True,
            )
            return True
        except (LockTimeout, OSError, RuntimeError):
            pass  # fall through to the unlocked write below

    try:
        path.write_text(new_text, encoding="utf-8", newline="\n")
    except OSError:
        return False
    return True


def merge_baton(
    sid: str,
    cwd: Optional[str] = None,
    *,
    created_at: Any = _UNSET,
    first_prompt: Any = _UNSET,
    title: Any = _UNSET,
    intent: Any = _UNSET,
    adopted_artifacts: Optional[List[str]] = None,
    minted_artifacts: Optional[List[str]] = None,
    commits: Optional[List[str]] = None,
    promoted_to: Any = _UNSET,
    closed_at: Any = _UNSET,
    closed_into: Any = _UNSET,
) -> Optional[Dict[str, Any]]:
    """Read-modify-write merge of ``sid``'s baton record. Idempotent and
    safe to call repeatedly for the same session (a mint op's second call
    updates, never duplicates — see C2's own contract, which this primitive
    exists to serve).

    Scalar fields (``created_at``, ``first_prompt``, ``title``, ``intent``,
    ``promoted_to``) are left UNTOUCHED when the caller omits the kwarg
    entirely, and overwritten with whatever value (including ``None``) the
    caller explicitly passes — this is why the ``_UNSET`` sentinel exists
    rather than defaulting every kwarg to ``None``: ``promoted_to`` is
    nullable by design and a caller must be able to explicitly (re)set it to
    ``None`` without that being indistinguishable from "leave it alone".

    ``closed_at``/``closed_into`` take the same sentinel but are FIRST-WINS on
    a non-``None`` value, EACH INDEPENDENTLY of the other: a journal already
    carrying a value in either field keeps it, so a session that adopts a
    second artifact does not re-point that field at the later adoption.
    Passing an explicit ``None`` still clears either field on its own —
    reopening is deliberate and possible, it just is not what a repeated
    adoption does by accident. This primitive does not enforce that the two
    fields move together: a caller passing only one of them non-``None`` can
    leave the record with one set and the other still ``None``. The sole
    current caller (``pickup_assemble._adopt_into_baton``) always passes both
    together, and any caller stamping a closure is expected to do the same;
    callers wanting an atomicity guarantee must enforce it themselves.

    List fields (``adopted_artifacts``, ``minted_artifacts``, ``commits``) are
    DEDUP-EXTENDED, not
    replaced: passing a list appends any entries not already present
    (order-preserving), so two callers merging overlapping lists never lose
    or duplicate an entry. Passing ``None`` (the default) leaves the
    existing list untouched.

    Returns the merged record on success, or ``None`` when ``sid`` is empty,
    the session hub is unresolvable, or the session directory does not yet
    exist — this store never creates it (rule ``30cd75d80``; see module
    docstring); a missing directory no-ops with a ``stderr`` advisory
    rather than minting one. A concurrent-write failure (``LockTimeout``)
    degrades to a best-effort unlocked read-modify-write (see
    :func:`write_baton`) rather than raising — matches this store's
    fail-open posture: an advisory record must never block its caller.
    """
    if not sid:
        return None
    path = baton_path(sid, cwd)
    if path is None:
        return None

    if not path.parent.is_dir():
        print(
            f"session_baton.merge_baton: no session directory for {sid}; merge skipped",
            file=sys.stderr,
        )
        return None

    merged: Dict[str, Any] = {}

    def _mutate(old_text: str) -> str:
        record = _parse_record(old_text, sid)
        if record.get("created_at") is None and created_at is _UNSET:
            # First-ever write for this session: stamp created_at once.
            record["created_at"] = core.now_iso()
        if created_at is not _UNSET:
            record["created_at"] = created_at
        if first_prompt is not _UNSET:
            record["first_prompt"] = first_prompt
        if title is not _UNSET:
            record["title"] = title
        if intent is not _UNSET:
            record["intent"] = intent
        if promoted_to is not _UNSET:
            record["promoted_to"] = promoted_to
        # First-wins, independently per field: an already-set closed_at (or
        # closed_into) is never overwritten by a later adoption. An explicit
        # None still clears whichever field is passed (the same escape
        # `promoted_to` has), so a caller can reopen deliberately. Callers
        # are expected to pass both together; nothing here enforces that.
        if closed_at is not _UNSET:
            if closed_at is None or record.get("closed_at") is None:
                record["closed_at"] = closed_at
        if closed_into is not _UNSET:
            if closed_into is None or record.get("closed_into") is None:
                record["closed_into"] = closed_into
        if adopted_artifacts:
            existing = list(record.get("adopted_artifacts") or [])
            for entry in adopted_artifacts:
                if entry not in existing:
                    existing.append(entry)
            record["adopted_artifacts"] = existing
        if minted_artifacts:
            existing_m = list(record.get("minted_artifacts") or [])
            for entry in minted_artifacts:
                if entry not in existing_m:
                    existing_m.append(entry)
            record["minted_artifacts"] = existing_m
        if commits:
            existing_c = list(record.get("commits") or [])
            for entry in commits:
                if entry not in existing_c:
                    existing_c.append(entry)
            record["commits"] = existing_c
        record["session_id"] = sid
        merged.clear()
        merged.update(record)
        return json.dumps(record, indent=2, sort_keys=True) + "\n"

    anchor = _lock_anchor(path)
    if anchor is not None:
        try:
            locked_rmw(
                path,
                _mutate,
                repo_root=anchor,
                timeout=_LOCK_TIMEOUT_SECS,
                missing_ok=True,
            )
            return merged
        except (LockTimeout, OSError, RuntimeError):
            pass  # fall through to the unlocked best-effort path below

    # Unlocked fallback (no derivable anchor, or the lock itself failed):
    # best-effort read-modify-write, matching this store's fail-open
    # posture — an advisory record must never raise on its caller.
    try:
        old_text = path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        old_text = ""
    new_text = _mutate(old_text)
    try:
        path.write_text(new_text, encoding="utf-8", newline="\n")
    except OSError:
        return None
    return merged
