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
    split = run_hunk_claims.own_hunks(root, run_id, rel, hunks, {})
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


def evict(
    root: Path,
    record_dir: Path,
    paths: Sequence[str],
    head_blobs: Mapping[str, bytes | None],
    run_id: str | None = None,
    refused: dict | None = None,
) -> dict:
    """Copy each path's tree bytes into `record_dir`, write `evicted.json`, and reset only the run's
    own hunks of the tree path to HEAD.

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
            if refused is not None:
                refused[rel] = f"evict refused, tree kept: {refusal}"
            continue
        if run_id is not None:
            from coordinator_core.ops.dispatch_emit import live_write_guard

            refusal = live_write_guard.check_live_write(root, run_id, rel, reset or b"")
            if refusal:
                if refused is not None:
                    refused[rel] = refusal
                continue
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
        index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
    recorded through `tree_write_fence.record_refusal`.
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
