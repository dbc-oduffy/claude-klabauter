"""Write-time refusal: a run may not replace a live-tree path with bytes that drop a peer's unlanded hunks.

`check_live_write(root, run_id, path, new_bytes)` returns a refusal string naming the holder, or None.
The holder lookup is `sibling_claims.sibling_run_claims`; ownership of each live hunk is
`run_hunk_claims.own_hunks`. Zero git spawns: HEAD is read through `git_state.head_blobs` and
`git_objects.read_object`, hunks come from `changed_opcodes` in process.

`merge_or_refuse(root, run_id, rel, new_bytes)` is the write-side twin: it returns merged bytes that
keep every foreign hunk, or a refusal; same attribution, same zero-spawn rule.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Sequence

UNATTRIBUTED = "unattributed"


def _lines(data: bytes) -> list[bytes]:
    return data.splitlines(keepends=True)


def _contains(haystack: list[bytes], needle: list[bytes]) -> bool:
    """Whether `needle` occurs as a contiguous run in `haystack`."""
    n = len(needle)
    if n == 0:
        return True
    first = needle[0]
    for i in range(len(haystack) - n + 1):
        if haystack[i] == first and haystack[i:i + n] == needle:
            return True
    return False


def _head_lines(root: Path, path: str) -> Optional[list[bytes]]:
    """HEAD content of `path` as lines; `[]` when absent from HEAD; None when unreadable."""
    from coordinator_core.git import git_objects, git_state
    from coordinator_core.git.git_dir import resolve_git_common_dir

    found = git_state.head_blobs(root, [path])
    if path not in found:
        return []
    obj = git_objects.read_object(resolve_git_common_dir(root), found[path][1])
    if obj is None or obj[0] != "blob":
        return None
    return _lines(obj[1])


def _hunk_dropped(head: list[bytes], live: list[bytes], new: list[bytes], op: tuple[str, int, int, int, int]) -> bool:
    _tag, i1, i2, j1, j2 = op
    added = live[j1:j2]
    removed = head[i1:i2]
    if added:
        return not _contains(new, added)
    return bool(removed) and _contains(new, removed)


def _as_hunks(rel: str, ops):
    from coordinator_core.ops.dispatch_emit import run_hunk_claims

    return [
        run_hunk_claims.Hunk(
            rel,
            i1 if i2 == i1 else i1 + 1, i2 - i1,
            j1 if j2 == j1 else j1 + 1, j2 - j1,
        )
        for _tag, i1, i2, j1, j2 in ops
    ]


def check_live_write(
    root, run_id: Optional[str], path: str, new_bytes: bytes, claims: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Refusal text naming the holder when writing `new_bytes` to live `path` would drop a hunk a
    peer's unlanded run holds (or an unattributed hunk: unknown is not own); None otherwise.

    A hunk is a difference of the live bytes from HEAD that `own_hunks` does not attribute to
    `run_id`. It is dropped when its added lines no longer occur in `new_bytes`, or, for a pure
    deletion, the deleted HEAD lines occur again. Never raises: an unreadable input yields None
    only when no peer claim is proven. `claims` is a caller's `sibling_run_claims` over a set holding
    `path`, passed so a loop over many paths computes the claim set once."""
    try:
        return _check(Path(root), run_id, path, new_bytes, claims)
    except Exception:  # noqa: BLE001 -- unknown claim: the commit-side gates still apply
        return None


def _check(
    root: Path, run_id: Optional[str], path: str, new_bytes: bytes, claims: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    from coordinator_core.ops.dispatch_emit import run_hunk_claims, sibling_claims

    rel = sibling_claims._norm(path)
    if claims is None:
        claims = sibling_claims.sibling_run_claims(root, run_id, [rel])
    holder = claims.get(rel)
    if not holder:
        return None
    try:
        live = _lines((root / rel).read_bytes())
    except FileNotFoundError:
        return None
    head = _head_lines(root, rel)
    if head is None:
        return None
    ops = run_hunk_claims.changed_opcodes(head, live)
    if not ops:
        return None
    hunks = _as_hunks(rel, ops)
    split = run_hunk_claims.own_hunks(root, run_id or "", rel, hunks, {holder: True})
    own = {id(h) for h in split.own}
    unattributed = {id(h) for h in split.unattributed}
    new = _lines(new_bytes)
    lost: list[str] = []
    for hunk, op in zip(hunks, ops):
        if id(hunk) in own or not _hunk_dropped(head, live, new, op):
            continue
        lost.append(UNATTRIBUTED if id(hunk) in unattributed else holder)
    if not lost:
        return None
    named = holder if holder in lost else UNATTRIBUTED
    return (
        f"write to {rel} drops {len(lost)} unlanded hunk(s) held by {named}. "
        "Build the fix on a HEAD export and land it by commit."
    )


def _slots(i1: int, i2: int) -> tuple[int, int]:
    """Closed interval in doubled HEAD slots (line k is 2k+2, the gap before line k is 2k+1), as
    `Hunk.span`: adjacent edits are disjoint, an insertion inside a changed range overlaps it."""
    if i1 == i2:
        return (2 * i1 + 1, 2 * i1 + 1)
    return (2 * i1 + 2, 2 * i2)


def _clash(a: tuple[int, int], b: tuple[int, int]) -> bool:
    x, y = _slots(a[0], a[1]), _slots(b[0], b[1])
    return x[0] <= y[1] and y[0] <= x[1]


def merge_or_refuse(
    root, run_id: Optional[str], rel: str, new_bytes: bytes, *,
    base_blob_sha: Optional[str] = None, live_bytes: Optional[bytes] = None, peers: Sequence[str] = (),
) -> tuple[Optional[bytes], Optional[str]]:
    """`(bytes, None)` to write, or `(None, refusal)` with live left untouched.

    3-way merge on lines, in process: base is HEAD, ours is the live file, theirs is `new_bytes`.
    Live-vs-HEAD hunks `own_hunks` does not attribute to `run_id` are foreign (unknown is not own,
    so a halted run's hunk counts); they are kept. The writer's own live hunks are superseded by
    `new_bytes`. A foreign hunk overlapping a `new_bytes` hunk with different text is refused naming
    its holder (or "unattributed"). `base_blob_sha`, when given, is the HEAD blob of `rel` the bytes
    were built on: a different HEAD blob refuses as "base moved" (speculative: `halt_tree_evict.restore`
    does not pass it; its `head` drift check covers that case today). Line endings are preserved byte for
    byte. Never raises: a failure refuses.

    `live_bytes`, when given, is the ours side instead of the file on disk (the bytes a writer already
    replaced, e.g. a leg's pre-write snapshot). Attribution runs on those bytes (`_own_in_bytes`): a hunk
    only `run_id`'s baseline delta explains is its own earlier work and yields to `new_bytes`; every other hunk
    is foreign, and one `new_bytes` already contains is kept, not a clash.

    `peers` (with `live_bytes`) are run ids whose baseline delta over `live_bytes` also explains a hunk, even
    where the claim scan on the file now on disk names none (a leg's write dropped the hunk that made the peer
    a dirty claimant). A hunk such a delta reaches is foreign, and a refusal over it names that run."""
    path = str(rel)
    try:
        return _merge(Path(root), run_id, path, new_bytes, base_blob_sha, live_bytes, tuple(peers))
    except Exception as exc:  # noqa: BLE001 -- unknown claim is a refusal, never a blind write
        return None, f"write to {path} refused: merge failed ({type(exc).__name__}); live left untouched."


def _merge(
    root: Path, run_id: Optional[str], path: str, new_bytes: bytes, base_blob_sha: Optional[str],
    live_override: Optional[bytes] = None, peers: Sequence[str] = (),
) -> tuple[Optional[bytes], Optional[str]]:
    from coordinator_core.git import git_state
    from coordinator_core.ops.dispatch_emit import run_hunk_claims, sibling_claims

    rel = sibling_claims._norm(path)
    if live_override is not None:
        live_bytes = live_override
    else:
        try:
            live_bytes = (root / rel).read_bytes()
        except FileNotFoundError:
            return new_bytes, None
    if live_bytes == new_bytes:
        return new_bytes, None
    head = _head_lines(root, rel)
    if head is None:
        return None, f"write to {rel} refused: HEAD blob unreadable; live left untouched."
    if base_blob_sha is not None:
        found = git_state.head_blobs(root, [rel])
        if (found[rel][1] if rel in found else None) != base_blob_sha:
            return None, f"write to {rel} refused: base moved (HEAD blob is not the one the bytes were built on); live left untouched."
    live = _lines(live_bytes)
    live_ops = run_hunk_claims.changed_opcodes(head, live)
    if not live_ops:
        return new_bytes, None
    holder = sibling_claims.sibling_run_claims(root, run_id, [rel]).get(rel)
    hunks = _as_hunks(rel, live_ops)
    owners_by_hunk: dict[int, str] = {}
    if live_override is not None:
        holders = [holder] if holder else []
        holders += [p for p in peers if p and p != holder]
        own, owners_by_hunk = _own_in_bytes(root, run_id, rel, live, hunks, holders)
        unattributed = {id(h) for h in hunks if id(h) not in own}
    else:
        split = run_hunk_claims.own_hunks(root, run_id or "", rel, hunks, {holder: True} if holder else {})
        own = {id(h) for h in split.own}
        unattributed = {id(h) for h in split.unattributed} | {id(h) for h in split.nonrun}
    new = _lines(new_bytes)
    new_ops = run_hunk_claims.changed_opcodes(head, new)
    theirs = {(i1, i2): new[j1:j2] for _t, i1, i2, j1, j2 in new_ops}
    foreign: list[tuple[int, int, list[bytes], str]] = []
    for hunk, (_t, i1, i2, j1, j2) in zip(hunks, live_ops):
        if id(hunk) not in own:
            owner = owners_by_hunk.get(id(hunk)) or (UNATTRIBUTED if (id(hunk) in unattributed or not holder) else holder)
            foreign.append((i1, i2, live[j1:j2], owner))
    clashing: list[str] = []
    kept: list[tuple[int, int, list[bytes], str]] = []
    for i1, i2, text, owner in foreign:
        hit = [(k, t) for k, t in theirs.items() if _clash((i1, i2), k)]
        if not hit:
            kept.append((i1, i2, text, owner))
        elif not (len(hit) == 1 and (hit[0] == ((i1, i2), text) or (text and _contains(hit[0][1], text)))):
            clashing.append(owner)
    if clashing:
        named = next((o for o in clashing if o != UNATTRIBUTED), UNATTRIBUTED)
        return None, (
            f"write to {rel} overlaps {len(clashing)} unlanded hunk(s) held by {named}; "
            "live left untouched. Rebuild on live HEAD or land by commit."
        )
    edits = {(i1, i2): text for i1, i2, text, _o in kept}
    for key, text in theirs.items():
        edits.setdefault(key, text)
    out: list[bytes] = []
    pos = 0
    for (i1, i2), text in sorted(edits.items()):
        out.extend(head[pos:i1])
        out.extend(text)
        pos = i2
    out.extend(head[pos:])
    return b"".join(out), None


def _own_in_bytes(
    root: Path, run_id: Optional[str], rel: str, live: list[bytes], hunks, holders: Sequence[str],
) -> tuple[set[int], dict[int, str]]:
    """`(own ids, owner by hunk id)` over `hunks` (live-vs-HEAD over the `live` bytes, not the file on disk).
    Own: only `run_id`'s baseline delta explains the hunk, its own earlier edit. A hunk any peer holder's delta
    also reaches (the first such holder is its owner), one the run's write print does not vouch for, or any
    unreadable input is not own (unknown holds)."""
    if not run_id:
        return set(), {}
    try:
        from coordinator_core.git.git_dir import resolve_git_common_dir
        from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc

        common = resolve_git_common_dir(root)
        mine = rhc._claimant_delta(root, common, run_id, rel, live, absent_claims_all=True)
        if not mine:
            return set(), {}
        theirs = [(h, rhc._claimant_delta(root, common, h, rel, live, coarse_ok=True)) for h in holders]
        foreign = rhc._foreign_spans(root, common, run_id, rel, live)
        if foreign is None:
            return set(), {}

        def reaches(spans, lo, hi) -> bool:
            return any(a <= hi and lo <= b for a, b in spans or [])

        out: set[int] = set()
        owners: dict[int, str] = {}
        for h in hunks:
            lo, hi = rhc._new_span(h)
            holder = next((who for who, spans in theirs if reaches(spans, lo, hi)), None)
            if holder:
                owners[id(h)] = holder
            elif reaches(mine, lo, hi) and not reaches(foreign, lo, hi):
                out.add(id(h))
        return out, owners
    except Exception:  # noqa: BLE001 -- unknown holds: the hunk stays foreign
        return set(), {}


def _declaring_peers(root: Path, run_id: Optional[str], rel: str) -> list[str]:
    """Unlanded runs other than `run_id` and its own earlier attempts whose manifest declares `rel`: the runs
    whose recorded delta may sit in a file a leg has just rewritten. Unlike `sibling_run_claims` this does not
    read the tree's dirt, which is exactly what a dropped hunk erased. Zero spawns; unknown yields none."""
    try:
        from coordinator_core.ops.dispatch_emit import run_hunk_claims, sibling_claims

        declared = [r for r in sibling_claims._unlanded_run_ids(root, run_id) if r != run_id and sibling_claims._declares(root, r, rel)]
        kin = run_hunk_claims.own_lineage(root, run_id or "", {r: False for r in declared}) if run_id else set()
        return [r for r in declared if r not in kin]
    except Exception:  # noqa: BLE001 -- unknown peers: the claim scan alone decides
        return []


def _pre_leg_bytes(root: Path, sha: str, post: bytes) -> Optional[bytes]:
    """The blob a `live-snap before` recorded as loose object `sha` ("raw:" prefix: its own sha), in `post`'s
    line-ending style; None when `sha` names no bytes (absent or unreadable file) or the object is gone."""
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir

    sha = sha[4:] if sha.startswith("raw:") else sha
    if len(sha) != 40:
        return None
    obj = git_objects.read_object(resolve_git_common_dir(root), sha)
    if obj is None or obj[0] != "blob":
        return None
    pre = obj[1]
    if b"\r\n" in post and b"\r\n" not in pre:
        pre = pre.replace(b"\n", b"\r\n")
    return pre


def restore_after_leg(root, snapshot, run_id: Optional[str], record_dir) -> dict:
    """Put back the foreign hunks a leg's own tool write dropped from the paths `snapshot` measured as written.

    `snapshot` is the `ledger_recovery` live-snap file: `before` maps each footprint path to its pre-leg blob (the
    bytes persisted as a loose object) and `live_tree_writes` lists the paths whose blob changed. For each, the
    pre-leg bytes are the ours side and the file now on disk the leg's bytes, merged through `merge_or_refuse`; the
    merged bytes are written, or a refusal is recorded in `record_dir`'s `tree-write-refusals.json` and the file is
    left as the leg wrote it. Returns `{"restored": [rel...], "refused": [{"path", "reason"}...], "peer_held": [rel...]}`;
    `peer_held` names the measured paths a peer's unlanded run also declares, where a blob change in the leg's window
    may be the peer's own edit, so the caller must not read it as the leg's unreported write. Zero spawns."""
    import json

    from coordinator_core.ops.dispatch_emit import sibling_claims, tree_write_fence

    root = Path(root)
    doc = json.loads(Path(snapshot).read_text(encoding="utf-8"))
    before = doc.get("before") or {}
    restored: list[str] = []
    refused: list[dict] = []
    written = [r for r in doc.get("live_tree_writes") or [] if not (Path(r).is_absolute() or ".." in Path(r).parts)]
    try:
        held = sibling_claims.sibling_run_claims(root, run_id, [sibling_claims._norm(r) for r in written])
    except Exception:  # noqa: BLE001 -- unknown claim: nothing is excused
        held = {}
    peer_held = [r for r in written if held.get(sibling_claims._norm(r))]
    for rel in written:
        sha = str(before.get(rel) or "")
        try:
            post = (root / rel).read_bytes()
        except OSError:
            continue
        pre = _pre_leg_bytes(root, sha, post) if sha else None
        if pre is None or pre == post:
            continue
        merged, refusal = merge_or_refuse(root, run_id, rel, post, live_bytes=pre, peers=_declaring_peers(root, run_id, rel))
        if refusal is not None:
            reason = f"{refusal} The pre-leg bytes are kept as blob {sha.removeprefix('raw:')}."
            tree_write_fence.record_refusal(record_dir, rel, reason, run_id)
            refused.append({"path": rel, "reason": reason})
        elif merged != post:
            (root / rel).write_bytes(merged)
            restored.append(rel)
    return {"restored": restored, "refused": refused, "peer_held": peer_held}


def main(argv: Optional[list[str]] = None) -> int:
    """`restore --root R --snapshot F --run-id ID --record-dir D`: prints one JSON line
    (`{"ok": true, "restored": [...], "refused": [...], "peer_held": [...]}`); exit 2 with `ok` false on a failure."""
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(prog="live_write_guard")
    ap.add_argument("phase", choices=("restore",))
    ap.add_argument("--root", required=True)
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--run-id", default="")
    ap.add_argument("--record-dir", required=True)
    ns = ap.parse_args(argv)
    try:
        out = restore_after_leg(ns.root, ns.snapshot, ns.run_id or None, ns.record_dir)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:300], "restored": [], "refused": []}))
        return 2
    print(json.dumps({"ok": True, **out}))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
