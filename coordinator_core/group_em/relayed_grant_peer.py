"""Far-box receivers for the cross-box grant legs that need a peer anchor: key pin and revoke.

``receive_key_pin`` pins an issuer box's key on this box, trusting signed Unimatrix membership as the
anchor: only when the claimed box is the relay's pinned ``--peer``, is listed in this box's verified
membership, and has no pin yet. ``receive_relayed_revoke`` verifies a signed ``suite-grant-revoke``
under the pinned issuer key, removes the relayed grant and leaves a tombstone. Both return
``{"ok": True, "reason"}`` or ``{"ok": False, "reason"}`` and never raise.

NEGATIVE SPEC:
  - An existing pin is never overwritten: a rotated key is ``pin_exists``, an operator act.
  - A revoke older than the stored grant's ``issued_at`` is ``stale_revoke``: a replayed revoke cannot
    withdraw a re-issued grant.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em import box_key, box_registry, member_box, suite_grant

TOMBSTONE_SUFFIX = ".revoked.json"


def _refuse(reason: str) -> dict:
    return {"ok": False, "reason": reason}


def _is_member(box_id: str, home: Path) -> Optional[str]:
    """None when ``box_id`` is in this box's verified membership, else the refusal reason."""
    mem = member_box._membership()
    try:
        members = mem.verified_members(home)
    except mem.MembershipError as exc:
        return f"membership_unverified:{str(exc)[:120]}"
    if not members:
        return "membership_unverified:no_snapshot"
    return None if any(m.get("box") == box_id for m in members) else "not_member"


def receive_key_pin(box_id: object, key_b64: object, peer: object, home: Optional[Path] = None) -> dict:
    """Pin ``box_id``'s key on this box, or refuse with a named reason.

    Refusals: ``no_peer`` (no forced-command peer), ``issuer_not_peer`` (claimed box is not the peer),
    ``bad_box``, ``bad_key``, ``not_member`` / ``membership_unverified:<why>``, ``pin_exists``.
    """
    try:
        if not isinstance(peer, str) or not peer:
            return _refuse("no_peer")
        if box_id != peer:
            return _refuse("issuer_not_peer")
        if not box_registry._valid_id(box_id):
            return _refuse("bad_box")
        if not isinstance(key_b64, str) or box_key._decode_key(key_b64) is None:
            return _refuse("bad_key")
        home_path = Path(home) if home is not None else box_registry.settings_home()
        why = _is_member(box_id, home_path)
        if why is not None:
            return _refuse(why)
        if box_key.pinned_fingerprint(box_id, home_path) is not None:
            return _refuse("pin_exists")
        box_key.pin_peer_key(box_id, key_b64, home_path)
        return {"ok": True, "reason": "pinned"}
    except Exception as exc:  # noqa: BLE001 -- the receive contract is never-raise
        return _refuse(f"keypin_failed:{type(exc).__name__}")


def _revoke_checks(envelope: dict, home: Path) -> Optional[str]:
    issuer_box = envelope.get("issuer_box_id")
    if not box_registry._valid_id(issuer_box):
        return "bad_issuer_box"
    why = box_key.diagnose(envelope, issuer_box, home)
    if why is not None:
        return suite_grant._bad_signature_reason(why)
    if (
        envelope.get("kind") != suite_grant.REVOKE_KIND
        or not suite_grant._safe_sid(envelope.get("grantee_session_id"))
        or not suite_grant._safe_sid(envelope.get("issuer_session_id"))
    ):
        return "bad_record"
    if envelope.get("grantee_box_id") != box_registry.local_box_id(home):
        return "wrong_grantee_box"
    return None


def receive_relayed_revoke(envelope: object, home: Optional[Path] = None) -> dict:
    """Withdraw the relayed grant a signed ``suite-grant-revoke`` names; ``{"ok": True, "reason": "revoked"}``.

    The tag must verify under the issuer box's pinned key and the stored grant must be that issuer's
    (``issuer_mismatch``); none stored is ``no_grant``. The grant file is replaced by
    ``relayed/<sid>.revoked.json``.
    """
    try:
        if not isinstance(envelope, dict):
            return _refuse("bad_envelope")
        home_path = Path(home) if home is not None else box_registry.settings_home()
        bad = _revoke_checks(envelope, home_path)
        if bad is not None:
            return _refuse(bad)
        sid = envelope["grantee_session_id"]
        grant_path = suite_grant._relayed_path(sid, None, home_path)
        try:
            stored = json.loads(grant_path.read_text(encoding="utf-8")).get("envelope")
        except (OSError, ValueError, AttributeError):
            return _refuse("no_grant")
        if not isinstance(stored, dict):
            return _refuse("no_grant")
        if (
            stored.get("issuer_box_id") != envelope["issuer_box_id"]
            or stored.get("issuer_session_id") != envelope["issuer_session_id"]
        ):
            return _refuse("issuer_mismatch")
        revoked = suite_grant._parse_iso(envelope.get("revoked_at"))
        issued = suite_grant._parse_iso(stored.get("issued_at"))
        if revoked is None or issued is None:
            return _refuse("bad_record")
        if revoked + suite_grant.CLOCK_SKEW_S < issued:
            return _refuse("stale_revoke")
        tombstone = grant_path.with_name(f"{sid}{TOMBSTONE_SUFFIX}")
        record = {"revoke": envelope, "grant": stored, "recorded_at": suite_grant._iso(time.time())}
        atomic_write_bytes(tombstone, (json.dumps(record, indent=2) + "\n").encode("utf-8"))
        grant_path.unlink(missing_ok=True)
        return {"ok": True, "reason": "revoked"}
    except Exception as exc:  # noqa: BLE001 -- the receive contract is never-raise
        return _refuse(f"revoke_failed:{type(exc).__name__}")
