"""Pin signing keys in both directions when a box is admitted to the fleet.

``exchange`` runs after a box row is written. Direction A: the new box pins the registrar's key and
every registered peer's key the registrar holds. Direction B: the registrar and each other peer pin
the new box's key. A local write goes through ``box_key.pin_peer_key``; a remote one through the
far box's ``groupem.box_key_pin`` over ``box_transport.invoke_remote``. The new box's own key is read
only from a locally readable root or the caller's ``key_b64``; no key is ever fetched from a far
box. Leaf module: imports ``box_key``, ``box_registry`` and ``box_transport`` only.

Negative-spec:
    - Never raises: a failed leg is an ``{id, reason}`` entry in an unpinned list.
    - Never overwrites a differing extant pin: local legs compare fingerprints first and report
      ``pin_mismatch``; remote pushes send ``refuse_mismatch: true`` (a far box on an older engine
      ignores it and overwrites).
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

from coordinator_core.group_em import box_key, box_registry, box_transport


def _local_root(box: dict) -> Optional[Path]:
    """The box's settings-home root when it is locally readable (an existing directory, no ssh transport)."""
    root = box.get("root")
    transport = box.get("transport")
    if not isinstance(root, str) or not root:
        return None
    if isinstance(transport, dict) and transport.get("kind") == "ssh":
        return None
    path = Path(root)
    try:
        return path if path.is_dir() else None
    except OSError:
        return None


def _b64(key: bytes) -> str:
    return base64.b64encode(key).decode("ascii")


def _pin_local(home: Optional[Path], box_id: str, key_b64: str) -> Optional[str]:
    """Pin ``key_b64`` for ``box_id`` under ``home``; None on success, else the unpinned reason."""
    key = box_key._decode_key(key_b64)
    if key is None:
        return "invalid_key"
    existing = box_key.pinned_fingerprint(box_id, home)
    if existing is not None:
        return None if existing == box_key.key_fingerprint(key) else "pin_mismatch"
    box_key.pin_peer_key(box_id, key_b64, home)
    return None


def _pin_remote(box: dict, box_id: str, key_b64: str, runner, timeout: float) -> Optional[str]:
    res = box_transport.invoke_remote(
        box,
        "groupem.box_key_pin",
        {"box_id": box_id, "key_b64": key_b64, "refuse_mismatch": True},
        runner,
        timeout=timeout,
    )
    if not res.get("ok"):
        return str(res.get("reason") or "unreachable")
    reply = res.get("reply")
    if isinstance(reply, dict) and reply.get("pinned") is True:
        return None
    reply = reply if isinstance(reply, dict) else {}
    return str(reply.get("reason") or reply.get("error") or "not_pinned")


def _pin_into(box: dict, box_id: str, key_b64: str, runner, timeout: float) -> Optional[str]:
    """Pin ``key_b64`` for ``box_id`` on ``box`` (local when its root is readable, else ssh)."""
    try:
        root = _local_root(box)
        if root is not None:
            return _pin_local(root, box_id, key_b64)
        if box_transport.ssh_host(box) is not None:
            return _pin_remote(box, box_id, key_b64, runner, timeout)
        return "no_route"
    except Exception as exc:  # noqa: BLE001 - contract: never raises
        return f"error:{type(exc).__name__}"


def _new_box_key(new_box: dict, key_b64: Optional[str]) -> Optional[str]:
    """The new box's key (base64): read from its local root, else the caller's ``key_b64``."""
    root = _local_root(new_box)
    if root is not None:
        own = box_key._read_own_key(root)
        if own is not None:
            return _b64(own)
    if isinstance(key_b64, str) and box_key._decode_key(key_b64) is not None:
        return key_b64.strip()
    return None


def exchange(
    new_box: dict,
    *,
    home: Optional[Path] = None,
    key_b64: Optional[str] = None,
    runner=None,
    timeout: float,
) -> dict:
    """Pin keys both ways for ``new_box`` (a ``box_registry`` record); report each direction by peer id.

    ``home`` is the registrar's settings home (default: the settings home). Returns ``{"new_box_pinned":
    [ids], "new_box_unpinned": [{id, reason}], "peers_pinned_new": [ids], "peers_unpinned_new":
    [{id, reason}]}``: the first two list the registrar/peers whose key the new box now holds, the
    last two the registrar/peers that now hold the new box's key. Reasons include ``no_route``,
    ``pin_mismatch``, ``no_own_key``, ``key_not_held``, ``new_key_unavailable`` and the transport's
    own refusal reason. Never raises.
    """
    out: dict = {"new_box_pinned": [], "new_box_unpinned": [], "peers_pinned_new": [], "peers_unpinned_new": []}
    try:
        _exchange(new_box, home, key_b64, runner, timeout, out)
    except Exception as exc:  # noqa: BLE001 - contract: never raises
        out["new_box_unpinned"].append({"id": "*", "reason": f"error:{type(exc).__name__}"})
    return out


def _exchange(new_box: dict, home, key_b64, runner, timeout: float, out: dict) -> None:
    new_id = new_box.get("box_id") if isinstance(new_box, dict) else None
    if not box_registry._valid_id(new_id):
        out["new_box_unpinned"].append({"id": str(new_id), "reason": "invalid_new_box"})
        out["peers_unpinned_new"].append({"id": str(new_id), "reason": "invalid_new_box"})
        return
    registrar_id = box_registry.local_box_id(home)
    peers = [
        row for row in box_registry.read_boxes()
        if row.get("box_id") not in (new_id, registrar_id)
    ]

    # Direction A: the new box pins the registrar and each peer whose key the registrar holds.
    if new_id != registrar_id:
        own = box_key._read_own_key(home)
        sources: list = [(registrar_id, _b64(own) if own is not None else None, "no_own_key")]
    else:
        sources = []
    for row in peers:
        held = box_key._read_peer_key(row["box_id"], home)
        sources.append((row["box_id"], _b64(held) if held is not None else None, "key_not_held"))
    for source_id, source_key, missing in sources:
        reason = missing if source_key is None else _pin_into(new_box, source_id, source_key, runner, timeout)
        if reason is None:
            out["new_box_pinned"].append(source_id)
        else:
            out["new_box_unpinned"].append({"id": source_id, "reason": reason})

    # Direction B: the registrar and each other peer pin the new box.
    targets: list = [(registrar_id, None, True)] if new_id != registrar_id else []
    targets += [(row["box_id"], row, False) for row in peers]
    new_key = _new_box_key(new_box, key_b64)
    for target_id, row, is_registrar in targets:
        if new_key is None:
            reason = "new_key_unavailable"
        elif is_registrar:
            try:
                reason = _pin_local(home, new_id, new_key)
            except Exception as exc:  # noqa: BLE001 - contract: never raises
                reason = f"error:{type(exc).__name__}"
        else:
            reason = _pin_into(row, new_id, new_key, runner, timeout)
        if reason is None:
            out["peers_pinned_new"].append(target_id)
        else:
            out["peers_unpinned_new"].append({"id": target_id, "reason": reason})
