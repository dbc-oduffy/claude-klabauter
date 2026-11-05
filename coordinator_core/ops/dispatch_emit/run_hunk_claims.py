"""Run-keyed hunk ownership: which hunks of a shared path one run may land, which a peer run holds.

`Hunk`, `HunkSplit`, `parse_diff`, and `own_hunks` (baseline-delta attribution, zero spawn).

Hunks are in HEAD (old-side) coordinates, read from a `git diff -U0` of HEAD against the worktree.

Spike answer (ownership source). `write_baseline` is `kept_work.fingerprint` of the LIVE TREE at
stage, not of HEAD. Measured: two runs that stage from one clean HEAD record equal blobs
(`b1 == b2`), so their hunks cannot be separated by baseline; a run that stages after a peer wrote
records a different blob (`b2late != b1`), which separates only the hunks written before that
stage. Baselines therefore attribute a hunk only when exactly one holder's baseline-to-worktree
delta contains it; equal baselines (halted_hunk_attribution rule 4) and any hunk no baseline delta
explains are `unattributed`. A per-row hunk record, written by the row executor, is outside this
plan's file scope; it is the interface to name if unattributed hunks block the disjoint-pair replay.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FILE = re.compile(r"^\+\+\+ (?:b/)?(.*)$")
_OLD_FILE = re.compile(r"^--- (?:a/)?(.*)$")


@dataclass(frozen=True)
class Hunk:
    """One diff hunk of `path`: `old_*` is the HEAD side, `new_*` the worktree side.

    `old_len == 0` is a pure insertion after line `old_start`; `new_len == 0` a pure deletion."""

    path: str
    old_start: int
    old_len: int
    new_start: int
    new_len: int

    def span(self) -> tuple[int, int]:
        """Closed interval in doubled HEAD coordinates: line k is slot 2k, the gap after line k is
        slot 2k+1. Hunks overlap iff their spans intersect, so edits to adjacent lines and an
        insertion beside a changed line are disjoint, while an insertion inside a changed range
        overlaps it."""
        if self.old_len <= 0:
            return (2 * self.old_start + 1, 2 * self.old_start + 1)
        return (2 * self.old_start, 2 * (self.old_start + self.old_len - 1))

    def overlaps(self, other: "Hunk") -> bool:
        a, b = self.span(), other.span()
        return a[0] <= b[1] and b[0] <= a[1]


@dataclass(frozen=True)
class HunkSplit:
    """`own`: hunks this run may land. `overlapping`: `{holder run_id: hunks}` a SENIOR holder's
    attributed hunk intersects, which hold the path. `unattributed`: hunks no holder's delta
    explains, returned and never assigned (the caller holds on them)."""

    own: list[Hunk] = field(default_factory=list)
    overlapping: dict[str, list[Hunk]] = field(default_factory=dict)
    unattributed: list[Hunk] = field(default_factory=list)


def parse_diff(text: str) -> dict[str, list[Hunk]]:
    """`{path: [Hunk]}` from unified-diff `text`, hunks in file order. Pure: no spawn, never raises
    (a malformed header or a non-string yields what parsed so far). Deleted files key on the old
    path; `/dev/null` is never a key."""
    out: dict[str, list[Hunk]] = {}
    if not isinstance(text, str):
        return out
    old_path = ""
    path = ""
    try:
        for line in text.splitlines():
            if line.startswith("diff --git "):
                old_path = path = ""
                continue
            m = _OLD_FILE.match(line) if line.startswith("--- ") else None
            if m:
                old_path = m.group(1).strip()
                continue
            m = _FILE.match(line) if line.startswith("+++ ") else None
            if m:
                new = m.group(1).strip()
                path = old_path if new == "/dev/null" else new
                continue
            m = _HEADER.match(line)
            if m and path and path != "/dev/null":
                os_, ol, ns, nl = m.groups()
                out.setdefault(path, []).append(
                    Hunk(path, int(os_), 1 if ol is None else int(ol), int(ns), 1 if nl is None else int(nl))
                )
    except (ValueError, TypeError):
        return out
    return out


def own_hunks(
    root: Path,
    run_id: str,
    path: str,
    hunks: list[Hunk],
    holders: Mapping[str, bool],
) -> HunkSplit:
    """Split `hunks` (HEAD coordinates, all of `path`) into what `run_id` may land and what holds.

    `holders` maps each OTHER live run id declaring `path` to whether it is senior (`minted_before`
    order, unchanged). A senior holder's attributed hunk that overlaps an own hunk holds: it is
    returned under that run id in `overlapping`. A junior holder's attributed hunks are excluded
    from `own` (the senior never commits them) but never hold the senior. A hunk no baseline delta
    attributes is `unattributed`. No spawn; blobs are read in process; never raises. Unknown
    (unreadable manifest, missing baseline, error) is unattributed, never own: unknown holds.

    Attribution: a claimant's delta is the worktree line ranges that differ from its baseline blob
    (difflib, in process). A hunk belongs to a claimant when its worktree-side span intersects that
    delta; a hunk claimed by exactly one of {run_id, holders} is attributed, any other is
    `unattributed`."""
    try:
        return _split(Path(root), run_id, path, list(hunks), holders)
    except Exception:  # noqa: BLE001 -- unknown holds
        return HunkSplit(own=[], overlapping={}, unattributed=list(hunks))


def _new_span(h: Hunk) -> tuple[int, int]:
    """Doubled worktree-side slots of `h` (see `Hunk.span`)."""
    if h.new_len <= 0:
        return (2 * h.new_start + 1, 2 * h.new_start + 1)
    return (2 * h.new_start, 2 * (h.new_start + h.new_len - 1))


MAX_MATCHED_MIDDLE = 250


def _trim(old: list[bytes], new: list[bytes]) -> tuple[int, int, list[bytes], list[bytes]]:
    """`(head, tail, mid_old, mid_new)`: the common prefix and suffix lengths and the changed middles."""
    limit = min(len(old), len(new))
    head = 0
    while head < limit and old[head] == new[head]:
        head += 1
    tail = 0
    while tail < limit - head and old[len(old) - 1 - tail] == new[len(new) - 1 - tail]:
        tail += 1
    return head, tail, old[head:len(old) - tail], new[head:len(new) - tail]


def exceeds_match_bound(old: list[bytes], new: list[bytes]) -> bool:
    """True when the changed middle of `old` against `new` is past `MAX_MATCHED_MIDDLE` lines, so
    `changed_opcodes` returns one coarse span instead of a match."""
    _head, _tail, mid_old, mid_new = _trim(old, new)
    return max(len(mid_old), len(mid_new)) > MAX_MATCHED_MIDDLE


def changed_opcodes(old: list[bytes], new: list[bytes]) -> list[tuple[str, int, int, int, int]]:
    """`difflib` opcodes of `old` against `new`, minus the `equal` ones, in whole-file coordinates.

    The common prefix and suffix are trimmed before matching, so the quadratic `SequenceMatcher` only
    sees the changed middle: a few edited lines in a file thousands of lines long cost a scan, not a
    full match. A trimmed middle past `MAX_MATCHED_MIDDLE` lines (edits near both ends) is not matched:
    it is one coarse changed span (`exceeds_match_bound`), which `_claimant_delta` reads as unknown.
    Trap: cost is superlinear on repeated identical lines; raise the bound only with a re-measure.
    """
    import difflib

    head, tail, mid_old, mid_new = _trim(old, new)
    if not mid_old and not mid_new:
        return []
    if max(len(mid_old), len(mid_new)) > MAX_MATCHED_MIDDLE:
        return [("replace", head, len(old) - tail, head, len(new) - tail)]
    matcher = difflib.SequenceMatcher(None, mid_old, mid_new, autojunk=False)
    return [
        (tag, i1 + head, i2 + head, j1 + head, j2 + head)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]


MAX_DIFFLIB_LINES = 2000

Opcode = tuple[str, int, int, int, int]


def _anchor_cuts(old: list[bytes], new: list[bytes]) -> list[tuple[int, int]]:
    """`(old index, new index)` of the longest order-keeping chain of lines unique to both sides."""
    old_at: dict = {}
    new_at: dict = {}
    for at, line in enumerate(old):
        old_at[line] = -1 if line in old_at else at
    for at, line in enumerate(new):
        new_at[line] = -1 if line in new_at else at
    pairs = sorted((i, new_at[line]) for line, i in old_at.items() if i >= 0 and new_at.get(line, -1) >= 0)
    tails: list = []  # tails[k]: index in `pairs` ending the best order-keeping chain of length k + 1
    prev: list = []
    for k, (_i, j) in enumerate(pairs):
        lo, hi = 0, len(tails)
        while lo < hi:
            mid = (lo + hi) // 2
            if pairs[tails[mid]][1] < j:
                lo = mid + 1
            else:
                hi = mid
        prev.append(tails[lo - 1] if lo else -1)
        if lo == len(tails):
            tails.append(k)
        else:
            tails[lo] = k
    chain: list = []
    cur = tails[-1] if tails else -1
    while cur >= 0:
        chain.append(pairs[cur])
        cur = prev[cur]
    chain.reverse()
    return chain


def anchored_opcodes(
    old: list[bytes], new: list[bytes], *, line_budget: Optional[int] = None
) -> tuple[list[Opcode], bool]:
    """`(opcodes, coarse)`: full `difflib`-shaped opcodes (equal included) tiling `old` and `new`.

    A changed middle within `MAX_MATCHED_MIDDLE` is matched whole. A longer one is split at patience
    anchors (lines unique to both sides, kept in order) and each segment is matched alone: linear in
    the file. A segment still past the bound, or past `line_budget` (total lines handed to `difflib`
    across the call; None is unlimited), is one coarse span (`replace`, or `insert`/`delete` when a
    side is empty) and sets `coarse`. Trap: a coarse span is not a match; callers read it as unknown.
    """
    import difflib

    head, tail, mid_old, mid_new = _trim(old, new)
    ops: list[Opcode] = []

    def emit(tag: str, i1: int, i2: int, j1: int, j2: int) -> None:
        if i1 == i2 and j1 == j2:
            return
        if tag == "equal" and ops and ops[-1][0] == "equal":
            ops[-1] = ("equal", ops[-1][1], i2, ops[-1][3], j2)
        else:
            ops.append((tag, i1, i2, j1, j2))

    emit("equal", 0, head, 0, head)
    coarse = False
    if max(len(mid_old), len(mid_new)) <= MAX_MATCHED_MIDDLE:
        cuts = [(-1, -1), (len(mid_old), len(mid_new))]
    else:
        cuts = [(-1, -1), *_anchor_cuts(mid_old, mid_new), (len(mid_old), len(mid_new))]
    spent = 0
    for (ia, ja), (ib, jb) in zip(cuts, cuts[1:]):
        if ia >= 0:
            emit("equal", head + ia, head + ia + 1, head + ja, head + ja + 1)
        s_old, s_new = mid_old[ia + 1:ib], mid_new[ja + 1:jb]
        o0, n0 = head + ia + 1, head + ja + 1
        if not s_old and not s_new:
            continue
        size = len(s_old) + len(s_new)
        over = max(len(s_old), len(s_new)) > MAX_MATCHED_MIDDLE
        if not over and line_budget is not None and spent + size > line_budget:
            over = True
        if over:
            coarse = True
            tag = "insert" if not s_old else "delete" if not s_new else "replace"
            emit(tag, o0, o0 + len(s_old), n0, n0 + len(s_new))
            continue
        if not s_old or not s_new:
            emit("insert" if not s_old else "delete", o0, o0 + len(s_old), n0, n0 + len(s_new))
            continue
        spent += size
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, s_old, s_new, autojunk=False).get_opcodes():
            emit(tag, o0 + i1, o0 + i2, n0 + j1, n0 + j2)
    emit("equal", len(old) - tail, len(old), len(new) - tail, len(new))
    return ops, coarse


def _delta_spans(baseline: list[bytes], tree: list[bytes]) -> list[tuple[int, int]]:
    """Doubled worktree-side slots where `tree` differs from `baseline`."""
    spans: list[tuple[int, int]] = []
    for _tag, _i1, _i2, j1, j2 in changed_opcodes(baseline, tree):
        spans.append((2 * j1 + 1, 2 * j1 + 1) if j2 == j1 else (2 * (j1 + 1), 2 * j2))
    return spans


def _claimant_delta(
    root: Path, common_dir: Path, claimant: str, path: str, tree: list[bytes], *, coarse_ok: bool = False,
    absent_claims_all: bool = False,
) -> Optional[list[tuple[int, int]]]:
    """Delta spans of `claimant`, or None when its baseline cannot explain anything.

    Past the match bound a holder (`coarse_ok`) claims its whole trimmed middle as one span, so a hunk
    the lander also edited there has two owners and holds; the lander gets None (unknown holds)."""
    from coordinator_core.git import git_objects
    from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha

    base = hha._baseline(root, claimant)
    if base is None:
        return None
    if path not in base:
        # A revision added the path after stage: the lander's baseline never saw it, so every hunk
        # is its own unless a competing claimant also claims it (two owners hold, as everywhere).
        return [(0, 1 << 62)] if absent_claims_all else None
    sha = base[path]
    if sha is None:
        old: list[bytes] = []
    else:
        obj = git_objects.read_object(common_dir, sha)
        if obj is None or obj[0] != "blob":
            return None
        old = obj[1].splitlines(keepends=True)
    if exceeds_match_bound(old, tree) and not coarse_ok:
        return None  # the coarse span would claim pre-baseline hunks too: unknown holds
    return _delta_spans(old, tree)


def _released_holders(root: Path, run_id: str, path: str, holders: Mapping[str, bool]) -> set[str]:
    """Holders whose delta on `path` is not a competing claim: a halted run that stopped before `run_id`
    started and staged against the same bytes (`halted_hunk_attribution` rule 4) left nothing of its own,
    so the tree delta is `run_id`'s. Its baseline equals the lander's, so without this release it would
    claim every hunk the lander does and the path would read unattributed. Unknown holds: a live holder,
    an unreadable record or a kept print matching the tree stays a claimant."""
    from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha

    others = [h for h in holders if h != run_id]
    if not others:
        return set()
    attributable = hha.reader(root, run_id)
    return {h for h in others if not attributable(h, [path])}


def _split(root: Path, run_id: str, path: str, hunks: list[Hunk], holders: Mapping[str, bool]) -> HunkSplit:
    from coordinator_core.git.git_dir import resolve_git_common_dir

    common_dir = resolve_git_common_dir(root)
    try:
        tree = (root / path).read_bytes().splitlines(keepends=True)
    except FileNotFoundError:
        tree = []
    released = _released_holders(root, run_id, path, holders)
    claimants = [run_id, *[h for h in holders if h != run_id and h not in released]]
    deltas = {
        c: _claimant_delta(root, common_dir, c, path, tree, coarse_ok=c != run_id, absent_claims_all=c == run_id)
        for c in claimants
    }

    split = HunkSplit()
    attributed: dict[str, list[Hunk]] = {}
    for hunk in hunks:
        lo, hi = _new_span(hunk)
        owners = [
            c for c in claimants
            if deltas[c] and any(a <= hi and lo <= b for a, b in deltas[c])
        ]
        if len(owners) == 1:
            attributed.setdefault(owners[0], []).append(hunk)
        else:
            split.unattributed.append(hunk)

    own = attributed.get(run_id, [])
    overlapping: dict[str, list[Hunk]] = {}
    for holder, held in attributed.items():
        if holder == run_id or not holders.get(holder):
            continue
        clash = [h for h in held if any(h.overlaps(o) for o in own)]
        if clash:
            overlapping[holder] = clash
    return HunkSplit(own=own, overlapping=overlapping, unattributed=split.unattributed)
