"""coordinator_core.hooks.rewrite_sendmessage_peer_name -- PreToolUse(SendMessage)
advisory rewrite of a near-miss peer name to the one live session it means.

The harness refuses a `to` that is not an exact live session name. When `to` names no
live session but exactly one live session matches it by an added repo prefix
(`claude-klabauter-c8` -> `claude-klabauter-c8`), a dropped prefix, or a dash-bounded suffix, `to` is
rewritten via `updatedInput` (every other input field kept) with one advisory line.
Zero or several matches, an exact live name, or any unreadable input pass through
untouched. This hook never denies.

Foreign reply address: a `to` of `uds:<socket>` shaped for the other platform (a Windows pipe on
macOS/Linux, a Unix path on Windows) that exactly one live roster mirror carries as its far socket
is rewritten to that peer's name; with no such mirror it gets an advisory naming the other box or
platform and passes through.

Sender stamp: when the payload `session_id` is a live roster session, the `message` gains a
leading `[from <name> on <box> · <session id>]` line (idempotent); an unresolvable sender
leaves the message unchanged.

Roster: one `harness_registry.snapshot()` read per call, names via
`reachability.resolve_candidates` -- not cwd-filtered, since a peer in another repo is a
legitimate target. No subprocess, no cache.

Op contract: `params` is the flat PreToolUse payload dict (`tool_name`, `tool_input.to`).

Hook wiring: PreToolUse, matcher `SendMessage`, op `hooks.rewrite_sendmessage_peer_name`.

Entrypoints: check(payload) -> Optional[envelope]; main() -- stdin JSON, stdout JSON.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core._hook_envelope import allow_advisory, no_advisory, payload_of, rewrite_input
from coordinator_core.ipc import register_op

GUARD_NAME = "rewrite-sendmessage-peer-name"
CLASS = "advisory"
MATCHERS = ("SendMessage",)


def _match_peer_name(to: str, candidates: list) -> Optional[Any]:
    """The single live candidate `to` near-misses, else None.

    None when `to` is exactly a live name or address, or when zero or several
    candidates match. A match is `to` plus a dash-bounded prefix, `to` minus one, or a
    dash-bounded suffix of a live name.
    """
    if any(to == c.name or to == c.address for c in candidates):
        return None
    hits = [
        c
        for c in candidates
        if c.name.endswith("-" + to) or to.endswith("-" + c.name)
    ]
    return hits[0] if len(hits) == 1 else None


_CONTRACT_MODULE: Any = None


def _peer_contract() -> Any:
    """The peer contract module, loaded by path from the sibling `coordinator/bin` once per process."""
    global _CONTRACT_MODULE
    if _CONTRACT_MODULE is not None:
        return _CONTRACT_MODULE
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "unimatrix-peer-contract.py"
    spec = importlib.util.spec_from_file_location("unimatrix_peer_contract_hook", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _CONTRACT_MODULE = module
    return module


def _sessions_dir() -> str:
    return os.path.expanduser("~/.claude/sessions")


def _session_entries() -> list:
    """Every readable dict entry in the sessions dir, in filename order."""
    sess = _sessions_dir()
    try:
        files = sorted(os.listdir(sess))
    except OSError:
        return []
    found = []
    for fname in files:
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(sess, fname), encoding="utf-8") as fh:
                entry = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict):
            found.append(entry)
    return found


def _stamped_box(session_id: str, field: str) -> str:
    """The `field` stamp on the sessions-dir entry whose `sessionId` is exactly `session_id`."""
    for entry in _session_entries():
        if entry.get("sessionId") == session_id:
            return str(entry.get(field) or "")
    return ""


def _sender_header(session_id: Any, candidates: list) -> Optional[str]:
    """`[from <name> on <box> · <session id>]` for the roster session `session_id`, else None.

    Box: the sender's sessions-dir stamp, else the peer contract's `local_box()` -- never a
    name suffix or the session-id prefix. Any unresolvable piece yields None (never a guess).
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    sender = next((c for c in candidates if c.session_id == session_id), None)
    if sender is None:
        return None
    try:
        contract = _peer_contract()
        box = _stamped_box(session_id, contract.BOX_FIELD) or contract.local_box()
    except Exception:
        return None
    if not box:
        return None
    return f"[from {sender.name} on {box} · {session_id}]"


def _is_windows() -> bool:
    return os.name == "nt"


def _foreign_platform(addr: str) -> str:
    """`Windows named pipe` / `Unix socket path` when `addr` is shaped for the other platform, else ''."""
    pipe = addr.startswith(("\\\\.\\pipe\\", "//./pipe/"))
    if pipe and not _is_windows():
        return "Windows named pipe"
    if addr.startswith("/") and _is_windows():
        return "Unix socket path"
    return ""


def _mirror_entries(addr: str) -> list:
    """Sessions-dir entries that mirror the far socket `addr` (macSocketPath or unimatrixRemoteSocket)."""
    return [e for e in _session_entries() if addr in (e.get("macSocketPath"), e.get("unimatrixRemoteSocket"))]


def _foreign_address(to: str, snapshot: dict, candidates: list) -> tuple:
    """(roster peer or None, advisory or None) for a `to` that is a foreign-platform `uds:` address.

    A peer only when exactly one live roster name mirrors that far socket. A local socket, a
    same-platform address or a non-`uds:` string yields (None, None).
    """
    if not to.startswith("uds:"):
        return None, None
    addr = to[len("uds:"):]
    platform = _foreign_platform(addr)
    if not platform:
        return None, None
    if any(addr == getattr(r, "messaging_socket_path", None) for r in snapshot.values()):
        return None, None
    entries = _mirror_entries(addr)
    by_sid = {c.session_id: c for c in candidates}
    peers = {by_sid[e["sessionId"]].name: by_sid[e["sessionId"]] for e in entries if e.get("sessionId") in by_sid}
    if len(peers) == 1:
        return next(iter(peers.values())), None
    box = next(
        (str(e.get(k)) for e in entries for k in ("unimatrixBox", "box", "unimatrixRemote") if e.get(k)), ""
    )
    where = f"box {box}" if box else "another box"
    return None, (
        f"PEER ADDRESS FOREIGN: {to!r} is a {platform} on {where}, not routable from this box; "
        "no live roster peer mirrors it. Send to the peer's roster name instead."
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    to = tool_input.get("to")
    to = to.strip() if isinstance(to, str) else ""
    try:
        from coordinator_core.session import harness_registry, reachability

        snapshot = harness_registry.snapshot()
        candidates = reachability.resolve_candidates(snapshot)
    except Exception:
        return None
    updated = dict(tool_input)
    notes = []
    hit = _match_peer_name(to, candidates) if to else None
    foreign_note = None
    if hit is None and to:
        hit, foreign_note = _foreign_address(to, snapshot, candidates)
    if hit is not None:
        updated["to"] = hit.name
        notes.append(f"PEER NAME MAPPED: {to!r} -> {hit.name!r}.")
    elif foreign_note:
        notes.append(foreign_note)
    header = _sender_header(payload.get("session_id"), candidates)
    message = updated.get("message")
    if header and isinstance(message, str) and not message.startswith(header):
        updated["message"] = f"{header}\n{message}"
        notes.append("SENDER STAMPED.")
    if not notes:
        return None
    if updated == tool_input:
        return allow_advisory("PreToolUse", " ".join(notes))
    return rewrite_input("PreToolUse", updated, " ".join(notes))


@register_op("hooks.rewrite_sendmessage_peer_name")
def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    try:
        return check(params) or no_advisory()
    except Exception:
        return no_advisory()


def main() -> int:
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    envelope = check(payload)
    if envelope:
        sys.stdout.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
