"""
coordinator_core.chain_attribution

P2 SHA -> Session-Id attribution: the whole-window sibling family to
`coordinator_core.session_attribution`'s P1 trailer classifier, added for the
chain-terminal discharge path and the N+1 amplification gate (see spec
backlink below). This is a NEW module, not an extension of
`session_attribution`, because that module's own docstring carries a dated
negative-spec paragraph excluding exactly what P2 needs (the grep-based
inverse mapping, and `_UUID_RE`-gated `--grep` interpolation) — putting P2
there would contradict text already committed to that file. See
`state/subagent-share/e5523616-4128-4a05-b6fe-706574127f41/fork-adjudication.md`
§ 10.1 for the full placement argument.

**Deliberate POSTURE DIVERGENCE from P1 — the actual reason for a second
module, not just a naming nicety.** `session_attribution.trailer_foreign_shas`
treats an ambiguous MULTI-VALUED `Session-Id` trailer with first-wins
(today's accidental behaviour, preserved there on purpose — see that
module's own docstring on failure posture). This module treats the same
shape as FOREIGN — fail-closed, not first-wins — because P2 backs a stricter
discharge/gate decision than P1's advisory foreign-set computation. Splitting
the two modules keeps each one's failure posture singular and auditable
rather than branching mid-function on which caller is asking. Two real
commits in this repo's history carry multiple `Session-Id:` trailers today
(verified, not assumed — see § 10.3 of the adjudication doc); this posture
split is exactly what determines how each module treats them.

Two independent signals, each derived from ONE `git log` walk over a range
(never batched across ranges — see anti-scope 1/2/4 of the governing plan):

  1. Trailer signal, from one `git log --format=%H%x1f%P%x1f%(trailers:...)`
     walk over the whole window, INCLUDING merges (`bulk_commit_attribution_map`).
  2. Grep signal, from one `git log --no-merges --grep=...` walk over the same
     window (`bulk_grep_attributed_shas`).

A caller resolves both once per window, then derives any number of per-range
foreign-sets from them via pure in-memory set math
(`foreign_shas_from_window`) — the N+1-to-1 spawn reduction this family
exists to enable. `unattributed_foreign_shas` is the per-range convenience
form with `trailer_foreign_shas`'s exact signature shape, for a caller that
wants the P1-shaped call without P1's posture.

Explicitly OUT of scope for this module (verified, not absorbed):
  - `session_attribution.trailer_foreign_shas` / `bulk_trailer_session_map` —
    P1. Not renamed, not re-signatured, not relocated here. They remain the
    byte-identity anchor for the write guard's Case 1 and are untouched by
    this module's addition.
  - `session_attribution.detect_foreign_commits` /
    `range_is_contiguous_suffix` — a different question (touched-path scope
    membership, not trailer/grep attribution), different signals, no shared
    git query. Out of this family (adjudication § 10.7 item 9).

Spec backlink: pln-kill-the-n-1-git-spawn-class-a-88897a
task A1; full contract in
state/subagent-share/e5523616-4128-4a05-b6fe-706574127f41/fork-adjudication.md
§ 10.2-10.3, § 10.7.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple

from coordinator_core.session_attribution import GitLogFailed

#: Same DI contract session_attribution.GitRunner documents: a "never
#: raises, returns (rc, stdout, stderr)" helper. Injected rather than owned
#: here so a caller's existing subprocess conventions (Windows
#: CREATE_NO_WINDOW, stdin=DEVNULL, etc.) and its existing test-time
#: monkeypatch hook keep working unchanged.
GitRunner = Callable[[List[str], Optional[str]], Tuple[int, str, str]]

#: Shape-validates a session_id before it is interpolated into a `--grep`
#: pattern. `session_id` arrives from on-disk records including the archive
#: union; unvalidated, a value like `.*` matches every commit and silently
#: over-credits a peer's range (adjudication § 10.7 item 3). Mirrors
#: coverage.py's own `_UUID_RE` shape (hex + hyphen), not imported from there
#: to avoid a coupling this module's remit does not need.
_UUID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]+[0-9a-fA-F]$")

#: Record separator for the one-walk format string below. `\x1e` framing (not
#: line-based splitting) is what lets a multi-valued Session-Id trailer —
#: which `%(trailers:...,valueonly)` emits as ONE LINE PER MATCHING TRAILER —
#: be detected as ambiguous instead of silently truncated to its first line.
_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"

_LOG_FORMAT = f"%H{_FIELD_SEP}%P{_FIELD_SEP}%(trailers:key=Session-Id,valueonly){_RECORD_SEP}"


@dataclass(frozen=True)
class CommitAttribution:
    """One commit's attribution evidence from a single window walk.

    `trailer_session_id is None` means the commit carries NO Session-Id
    trailer at all — distinct from a trailer naming a (single or ambiguous)
    session, and distinct from the commit being absent from a
    `bulk_commit_attribution_map` result entirely (which means "outside the
    walked window", nothing else). That three-way distinction is the entire
    semantic difference between P1 (`Dict[str, str]`-shaped,
    untrailered-vs-absent collapsed) and P2, and must never collapse to a
    two-way one (adjudication § 10.7 item 4).

    `trailer_ambiguous=True` marks a commit whose Session-Id trailer atom
    contained MORE THAN ONE value (multi-valued trailer) — this module's
    fail-closed posture treats such a commit as foreign to every session,
    including its own, unlike `session_session_attribution`'s first-wins
    posture for the same shape. When ambiguous, `trailer_session_id` is set
    to the first value only for diagnostic/display purposes — attribution
    logic MUST check `trailer_ambiguous` first, never infer non-ambiguity
    from `trailer_session_id` being non-None.
    """

    sha: str
    trailer_session_id: Optional[str]
    is_merge: bool
    trailer_ambiguous: bool = False


def _parse_log_records(stdout: str) -> List[Tuple[str, str, str]]:
    """Split `stdout` on the record separator, then each record's 3 fields.

    Defensive parse contract (adjudication § 10.3), not optional:
      - split on `\\x1e` first, never line-by-line — a multi-valued
        Session-Id trailer emits one line per value inside a single record,
        and a line-based parser silently drops every value after the first;
      - never `.strip()` the whole stdout blob — `str.strip()` eats `\\x1f`
        as whitespace and would corrupt field framing;
      - `str.split(_FIELD_SEP, maxsplit=2)` so the untrusted variable-length
        trailer field always lands last — a stray 0x1f inside a trailer
        VALUE then corrupts only that value, never shifts `sha`/`parents`.
    """
    records: List[Tuple[str, str, str]] = []
    for raw_record in stdout.split(_RECORD_SEP):
        record = raw_record.strip("\n")
        if not record or _FIELD_SEP not in record:
            continue
        parts = record.split(_FIELD_SEP, 2)
        if len(parts) != 3:
            continue
        sha, parents, trailer = parts
        sha = sha.strip()
        if not sha:
            continue
        records.append((sha, parents, trailer))
    return records


def bulk_commit_attribution_map(
    range_str: str,
    cwd: str,
    run: GitRunner,
) -> Dict[str, CommitAttribution]:
    """EVERY commit in `range_str`, merges included, keyed by sha.

    ONE `git log` walk, no `--no-merges` — the walk must see merges so a
    caller can classify them foreign; a merge-filtered walk here would
    silently drop them from the map instead (adjudication § 10.7 item 2 —
    this is the "walk sees merges" half of that item; do not add
    `--no-merges` here even though `bulk_grep_attributed_shas` below has it
    for the opposite reason).

    Untrailered commits are PRESENT with `trailer_session_id=None` — never
    omitted. Absence from the returned map means "not in the walked window",
    and nothing else (never collapsed with "untrailered" — see
    `CommitAttribution`'s docstring).

    A commit whose Session-Id trailer atom carries MORE THAN ONE value is
    present with `trailer_ambiguous=True` — this module's fail-closed
    posture on that shape (see module docstring's POSTURE DIVERGENCE note).

    Raises `session_attribution.GitLogFailed` on a non-zero `git log`
    returncode — not swallowed to an empty map (adjudication § 10.7 item 7).
    """
    rc, out, err = run(
        ["git", "log", f"--format={_LOG_FORMAT}", range_str],
        cwd,
    )
    if rc != 0:
        raise GitLogFailed(
            f"git log failed while bulk-resolving commit attribution for "
            f"range={range_str!r}: {err.strip() or 'unknown error'}"
        )
    result: Dict[str, CommitAttribution] = {}
    for sha, parents, trailer in _parse_log_records(out):
        parent_shas = [p for p in parents.split(" ") if p]
        is_merge = len(parent_shas) > 1
        trailer_values = [v for v in trailer.split("\n") if v.strip()]
        if not trailer_values:
            trailer_session_id: Optional[str] = None
            ambiguous = False
        elif len(trailer_values) == 1:
            trailer_session_id = trailer_values[0].strip()
            ambiguous = False
        else:
            trailer_session_id = trailer_values[0].strip()
            ambiguous = True
        result[sha] = CommitAttribution(
            sha=sha,
            trailer_session_id=trailer_session_id,
            is_merge=is_merge,
            trailer_ambiguous=ambiguous,
        )
    return result


def bulk_grep_attributed_shas(
    range_str: str,
    session_id: Optional[str],
    cwd: str,
    run: GitRunner,
) -> FrozenSet[str]:
    """Message-line `--grep=^Session-Id: <sid>$` attribution over the whole
    window — the same grep shape `coverage._derive_dag_chain_set` uses for
    chain membership.

    `--no-merges` is LOAD-BEARING here (adjudication § 10.7 item 2, the other
    half): without it, a merge commit whose OWN message happens to carry (or
    inherit via squash) a Session-Id line matching `session_id` would be
    grep-attributed, then get subtracted back OUT of the foreign set by
    `foreign_shas_from_window`'s merge-is-foreign rule — netting a merge that
    is provably not this session's own work being treated as attributed
    anyway. Keeping `--no-merges` on this leg while `bulk_commit_attribution_map`
    above deliberately omits it is not a contradiction; each omission closes
    a different bypass (see that function's own docstring).

    Returns the empty frozenset on a malformed `session_id` (`_UUID_RE`
    shape-validation fails) or any git failure — never fail-open.
    """
    if not session_id or not _UUID_RE.match(session_id):
        return frozenset()
    rc, out, err = run(
        [
            "git", "log", "--no-merges",
            f"--grep=^Session-Id: {session_id}$",
            "--format=%H",
            range_str,
        ],
        cwd,
    )
    if rc != 0:
        return frozenset()
    return frozenset(line.strip() for line in out.splitlines() if line.strip())


def unattributed_foreign_shas(
    sha_range: str,
    own_session_id: Optional[str],
    cwd: str,
    cache: Dict[Tuple[str, Optional[str]], FrozenSet[str]],
    run: GitRunner,
) -> FrozenSet[str]:
    """P2 per-range: commits `own_session_id` cannot be shown to have authored.

    Mirrors `session_attribution.trailer_foreign_shas`'s signature exactly —
    same DI `cache` and `run` seams, same `GitLogFailed` propagation posture
    — so a caller swaps one for the other without touching its subprocess
    conventions. NOT a rename or edit of that function (adjudication § 10.7
    item 5); this is a distinct sibling with a distinct (stricter) posture on
    ambiguous multi-valued trailers.

    A commit is foreign iff it is a merge, OR its Session-Id trailer names a
    different session, OR its Session-Id trailer is ambiguous (multi-valued),
    OR it is untrailered and not grep-attributed to `own_session_id`.

    Cached per (sha_range, own_session_id) in the caller-supplied `cache`.
    Raises `GitLogFailed` on any backing `git log` subprocess failure — never
    swallowed to an empty result (adjudication § 10.7 item 7).
    """
    key = (sha_range, own_session_id)
    if key in cache:
        return cache[key]
    window = bulk_commit_attribution_map(sha_range, cwd, run)
    grep_attributed = bulk_grep_attributed_shas(sha_range, own_session_id, cwd, run)
    result = foreign_shas_from_window(window.keys(), own_session_id, window, grep_attributed)
    cache[key] = result
    return result


def window_needs_grep_signal(window: Mapping[str, CommitAttribution]) -> bool:
    """True iff any commit in `window` can reach `foreign_shas_from_window`'s
    grep branch — i.e. the grep leg's ONE `git log` spawn can still change an
    answer. Pure set math, ZERO spawns.

    Read `foreign_shas_from_window`'s rule ladder alongside this: `grep_
    attributed` is consulted on the LAST branch only, for a commit that is in
    the window, is not a merge, and carries no Session-Id trailer at all. Every
    other shape is decided before the grep set is touched. So a window in which
    no commit is untrailered-and-non-merge produces a byte-identical foreign set
    whether `bulk_grep_attributed_shas` ran or returned the empty frozenset —
    and running it spends a process to learn nothing.

    That is the common shape in this repo, not an edge case: the
    `prepare-commit-msg` hook attaches a `Session-Id` trailer to every commit it
    sees, so a window of ordinary trailered commits skips the leg entirely.

    Negative-spec: this is a SPAWN-AVOIDANCE predicate, never an attribution
    one. It answers "can the grep signal still matter", never "is this commit
    foreign" — a caller that treats a False here as an attribution verdict has
    inverted its meaning. And it must stay in lockstep with
    `foreign_shas_from_window`'s ladder: a future edit that widens which shapes
    consult `grep_attributed` MUST widen this predicate in the same commit, or
    the leg gets skipped where it would have changed the answer. `tests/
    test_chain_attribution_grep_leg_elision.py` pins the two together by
    exercising the ladder rather than by restating it.
    """
    for attribution in window.values():
        if attribution.trailer_session_id is None and not attribution.is_merge:
            return True
    return False


def foreign_shas_from_window(
    shas: Iterable[str],
    own_session_id: Optional[str],
    window: Mapping[str, CommitAttribution],
    grep_attributed: FrozenSet[str],
) -> FrozenSet[str]:
    """P2 bulk-derived: pure in-memory, ZERO spawns.

    The bulk entrypoint — a caller resolves `window`
    (`bulk_commit_attribution_map`) and `grep_attributed`
    (`bulk_grep_attributed_shas`) ONCE per window, then answers any number of
    sha-sets from them via this function's set math alone. This is the N+1
    -to-1 spawn reduction this module exists to enable.

    A sha absent from `window` is treated as foreign (cannot be shown to be
    this session's own work — the window did not cover it, so there is
    nothing to attribute it with; adjudication § 10.5 step 4's "window must
    provably cover every sha being classified" note names this as the
    caller's responsibility to avoid, not license to read absence as safe).

    Foreign iff: sha not in window; OR window[sha].is_merge; OR
    window[sha].trailer_ambiguous; OR window[sha].trailer_session_id is not
    None and != own_session_id; OR (window[sha].trailer_session_id is None
    and sha not in grep_attributed).
    """
    foreign: Set[str] = set()
    for sha in shas:
        attribution = window.get(sha)
        if attribution is None:
            foreign.add(sha)
            continue
        if attribution.is_merge:
            foreign.add(sha)
            continue
        if attribution.trailer_ambiguous:
            foreign.add(sha)
            continue
        if attribution.trailer_session_id is not None:
            if attribution.trailer_session_id != own_session_id:
                foreign.add(sha)
            continue
        # Untrailered — fall back to the grep signal.
        if sha not in grep_attributed:
            foreign.add(sha)
    return frozenset(foreign)
