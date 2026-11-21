"""Move a halt's unimportable bytes into the halted record, and put them back on refire.

Bytes live at `<record_dir>/withheld-blobs/<blob sha1>`, indexed by `<record_dir>/evicted.json`:
`{path: {"blob": sha1, "head": sha1 | null, "base": sha1 | null}}` (`head` is the blob sha the tree was
reset to, null when the tree file was removed; `base`, present only when it differs from `head`
because foreign hunks were kept, is the HEAD blob the reset was taken from). Blob shas come from
`kept_work.blob_sha`. In process; the caller supplies HEAD bytes from one batched read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from coordinator_core.ops.dispatch_emit import tree_write_fence
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha
from coordinator_core.session.declared_writes import declare_write

EVICTED_FILE = "evicted.json"
BLOBS_DIR = "withheld-blobs"


def _load(record_dir: Path) -> dict:
    try:
        data = json.loads((Path(record_dir) / EVICTED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        p: e
        for p, e in data.items()
        if isinstance(e, dict)
        and isinstance(e.get("blob"), str)
        and (e.get("head") is None or isinstance(e.get("head"), str))
    }


def _read(root: Path, rel: str) -> bytes | None:
    try:
        return (Path(root) / rel).read_bytes()
    except OSError:
        return None


def _reset_plan(
    root: Path, run_id: str | None, rel: str, data: bytes, head: bytes | None
) -> tuple[bytes | None, str | None]:
    """`(bytes to leave in the tree, None)`, or `(None, refusal)` when the run's hunks cannot be
    separated from foreign ones. Bytes None with no refusal means remove the file.

    Hunks are taken over raw bytes (never check-in normalised, or a CRLF tree is one whole-file
    hunk). Foreign hunks (unattributed or off the run's write print) are kept byte-identical."""
    from coordinator_core.ops.dispatch_emit import own_hunk_stage, run_hunk_claims

    if run_id is None:
        return None, "run unknown: hunks unattributable"
    head_lines = [] if head is None else head.splitlines(keepends=True)
    tree_lines = data.splitlines(keepends=True)
    hunks = own_hunk_stage._hunks_of(rel, head_lines, tree_lines)
    split = run_hunk_claims.own_hunks(
        root, run_id, rel, hunks, run_hunk_claims.lineage_holders(root, run_id),
        head_blob=None if head is None else blob_sha(head),
    )
    if not split.own:
        return None, "no hunk attributable to this run"
    own = set(split.own)
    foreign = [h for h in hunks if h not in own]
    if not foreign:
        return head, None
    if any(o.overlaps(f) for o in split.own for f in foreign):
        return None, "run hunk overlaps a foreign hunk"
    if run_hunk_claims.exceeds_match_bound(head_lines, tree_lines):
        return None, "diff too coarse to separate run hunks from foreign ones"
    try:
        spliced = own_hunk_stage.splice_hunks(
            rel, head_lines, tree_lines, [(h.old_start, h.old_len, h.new_start, h.new_len) for h in foreign]
        )
    except own_hunk_stage.OwnHunkRefused as exc:
        return None, str(exc)
    return spliced, None


_LINKED = "evict refused, tree kept: import-linked to "


def evict(
    root: Path,
    record_dir: Path,
    paths: Sequence[str],
    head_blobs: Mapping[str, bytes | None],
    run_id: str | None = None,
    refused: dict | None = None,
    units: Sequence[Sequence[str]] = (),
) -> dict:
    """Copy each path's tree bytes into `record_dir`, write `evicted.json`, and reset only the run's
    own hunks of the tree path to HEAD.

    `units` groups paths that import each other: a refusal of one path refuses every path sharing its
    unit (`import-linked to <path> (<reason>)`), so the tree never holds half of a cross-file edit.
    Every reset is computed before any byte moves; a refused unit never touches disk.

    `head_blobs` maps path to its HEAD bytes, or None when HEAD does not track it (the tree path is
    then removed when wholly the run's). Returns the `evicted.json` mapping. Never touches a path
    outside `paths`. A path whose run hunks cannot be split from foreign ones (or any path when
    `run_id` is None) is left untouched and recorded in `refused`; a whole-file write over foreign
    hunks never happens. `index[path]["head"]` is the sha of the bytes actually left in the tree.
    With `run_id`, a path whose reset bytes would drop a peer's unlanded hunk (`live_write_guard`) is left
    untouched and recorded in `refused` as `{path: holder-naming message}`.
    """
    root, record_dir = Path(root), Path(record_dir)
    index = _load(record_dir)
    blobs = record_dir / BLOBS_DIR
    planned: dict[str, tuple[bytes, str, bytes | None, bytes | None]] = {}
    refusals: dict[str, str] = {}
    claims: dict | None = None
    for rel in paths:
        data = _read(root, rel)
        if data is None:
            continue
        head = head_blobs.get(rel)
        sha = blob_sha(data)
        if rel in index and index[rel]["blob"] != sha:
            # Already evicted once: the tree holds HEAD bytes now, so differing bytes are not ours to drop.
            continue
        reset, refusal = _reset_plan(root, run_id, rel, data, head)
        if refusal:
            refusals[rel] = f"evict refused, tree kept: {refusal}"
            continue
        if run_id is not None:
            from coordinator_core.ops.dispatch_emit import live_write_guard, sibling_claims

            if claims is None:  # one claim lookup for the whole eviction, not one per path
                claims = sibling_claims.sibling_run_claims(root, run_id, [sibling_claims._norm(p) for p in paths])
            refusal = live_write_guard.check_live_write(root, run_id, rel, reset or b"", claims)
            if refusal:
                refusals[rel] = refusal
                continue
        planned[rel] = (data, sha, head, reset)
    for unit in units:
        culprit = next((p for p in unit if p in refusals and not refusals[p].startswith(_LINKED)), None)
        if culprit is None:
            continue
        for p in unit:
            if p != culprit and p in planned:
                del planned[p]
                refusals[p] = f"{_LINKED}{culprit} ({refusals[culprit]})"
    if refused is not None:
        refused.update(refusals)
    for rel, (data, sha, head, reset) in planned.items():
        blobs.mkdir(parents=True, exist_ok=True)
        blob_path = blobs / sha
        blob_path.write_bytes(data)
        declare_write(blob_path)
        reset_sha = None if reset is None else blob_sha(reset)
        index[rel] = {"blob": sha, "head": reset_sha}
        base_sha = None if head is None else blob_sha(head)
        if base_sha != reset_sha:
            index[rel]["base"] = base_sha  # spliced: the tree keeps foreign hunks, so HEAD's blob is a separate fact
        target = root / rel
        if reset is None:
            target.unlink()
        else:
            target.write_bytes(reset)
    if index:
        record_dir.mkdir(parents=True, exist_ok=True)
        index_path = record_dir / EVICTED_FILE
        index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        declare_write(index_path)
    return index


def _current_heads(root: Path, paths: Sequence[str]) -> dict[str, str]:
    """`{path: HEAD blob sha}` for the tracked `paths`, one batched in-process read; unreadable: empty."""
    from coordinator_core.git import git_state

    try:
        return {p: sha for p, (_mode, sha) in git_state.head_blobs(root, list(paths)).items()}
    except Exception:  # noqa: BLE001 - an unreadable HEAD makes every head None, which fences
        return {}


def restore(
    root: Path,
    record_dir: Path,
    *,
    owned_paths: Iterable[str] | None = None,
    run_id: str | None = None,
) -> dict:
    """Write evicted bytes back to the tree; idempotent.

    A path is written only while the tree still holds the recorded HEAD bytes (absent when `head`
    is null); otherwise the tree is left untouched and the path is reported as `restore-drift`.
    A path outside `owned_paths` (the record run's write set; None or empty owns nothing), or whose
    recorded `head` is no longer HEAD's blob, is not written: it is returned under `refused` and
    recorded through `tree_write_fence.record_refusal`. An entry carrying `base` (the tree kept
    foreign hunks) is written as `live_write_guard.merge_or_refuse` output, or refused the same way.
    Returns `{"restored": [paths], "drift": {path: "restore-drift"}, "refused": {path: reason}}`.
    No `evicted.json`: all empty.
    """
    root, record_dir = Path(root), Path(record_dir)
    restored: list[str] = []
    drift: dict[str, str] = {}
    refused: dict[str, str] = {}
    index = _load(record_dir)
    heads = _current_heads(root, list(index)) if index else {}
    for rel, entry in sorted(index.items()):
        current = _read(root, rel)
        current_sha = None if current is None else blob_sha(current)
        if current_sha == entry["blob"]:
            continue
        if current_sha != entry["head"]:
            drift[rel] = "restore-drift"
            continue
        try:
            data = (record_dir / BLOBS_DIR / entry["blob"]).read_bytes()
        except OSError:
            drift[rel] = "restore-drift"
            continue
        if blob_sha(data) != entry["blob"]:
            drift[rel] = "restore-drift"
            continue
        reason = tree_write_fence.check_tree_write(
            rel, owned_paths=owned_paths, base_blob=entry.get("base", entry["head"]), head_blob=heads.get(rel)
        )
        if reason:
            refused[rel] = reason
            tree_write_fence.record_refusal(record_dir, rel, reason, run_id)
            continue
        if "base" in entry:
            # The tree holds HEAD plus kept foreign hunks: write the merge, never the bare blob.
            from coordinator_core.ops.dispatch_emit import live_write_guard

            merged, refusal = live_write_guard.merge_or_refuse(root, run_id, rel, data)
            if refusal:
                refused[rel] = refusal
                tree_write_fence.record_refusal(record_dir, rel, refusal, run_id)
                continue
            data = merged
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        restored.append(rel)
    return {"restored": restored, "drift": drift, "refused": refused}


def restore_for_run(root: Path, record_dir: Path, run_id: str) -> dict:
    """`restore` fenced to run `run_id`'s own write set (`run_write_set`, no own session)."""
    from coordinator_core.ops.dispatch_emit import run_write_set

    owned = run_write_set.run_write_set(Path(root), run_id, "")
    return restore(root, record_dir, owned_paths=owned, run_id=run_id)


def restore_own_delta(root: Path, record_dir: Path, run_id: str) -> dict:
    """Restore only the halt's HEAD-plus-own-delta image, never its raw parked image."""
    from coordinator_core.atomic_replace import atomic_write_bytes
    from coordinator_core.git import git_state
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object
    from coordinator_core.ops.dispatch_emit import live_write_guard, run_write_set
    from coordinator_core.ops.dispatch_emit.halt_refire import WITHHELD_OWN_DELTA_FILE

    index = _load(record_dir)
    result: dict[str, dict] = {}
    if not index:
        return result
    try:
        sidecar = json.loads((record_dir / WITHHELD_OWN_DELTA_FILE).read_text(encoding="utf-8"))
        deltas = sidecar.get("paths", {}) if isinstance(sidecar, dict) else {}
    except (OSError, ValueError):
        deltas = {}
    owned = run_write_set.run_write_set(root, run_id, "")
    try:
        heads = {p: sha for p, (_mode, sha) in git_state.head_blobs(root, list(index)).items()}
    except (OSError, ValueError):
        return {p: {"status": "skipped", "reason": "unreadable-head"} for p in index}
    common = resolve_git_common_dir(root)
    for rel, entry in sorted(index.items()):
        reason = tree_write_fence.check_tree_write(
            rel, owned_paths=owned, base_blob=entry.get("base", entry["head"]), head_blob=heads.get(rel),
        )
        target = root / rel
        if not reason and (target.resolve() != target.absolute() or not target.resolve().is_relative_to(root.resolve())):
            reason = "outside-own-paths"
        delta = deltas.get(rel) if isinstance(deltas, dict) else None
        oid = delta.get("blob") if isinstance(delta, dict) else None
        if not reason and (not isinstance(oid, str) or len(oid) != 40 or any(c not in "0123456789abcdef" for c in oid)):
            reason = "missing-own-delta"
        if not reason:
            try:
                current = target.read_bytes()
            except FileNotFoundError:
                current = None
            except OSError:
                result[rel] = {"status": "skipped", "reason": "unreadable-worktree"}
                continue
            current_sha = None if current is None else blob_sha(current)
            if current_sha == oid:
                result[rel] = {"status": "already-restored"}
                continue
            if current_sha != heads.get(rel):
                reason = "restore-drift"
        if not reason:
            obj = read_object(common, oid)
            if obj is None or obj[0] != "blob" or blob_sha(obj[1]) != oid:
                reason = "missing-own-delta"
            else:
                reason = live_write_guard.check_live_write(root, run_id, rel, obj[1])
                if not reason:
                    # Trap: no lock spans check and replace; a foreign write in that gap must survive.
                    try:
                        latest = target.read_bytes()
                    except FileNotFoundError:
                        latest = None
                    except OSError:
                        latest = Ellipsis
                    if latest != current:
                        result[rel] = {"status": "skipped", "reason": "restore-drift"}
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_bytes(target, obj[1])
                    declare_write(target)
                    result[rel] = {"status": "restored"}
                    continue
        result[rel] = {"status": "skipped", "reason": reason}
    return result
