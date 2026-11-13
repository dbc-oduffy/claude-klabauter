"""Engine-side build of a distill Workflow's launch `args`: the whole run input inlined, written to one file.

`build(payload)` writes ``launch-args.json`` under the distill run dir and returns its path, its
sha256 and the args object. A PreToolUse hook (`hooks/preuse_workflow_args_inline.py`) swaps the
`{"argsFile", "argsSha256"}` stub a model types for that file's content, so the Workflow script
receives batches, wiki slugs and curated content verbatim with no load relay agent.

Pure file write: no git, no subprocess.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ops.distill_scope import RUN_DIR_REL

__all__ = ["ARGS_FILE_NAME", "MAX_ARRAY_ENTRIES", "CHUNK_KEY", "INLINED_KEY", "build", "chunk_arrays", "unchunk", "stub"]

ARGS_FILE_NAME = "launch-args.json"

#: The Workflow VM refuses arrays longer than this across its boundary.
MAX_ARRAY_ENTRIES = 4096

#: Wrapper key of a list longer than MAX_ARRAY_ENTRIES: ``{CHUNK_KEY: [[...], [...]]}``, chunks in order.
CHUNK_KEY = "$chunked"

#: Marker the cost model reads to know the load phase needs no relay agent.
INLINED_KEY = "argsInlined"


def chunk_arrays(value: Any) -> Any:
    """Return `value` with every list over MAX_ARRAY_ENTRIES wrapped as ``{CHUNK_KEY: chunks}``.

    Chunks and the chunk count are each <= MAX_ARRAY_ENTRIES (16.7M entries total); more raises
    ValueError. Recurses into dicts and lists; `unchunk` is the exact inverse.
    """
    if isinstance(value, dict):
        return {k: chunk_arrays(v) for k, v in value.items()}
    if isinstance(value, list):
        items = [chunk_arrays(v) for v in value]
        if len(items) <= MAX_ARRAY_ENTRIES:
            return items
        chunks = [items[i : i + MAX_ARRAY_ENTRIES] for i in range(0, len(items), MAX_ARRAY_ENTRIES)]
        if len(chunks) > MAX_ARRAY_ENTRIES:
            raise ValueError(f"list of {len(items)} entries exceeds {MAX_ARRAY_ENTRIES}x{MAX_ARRAY_ENTRIES}")
        return {CHUNK_KEY: chunks}
    return value


def unchunk(value: Any) -> Any:
    """Inverse of `chunk_arrays`: flatten every ``{CHUNK_KEY: chunks}`` wrapper back into one list."""
    if isinstance(value, dict):
        if set(value) == {CHUNK_KEY} and isinstance(value[CHUNK_KEY], list):
            return [unchunk(item) for chunk in value[CHUNK_KEY] for item in chunk]
        return {k: unchunk(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unchunk(v) for v in value]
    return value


def stub(path: str, sha256: str) -> dict[str, str]:
    """The Workflow `args` a model passes; the hook expands it. Keys are the hook's match contract."""
    return {"argsFile": path, "argsSha256": sha256}


def build(payload: dict[str, Any], *, run_dir: Optional[Path] = None) -> dict[str, Any]:
    """Write ``launch-args.json`` for `payload` (a `distill.workflow_input` result) and return
    ``{"path", "sha256", "args"}``.

    The file lives in `run_dir`, default ``<repoRoot>/<RUN_DIR_REL>/<runId>``, both from the payload.
    `args` is the payload plus ``argsInlined: true``, every array chunked to <= MAX_ARRAY_ENTRIES.
    The sha256 is over the file's bytes, as the hook re-hashes them.
    """
    if not isinstance(payload, dict) or not payload.get("runId") or not payload.get("repoRoot"):
        raise ValueError("launch_args.build needs a payload carrying runId and repoRoot")
    directory = Path(run_dir) if run_dir is not None else Path(payload["repoRoot"]) / RUN_DIR_REL / payload["runId"]
    args = chunk_arrays({**payload, INLINED_KEY: True})
    body = json.dumps(args, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ARGS_FILE_NAME
    target.write_bytes(body)
    return {"path": str(target), "sha256": hashlib.sha256(body).hexdigest(), "args": args}
