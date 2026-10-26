"""The idempotent re-run receipt for dispatch.ask_stage.

`write` lands `stage-receipt.json` in the run's record dir after `manifest.json`; `landed` answers a
repeat call with the recorded manifest handle only while the params digest matches and the manifest
still hashes to the handle. `partial` flags a manifest with no receipt that vouches for it. No git,
no subprocess.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em import session_registry
from coordinator_core.ops.dispatch_emit import reply_sidecar

ALREADY_STAGED_KEY = "already_staged"
STAGE_IN_FLIGHT_KEY = "stage_in_flight"
CLAIM_NAME = "stage-claim.json"
# A pool-worker pid can be recycled, so a claim older than this is dead. It clears the longest
# observed stage (manifest-to-receipt ~2m12s, one create blocked ~5m on run 2f58ad); the 30s pool
# result deadline would declare that still-running handler dead.
CLAIM_STALE_SECS = 900.0
_UNREADABLE_CLAIM_GRACE_SECS = 5.0
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


def write(record_dir: Path, digest: str, handle: Mapping, spine_sha: Optional[str] = None) -> None:
    """Atomically write the receipt (ASCII JSON, trailing newline). Call it after manifest.json landed.
    `spine_sha` fingerprints the plan spine the manifest was staged from."""
    body = {"digest": digest, "manifest_handle": dict(handle)}
    if spine_sha is not None:
        body["spine_sha"] = spine_sha
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


def landed(record_dir: Path, digest: str, spine_sha: Optional[str] = None) -> Optional[dict]:
    """The recorded manifest handle when the receipt's digest equals `digest` and the manifest still
    hashes to it; None otherwise. With `spine_sha`, a receipt staged from a different spine (a re-plan
    rewrote the rows) is a miss, so the re-run stages the new rows and briefs."""
    data = _read_receipt(record_dir)
    if data is None or data["digest"] != digest:
        return None
    if spine_sha is not None and data.get("spine_sha") != spine_sha:
        return None
    handle = data["manifest_handle"]
    return dict(handle) if _manifest_matches(record_dir, handle) else None


def _read_claim(record_dir: Path) -> Optional[dict]:
    try:
        data = json.loads((Path(record_dir) / CLAIM_NAME).read_bytes().decode("ascii"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("digest"), str):
        return None
    if not isinstance(data.get("owner_pid"), int) or not isinstance(data.get("started_at"), (int, float)):
        return None
    return data


def _claim_live(data: dict) -> bool:
    if time.time() - data["started_at"] > CLAIM_STALE_SECS:
        return False
    return session_registry.pid_alive(data["owner_pid"])


def claim(record_dir: Path, digest: str) -> bool:
    """Take the stage claim with an exclusive create; False while another live claim holds it.

    A dead, stale or unreadable-and-old claim is unlinked and re-created exclusively, so two racing
    re-runs cannot both win. Call before `stage()` writes anything."""
    path = Path(record_dir) / CLAIM_NAME
    body = {"digest": digest, "owner_pid": os.getpid(), "started_at": time.time()}
    payload = (json.dumps(body, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            data = _read_claim(record_dir)
            if data is None:
                try:
                    fresh = time.time() - path.stat().st_mtime < _UNREADABLE_CLAIM_GRACE_SECS
                except OSError:
                    continue
                if fresh:
                    return False
            elif _claim_live(data):
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except PermissionError:
                return False
            continue
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True
    return False


def in_flight(record_dir: Path, digest: str) -> Optional[dict]:
    """The live claim when its digest equals `digest` and no receipt for it exists; None otherwise."""
    data = _read_claim(record_dir)
    if data is None or data["digest"] != digest or not _claim_live(data):
        return None
    receipt = _read_receipt(record_dir)
    if receipt is not None and receipt["digest"] == digest:
        return None
    return data


def release(record_dir: Path) -> None:
    """Drop this process's claim; absence is fine. Call after `write`.

    A claim another process took over after this one went stale is left alone. A Windows reader
    holding the file open makes `unlink` raise PermissionError, so it is retried briefly."""
    data = _read_claim(record_dir)
    if data is not None and data["owner_pid"] != os.getpid():
        return
    for attempt in range(5):
        try:
            (Path(record_dir) / CLAIM_NAME).unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.01)


def partial(record_dir: Path) -> bool:
    """True when a manifest is on disk and no receipt vouches for its current bytes.

    Any claim on disk answers False: a live one is reported by `in_flight`, and a dead or stale
    owner can never write its receipt, so the re-run is licensed to re-stage.

    A receipt with another digest that still matches the manifest is a landed stage of different
    params (a revise), not a partial one. A run that halted after staging (`halted.json` beside it)
    finished its stage: its manifest predates the receipt or was rewritten by later legs, and a
    refire must re-stage it rather than be refused forever."""
    if not (Path(record_dir) / MANIFEST_NAME).is_file() or (Path(record_dir) / HALTED_NAME).is_file():
        return False
    if _read_claim(record_dir) is not None:
        return False
    data = _read_receipt(record_dir)
    return data is None or not _manifest_matches(record_dir, data["manifest_handle"])
