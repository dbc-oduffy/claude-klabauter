"""SSH box transport: detect sessions on a remote box and relay a cross-box send over SSH.

A box record (``box_registry`` row) may carry ``transport: {kind: "ssh", host, invoke}``; the
default kind ``fs`` is the loopback path and is not handled here. ``invoke`` is the far box's
absolute path to its settings-home ``bin/coordinator-invoke`` (a non-interactive ssh session
loads no PATH). Optional row fields: ``remote_shell`` ("posix" default; "cmd" for a Windows far
box, whose OpenSSH runs the command through cmd.exe) and ``ssh`` (pinned path of the local ssh
binary), ``identity_file`` (a pinned key: the call adds ``-i <key> -o IdentitiesOnly=yes`` so
agent keys cannot exhaust the far box's MaxAuthTries) and ``tunnel_host`` (tried once, only after
ssh itself failed with exit 255 on ``host``). Both calls run the far box's own engine op through
``<ssh> -o BatchMode=yes -o ConnectTimeout=<n> <host> <invoke> <op> <json>``: no remote glob, no
local filesystem reach. The ssh child is spawned argv-form (never a shell); the ``runner``
parameter is injectable so tests spawn nothing. A remote round trip is outside the 500ms process
bar; callers run these off the hot path.

Containment (enforced in ``_remote_invoke`` and re-checked in ``_run``, refusal is a reason):
    - the host must be the ssh host (or ``tunnel_host``) of a ``box_registry`` row for the same
      ``box_id``, with the row's own ``invoke``, ``remote_shell``, ``ssh``, ``identity_file`` and
      ``tunnel_host``; an option-like ``identity_file`` or ``tunnel_host`` is refused;
    - the only remote command is that row's registered ``coordinator-invoke`` with an allowlisted op
      (``ALLOWED_OPS`` or a ``policy.remote_safe`` op), quoted for the row's remote shell;
    - argv-form with ``-o BatchMode=yes -o ConnectTimeout=<n>`` (keyed: led by
      ``-i <key> -o IdentitiesOnly=yes``);
    - at most one in-flight call per box and ``RATE_MAX_CALLS`` per ``RATE_WINDOW_S`` per box,
      fleet-wide on this machine: an O_EXCL lock file per box (a dead holder or an expired lock is
      reclaimed) and a shared rate-window file, both under ``box_registry.lock_dir()``; a refused
      ssh still spends its rate token, and a tunnel retry is admitted (and spends) as its own call.

Negative-spec:
    - Never raises: a failure is ``{"ok": False, "state": "unknown", "reason": ...}``; an
      empty session list is never returned for an unreachable box.
    - Every ssh call goes through ``_run`` (the single sanctioned spawn site).
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em import box_registry
from coordinator_core.win_portability import no_console_creationflags

DEFAULT_TIMEOUT_S = 30.0

#: Ops this module itself issues; the fallback's ops are admitted by ``policy.remote_safe``.
ALLOWED_OPS: frozenset = frozenset(
    {
        "session.peer_roster",
        "groupem.box_send",
        "groupem.suite_grant_receive",
        "groupem.box_key_pin",
        "groupem.suite_grant_revoke_receive",
        "groupem.box_key_pin_from_peer",
    }
)
RATE_MAX_CALLS = 12
RATE_WINDOW_S = 60.0
CONNECT_TIMEOUT_S = 10.0
LOCK_GRACE_S = 10.0

Runner = Callable[[list, float], "subprocess.CompletedProcess"]

_clock: Callable[[], float] = time.time
_INVOKE_NAMES = frozenset({"coordinator-invoke", "coordinator-invoke.exe", "coordinator-invoke.cmd"})
_CMD_UNSAFE = re.compile(r'[&|<>^%!"]')
_CMD_JSON_ESCAPES = {c: "\\u%04x" % ord(c) for c in '&|<>^%!()'}


def _lock_path(box_id: str) -> Path:
    return box_registry.lock_dir() / f"{box_id}.lock"


def _rate_path(box_id: str) -> Path:
    return box_registry.lock_dir() / f"{box_id}.rate.json"


def _reset_limits() -> None:
    """Forget lock and rate state for every box (tests)."""
    try:
        names = os.listdir(box_registry.lock_dir())
    except OSError:
        return
    for name in names:
        try:
            os.unlink(box_registry.lock_dir() / name)
        except OSError:
            pass


def _own_start_epoch() -> int:
    try:
        import psutil

        return int(psutil.Process().create_time())
    except Exception:
        return 0


def _holder_stale(holder: dict) -> bool:
    """True when the lock's holder is provably dead or its hold has expired; any doubt is alive."""
    try:
        if float(holder["expires"]) < _clock():
            return True
        epoch = int(holder["start_epoch"])
        if not epoch:
            return False
        from coordinator_core.session import core

        return not core.stable_pid_alive(holder["pid"], "", str(epoch))
    except Exception:
        return False


def _read_holder(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _unreadable_lock_aged(path: Path) -> bool:
    """A lock file with no parseable holder (writer died between create and write) is reclaimable once older than any live hold."""
    try:
        return _clock() - path.stat().st_mtime > DEFAULT_TIMEOUT_S + LOCK_GRACE_S
    except OSError:
        return False


def _reclaim(path: Path, observed: Optional[dict]) -> bool:
    """Remove the stale lock ``observed`` by renaming it aside; a lock that changed hands meanwhile is put back and refused."""
    current = _read_holder(path)
    if (current or {}).get("nonce") != (observed or {}).get("nonce"):
        return False
    aside = path.with_name(f"{path.name}.reclaim.{uuid.uuid4().hex}")
    try:
        os.rename(path, aside)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    taken = _read_holder(aside)
    same = (taken is None and observed is None) or (
        taken is not None and observed is not None and taken.get("nonce") == observed.get("nonce")
    )
    if not same:
        # Residual race: if a new holder created the lock after our rename, the link fails and the
        # displaced holder loses its lock; the refusal still returns False (over-admits at most once).
        try:
            os.link(aside, path)
        except OSError:
            pass
    try:
        os.unlink(aside)
    except OSError:
        pass
    return same


def _acquire(box_id: str, hold_s: float) -> "tuple[Optional[str], Optional[str]]":
    """Take the box's cross-process lock: ``(nonce, None)`` or ``(None, refusal reason)``."""
    path = _lock_path(box_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None, "box_lock_unavailable"
    nonce = uuid.uuid4().hex
    record = json.dumps(
        {
            "pid": os.getpid(),
            "start_epoch": _own_start_epoch(),
            "nonce": nonce,
            "expires": _clock() + hold_s + LOCK_GRACE_S,
        }
    )
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except (FileExistsError, PermissionError):
            holder = _read_holder(path)
            if holder is None:
                if not _unreadable_lock_aged(path):
                    return None, "box_busy"
            elif not _holder_stale(holder):
                return None, "box_busy"
            if not _reclaim(path, holder):
                return None, "box_busy"
            continue
        except OSError:
            return None, "box_lock_unavailable"
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(record)
        return nonce, None
    return None, "box_busy"


def _unlock(box_id: str, nonce: str) -> None:
    path = _lock_path(box_id)
    holder = _read_holder(path)
    if holder is not None and holder.get("nonce") == nonce:
        try:
            os.unlink(path)
        except OSError:
            pass


def _spend_rate_token(box_id: str) -> Optional[str]:
    """Record one call in the shared window (caller holds the box lock); a refusal reason or None."""
    now = _clock()
    path = _rate_path(box_id)
    try:
        stamps = json.loads(path.read_text(encoding="utf-8"))
        window = [t for t in stamps if isinstance(t, (int, float)) and t <= now and now - t < RATE_WINDOW_S]
    except FileNotFoundError:
        window = []
    except (OSError, ValueError, TypeError):
        window = [now] * RATE_MAX_CALLS
        try:
            atomic_write_bytes(path, json.dumps(window).encode("utf-8"))
        except OSError:
            pass
    if len(window) >= RATE_MAX_CALLS:
        return "rate_limited"
    window.append(now)
    try:
        atomic_write_bytes(path, json.dumps(window).encode("utf-8"))
    except OSError:
        return "box_lock_unavailable"
    return None


def _admit(box_id: str, hold_s: float) -> "tuple[Optional[str], Optional[str]]":
    """Reserve the box's lock and one rate token: ``(nonce, None)`` or ``(None, refusal reason)``."""
    nonce, refused = _acquire(box_id, hold_s)
    if refused:
        return None, refused
    refused = _spend_rate_token(box_id)
    if refused:
        _unlock(box_id, nonce)
        return None, refused
    return nonce, None


def _registered(box: dict, host: str, invoke: str) -> Optional[str]:
    """None when ``box`` matches a ``box_registry`` row by id, host and invoke; else a refusal reason."""
    from coordinator_core.group_em import box_registry

    box_id = box.get("box_id")
    if not isinstance(box_id, str) or not box_id:
        return "box_not_registered"
    for row in box_registry.read_boxes():
        if row.get("box_id") != box_id:
            continue
        reg = row.get("transport")
        if (
            isinstance(reg, dict)
            and reg.get("kind") == "ssh"
            and reg.get("host") == host
            and reg.get("invoke") == invoke
            and reg.get("remote_shell", "posix") == (box.get("transport") or {}).get("remote_shell", "posix")
            and reg.get("ssh") == (box.get("transport") or {}).get("ssh")
            and reg.get("identity_file") == (box.get("transport") or {}).get("identity_file")
            and reg.get("tunnel_host") == (box.get("transport") or {}).get("tunnel_host")
        ):
            return None
        return "box_not_registered"
    return "box_not_registered"


def _op_allowed(op: str) -> bool:
    if op in ALLOWED_OPS:
        return True
    from coordinator_core.remote_engine import policy

    return policy.remote_safe(op)


def _shape_ok(argv: list) -> bool:
    """``<ssh> [-i <key> -o IdentitiesOnly=yes] -o BatchMode=yes -o ConnectTimeout=<n> <host> <remote>``."""
    if not (len(argv) in (7, 11) and isinstance(argv[0], str)):
        return False
    rest = argv[1:]
    if len(argv) == 11:
        if not (rest[0] == "-i" and not str(rest[1]).startswith("-") and rest[2:4] == ["-o", "IdentitiesOnly=yes"]):
            return False
        rest = rest[4:]
    return (
        re.split(r"[\\/]", argv[0])[-1] in box_registry.SSH_BINARY_NAMES
        and rest[0:2] == ["-o", "BatchMode=yes"]
        and rest[2] == "-o"
        and re.fullmatch(r"ConnectTimeout=\d{1,3}", str(rest[3])) is not None
        and not str(rest[4]).startswith("-")
    )


def _safe_arg(value: object) -> Optional[str]:
    if not isinstance(value, str) or not value or value.startswith("-"):
        return None
    return value


def _remote_command(shell: str, invoke: str, op: str, params: dict) -> Optional[str]:
    """The single remote command string quoted for ``shell``; None when ``invoke`` cannot be quoted safely."""
    text = json.dumps(params)
    if shell != "cmd":
        return shlex.quote(invoke) + " " + shlex.quote(op) + " " + shlex.quote(text)
    if _CMD_UNSAFE.search(invoke) or _CMD_UNSAFE.search(op):
        return None
    for ch, esc in _CMD_JSON_ESCAPES.items():
        text = text.replace(ch, esc)
    return '"' + subprocess.list2cmdline([invoke, op, text]) + '"'


def _run(argv: list, timeout: float) -> "subprocess.CompletedProcess":
    if not _shape_ok(argv):
        raise ValueError("ssh argv outside the contained shape")
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
    return _safe_arg(t.get("host"))


def _remote_invoke(
    box: dict, op: str, params: dict, runner: Optional[Runner], timeout: float
) -> dict:
    host = ssh_host(box)
    if host is None:
        return _unknown("not_ssh_transport")
    invoke = box["transport"].get("invoke")
    if not isinstance(invoke, str) or not invoke:
        return _unknown("no_invoke_path")
    transport = box["transport"]
    shell = transport.get("remote_shell", "posix")
    if box_registry.transport_problem(transport):
        return _unknown("transport_row_invalid")
    ssh_bin = transport.get("ssh") or "ssh"
    if runner is None and shutil.which(ssh_bin) is None:
        return _unknown("ssh_absent")
    if re.split(r"[\\/]", invoke)[-1] not in _INVOKE_NAMES:
        return _unknown("invoke_not_coordinator_invoke")
    if not _op_allowed(op):
        return _unknown("op_not_allowed")
    not_registered = _registered(box, host, invoke)
    if not_registered:
        return _unknown(not_registered)
    remote = _remote_command(shell, invoke, op, params)
    if remote is None:
        return _unknown("invoke_unquotable_for_shell")
    identity = transport.get("identity_file")
    tunnel = transport.get("tunnel_host")
    if (identity is not None and _safe_arg(identity) is None) or (
        tunnel is not None and _safe_arg(tunnel) is None
    ):
        return _unknown("transport_row_invalid")
    connect_s = max(1, math.ceil(min(timeout, CONNECT_TIMEOUT_S)))
    head = [ssh_bin] + (["-i", identity, "-o", "IdentitiesOnly=yes"] if identity else [])
    head += ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_s}"]
    hosts = [host] + ([tunnel] if tunnel and tunnel != host else [])
    for attempt, target in enumerate(hosts):
        res = _invoke_once(box["box_id"], head + [target, remote], runner, timeout)
        # LAN first; the tunnel is tried once, only after ssh itself failed (exit 255) on host.
        if res.get("reason") != "ssh_exit_255" or attempt == len(hosts) - 1:
            return res
    return res


def _invoke_once(box_id: str, argv: list, runner: Optional[Runner], timeout: float) -> dict:
    """One admitted ssh call (box lock plus one rate token) and its parsed reply."""
    nonce, refused = _admit(box_id, timeout)
    if refused:
        return _unknown(refused)
    try:
        proc = (runner or _run)(argv, timeout)
    except subprocess.TimeoutExpired:
        return _unknown("ssh_timeout")
    except OSError as exc:
        return _unknown(f"ssh_spawn_failed:{type(exc).__name__}:{exc}")
    except ValueError:
        return _unknown("ssh_argv_refused")
    finally:
        _unlock(box_id, nonce)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-300:]
        return {**_unknown(f"ssh_exit_{proc.returncode}"), "stderr": tail}
    try:
        data: Any = json.loads(proc.stdout or "")
    except ValueError:
        return _unknown("bad_reply")
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        data = data["result"]
    elif isinstance(data, dict) and "error" in data and "jsonrpc" in data:
        return _unknown("remote_error")
    if not isinstance(data, dict):
        return _unknown("bad_reply")
    return {"ok": True, "reply": data}


def _unknown(reason: str) -> dict:
    """The never-raises failure reply."""
    return {"ok": False, "state": "unknown", "reason": reason}


def invoke_remote(
    box: dict,
    op: str,
    params: dict,
    runner: Optional[Runner] = None,
    *,
    timeout: float,
) -> dict:
    """Run ``op`` on the far box's engine through ``_remote_invoke`` under the caller's ``timeout``.

    ``timeout`` has no default: the caller sets the budget (``DEFAULT_TIMEOUT_S`` is too long for a
    fallback). Never raises; returns ``{"ok": True, "reply": {...}}`` or ``{"ok": False, "state":
    "unknown", "reason": ...}``. The caller decides whether ``op`` is safe to run remotely.
    """
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not timeout > 0
        or not isinstance(op, str)
        or not op
        or not isinstance(params, dict)
        or not isinstance(box, dict)
    ):
        return _unknown("bad_invoke_args")
    try:
        return _remote_invoke(box, op, params, runner, float(timeout))
    except Exception as exc:  # noqa: BLE001 - contract: never raises
        return _unknown(f"invoke_failed:{type(exc).__name__}:{exc}")


def _relay_grant_command(envelope: dict) -> Optional[str]:
    """The relay's ``grant <grantee> <note> <ttl> <repo>`` command restating ``envelope``; None when it cannot.

    The far relay refuses any argv field that disagrees with the signed envelope, so every field is
    derived from the envelope, never supplied beside it.
    """
    from coordinator_core.group_em import suite_grant

    try:
        sid, box_id = envelope["grantee_session_id"], envelope["grantee_box_id"]
        issued = suite_grant._parse_iso(envelope["issued_at"])
        expires = suite_grant._parse_iso(envelope["expires_at"])
        note = str(envelope["note"]).strip()
        scope = envelope.get("scope_root")
    except (KeyError, TypeError):
        return None
    if issued is None or expires is None or not note or not isinstance(sid, str) or not isinstance(box_id, str):
        return None
    ttl, rem = divmod(round(expires - issued), 60)
    if rem or ttl <= 0 or (scope is not None and not (isinstance(scope, str) and scope)):
        return None
    return shlex.join(["grant", f"{box_id}:{sid}", note, str(ttl), scope or "-"])


def _run_relay(argv: list, timeout: float, stdin_text: str = "") -> "subprocess.CompletedProcess":
    """The single spawn site for the relay-key ssh: argv-form, the envelope line on stdin."""
    if not (
        len(argv) == 11
        and re.split(r"[\\/]", str(argv[0]))[-1] in box_registry.SSH_BINARY_NAMES
        and argv[1] == "-i"
        and not str(argv[2]).startswith("-")
        and argv[3:7] == ["-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"]
        and argv[7] == "-o"
        and re.fullmatch(r"ConnectTimeout=\d{1,3}", str(argv[8])) is not None
        and not str(argv[9]).startswith("-")
    ):
        raise ValueError("relay ssh argv outside the contained shape")
    return subprocess.run(
        argv,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        **no_console_creationflags(),
    )


def _relay_registered(box: dict, host: str, key: str) -> bool:
    for row in box_registry.read_boxes():
        if row.get("box_id") != box.get("box_id"):
            continue
        reg = row.get("transport")
        return (
            isinstance(reg, dict)
            and reg.get("kind") == "ssh"
            and reg.get("host") == host
            and reg.get("relay_key") == key
            and reg.get("ssh") == box["transport"].get("ssh")
        )
    return False


def _relay_revoke_command(envelope: dict) -> Optional[str]:
    """The relay's ``revoke <box>:<sid>`` command naming the grantee of the signed revoke ``envelope``."""
    try:
        sid, box_id = envelope["grantee_session_id"], envelope["grantee_box_id"]
    except (KeyError, TypeError):
        return None
    if not isinstance(sid, str) or not isinstance(box_id, str) or not sid or not box_id:
        return None
    return shlex.join(["revoke", f"{box_id}:{sid}"])


def _relay_keypin_command(payload: dict) -> Optional[str]:
    """The relay's ``keypin <box_id>`` command for ``payload`` (``{box_id, key_b64}``)."""
    try:
        box_id, key = payload["box_id"], payload["key_b64"]
    except (KeyError, TypeError):
        return None
    if not isinstance(box_id, str) or not box_id or box_id.startswith("-") or not isinstance(key, str) or not key:
        return None
    return shlex.join(["keypin", box_id])


def _deliver_grant_relay(box: dict, envelope: dict, runner: Optional[Callable], timeout: float) -> dict:
    """Hand ``envelope`` to the far box's ``unimatrix-relay`` ``grant`` mode over the box row's ``relay_key``."""
    command = _relay_grant_command(envelope)
    if command is None:
        return _unknown("grant_args_unrestatable")
    return _relay_deliver(box, command, json.dumps(envelope, separators=(",", ":")) + "\n", runner, timeout)


def _relay_deliver(
    box: dict, command: str, payload_line: str, runner: Optional[Callable], timeout: float
) -> dict:
    """Run one relay-mode ``command`` on the far box over the box row's ``relay_key``, ``payload_line`` on stdin.

    The far relay prints the arrival check's JSON reply and exits 0 only when accepted, 1 when it
    refused; any other exit or a non-JSON reply is an unknown, never an acceptance.
    """
    host = ssh_host(box)
    key = box["transport"].get("relay_key")
    if host is None or not isinstance(key, str) or not key or key.startswith("-"):
        return _unknown("relay_key_invalid")
    if box_registry.transport_problem(box["transport"]):
        return _unknown("transport_row_invalid")
    ssh_bin = box["transport"].get("ssh") or "ssh"
    if runner is None and shutil.which(ssh_bin) is None:
        return _unknown("ssh_absent")
    if not _relay_registered(box, host, key):
        return _unknown("box_not_registered")
    connect_s = max(1, math.ceil(min(timeout, CONNECT_TIMEOUT_S)))
    argv = [ssh_bin, "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={connect_s}", host, command]
    box_id = box["box_id"]
    nonce, refused = _admit(box_id, timeout)
    if refused:
        return _unknown(refused)
    try:
        proc = (runner or _run_relay)(argv, timeout, payload_line)
    except subprocess.TimeoutExpired:
        return _unknown("ssh_timeout")
    except OSError as exc:
        return _unknown(f"ssh_spawn_failed:{type(exc).__name__}:{exc}")
    except ValueError:
        return _unknown("ssh_argv_refused")
    finally:
        _unlock(box_id, nonce)
    try:
        reply: Any = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        reply = None
    if proc.returncode not in (0, 1) or not isinstance(reply, dict):
        tail = (proc.stderr or "").strip()[-300:]
        return {**_unknown(f"ssh_exit_{proc.returncode}" if proc.returncode not in (0, 1) else "bad_reply"), "stderr": tail}
    if proc.returncode == 0 and reply.get("ok") is True:
        return {"ok": True, "reply": reply}
    return {"ok": False, "reason": str(reply.get("reason", "far_refused"))}


def deliver_grant(
    box: dict,
    envelope: dict,
    runner: Optional[Runner] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Hand a signed grant envelope to the far box.

    A box row whose ssh transport carries ``relay_key`` (the unimatrix key) is reached through the
    far ``unimatrix-relay`` ``grant`` mode, with ``runner(argv, timeout, stdin_text)`` injectable;
    any other ssh box goes through its ``groupem.suite_grant_receive`` op. Returns
    ``{"ok": True, "reply": {...}}`` only when the far box replies ``ok: true``; a far refusal is
    ``{"ok": False, "reason": <far reason>}``, an unreachable box the usual unknown reply.
    """
    transport = box.get("transport") if isinstance(box, dict) else None
    if isinstance(transport, dict) and transport.get("relay_key") is not None:
        return _deliver_grant_relay(box, envelope, runner, timeout)
    res = _remote_invoke(box, "groupem.suite_grant_receive", {"envelope": envelope}, runner, timeout)
    if not res["ok"]:
        return res
    reply = res["reply"]
    if reply.get("ok") is True:
        return res
    return {"ok": False, "reason": str(reply.get("reason", "far_refused"))}


def _deliver_via(
    box: dict,
    relay_command: Optional[str],
    payload: dict,
    op: str,
    params: dict,
    runner: Optional[Runner],
    timeout: float,
) -> dict:
    """One far call: the relay mode when the row carries ``relay_key``, else the allowlisted op ``op``."""
    transport = box.get("transport") if isinstance(box, dict) else None
    if isinstance(transport, dict) and transport.get("relay_key") is not None:
        if relay_command is None:
            return _unknown("relay_args_unrestatable")
        return _relay_deliver(box, relay_command, json.dumps(payload, separators=(",", ":")) + "\n", runner, timeout)
    res = _remote_invoke(box, op, params, runner, timeout)
    if not res["ok"]:
        return res
    reply = res["reply"]
    if reply.get("ok") is True:
        return res
    return {"ok": False, "reason": str(reply.get("reason", "far_refused"))}


def deliver_revoke(
    box: dict,
    envelope: dict,
    runner: Optional[Runner] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Hand a signed revoke envelope to the far box (relay ``revoke`` mode, else its
    ``groupem.suite_grant_revoke_receive`` op); same reply shape as ``deliver_grant``."""
    return _deliver_via(
        box, _relay_revoke_command(envelope), envelope,
        "groupem.suite_grant_revoke_receive", {"envelope": envelope}, runner, timeout,
    )


def deliver_key_pin(
    box: dict,
    payload: dict,
    runner: Optional[Runner] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Ask the far box to pin ``payload`` (``{box_id, key_b64}``: this box's own key) through its relay
    ``keypin`` mode, else its ``groupem.box_key_pin_from_peer`` op; same reply shape as ``deliver_grant``.

    The op fallback sends no ``peer``, so the far op refuses ``no_peer``: only the relay mode (whose
    forced command supplies ``--peer``) can pin."""
    return _deliver_via(
        box, _relay_keypin_command(payload), payload,
        "groupem.box_key_pin_from_peer", dict(payload) if isinstance(payload, dict) else {}, runner, timeout,
    )


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
    from_session: Optional[str] = None,
    from_box: Optional[str] = None,
    home: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict:
    """Relay one line to ``to_session`` on the far box through its ``groupem.box_send`` op.

    One ssh call. The far op is sent ``local: true`` so it delivers into its own inbox; the near
    box's name for it is never looked up in the far registry. With ``from_session`` and
    ``from_box`` the params also carry them plus ``sig``, this box's ``box_key`` signature over
    ``{from_box, from_session, to_session, text, ts}``; the far op stamps the sender only when
    ``sig`` verifies under the key it pinned for ``from_box`` and ``ts`` is within its freshness window.
    Returns the far op's reply (``{sent, ...}``) as ``{"ok": True, "reply": ...}``,
    or ``{"ok": False, "reason"}`` for a transport failure.
    """
    params: dict = {"local": True, "to_session": to_session, "text": text}
    if from_session and from_box:
        from coordinator_core.group_em import box_key

        claim = {
            "from_box": from_box,
            "from_session": from_session,
            "to_session": to_session,
            "text": text,
            "ts": int(time.time() if now is None else now),
        }
        params["ts"] = claim["ts"]
        params["from_box"] = from_box
        params["from_session"] = from_session
        params["sig"] = box_key.sign(claim, home)["sig"]
    return _remote_invoke(box, "groupem.box_send", params, runner, timeout)
