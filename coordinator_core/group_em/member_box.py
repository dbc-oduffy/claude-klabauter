"""Resolve a grantee box token against the verified Unimatrix membership into a box.json ssh row.

A member box absent from ``box.json`` is still reachable: the signed membership snapshot names its
host, user and box id, and autolink's ``resolve_key`` names the relay key. The resolved row is
materialised through ``box_registry.register`` so ``box_transport._relay_registered`` admits it.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Optional

from coordinator_core.group_em import box_registry

BOX_UNKNOWN = "BOX_UNKNOWN"
_BIN = Path(__file__).resolve().parents[2] / "coordinator" / "bin"
_MODULES: dict = {}


def _load(name: str, filename: str):
    if name not in _MODULES:
        spec = importlib.util.spec_from_file_location(name, _BIN / filename)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODULES[name] = mod
    return _MODULES[name]


def _membership():
    return _load("unimatrix_membership", "unimatrix-membership.py")


def _autolink():
    return _load("unimatrix_autolink", "unimatrix-autolink.py")


def _roster():
    return _load("unimatrix_roster", "unimatrix-roster.py")


def _matches(member: dict, user: Optional[str], name: str) -> bool:
    low = name.lower()
    names = {member["box"].lower(), member["host"].lower(), member["host"].split(".")[0].lower()}
    return low in names and (user is None or user == member["user"])


def resolve_member_box(token: str, home=None, run=None) -> dict:
    """Resolve ``token`` (box id, ``user@box`` or member host) to a registered ssh box row.

    Returns ``{"ok": True, "box": <box.json row>}`` or ``{"ok": False, "reason": BOX_UNKNOWN |
    "membership_unverified:<why>"}``. An unverifiable, absent or unpinned snapshot is
    ``membership_unverified``; a verified snapshot that does not list the token is ``BOX_UNKNOWN``.
    A cache-hit ``verified_members`` spawns nothing; the relay key lookup is the only other spawn.
    """
    if not isinstance(token, str) or not token.strip():
        return {"ok": False, "reason": BOX_UNKNOWN}
    run = run or subprocess.run
    user, at, name = token.rpartition("@")
    if not at:
        user = None
    elif not user or not name:
        return {"ok": False, "reason": BOX_UNKNOWN}
    try:
        mem = _membership()
    except OSError as exc:
        return {"ok": False, "reason": f"membership_unverified:library_absent:{type(exc).__name__}"}
    home_path = Path(home) if home is not None else box_registry.settings_home()
    try:
        members = mem.verified_members(home_path, run=run)
    except mem.MembershipError as exc:
        return {"ok": False, "reason": f"membership_unverified:{str(exc)[:120]}"}
    if not members:
        return {"ok": False, "reason": "membership_unverified:no_snapshot"}
    member = next((m for m in members if _matches(m, user, name)), None)
    if member is None:
        return {"ok": False, "reason": BOX_UNKNOWN}
    remote = f"{member['user']}@{member['host']}"
    al, roster = _autolink(), _roster()
    key = al.resolve_key(
        roster,
        remote,
        local_fn=lambda r, a: al.machine_local_key(r, a, run=run),
        ssh_fn=lambda r, a: al.ssh_identity_file(r, a, run=run),
    )
    if not isinstance(key, str) or not key:
        return {"ok": False, "reason": "membership_unverified:no_relay_key"}
    directory = box_registry._group_em_dir(home_path)
    box_id = member["box"]
    existing = next((b for b in box_registry.read_boxes(directory) if b.get("box_id") == box_id), None)
    prior = existing.get("transport") if isinstance(existing, dict) else None
    transport = {"kind": "ssh", "host": remote, "relay_key": key}
    if isinstance(prior, dict):
        for extra in ("remote_shell", "ssh"):
            if extra in prior:
                transport[extra] = prior[extra]
        if prior == transport:
            return {"ok": True, "box": existing}
    row = box_registry.register(
        home_path, existing["capability"] if existing else {}, directory=directory,
        box_id=box_id, transport=transport,
    )
    return {"ok": True, "box": row}
