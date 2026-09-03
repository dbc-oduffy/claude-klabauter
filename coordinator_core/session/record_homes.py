"""The work-record root: one owner for where a work-record kind lives, on
both the write side (a directory join) and the read side (a path pattern).

WHAT THIS IS. The declared set of work-record kinds, which are the first
segments under `state/` (`HOMES`), plus the accessors over it: `home_dir` and
`record_path` on the write side, `home_pattern` on the read side. Nothing
here reads, writes, or interprets a record's contents; this module owns
only WHERE a kind lives and WHETHER a given path is one.

WHY IT EXISTS. The machinery relocation
(`docs/plans/2026-09-02-state-keeps-the-work-not-the-machinery.md`) gave
derived-state buckets a single owner, `session/machinery_paths.py`. The
work-record half of `state/` got no equivalent: a record's home is still a
`"state/<segment>"` literal spelled out at every site that needs one --
271 code-context sites across 119 production files, measured the session
this module was written. A missed WRITER puts a file in the wrong place,
which someone sees. A missed READER returns a well-formed empty answer --
`artifact_owner._SUBAGENT_SHARE_DIR_RE` hard-required the pre-relocation
root and silently reported "no owner" for every post-relocation sidecar
until fixed by hand at `2acd5ca032`. That is the half this module exists
to close: `home_pattern` is the one place a reader asks "is this path a
record of kind X," instead of each reader spelling its own guess.

THE DECLARATION IS CODE, NOT A DATA FILE. The simpler shape -- read
`docs/reference/state-corpus-allowlist.txt` at import and derive `HOMES`
from it -- was considered and rejected on two counts. (1) `session/` is on
the per-turn Stop-family hook path, where `machinery_paths.py`'s own
negative-spec forbids import-time work: a file read per hook invocation,
on a box running ~50 concurrent sessions, is spend against the 500ms
brightline for a value that never changes within a process. (2) It points
the arrow the wrong way -- a module deriving itself from a file that
records where files ended up is descriptive by construction, which is the
exact property this plan exists to remove. `HOMES` is declared here, and
`session/tests/test_record_homes.py` asserts it AGREES with the allowlist
at test time, so a divergence names which side moved instead of one side
silently winning.

`HOMES` was seeded by measuring, not by copying a count out of a plan:
every directory-shaped segment the allowlist names that is also a real
`git`-tracked directory under `state/` at the time this module was
written, EXCLUDING the machinery relocation set
(`state/memo-outbox` and its siblings) that `machinery_paths.py` already
owns and that the allowlist itself deliberately excludes
(`test_machinery_paths.test_every_tracked_state_first_segment_is_on_the_allowlist`'s
own `relocation_set`). A loose top-level file (`cruft-sweep-log.md`, a
dated one-off note) is not a work-record KIND -- there is no directory to
join a basename onto -- so those allowlist lines are not represented here.

WHY `session/` AND NOT SOMEWHERE ELSE. Same reasoning as
`machinery_paths.py`'s own docstring: `session/` is imported by both
`group_em` and `hooks` and imports neither, so homing a path accessor here
never creates a dependency arrow the relocation exists to avoid.

Negative-spec:
    - Owns the write-side join and the read-side pattern. NOT the record
      shapes, NOT which sessions may write a kind, NOT the allowlist file
      itself (read-only from this chunk's Anti-scope -- it sits in a live
      peer plan's scope and is never edited here).
    - Stdlib only, and no import-time work -- same Stop-family-hook
      constraint `machinery_paths.py` states for the same package.
    - Never creates a directory. A caller that writes makes its own, so a
      reader importing this module leaves no trail of empty dirs behind
      (`machinery_paths.py`'s own rule, inherited rather than restated
      differently).
    - `home_pattern` matches EITHER root (`state/` and
      `.coordinator-local/`) rather than only the current one. No work
      record has moved -- Out of scope, this plan's own text: "nothing on
      disk shifts" -- but the read side exists precisely so the next
      relocation does not repeat `2acd5ca032`'s silent-empty-answer
      failure, and a pattern that only accepts today's root would recreate
      the defect this module exists to close the day a home actually
      moves.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Pattern

#: The declared work-record kinds -- the closed set of first segments under
#: `state/`. The SSOT (module docstring): declared here, never derived from
#: the allowlist at import time.
#:
#: A SET, not a kind->segment mapping. A record kind IS its directory
#: segment; the two are one fact, and a mapping would be a second spelling
#: of it -- 43 identity rows inviting exactly the divergence this module
#: exists to make impossible. What the set buys is membership, which is the
#: whole read-side contract: an undeclared kind raises rather than resolving
#: to a plausible path nobody declared. If a kind and its segment ever must
#: genuinely differ, that is a real fact and earns a real mapping THEN --
#: never a mapping kept empty-handed against the day it might.
HOMES = frozenset({
    "audits",
    "backlogs",
    "baselines",
    "bash-guards",
    "bug-backlog",
    "capabilities",
    "cross-repo-commitments",
    "cross-repo-declarations",
    "cross-repo-outbound",
    "debt-backlog",
    "fact-contract-gate",
    "generator-provenance",
    "goals",
    "handoffs",
    "health",
    "improvement-queue",
    "initiatives",
    "lessons",
    "measurements",
    "memos",
    "mise-inventory",
    "parked",
    "pending-patches",
    "problems",
    "records",
    "recovery",
    "red-baseline-2026-07-20",
    "review-claims",
    "review-findings",
    "review-sidecars",
    "review-slices",
    "reviews",
    "roadmap",
    "sizings",
    "spawn-deletions",
    "strategic",
    "tasks",
    "test-red",
    "tests",
    "warm",
    "wave-maps",
    "week-changelog",
    "workstreams",
})


def _declared(kind: str) -> str:
    """`kind` back, once it is a declared one. Raises `KeyError` otherwise.

    `KeyError` and not a bespoke exception because every caller here used
    to index a dict, and a reader tracing a failure should land on the same
    "you asked for a kind nobody declared" answer the mapping gave.
    """
    if kind not in HOMES:
        raise KeyError(kind)
    return kind


def home_dir(repo_root: str, kind: str) -> str:
    """`<repo_root>/state/<kind>` -- the directory a record of this declared
    kind lives in.

    `repo_root` is always a parameter, never resolved from `$HOME` or a
    single-machine literal. Raises `KeyError` for an undeclared kind --
    the caller asked this module a question it does not have an answer
    for, and a guess would be exactly the silent-empty-answer failure this
    module exists to prevent on the read side; the write side earns the
    same discipline by not inventing a segment either.
    """
    return os.path.join(repo_root, "state", _declared(kind))


def record_path(repo_root: str, kind: str, basename: str) -> str:
    """`<home_dir(repo_root, kind)>/<basename>` -- one record file of this
    kind. Never creates the directory (module docstring) -- a caller that
    writes makes its own.
    """
    return os.path.join(home_dir(repo_root, kind), basename)


@lru_cache(maxsize=None)
def home_pattern(kind: str) -> Pattern[str]:
    """Compiled pattern matching a path of `kind` under EITHER root
    (`state/` or `.coordinator-local/`), accepting `/` and `\\` alike
    regardless of host OS.

    Precedent: `artifact_owner._SUBAGENT_SHARE_DIR_RE` and
    `artifact_owner._basename_cross_platform` already state and accept
    this same tradeoff -- a genuine POSIX filename containing a literal
    backslash byte would split on it too. This module's record-kind
    universe (machine-authored directory segments, never a filename) never
    contains one, so the tradeoff is inherited unchanged rather than
    rediscovered.

    Raises `KeyError` for an undeclared kind, same as `home_dir`.
    """
    segment = _declared(kind)
    return re.compile(
        r"(?:^|[/\\])(?:state|\.coordinator-local)[/\\]"
        + re.escape(segment)
        + r"(?:[/\\]|$)"
    )
