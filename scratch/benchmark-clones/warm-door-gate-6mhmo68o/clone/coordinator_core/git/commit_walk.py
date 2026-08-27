"""coordinator_core.git.commit_walk -- in-process session->commit
attribution: which commits belong to which session, read off the object
store instead of spawned `git log`.

Purpose: the close ceremony's gate path asked that one question three ways
and paid a `git` process for each -- a trailer-map `log`, a per-session
`log --reverse --grep=^Session-Id:`, and a `show --raw --numstat` over the
resulting sha set. Measured on one `brief()`, git spawns were **92%** of
the gate path's cost (3,805ms of 4,128ms across four `subprocess` calls),
so this is not a spawn-count tidy-up: the spawns ARE the cost. Four prior
rounds on that path tried to make each call cheaper and each measured zero,
because a cheaper call is still a process.

This module is the shared home for the walk `pickup_assemble` already
proved. That module replaced its own `git log`/`git show` spawns with an
object-store walk and pinned the result with parity tests against real
`git` (`coordinator_core/tests/test_pickup_assemble_git_readmodel_
parity.py` -- `show`-at-revision, `log --oneline` with a path filter,
`cat-file`, `--since` bounding). What lives here is that walk, minus the
`pickup_assemble` decision-object machinery it was embedded in, so
`workstream_complete` can use it without importing across a sibling
package boundary.

MEASURED COST, AND THE REASON THIS MODULE IS NOT WIRED INTO THE GATE PATH
(2026-08-26, against this repo, 630-commit window):

    spawned `git log` trailer map   ....    34.7 ms
    this module, same answer        ....  1804.2 ms   (686 object reads,
                                                       2.63 ms each)

**52x slower, at exact parity** -- 630/630 trailer entries and 7/7 session
shas identical to real `git`. Correctness was never the problem. Each
commit read decompresses a zlib stream and, in a pack, usually walks a
delta chain with another decompress per link; git does the same work at
~0.05 ms per object in C. `git_objects`' mmap made file ACCESS free and
did nothing for per-object DECODE, which is the entire cost.

The rule this establishes, and the one worth carrying: `git ls-files` was
replaced in process and won, because it is ONE index-file parse with NO
graph walk. Anything that walks N commit objects loses to a single spawn
for any N above roughly a dozen. **Convert file-parse questions; never
convert graph walks.** This module is kept as the parity-verified evidence
for that boundary, not as a component to reach for.

Negative-spec:

    - This is an APPROXIMATION of `git log`'s date-order topological walk,
      by the same construction and with the same caveat as the original:
      a parent is only pushed once its child is popped, so a parent whose
      committer timestamp is later than its already-popped child (clock
      skew, rebase, import) can pop next and produce a local increase in
      the emitted order. Nothing here may depend on a strictly
      non-increasing sequence; `walk_since`'s bound tolerates local
      increases with a slop budget rather than a hard `break`.

    - TRAILER ATTRIBUTION IS THE ONLY SOUND SELECTOR ON A SHARED BRANCH,
      and this module offers no temporal-only alternative on purpose. A
      `--since` window over a branch ~50 concurrent sessions commit to
      sweeps every peer that committed during the session -- measured live
      at 17 commits for one session's 1. `since_epoch` here BOUNDS THE
      WALK, it never selects: every public entry point below still filters
      on the `Session-Id` trailer.

    - `session_owned_shas` returns `[]` for "this session owns no commits"
      and that is a real answer; a caller that cannot distinguish it from
      "could not tell" must check `head_sha` resolution itself. This
      module never invents a sentinel to paper over that difference.
"""

from __future__ import annotations

import heapq
import re
from pathlib import Path
from typing import Any, Iterator, Optional

from coordinator_core.git.git_objects import _read_object

#: Out-of-window commits tolerated before a bounded walk gives up. Mirrors
#: `pickup_assemble`'s own budget; see `walk_since` for why a hard `break`
#: is wrong here.
_SINCE_SLOP = 50

_SESSION_ID_TRAILER_RE = re.compile(r"^Session-Id:[ \t]*(\S+)[ \t]*$", re.MULTILINE)


def parse_commit(content: bytes) -> dict[str, Any]:
    """`{tree, parents, committer_epoch, message}` from a raw commit object.

    Carries the full `message` rather than just the subject -- a trailer
    lives in the body, which is the whole reason this module exists as a
    sibling of `pickup_assemble._parse_commit` rather than a reuse of it.
    """
    text = content.decode("utf-8", errors="replace")
    parents: list[str] = []
    tree: Optional[str] = None
    committer_epoch: Optional[int] = None
    for line in text.split("\n"):
        if not line:
            break
        if line.startswith("tree "):
            tree = line[5:].strip()
        elif line.startswith("parent "):
            parents.append(line[7:].strip())
        elif line.startswith("committer "):
            tokens = line.split(" ")
            try:
                committer_epoch = int(tokens[-2])
            except (IndexError, ValueError):
                committer_epoch = None
    _header, _, message = text.partition("\n\n")
    return {
        "tree": tree,
        "parents": parents,
        "committer_epoch": committer_epoch,
        "message": message,
    }


#: Content-addressed, so sound for this process's whole lifetime -- a sha
#: cannot come to name different bytes. Same reasoning as
#: `git_objects._OBJECT_CACHE`, and bounded for the same reason: this
#: module is reachable from a warm long-running engine.
_COMMIT_CACHE_MAX_ENTRIES = 4096
_COMMIT_CACHE: "dict[tuple[str, str], Optional[dict[str, Any]]]" = {}


def commit_meta(common_dir: Path, sha: str) -> Optional[dict[str, Any]]:
    """The one read-and-parse entry point for a commit object here, so a
    given commit costs at most one object read per process."""
    key = (str(common_dir), sha)
    if key in _COMMIT_CACHE:
        return _COMMIT_CACHE[key]
    obj = _read_object(common_dir, sha)
    parsed: Optional[dict[str, Any]] = None
    if obj is not None and obj[0] == "commit":
        parsed = parse_commit(obj[1])
    if len(_COMMIT_CACHE) >= _COMMIT_CACHE_MAX_ENTRIES:
        _COMMIT_CACHE.clear()
    _COMMIT_CACHE[key] = parsed
    return parsed


def walk(common_dir: Path, start_sha: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yields `(sha, commit)` from `start_sha`, max-heap ordered on
    committer epoch. See this module's negative-spec for the ordering
    caveat -- it is an approximation, deliberately."""
    seen: set[str] = set()
    heap: list[tuple[int, str, dict[str, Any]]] = []

    def push(sha: str) -> None:
        if sha in seen:
            return
        seen.add(sha)
        commit = commit_meta(common_dir, sha)
        if commit is None:
            return
        heapq.heappush(heap, (-(commit["committer_epoch"] or 0), sha, commit))

    push(start_sha)
    while heap:
        _neg_ts, sha, commit = heapq.heappop(heap)
        yield sha, commit
        for parent in commit["parents"]:
            push(parent)


def walk_since(
    common_dir: Path, head_sha: str, since_epoch: Optional[int]
) -> Iterator[tuple[str, dict[str, Any]]]:
    """`walk`, bounded to a `--since` window with a slop budget.

    Committer dates are not monotonic along parent edges, so a plain
    `break` on the first out-of-window commit silently truncates: a
    skewed-newer ancestor may only be reachable by descending THROUGH an
    out-of-window commit. Such a commit is never yielded, but its parents
    are still pushed; each in-window commit resets the budget, and the walk
    gives up only once the budget is exhausted. `since_epoch=None` yields
    everything.
    """
    slop = _SINCE_SLOP
    for sha, commit in walk(common_dir, head_sha):
        ts = commit["committer_epoch"] or 0
        if since_epoch is not None and ts < since_epoch:
            slop -= 1
            if slop == 0:
                break
            continue  # not emitted, but its parents still enter the walk
        slop = _SINCE_SLOP
        yield sha, commit


def session_id_of(commit: dict[str, Any]) -> Optional[str]:
    """The `Session-Id:` trailer value, or None. Last occurrence wins,
    matching `git interpret-trailers`' own precedence for a repeated key."""
    matches = _SESSION_ID_TRAILER_RE.findall(commit.get("message") or "")
    return matches[-1] if matches else None


def session_trailer_map(
    common_dir: Path, head_sha: str, since_epoch: Optional[int] = None
) -> dict[str, str]:
    """`{sha: session_id}` for every trailer-carrying commit in the window.

    Replaces `git log --format=%H%x1f%(trailers:key=Session-Id,valueonly)
    --since=…`. Commits with no trailer are absent rather than mapped to
    an empty string -- "no attribution" and "attributed to nothing" are
    different facts, and a caller keying on membership must be able to
    tell them apart.
    """
    result: dict[str, str] = {}
    for sha, commit in walk_since(common_dir, head_sha, since_epoch):
        sid = session_id_of(commit)
        if sid:
            result[sha] = sid
    return result


def session_owned_shas(
    common_dir: Path, head_sha: str, session_id: str, since_epoch: Optional[int] = None
) -> list[str]:
    """This session's own commits, oldest-first.

    Replaces `git log --reverse --grep=^Session-Id: <sid> --format=%H`.
    Selection is on the trailer, never on the window -- see this module's
    negative-spec. `since_epoch` only bounds how far back the walk goes,
    and a caller that passes one is asserting the session started inside
    it; `None` walks to the root, which is what an unbounded `--grep` did.
    """
    owned: list[tuple[int, str]] = []
    for sha, commit in walk_since(common_dir, head_sha, since_epoch):
        if session_id_of(commit) == session_id:
            owned.append((commit["committer_epoch"] or 0, sha))
    owned.sort()
    return [sha for _ts, sha in owned]
