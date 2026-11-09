"""agent_pipe.roster -- durable identity name -> current binding (kind, id, token, argv).

A name survives session-id and token change: rebinding replaces the binding in place. The token is
plain data carried for the delivery leg, never a gate. Writes are atomic (temp file + os.replace).
"""
from __future__ import annotations

import json
import os
from typing import Optional

from coordinator_core.agent_pipe.contract import KIND_CODEX, KINDS, _checked_id, peers_path, roster_path

_LIST = list  # `list` is rebound below as the public roster verb


def _load() -> dict:
    try:
        data = json.loads(roster_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def _store(data: dict) -> None:
    path = roster_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def bind(name: str, kind: str, id: str, token: Optional[str] = None, argv: Optional[list[str]] = None) -> dict:
    """Bind `name` to (kind, id), replacing any prior binding; returns the stored binding.

    `argv` is the app-server launch argv a codex binding needs for `codex_app_server.deliver`.
    """
    _checked_id("name", name)
    if kind not in KINDS:
        raise ValueError(f"invalid kind: {kind!r}")
    if not id:
        raise ValueError("invalid id: empty")
    binding = {"kind": kind, "id": id, "token": token, "argv": [*argv] if argv is not None else None}
    data = _load()
    data[name] = binding
    _store(data)
    return dict(binding)


def _peer_bindings() -> dict:
    """Bindings from peers.json rows `{name, vendor, target}` whose vendor is a known kind; {} when unreadable.

    A codex row carries no argv; it gets the app-server proxy argv, or None when codex cannot be located.
    """
    try:
        data = json.loads(peers_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = data.get("peers") if isinstance(data, dict) else data
    out: dict = {}
    for row in rows if isinstance(rows, _LIST) else ():
        if not isinstance(row, dict):
            continue
        name, vendor, target = row.get("name"), row.get("vendor"), row.get("target")
        if not (isinstance(name, str) and name and isinstance(target, str) and target and vendor in KINDS):
            continue
        out[name] = {"kind": vendor, "id": target, "token": None, "argv": None}
    return out


def _with_codex_argv(binding: dict) -> dict:
    if binding["kind"] == KIND_CODEX:
        try:
            from coordinator_core.agent_pipe.codex import CodexAdapter

            binding["argv"] = CodexAdapter().proxy_argv()
        except Exception:  # noqa: BLE001 - codex not installed: delivery reports the missing argv
            pass
    return binding


def _external_binding(name: str) -> Optional[dict]:
    """Project a live owned thread UUID binding; no second mutable identity store."""
    from coordinator_core.session import external_registry
    row = external_registry.lookup(name)
    if row is None:
        return None
    return {"kind": KIND_CODEX, "id": row["session_id"], "token": None, "argv": None}


def resolve(name: str) -> Optional[dict]:
    """Current binding for `name`, or None when unbound.

    roster.json wins; a name only the SessionStart hook bound resolves from peers.json.
    """
    binding = _load().get(name)
    if isinstance(binding, dict):
        return dict(binding)
    peer = _peer_bindings().get(name)
    binding = peer or _external_binding(name)
    return _with_codex_argv(binding) if binding else None


def list() -> dict:  # noqa: A001 - public roster verb
    """Every binding, name -> binding; roster.json entries shadow peers.json rows (peer codex argv left None)."""
    from coordinator_core.session import external_registry
    owned = {sid: {"kind": KIND_CODEX, "id": sid, "token": None, "argv": None}
             for sid in external_registry.snapshot()}
    return {**owned, **_peer_bindings(), **{k: dict(v) for k, v in _load().items() if isinstance(v, dict)}}
