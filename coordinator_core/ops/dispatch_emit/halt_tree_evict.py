"""Move a halt's unimportable bytes into the halted record, and put them back on refire.

Bytes live at `<record_dir>/withheld-blobs/<blob sha1>`, indexed by `<record_dir>/evicted.json`:
`{path: {"blob": sha1, "head": sha1 | null}}` (`head` is the HEAD blob sha the tree was reset to,
null when HEAD does not track the path and the tree file was removed). Blob shas come from
`kept_work.blob_sha`. In process; the caller supplies HEAD bytes from one batched read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Sequence

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


def evict(
    root: Path,
    record_dir: Path,
    paths: Sequence[str],
    head_blobs: Mapping[str, bytes | None],
) -> dict:
    """Copy each path's tree bytes into `record_dir`, write `evicted.json`, reset the tree path.

    `head_blobs` maps path to its HEAD bytes, or None when HEAD does not track it (the tree path is
    then removed). Returns the `evicted.json` mapping. Never touches a path outside `paths`.
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
        blobs.mkdir(parents=True, exist_ok=True)
        blob_path = blobs / sha
        blob_path.write_bytes(data)
        declare_write(blob_path)
        index[rel] = {"blob": sha, "head": None if head is None else blob_sha(head)}
        target = root / rel
        if head is None:
            target.unlink()
        else:
            target.write_bytes(head)
    if index:
        record_dir.mkdir(parents=True, exist_ok=True)
        index_path = record_dir / EVICTED_FILE
        index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        declare_write(index_path)
    return index


def restore(root: Path, record_dir: Path) -> dict:
    """Write evicted bytes back to the tree; idempotent.

    A path is written only while the tree still holds the recorded HEAD bytes (absent when `head`
    is null); otherwise the tree is left untouched and the path is reported as `restore-drift`.
    Returns `{"restored": [paths], "drift": {path: "restore-drift"}}`. No `evicted.json`: empty.
    """
    root, record_dir = Path(root), Path(record_dir)
    restored: list[str] = []
    drift: dict[str, str] = {}
    for rel, entry in sorted(_load(record_dir).items()):
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
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        restored.append(rel)
    return {"restored": restored, "drift": drift}
