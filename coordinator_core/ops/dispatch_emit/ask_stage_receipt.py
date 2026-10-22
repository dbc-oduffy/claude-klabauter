"""The idempotent re-run receipt for dispatch.ask_stage.

`write` lands `stage-receipt.json` in the run's record dir after `manifest.json`; `landed` answers a
repeat call with the recorded manifest handle only while the params digest matches and the manifest
still hashes to the handle. `partial` flags a manifest with no receipt that vouches for it. No git,
no subprocess.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.ops.dispatch_emit import reply_sidecar

ALREADY_STAGED_KEY = "already_staged"
RECEIPT_NAME = "stage-receipt.json"
MANIFEST_NAME = "manifest.json"
HALTED_NAME = "halted.json"
GATE_DIGEST = "ask-gate-resume"
_DIGEST_EXCLUDED = ("session_id",)


def params_digest(params: Mapping) -> str:
    """sha256 hex over the canonical JSON of the stage params, `session_id` excluded."""
    kept = {k: v for k, v in params.items() if k not in _DIGEST_EXCLUDED}
    raw = reply_sidecar.canonical_json(kept).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def write(record_dir: Path, digest: str, handle: Mapping) -> None:
    """Atomically write the receipt (ASCII JSON, trailing newline). Call it after manifest.json landed."""
    body = {"digest": digest, "manifest_handle": dict(handle)}
    text = json.dumps(body, sort_keys=True, ensure_ascii=True) + "\n"
    atomic_write_bytes(Path(record_dir) / RECEIPT_NAME, text.encode("ascii"), preserve_mode=False)


def write_gate(record_dir: Path, handle: Mapping) -> None:
    """Receipt for a manifest the resume gate staged itself. Its digest never equals an ask_stage
    params digest, so `landed` misses and the next ask_stage re-stages; `partial` sees a vouched
    manifest and does not refuse it."""
    write(record_dir, GATE_DIGEST, handle)


def _read_receipt(record_dir: Path) -> Optional[dict]:
    try:
        data = json.loads((Path(record_dir) / RECEIPT_NAME).read_bytes().decode("ascii"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("digest"), str):
        return None
    handle = data.get("manifest_handle")
    if not isinstance(handle, dict) or set(handle) != set(reply_sidecar.HANDLE_KEYS):
        return None
    return data


def _manifest_matches(record_dir: Path, handle: Mapping) -> bool:
    try:
        raw = (Path(record_dir) / MANIFEST_NAME).read_bytes()
    except OSError:
        return False
    return hashlib.sha256(raw).hexdigest() == handle["sha256"] and len(raw) == handle["bytes"]


def landed(record_dir: Path, digest: str) -> Optional[dict]:
    """The recorded manifest handle when the receipt's digest equals `digest` and the manifest still
    hashes to it; None otherwise."""
    data = _read_receipt(record_dir)
    if data is None or data["digest"] != digest:
        return None
    handle = data["manifest_handle"]
    return dict(handle) if _manifest_matches(record_dir, handle) else None


def partial(record_dir: Path) -> bool:
    """True when a manifest is on disk and no receipt vouches for its current bytes.

    A receipt with another digest that still matches the manifest is a landed stage of different
    params (a revise), not a partial one. A run that halted after staging (`halted.json` beside it)
    finished its stage: its manifest predates the receipt or was rewritten by later legs, and a
    refire must re-stage it rather than be refused forever."""
    if not (Path(record_dir) / MANIFEST_NAME).is_file() or (Path(record_dir) / HALTED_NAME).is_file():
        return False
    data = _read_receipt(record_dir)
    return data is None or not _manifest_matches(record_dir, data["manifest_handle"])
