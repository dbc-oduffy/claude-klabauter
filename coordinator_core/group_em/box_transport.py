"""SSH box transport: detect sessions on a remote box and relay a cross-box send over SSH.

A box record (``box_registry`` row) may carry ``transport: {kind: "ssh", host, invoke}``; the
default kind ``fs`` is the loopback path and is not handled here. ``invoke`` is the far box's
absolute path to its settings-home ``bin/coordinator-invoke`` (a non-interactive ssh session
loads no PATH). Both calls run the far box's own engine op through
``ssh -o BatchMode=yes <host> <invoke> <op> <json>``: no remote glob, no local filesystem
reach. The ssh child is spawned argv-form (never a shell); the ``runner`` parameter is
injectable so tests spawn nothing. A remote round trip is outside the 500ms process bar;
callers run these off the hot path.

Negative-spec:
    - Never raises: a failure is ``{"ok": False, "state": "unknown", "reason": ...}``; an
      empty session list is never returned for an unreachable box.
    - Every ssh call goes through ``_run`` (the single sanctioned spawn site).
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from typing import Any, Callable, Optional

from coordinator_core.win_portability import no_console_creationflags

DEFAULT_TIMEOUT_S = 30.0

Runner = Callable[[list, float], "subprocess.CompletedProcess"]


def _run(argv: list, timeout: float) -> "subprocess.CompletedProcess":
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )


def ssh_host(box: dict) -> Optional[str]:
    """The ssh host of ``box`` when its transport is ``{kind: ssh, host, ...}``; else None."""
    t = box.get("transport") if isinstance(box, dict) else None
    if not isinstance(t, dict) or t.get("kind") != "ssh":
        return None
    host = t.get("host")
    if not isinstance(host, str) or not host or host.startswith("-"):
        return None
    return host


def _remote_invoke(
    box: dict, op: str, params: dict, runner: Optional[Runner], timeout: float
) -> dict:
    host = ssh_host(box)
    if host is None:
        return _unknown("not_ssh_transport")
    invoke = box["transport"].get("invoke")
    if not isinstance(invoke, str) or not invoke:
        return _unknown("no_invoke_path")
    if runner is None and shutil.which("ssh") is None:
        return _unknown("ssh_absent")
    remote = shlex.quote(invoke) + " " + shlex.quote(op) + " " + shlex.quote(json.dumps(params))
    argv = ["ssh", "-o", "BatchMode=yes", host, remote]
    try:
        proc = (runner or _run)(argv, timeout)
    except subprocess.TimeoutExpired:
        return _unknown("ssh_timeout")
    except OSError as exc:
        return _unknown(f"ssh_spawn_failed:{type(exc).__name__}:{exc}")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-300:]
        return {**_unknown(f"ssh_exit_{proc.returncode}"), "stderr": tail}
    try:
        data: Any = json.loads(proc.stdout or "")
    except ValueError:
        return _unknown("bad_reply")
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        data = data["result"]
    if not isinstance(data, dict):
        return _unknown("bad_reply")
    return {"ok": True, "reply": data}


def _unknown(reason: str) -> dict:
    return {"ok": False, "state": "unknown", "reason": reason}


def list_remote_sessions(
    box: dict,
    run: Optional[str] = None,
    runner: Optional[Runner] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Sessions on the far box via its ``session.peer_roster`` op, each row tagged ``box_id``.

    ``box`` is a registry record carrying ``box_id`` and an ssh ``transport``. ``run`` keeps only
    rows whose ``name`` or ``ref`` contains it. Returns ``{"ok": True, "box_id", "sessions": [...]}``
    or ``{"ok": False, "reason"}``.
    """
    res = _remote_invoke(box, "session.peer_roster", {}, runner, timeout)
    if not res["ok"]:
        return res
    rows = res["reply"].get("rows")
    if not isinstance(rows, list):
        return _unknown("bad_reply")
    box_id = box.get("box_id")
    sessions = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if run is not None and run not in str(row.get("name", "")) + str(row.get("ref", "")):
            continue
        sessions.append({**row, "box_id": box_id})
    return {"ok": True, "box_id": box_id, "sessions": sessions}


def send(
    box: dict,
    to_session: str,
    text: str,
    runner: Optional[Runner] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Relay one line to ``to_session`` on the far box through its ``groupem.box_send`` op.

    One ssh call. The far op is sent ``local: true`` so it delivers into its own inbox; the near
    box's name for it is never looked up in the far registry.
    Returns the far op's reply (``{sent, ...}``) as ``{"ok": True, "reply": ...}``,
    or ``{"ok": False, "reason"}`` for a transport failure.
    """
    params: dict = {"local": True, "to_session": to_session, "text": text}
    return _remote_invoke(box, "groupem.box_send", params, runner, timeout)
