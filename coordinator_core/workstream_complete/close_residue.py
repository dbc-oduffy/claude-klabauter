"""coordinator_core.workstream_complete.close_residue -- what a session still
owns when its close runs: its claimed-but-uncommitted files and its held,
unstamped run batons.

`snapshot` is taken BEFORE `run_close_commit_and_release_claims`, whose
`finally` releases the session's whole claim surface and so empties the very
set being measured. The payload builders feed `completion_verdict`'s
`owned_uncommitted` / `unstamped_run_batons` readers; `landing_states` is the
shared two-hop index/HEAD/worktree landing check.

Never imports apply.py (it imports this module). Residue is measured over the
session's own snapshot only, never the shared dirty set. Spawns: none, except
one batched `git check-ignore` when an owned untracked file exists, and one
batched `git status` when an owned untracked directory exists (a directory
claim with nothing committable beneath it is dropped, never expanded).
"""

import os
import time
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Optional, Union

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.git.content_hash import content_matches_index_sha
from coordinator_core.git.divergence import _run_git
from coordinator_core.git.git_index import IndexParseError as _IndexStatParseError
from coordinator_core.git.git_index import scoped_status as _scoped_worktree_status
from coordinator_core.git.git_state import IndexParseError as _IndexParseError
from coordinator_core.git.git_state import head_blobs as _head_blobs
from coordinator_core.git.git_state import read_index as _read_index
from coordinator_core.ops.ceremony import git_native
from coordinator_core.session import core, record_homes, touch_record
from coordinator_core.session.claims import list_claims_by_session_checked
from coordinator_core.session.scope import _TOUCH_RECORD_FILENAME, own_path_claims

LANDED = "landed"
UNTRACKED = "untracked"
UNCOMMITTED = "uncommitted"
DIRTY = "dirty"
DELETED = "deleted"
ABSENT = "absent"
UNVERIFIED = "unverified"

#: States that mean the path's current bytes are not durably in HEAD.
NOT_DURABLE_STATES = frozenset({UNTRACKED, UNCOMMITTED, DIRTY, DELETED})

#: `deployment_state` values of a baton whose work is still open.
_OPEN_BATON_STATES = frozenset({"awaiting_gate", "ready_to_fire", "in_flight"})

_HANDOFF_CLAIM_CLASS = "handoff-claims"


class ResidueSnapshot(NamedTuple):
    owned_paths: tuple[str, ...]
    held_batons: tuple[str, ...]
    degraded: bool


def snapshot(worktree_root: Union[Path, str], sid: Optional[str]) -> ResidueSnapshot:
    """The session's owned paths and held handoff-claim basenames right now.

    `degraded` is set from the claim accessor's errors and from the session's
    own touch-record read; `own_path_claims` drops a degraded sink silently,
    so it cannot report this itself. A falsy `sid` yields a degraded, empty
    snapshot."""
    if not sid:
        return ResidueSnapshot((), (), True)
    cwd = str(worktree_root)
    degraded = False
    try:
        owned = tuple(own_path_claims(sid, cwd))
    except (OSError, ValueError):
        owned = ()
        degraded = True
    try:
        matches, errors = list_claims_by_session_checked(sid, cwd=cwd)
    except (OSError, ValueError):
        matches, errors = [], ["claim read raised"]
    if errors:
        degraded = True
    held = tuple(basename for class_, basename in matches if class_ == _HANDOFF_CLAIM_CLASS)
    try:
        sdir = core.session_dir(sid, cwd)
    except ValueError:
        sdir = ""
    if sdir:
        sink = os.path.join(sdir, _TOUCH_RECORD_FILENAME)
        if os.path.exists(sink) and touch_record.read_stream_claims_and_held_kind(sink)[2]:
            degraded = True
    return ResidueSnapshot(owned, held, degraded)


def landing_states(worktree_root: Union[Path, str], rels: Iterable[str]) -> dict[str, str]:
    """`{rel: state}` for repo-relative forward-slash `rels`; state is one of
    `landed`, `untracked` (not in the index), `uncommitted` (staged, absent
    from HEAD), `dirty` (tracked, index or worktree differs from HEAD),
    `deleted` (in HEAD, gone from disk), `absent` (not on disk, not in HEAD:
    nothing to commit) or `unverified` (the index, HEAD or worktree could not
    be read, or the byte comparison declined).

    Two hops, because HEAD is the only durable side: worktree to index by
    stat (settled by `content_matches_index_sha` on a mismatch), index to HEAD
    by `(mode, sha)`. One index read and one HEAD tree read for all paths."""
    paths = list(dict.fromkeys(rels))
    states = {rel: UNVERIFIED for rel in paths}
    if not paths:
        return states
    root = Path(worktree_root)
    if not (root / ".git").exists():
        return states
    try:
        index = _read_index(root, fresh=True)
        heads = _head_blobs(root, paths)
        in_sync: list[str] = []
        for rel in paths:
            index_entry = index.get(rel)
            head_entry = heads.get(rel)
            on_disk = (root / rel).exists()
            if index_entry is None:
                if on_disk:
                    states[rel] = UNTRACKED
                elif head_entry is None:
                    states[rel] = ABSENT
                else:
                    states[rel] = DELETED
            elif head_entry is None:
                states[rel] = UNCOMMITTED
            elif (index_entry.mode, index_entry.sha) != head_entry:
                states[rel] = DIRTY
            else:
                in_sync.append(rel)
        if in_sync:
            verdicts = _scoped_worktree_status(root, in_sync)
            for rel in in_sync:
                verdict = verdicts.get(rel)
                if verdict == "candidate":
                    matches = content_matches_index_sha(root, rel, index.get(rel).sha)
                    if matches is not None:
                        verdict = "clean" if matches else "dirty"
                    else:
                        rc, out = _run_git(["status", "--porcelain", "--", rel], cwd=str(root))
                        verdict = None if rc != 0 else ("dirty" if out.strip() else "clean")
                if verdict == "clean":
                    states[rel] = LANDED
                elif verdict == "dirty":
                    states[rel] = DIRTY
                elif verdict == "deleted":
                    states[rel] = DELETED
    except (OSError, ValueError, _IndexParseError, _IndexStatParseError):
        return {rel: UNVERIFIED for rel in paths}
    return states


def _payload(verdict: str, items: Iterable[str] = (), contested: Iterable[str] = (), reason: Optional[str] = None) -> dict[str, Any]:
    return {"verdict": verdict, "items": list(items), "contested": list(contested), "reason": reason}


def _ignored(worktree_root: Union[Path, str], rels: list[str]) -> Optional[set[str]]:
    """Subset of `rels` that `.gitignore` matches, or `None` when the check
    itself failed."""
    result = git_native.check_ignore(worktree_root, rels)
    if result.returncode not in (0, 1):
        return None
    return {match[3] for match in git_native.parse_check_ignore_stdin_z(result.stdout)}


def _porcelain_paths(stdout: str) -> list[str]:
    """Paths from `status --porcelain=v1 -z` output; a rename/copy record's
    second NUL field (the origin path) is skipped."""
    fields = stdout.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC":
            i += 1
    return paths


def _under_dot_git(rel: str) -> bool:
    """Paths inside `.git/` can never be committed, so they are never residue."""
    return rel.replace("\\", "/").split("/", 1)[0] == ".git"


def owned_uncommitted_payload(
    worktree_root: Union[Path, str],
    snap: ResidueSnapshot,
    *,
    excluded: Iterable[str] = (),
    peer_claimed: Iterable[str] = (),
) -> dict[str, Any]:
    """Owned paths not durably in HEAD, minus `excluded` and gitignored
    untracked paths. Paths in `peer_claimed` are `contested`: reported, never
    residue, since a co-held file can stay dirty with the peer's hunks."""
    if snap.degraded:
        return _payload("indeterminate", reason="session claim read degraded")
    if not snap.owned_paths:
        return _payload("not-applicable")
    skip = set(excluded)
    candidates = [rel for rel in snap.owned_paths if rel not in skip and not _under_dot_git(rel)]
    states = landing_states(worktree_root, candidates)
    untracked = [rel for rel in candidates if states[rel] == UNTRACKED]
    root = Path(worktree_root)
    dirs = [rel for rel in untracked if (root / rel).is_dir()]
    if dirs:
        status = git_native.status_porcelain_scoped(worktree_root, dirs)
        if status.returncode != 0:
            return _payload("indeterminate", reason="git status failed for directory claim(s)")
        dirty_paths = _porcelain_paths(status.stdout)
        for rel in dirs:
            prefix = rel.rstrip("/") + "/"
            if not any(path.startswith(prefix) for path in dirty_paths):
                states.pop(rel, None)
        untracked = [rel for rel in untracked if rel not in dirs]
    if untracked:
        ignored = _ignored(worktree_root, untracked)
        if ignored is None:
            return _payload("indeterminate", reason="git check-ignore failed")
        for rel in ignored:
            states.pop(rel, None)
    peers = set(peer_claimed)
    items = [rel for rel, state in states.items() if state in NOT_DURABLE_STATES and rel not in peers]
    contested = [rel for rel, state in states.items() if state in NOT_DURABLE_STATES and rel in peers]
    if items:
        return _payload("open", items, contested)
    unverified = [rel for rel, state in states.items() if state == UNVERIFIED]
    if unverified:
        return _payload("indeterminate", contested=contested, reason=f"landing unverified for {len(unverified)} path(s)")
    return _payload("clean", contested=contested)


def unstamped_run_batons_payload(
    worktree_root: Union[Path, str],
    snap: ResidueSnapshot,
    *,
    dispositions: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Held batons still open. Pre-close (`dispositions` given): those with no
    disposition entry. Post-close (`None`): those whose
    `state/handoffs/<baton>` is still present with an open `deployment_state`."""
    if snap.degraded:
        return _payload("indeterminate", reason="session claim read degraded")
    if not snap.held_batons:
        return _payload("not-applicable")
    if dispositions is not None:
        return _payload_from_items([b for b in snap.held_batons if b not in dispositions])
    items: list[str] = []
    for baton in snap.held_batons:
        path = Path(worktree_root) / "state" / "handoffs" / baton
        if not path.is_file():
            continue
        try:
            frontmatter = parse_frontmatter(path.read_text(encoding="utf-8")).get("frontmatter")
        except (OSError, UnicodeDecodeError):
            frontmatter = None
        if not isinstance(frontmatter, dict):
            return _payload("indeterminate", reason=f"baton {baton!r} frontmatter unreadable")
        if frontmatter.get("deployment_state") in _OPEN_BATON_STATES:
            items.append(baton)
    return _payload_from_items(items)


def _payload_from_items(items: list[str]) -> dict[str, Any]:
    return _payload("open", items) if items else _payload("clean")


#: An untracked sizing younger than this may belong to a live session.
ORPHAN_MIN_AGE_S = 3600

_SIZING_SCHEMA = Path(__file__).resolve().parents[1] / "frontmatter" / "schemas" / "sizing-object.schema.json"


def orphan_sizing_paths(
    worktree_root: Union[Path, str],
    *,
    exclude: Iterable[str] = (),
    peer_claimed: Iterable[str] = (),
) -> tuple[list[str], list[tuple[str, str]]]:
    """Which untracked sizings no live session owns: `(sweepable, refused)`.

    Candidates are the flat `*.yaml` files under the sizings record home that
    are absent from the index, not in `exclude` or `peer_claimed`, not
    gitignored, and at least `ORPHAN_MIN_AGE_S` old by mtime. A candidate
    valid against the sizing-object schema is sweepable; an invalid or
    unparsable one is refused with its reason. Nothing is ever deleted. An
    unreadable index or failed ignore check yields `([], [])`."""
    import yaml

    from coordinator_core.frontmatter.schema_validate import format_validation_errors, validate_frontmatter

    root = Path(worktree_root)
    home = record_homes.home_dir("", "sizings").replace(os.sep, "/")
    try:
        index = _read_index(root, fresh=True)
        skip = set(exclude) | set(peer_claimed)
        cutoff = time.time() - ORPHAN_MIN_AGE_S
        candidates: list[str] = []
        with os.scandir(root / home) as it:
            for entry in it:
                rel = f"{home}/{entry.name}"
                if not entry.name.endswith(".yaml") or rel in index or rel in skip:
                    continue
                if entry.is_file() and entry.stat().st_mtime <= cutoff:
                    candidates.append(rel)
    except (OSError, ValueError, _IndexParseError):
        return [], []
    candidates.sort()
    if not candidates:
        return [], []
    ignored = _ignored(root, candidates)
    if ignored is None:
        return [], []
    sweepable: list[str] = []
    refused: list[tuple[str, str]] = []
    for rel in candidates:
        if rel in ignored:
            continue
        try:
            errors = validate_frontmatter(yaml.safe_load((root / rel).read_text(encoding="utf-8")) or {}, _SIZING_SCHEMA)
        except Exception as exc:  # noqa: BLE001 -- surfaced as a refusal
            refused.append((rel, str(exc)))
            continue
        if errors:
            refused.append((rel, format_validation_errors(errors)))
        else:
            sweepable.append(rel)
    return sweepable, refused


def owned_dirty_sizing_paths(
    worktree_root: Union[Path, str],
    snap: ResidueSnapshot,
    *,
    peer_claimed: Iterable[str] = (),
) -> list[str]:
    """Owned paths under the sizings record home that are untracked,
    uncommitted or dirty, minus those a live peer also claims."""
    prefix = record_homes.home_dir("", "sizings").replace(os.sep, "/") + "/"
    peers = set(peer_claimed)
    candidates = [rel for rel in snap.owned_paths if rel.startswith(prefix) and rel not in peers]
    states = landing_states(worktree_root, candidates)
    return [rel for rel in candidates if states[rel] in (UNTRACKED, UNCOMMITTED, DIRTY)]
