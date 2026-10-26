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


def _delta_spans(baseline: list[bytes], tree: list[bytes]) -> list[tuple[int, int]]:
    """Doubled worktree-side slots where `tree` differs from `baseline`."""
    spans: list[tuple[int, int]] = []
    for _tag, _i1, _i2, j1, j2 in changed_opcodes(baseline, tree):
        spans.append((2 * j1 + 1, 2 * j1 + 1) if j2 == j1 else (2 * (j1 + 1), 2 * j2))
    return spans


def _claimant_delta(
    root: Path, common_dir: Path, claimant: str, path: str, tree: list[bytes], *, coarse_ok: bool = False,
) -> Optional[list[tuple[int, int]]]:
    """Delta spans of `claimant`, or None when its baseline cannot explain anything.

    Past the match bound a holder (`coarse_ok`) claims its whole trimmed middle as one span, so a hunk
    the lander also edited there has two owners and holds; the lander gets None (unknown holds)."""
    from coordinator_core.git import git_objects
    from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha

    base = hha._baseline(root, claimant)
    if base is None or path not in base:
        return None
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


def _split(root: Path, run_id: str, path: str, hunks: list[Hunk], holders: Mapping[str, bool]) -> HunkSplit:
    from coordinator_core.git.git_dir import resolve_git_common_dir

    common_dir = resolve_git_common_dir(root)
    try:
        tree = (root / path).read_bytes().splitlines(keepends=True)
    except FileNotFoundError:
        tree = []
    claimants = [run_id, *[h for h in holders if h != run_id]]
    deltas = {c: _claimant_delta(root, common_dir, c, path, tree, coarse_ok=c != run_id) for c in claimants}

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
