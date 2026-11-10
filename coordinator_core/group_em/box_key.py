"""Per-box HMAC key: mint, pin a peer's key, sign and verify canonical-JSON records.

A box's own key lives at ``<home>/state/group-em/box-key`` (base64 text, 32 random bytes); a
peer's key, pinned by an operator act, at ``<home>/state/group-em/peer-keys/<box_id>.json``.
Leaf module like ``box_registry`` -- never import ``nomination``. ``verify`` never raises: any
malformed input, missing pin or bad tag reads False.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em.box_registry import _group_em_dir, _valid_id

SCHEMA_VERSION = 1
KEY_BYTES = 32


def _key_path(home: Optional[Path] = None) -> Path:
    return _group_em_dir(home) / "box-key"


def _peer_key_path(box_id: str, home: Optional[Path] = None) -> Path:
    return _group_em_dir(home) / "peer-keys" / (box_id + ".json")


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _decode_key(text: object) -> Optional[bytes]:
    if not isinstance(text, str):
        return None
    try:
        key = base64.b64decode(text.strip().encode("ascii"), validate=True)
    except (ValueError, binascii.Error):
        return None
    return key or None


def _read_own_key(home: Optional[Path]) -> Optional[bytes]:
    try:
        return _decode_key(_key_path(home).read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None


def ensure_key(home: Optional[Path] = None) -> bytes:
    """Return this box's signing key, minting 32 random bytes once; never rewrites an extant key."""
    existing = _read_own_key(home)
    if existing is not None:
        return existing
    key = secrets.token_bytes(KEY_BYTES)
    path = _key_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, base64.b64encode(key) + b"\n")
    _chmod_private(path)
    return key


def pin_peer_key(box_id: str, key_b64: str, home: Optional[Path] = None) -> dict:
    """Pin ``box_id``'s key (base64) on this box; returns the stored record.

    Raises ValueError for an unsafe ``box_id`` or a key that is not base64. Re-pinning overwrites.
    """
    if not _valid_id(box_id):
        raise ValueError(f"invalid box id: {box_id!r}")
    if _decode_key(key_b64) is None:
        raise ValueError("key_b64 is not valid base64")
    record = {
        "schema_version": SCHEMA_VERSION,
        "box_id": box_id,
        "key_b64": key_b64.strip(),
        "pinned_at": time.time(),
    }
    path = _peer_key_path(box_id, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, (json.dumps(record, indent=2) + "\n").encode("utf-8"))
    _chmod_private(path)
    return record


def _read_peer_key(box_id: str, home: Optional[Path]) -> Optional[bytes]:
    if not _valid_id(box_id):
        return None
    try:
        data = json.loads(_peer_key_path(box_id, home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _decode_key(data.get("key_b64")) if isinstance(data, dict) else None


def _canonical(record: dict) -> bytes:
    body = {k: v for k, v in record.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _tag(key: bytes, record: dict) -> str:
    return hmac.new(key, _canonical(record), hashlib.sha256).hexdigest()


def sign(record: dict, home: Optional[Path] = None) -> dict:
    """Return a copy of ``record`` carrying ``"sig"``: HMAC-SHA256 hex under this box's key."""
    signed = {k: v for k, v in record.items() if k != "sig"}
    signed["sig"] = _tag(ensure_key(home), signed)
    return signed


def key_fingerprint(key: bytes) -> str:
    """Non-secret 12-hex identity of ``key``: a domain-separated hash prefix, never the key."""
    return hashlib.sha256(b"box-key-fp:" + key).hexdigest()[:12]


def own_fingerprint(home: Optional[Path] = None) -> str:
    """Fingerprint of this box's signing key (minted on first use, like ``sign``)."""
    return key_fingerprint(ensure_key(home))


def pinned_fingerprint(box_id: str, home: Optional[Path] = None) -> Optional[str]:
    """Fingerprint of the key pinned here for ``box_id``; None when no usable pin exists."""
    key = _read_peer_key(box_id, home)
    return None if key is None else key_fingerprint(key)


def diagnose(record: dict, box_id: str, home: Optional[Path] = None) -> Optional[dict]:
    """None when ``verify`` passes, else ``{"kind", "expected", "got"}`` saying why it fails.

    ``kind``: ``unsigned``; ``no_pin`` (nothing pinned for ``box_id``); ``pin_mismatch`` (the issuer
    stamped ``issuer_key_fp`` and it differs from the pin's); ``tag_mismatch`` (stamped fingerprint
    equals the pin's, tag still wrong: tampering, canonicalization or version skew);
    ``unverified_issuer_key`` (no stamp, tag wrong: issuer predates the stamp). ``expected`` is the
    pinned fingerprint, ``got`` the issuer's stamped one; either may be None. Never raises.
    """
    try:
        if verify(record, box_id, home):
            return None
        sig = record.get("sig")
        stamped = record.get("issuer_key_fp")
        got = stamped if isinstance(stamped, str) and stamped else None
        if not isinstance(sig, str) or not sig:
            return {"kind": "unsigned", "expected": pinned_fingerprint(box_id, home), "got": got}
        expected = pinned_fingerprint(box_id, home)
        if expected is None:
            kind = "no_pin"
        elif got is None:
            kind = "unverified_issuer_key"
        elif got != expected:
            kind = "pin_mismatch"
        else:
            kind = "tag_mismatch"
        return {"kind": kind, "expected": expected, "got": got}
    except Exception:
        return {"kind": "unsigned", "expected": None, "got": None}


def verify(record: dict, box_id: str, home: Optional[Path] = None) -> bool:
    """True only when ``record`` carries a valid tag under the key pinned here for ``box_id``."""
    try:
        sig = record.get("sig")
        if not isinstance(sig, str) or not sig:
            return False
        key = _read_peer_key(box_id, home)
        if key is None:
            return False
        return hmac.compare_digest(_tag(key, record), sig)
    except Exception:
        return False
