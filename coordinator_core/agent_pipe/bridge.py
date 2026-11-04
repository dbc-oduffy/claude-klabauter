"""agent_pipe.bridge -- resident router delivering labelled, provenance-stamped messages between roster identities.

`enqueue` appends a BridgeMessage line to `<pipe root>/bridge.jsonl`; `serve` drains that file and calls
`route`, which resolves `to_identity` through the roster at dispatch time (so a line queued before a rebind
reaches the new binding), applies PM routing control from `routes.json` (default allow), prepends the
label line and hands the text to the target's leg: claude -> mailbox, codex -> app-server thread, agy ->
resumed conversation. The codex and agy legs hand off and never hold the drain loop for the remote turn.
No leg checks a messaging token; a stored token is data.

routes.json: {"paused": ["a>b", "*>b", "a>*"], "muted": ["name"]}. A paused pair is refused; a muted
identity neither sends nor receives. Refusals are appended to `<pipe root>/bridge.log.jsonl`.
Trap: a body starting "[reply" is refused (loop guard) so replies are never auto-forwarded back.
Trap: `_claim_inbox` shares `mailbox.drain`'s race (an appender that opened the path before the rename loses its line; `_append_lock` is win32-only), and roster/routes read-modify-write is unlocked.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.agent_pipe import codex_app_server, mailbox, roster
from coordinator_core.agent_pipe.agy import AgyAdapter
from coordinator_core.agent_pipe.contract import (
    KIND_AGY,
    KIND_CLAUDE,
    KIND_CODEX,
    BridgeMessage,
    CAUSE_HANDSHAKE_TIMEOUT,
    CAUSE_TURN_TIMEOUT,
    PipeMessage,
    classify_exit,
    _checked_id,
    _pipe_root,
    routes_path,
)

ORIGIN_BRIDGE = "bridge"
LOOP_GUARD_PREFIX = "[reply"
UNKNOWN_KIND = "unknown"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_IDLE_POLL_S = 0.2


def inbox_path() -> Path:
    """Bridge inbox `<settings_home>/agent-pipe/bridge.jsonl`, one BridgeMessage JSON line per message."""
    return _pipe_root() / "bridge.jsonl"


def log_path() -> Path:
    """Refusal/failure log `<settings_home>/agent-pipe/bridge.log.jsonl`."""
    return _pipe_root() / "bridge.log.jsonl"


def label_line(from_identity: str, from_kind: str) -> str:
    """The provenance line prepended to every delivered body."""
    return f"[from {from_identity} ({from_kind}) via bridge]"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (line + "\n").encode("utf-8")
    with mailbox._append_lock(path):
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)


def _log(event: str, msg: BridgeMessage, reason: str, cause: Optional[str] = None) -> None:
    row = {"ts": _now(), "event": event, "reason": reason, "id": msg.id,
           "from": msg.from_identity, "to": msg.to_identity}
    if cause:
        row["cause"] = cause
    _append_line(log_path(), json.dumps(row, ensure_ascii=False, separators=(",", ":")))


def make_message(from_identity: str, to_identity: str, content: str) -> BridgeMessage:
    """BridgeMessage with a fresh id/ts; `from_kind` is the sender's current roster kind (or "unknown")."""
    _checked_id("from_identity", from_identity)
    _checked_id("to_identity", to_identity)
    sender = roster.resolve(from_identity)
    return BridgeMessage(
        id=uuid.uuid4().hex, from_identity=from_identity,
        from_kind=sender["kind"] if sender else UNKNOWN_KIND,
        to_identity=to_identity, content=content, ts=_now(), origin=ORIGIN_BRIDGE,
    )


def enqueue(msg: BridgeMessage) -> Path:
    """Append msg to the bridge inbox so the sender never blocks on delivery; returns the inbox path."""
    _append_line(inbox_path(), msg.to_json_line())
    return inbox_path()


def _load_routes() -> dict:
    try:
        data = json.loads(routes_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _store_routes(data: dict) -> None:
    path = routes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def set_pause(from_identity: str, to_identity: str, paused: bool = True) -> dict:
    """PM control: pause (or resume) one route; either side may be "*". Returns the routes document."""
    pair = f"{_checked_id('from', from_identity)}>{_checked_id('to', to_identity)}"
    data = _load_routes()
    pairs = [p for p in data.get("paused", []) if p != pair]
    if paused:
        pairs.append(pair)
    data["paused"] = sorted(pairs)
    _store_routes(data)
    return data


def set_mute(identity: str, muted: bool = True) -> dict:
    """PM control: mute (or unmute) one identity for both sending and receiving."""
    _checked_id("identity", identity)
    data = _load_routes()
    names = [n for n in data.get("muted", []) if n != identity]
    if muted:
        names.append(identity)
    data["muted"] = sorted(names)
    _store_routes(data)
    return data


def refusal(msg: BridgeMessage) -> Optional[str]:
    """Why routes.json or the loop guard refuses msg, or None when it may be delivered."""
    if msg.content.lstrip().startswith(LOOP_GUARD_PREFIX):
        return "loop-guard"
    routes = _load_routes()
    muted = routes.get("muted", [])
    for name in (msg.from_identity, msg.to_identity):
        if name in muted:
            return f"muted:{name}"
    paused = set(routes.get("paused", []))
    for pair in (f"{msg.from_identity}>{msg.to_identity}", f"*>{msg.to_identity}", f"{msg.from_identity}>*", "*>*"):
        if pair in paused:
            return f"paused:{pair}"
    return None


@dataclass
class Delivery:
    """Outcome of one route(). status: delivered (claude mailbox written), handed-off (codex/agy running
    detached), refused (routing control), unroutable (no binding or leg failed to start)."""

    status: str
    reason: Optional[str]
    msg: BridgeMessage
    handle: object = None

    def wait(self, timeout: Optional[float] = None) -> None:
        """Block until a handed-off codex/agy leg finishes; no-op for the other statuses."""
        if isinstance(self.handle, threading.Thread):
            self.handle.join(timeout)
        elif isinstance(self.handle, subprocess.Popen):
            try:
                self.handle.wait(timeout)
            except subprocess.TimeoutExpired:
                pass


@dataclass(frozen=True)
class _BridgeMailLine(PipeMessage):
    """PipeMessage whose mailbox line also carries the bridge provenance object."""

    provenance: Optional[dict] = None

    def to_json_line(self) -> str:
        d = asdict(self)
        if d["origin"] is None:
            del d["origin"]
        return json.dumps(d, ensure_ascii=False, separators=(",", ":"))


def _body(msg: BridgeMessage) -> str:
    return f"{msg.label}\n{msg.content}"


def _deliver_claude(binding: dict, msg: BridgeMessage) -> Delivery:
    provenance = {"from_identity": msg.from_identity, "from_kind": msg.from_kind, "origin": msg.origin, "ts": msg.ts}
    mailbox.append(_BridgeMailLine(
        id=msg.id, sender_agent=msg.from_identity, sender_conversation=msg.from_identity,
        to_session=binding["id"], content=_body(msg), ts=msg.ts, origin=msg.origin, provenance=provenance,
    ))
    return Delivery("delivered", None, msg)


def _deliver_codex(binding: dict, msg: BridgeMessage) -> Delivery:
    argv = binding.get("argv")
    if not argv:
        return Delivery("unroutable", "codex binding has no app-server argv", msg)
    text = _body(msg)

    def run() -> None:
        try:
            for _ in codex_app_server.deliver(argv, binding["id"], text):
                pass
        except Exception as exc:  # noqa: BLE001 - a failed remote turn must not kill the router
            cause = classify_exit(str(exc))
            if cause == CAUSE_TURN_TIMEOUT and isinstance(exc, codex_app_server.AppServerUnreachable):
                cause = CAUSE_HANDSHAKE_TIMEOUT
            _log("leg-failed", msg, f"codex: {exc}", cause)

    thread = threading.Thread(target=run, daemon=True, name=f"bridge-codex-{msg.id[:8]}")
    thread.start()
    return Delivery("handed-off", None, msg, thread)


def _deliver_agy(binding: dict, msg: BridgeMessage) -> Delivery:
    argv = AgyAdapter().resume_argv(binding["id"], _body(msg))
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        return Delivery("unroutable", f"cannot spawn agy: {exc}", msg)

    def watch() -> None:
        cause = None
        try:
            for line in proc.stdout:
                cause = cause or (classify_exit(line) if line.lstrip().startswith("{") else None)
            proc.wait()
        except Exception as exc:  # noqa: BLE001 - a failed read must not kill the watcher silently
            _log("leg-failed", msg, f"agy: {exc}")
            return
        if cause:
            _log("leg-failed", msg, f"agy: {cause}", cause)

    thread = threading.Thread(target=watch, daemon=True, name=f"bridge-agy-{msg.id[:8]}")
    thread.start()
    return Delivery("handed-off", None, msg, thread)


_LEGS = {KIND_CLAUDE: _deliver_claude, KIND_CODEX: _deliver_codex, KIND_AGY: _deliver_agy}


def route(msg: BridgeMessage) -> Delivery:
    """Resolve msg.to_identity now, apply routing control, label, and dispatch to the target's leg.

    Refusals and unroutable outcomes are logged, never raised.
    """
    why = refusal(msg)
    if why:
        _log("refused", msg, why)
        return Delivery("refused", why, msg)
    binding = roster.resolve(msg.to_identity)
    if binding is None or binding.get("kind") not in _LEGS:
        _log("unroutable", msg, "no binding")
        return Delivery("unroutable", "no binding", msg)
    labelled = replace(msg, label=label_line(msg.from_identity, msg.from_kind))
    result = _LEGS[binding["kind"]](binding, labelled)
    if result.status == "unroutable":
        _log("unroutable", labelled, result.reason or "")
    return result


def _claim_inbox() -> list[str]:
    """Take every line in the inbox (rename, read, delete); [] when empty."""
    path = inbox_path()
    claimed = path.with_name(f"{path.name}.claimed.{os.getpid()}.{os.urandom(4).hex()}")
    try:
        os.replace(path, claimed)
    except FileNotFoundError:
        return []
    try:
        return claimed.read_text(encoding="utf-8").splitlines()
    finally:
        claimed.unlink(missing_ok=True)


def drain_once() -> list[Delivery]:
    """Route every queued inbox line; a malformed line is logged and skipped."""
    out: list[Delivery] = []
    for line in _claim_inbox():
        if not line.strip():
            continue
        try:
            msg = BridgeMessage(**json.loads(line))
        except (ValueError, TypeError):
            _append_line(log_path(), json.dumps({"ts": _now(), "event": "malformed", "line": line[:200]}))
            continue
        try:
            out.append(route(msg))
        except Exception as exc:  # noqa: BLE001 - one bad binding must not kill the resident loop or drop the claimed batch
            _log("route-failed", msg, f"{type(exc).__name__}: {exc}")
            out.append(Delivery("unroutable", f"{type(exc).__name__}: {exc}", msg))
    return out


def serve(stop: Optional[threading.Event] = None, poll_s: float = _IDLE_POLL_S, max_cycles: Optional[int] = None) -> None:
    """Resident loop: drain the inbox, idle on a short event wait when empty (no spawns), until `stop` is set.

    `max_cycles` bounds the loop for tests; None runs until stopped.
    """
    stop = stop or threading.Event()
    cycles = 0
    while not stop.is_set():
        if not drain_once():
            stop.wait(poll_s)
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return
