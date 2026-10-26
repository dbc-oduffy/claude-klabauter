"""Move a halt's unimportable bytes into the halted record, and put them back on refire.

Bytes live at `<record_dir>/withheld-blobs/<blob sha1>`, indexed by `<record_dir>/evicted.json`:
`{path: {"blob": sha1, "head": sha1 | null}}` (`head` is the HEAD blob sha the tree was reset to,
null when HEAD does not track the path and the tree file was removed). Blob shas come from
`kept_work.blob_sha`. In process; the caller supplies HEAD bytes from one batched read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence


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
    raise NotImplementedError("evict: implemented by C2")


def restore(root: Path, record_dir: Path) -> dict:
    """Write evicted bytes back to the tree; idempotent.

    A path is written only while the tree still holds the recorded HEAD bytes (absent when `head`
    is null); otherwise the tree is left untouched and the path is reported as `restore-drift`.
    Returns `{"restored": [paths], "drift": {path: "restore-drift"}}`. No `evicted.json`: empty.
    """
    raise NotImplementedError("restore: implemented by C2")
