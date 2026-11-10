"""
coordinator_core.ops.group_em_box_federation -- JSON-RPC "groupem.box_register", "groupem.box_place",
"groupem.box_send", "groupem.box_roster", "groupem.suite_grant_receive", "groupem.box_key_pin".

Purpose: the engine surface of multi-box group-em. Register a box (settings-home root + capability)
in this box's registry, place a whole run on a registered box, append one line to a box's inbox, read
the federated roster of remote boxes, accept a signed suite grant relayed from another box, and pin a
peer box's signing key.

Registered in `coordinator_core/ops/__init__.py`'s registration list, `_registry_map.py`'s
`OP_MODULE_MAP`, `op_scopes.py` (scope "none") and `authz/classification.py` (register, place and
send MUTATING; roster COMPUTE_ONLY). Enrolled in `_BUDGETED_ENTRYPOINTS` of the spawn audit with an
empty reachable spawn set.

Negative-spec:
    - Never raises: an invalid param or I/O failure is `{"error": ...}`.
    - `groupem.box_register` spawns only the ssh processes `box_transport.invoke_remote` reaches (one per
      remote pin leg, each under `CONNECT_TIMEOUT_S`); every other op spawns nothing, and every
      effect is a file read or write under a settings-home.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.group_em import box_probe, box_registry, box_send, placement
from coordinator_core.ipc import register_op


def _str(p: dict, key: str) -> Optional[str]:
    v = p.get(key)
    return v if isinstance(v, str) and v else None


@register_op("groupem.box_register")
def _groupem_box_register(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.box_register" handler.

    Params:
        root (str) -- the settings-home root of the box to register.
        capability (dict, optional) -- the capability record; default is the `capability` already in
            `<root>/state/group-em/box.json`, else `box_probe.write_capability(root)` (this machine's probe).
        box_id (str, optional) -- the box's id, for a remote box whose root is not locally readable;
            without it the id is minted under ``root``.
        transport (dict, optional) -- the remote box's transport (``{kind: ssh, host, invoke}``).
        key_b64 (str, optional) -- the new box's signing key, base64, when its root is not locally
            readable; without it that direction is reported unpinned.

    Returns:
        {"registered": True, "box": <registry record>, "pins": {"new_box_pinned": [ids],
        "new_box_unpinned": [{id, reason}], "peers_pinned_new": [ids], "peers_unpinned_new":
        [{id, reason}]}} or {"error": str}. A pin failure never fails the registration; remedy each
        named id with `groupem.box_key_pin`. A far box on an older engine ignores ``refuse_mismatch``.
    """
    p = params if isinstance(params, dict) else {}
    root = _str(p, "root")
    if root is None:
        return {"error": "root (non-empty str) is required"}
    box_id, transport = p.get("box_id"), p.get("transport")
    if box_id is not None and not isinstance(box_id, str):
        return {"error": "box_id must be str"}
    if transport is not None and not isinstance(transport, dict):
        return {"error": "transport must be an object"}
    cap = p.get("capability")
    if cap is not None and not isinstance(cap, dict):
        return {"error": "capability must be an object"}
    try:
        if cap is None:
            cap = box_probe.stored_capability(Path(root))
        if cap is None:
            cap = box_probe.write_capability(Path(root)).get("capability") or {}
        record = box_registry.register(root, cap, box_id=box_id, transport=transport)
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}
    from coordinator_core.group_em import box_onboarding_pins, box_transport

    pins = box_onboarding_pins.exchange(
        record, key_b64=_str(p, "key_b64"), timeout=box_transport.CONNECT_TIMEOUT_S
    )
    return {"registered": True, "box": record, "pins": pins}


@register_op("groupem.box_place")
def _groupem_box_place(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.box_place" handler.

    Params:
        run (str) -- the run label to place.
        requirement (dict, optional) -- capability requirement (`min_<key>` floors, exact matches).
        box_id (str, optional) -- explicit target box.
        brief (str, optional) -- brief path or text carried on the placement.

    Returns:
        {"ok": True, "placement"} | {"ok": False, "reason"} or {"error": str}.
    """
    p = params if isinstance(params, dict) else {}
    run = _str(p, "run")
    if run is None:
        return {"error": "run (non-empty str) is required"}
    req = p.get("requirement", {})
    if not isinstance(req, dict):
        return {"error": "requirement must be an object"}
    box_id = p.get("box_id")
    if box_id is not None and not isinstance(box_id, str):
        return {"error": "box_id must be str"}
    brief = p.get("brief", "")
    if not isinstance(brief, str):
        return {"error": "brief must be str"}
    try:
        return placement.place_run(run, req, box_id=box_id, brief=brief)
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}


SENDER_CLAIM_WINDOW_S = 300


def _verified_sender(p: dict, to: str, text: str, now: Optional[float] = None) -> Optional[tuple]:
    """``(from_box, from_session)`` when ``p`` carries a fresh ``ts`` and a ``sig`` valid under the key pinned for ``from_box``."""
    import time

    from coordinator_core.group_em import box_key

    ts = p.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, int):
        return None
    if abs((time.time() if now is None else now) - ts) > SENDER_CLAIM_WINDOW_S:
        return None

    from_box, from_session, sig = _str(p, "from_box"), _str(p, "from_session"), _str(p, "sig")
    if from_box is None or from_session is None or sig is None:
        return None
    claim = {"from_box": from_box, "from_session": from_session, "to_session": to, "text": text, "ts": ts, "sig": sig}
    return (from_box, from_session) if box_key.verify(claim, from_box) else None


@register_op("groupem.box_send")
def _groupem_box_send(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.box_send" handler.

    Params:
        box_id (str) -- registered target box; omitted when ``local`` is true.
        local (bool, optional) -- deliver into THIS box's own inbox (the far end of a cross-box relay,
            which cannot know the id the near box registered it under); self-registers if absent.
        to_session (str) -- the EM session the line is addressed to.
        text (str) -- the message.
        from_box, from_session, sig (str), ts (int epoch seconds) (optional) -- with ``local``: the sender, stamped only when
            ``ts`` is within ``SENDER_CLAIM_WINDOW_S`` and ``sig`` verifies (``box_key``) under the key pinned for ``from_box``; otherwise ignored
            and the line is unattributed, stamped with this box's own id.

    Returns:
        {"sent": True, "box_id", "path"} | {"sent": False, "reason"} or {"error": str}.
    """
    p = params if isinstance(params, dict) else {}
    box_id, to = _str(p, "box_id"), _str(p, "to_session")
    local = p.get("local") is True
    if (box_id is None and not local) or to is None:
        return {"error": "box_id (or local: true) and to_session (non-empty str) are required"}
    text = p.get("text")
    if not isinstance(text, str):
        return {"error": "text (str) is required"}
    try:
        if local:
            box_id = box_registry.local_box_id()
            if box_registry.box_root(box_id) is None:
                home = settings_home()
                box_id = box_registry.ensure_identity(home)
                box_registry.register(home, box_probe.stored_capability(home) or {}, box_id=box_id)
        sender = _verified_sender(p, to, text) if local else None
        if sender is not None:
            return box_send.send(box_id, to, text, from_session=sender[1], from_box=sender[0])
        return box_send.send(box_id, to, text)
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}


@register_op("groupem.suite_grant_receive")
def _groupem_suite_grant_receive(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.suite_grant_receive" handler: the far end of a relayed suite grant.

    Params:
        envelope (dict) -- the signed grant the issuing box built; verified here against the key
            pinned for its ``issuer_box_id``.

    Returns:
        {"ok": True, "reason": "received"} | {"ok": False, "reason"} -- a refusal writes nothing.
    """
    p = params if isinstance(params, dict) else {}
    envelope = p.get("envelope")
    if not isinstance(envelope, dict):
        return {"error": "envelope (object) is required"}
    try:
        from coordinator_core.group_em import suite_grant

        return suite_grant.receive_relayed_grant(envelope)
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}


@register_op("groupem.box_key_pin")
def _groupem_box_key_pin(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.box_key_pin" handler: pin a peer box's signing key on this box.

    Trust anchor of every relayed grant: any caller of this op can pin a key it also holds and so
    mint a verifying envelope. No code gates the caller; the MUTATING classification is the only
    fence, so run it as an operator act only.

    Params:
        box_id (str) -- the peer box whose grants this box will accept.
        key_b64 (str) -- that box's key, base64 (its ``state/group-em/box-key`` text).
        refuse_mismatch (bool, optional) -- when true and a pin for ``box_id`` exists with a different
            fingerprint, write nothing and return ``{"pinned": False, "box_id", "reason": "pin_mismatch"}``.

    Returns:
        {"pinned": True, "box_id"} | {"pinned": False, "box_id", "reason"} or {"error": str}.
    """
    p = params if isinstance(params, dict) else {}
    box_id, key_b64 = _str(p, "box_id"), _str(p, "key_b64")
    if box_id is None or key_b64 is None:
        return {"error": "box_id and key_b64 (non-empty str) are required"}
    try:
        from coordinator_core.group_em import box_key

        if p.get("refuse_mismatch") is True:
            key = box_key._decode_key(key_b64)
            have = box_key.pinned_fingerprint(box_id)
            if key is not None and have is not None and have != box_key.key_fingerprint(key):
                return {"pinned": False, "box_id": box_id, "reason": "pin_mismatch"}
        box_key.pin_peer_key(box_id, key_b64)
        return {"pinned": True, "box_id": box_id}
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}


@register_op("groupem.box_roster")
def _groupem_box_roster(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.box_roster" handler: the remote boxes' live runs, via `watch.remote_box_rows`.

    Returns:
        {"boxes": [registered box ids], "rows": [{box_id, session_id, run, live, reachable}, ...]} or {"error": str}.
    """
    try:
        from coordinator_core.group_em import watch

        rows = watch.remote_box_rows()
        return {"boxes": [b["box_id"] for b in box_registry.read_boxes()], "rows": rows}
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}
