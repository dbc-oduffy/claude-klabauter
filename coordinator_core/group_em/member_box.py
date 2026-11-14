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


def _fold(name: str) -> str:
    """Case-folded ``name`` without a trailing ``.local`` (the mDNS suffix is never part of a box identity)."""
    low = name.lower()
    return low[: -len(".local")] if low.endswith(".local") else low


def _matches(member: dict, user: Optional[str], name: str) -> bool:
    names = {_fold(member["box"]), _fold(member["host"]), member["host"].split(".")[0].lower()}
    return _fold(name) in names and (user is None or user == member["user"])


def _split_token(token: str) -> Optional[tuple[Optional[str], str]]:
    """``(user, name)`` for ``[user@]name``; None for a blank token or an empty user/name part."""
    if not isinstance(token, str) or not token.strip():
        return None
    user, at, name = token.rpartition("@")
    if not at:
        return None, name
    return (user, name) if user and name else None


def member_box_id(token: str, home=None, run=None) -> Optional[str]:
    """The verified membership's box id naming ``token`` (box id in any case, member host, ``.local`` form,
    optional ``user@``), else None. Spawns nothing on a cache-hit snapshot; never registers a row."""
    parts = _split_token(token)
    if parts is None:
        return None
    try:
        members = _membership().verified_members(
            Path(home) if home is not None else box_registry.settings_home(), run=run or subprocess.run
        )
    except Exception:  # noqa: BLE001 - an unverifiable membership leaves the token as typed
        return None
    member = next((m for m in members or () if _matches(m, *parts)), None)
    return member["box"] if member else None


def _registered_alias(al, roster, member: dict, run) -> Optional[str]:
    """The ``unimatrix.far_boxes`` ssh alias this box's links dial for ``member``, else None.

    The membership host is an mDNS name that need not resolve from here; the registered alias carries the
    working route. An absent registry, no match or any lookup failure is None: the caller dials the member host."""
    if not (callable(getattr(al, "far_boxes_status", None)) and callable(getattr(al, "member_alias", None))):
        return None
    try:
        aliases = al.far_boxes_status(roster)[0]
        alias = al.member_alias(roster, member["box"], member["host"], aliases, run=run)
    except Exception:  # noqa: BLE001 - the lookup is best effort; the member host is the fallback route
        return None
    return alias if alias in aliases else None


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
    al, roster = _autolink(), _roster()
    remote = _registered_alias(al, roster, member, run) or f"{member['user']}@{member['host']}"
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
