"""agent_pipe.roster -- durable identity name -> current binding (kind, id, token, argv).

A name survives session-id and token change: rebinding replaces the binding in place. The token is
plain data carried for the delivery leg, never a gate. Writes are atomic (temp file + os.replace).
"""
from __future__ import annotations

import json
import os
from typing import Optional

from coordinator_core.agent_pipe.contract import KINDS, _checked_id, roster_path


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


def resolve(name: str) -> Optional[dict]:
    """Current binding for `name`, or None when unbound."""
    binding = _load().get(name)
    return dict(binding) if isinstance(binding, dict) else None


def list() -> dict:  # noqa: A001 - public roster verb
    """Every binding, name -> binding."""
    return {k: dict(v) for k, v in _load().items() if isinstance(v, dict)}
