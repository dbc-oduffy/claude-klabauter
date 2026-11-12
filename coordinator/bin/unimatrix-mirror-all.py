#!/usr/bin/env python3
"""Keep every live local session mirrored on the far box: one relay `inbox` client per
session, started when the session appears and tree-killed when it exits.

Usage:
  unimatrix-mirror-all.py <log> [<skip-name> ...] --remote <lan-ssh-alias> [--tunnel-remote <ssh-alias>] [--key <path>]

--remote is tried first; --tunnel-remote (or $UNIMATRIX_TUNNEL_REMOTE) is used only while
--remote does not answer.

Skipped names are mirrored by a client started elsewhere. Our own mirror entries
(those carrying macSocketPath) are never exported back.
"""
import argparse
import importlib.util
import json
import os
import re
import signal
import socket
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
POLL_SECONDS = 30


def _shared():
    spec = importlib.util.spec_from_file_location("unimatrix_roster", HERE / "unimatrix-roster.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ROSTER = None


def _roster():
    """The roster module, loaded on first use: a bin entrypoint stays inert at import."""
    global _ROSTER
    if _ROSTER is None:
        _ROSTER = _shared()
    return _ROSTER


def __getattr__(name):
    if name == "R":
        return _roster()
    raise AttributeError(name)


def parse_args(argv):
    p = argparse.ArgumentParser(prog="unimatrix-mirror-all.py")
    p.add_argument("log")
    p.add_argument("skip", nargs="*", help="session names mirrored by a client started elsewhere")
    p.add_argument("--remote", required=True, help="ssh alias of the far box on the LAN, tried first")
    p.add_argument("--tunnel-remote", default=None, help=f"ssh alias over the tunnel, used only when --remote does not answer (default: ${_roster().TUNNEL_ENV})")
    p.add_argument("--key", default=None)
    a = p.parse_args(argv)
    a.key = a.key or _roster().default_key()
    a.tunnel_remote = a.tunnel_remote or _roster().default_tunnel()
    return a


def live_sessions(skip):
    sess = _roster().sessions_dir()
    out = {}
    for f in os.listdir(sess):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(sess, f), encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        name = d.get("name")
        if d.get(_roster().FAR_SOCKET_FIELD) or not name or name in skip or not NAME_RE.fullmatch(str(name)):
            continue
        if not isinstance(d.get("pid"), int) or not d.get("messagingSocketPath"):
            continue
        if _roster().pid_alive(d["pid"]):
            out[name] = d
    return out


def inbox_cmd(d, name, log_dir, remote, key, tunnel_remote=None):
    cmd = [sys.executable, str(HERE / "unimatrix-inbox.py"), str(d["pid"]), name,
           os.path.join(log_dir, f"unimatrix-inbox-{name}.log"), d["messagingSocketPath"],
           "--remote", remote, "--key", key]
    return cmd + ["--tunnel-remote", tunnel_remote] if tunnel_remote else cmd


UNSUPPORTED_HOST_RC = 3  # the relay's inbox exit code for a Windows far box
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 600

_now = time.monotonic
ABSENT_POLLS_BEFORE_UNMIRROR = 2  # a router or engine respawn re-registers a session within seconds
# Per supervisor life: `unsupported` stops all dialing; `fails`, `retry_at` and `absent` (consecutive
# polls a session was missing) are per session name.
# `up_since` is the monotonic time a client was spawned (or first seen alive).
STABLE_SECONDS = 300  # a client must stay up this long before its death count resets
# `rc` is each name's last client exit code; `state_sig` is what the mirror-state file last recorded.
_STATE = {"unsupported": False, "fails": {}, "retry_at": {}, "absent": {}, "up_since": {},
          "rc": {}, "state_sig": None}


def backoff_seconds(fails):
    """Wait before respawning a client that has died `fails` times in a row without staying up."""
    return min(BACKOFF_BASE_SECONDS * 2 ** fails, BACKOFF_CAP_SECONDS)


FAR_OWNER_REFUSALS = ("belongs to a live local session", "already mirrored by live relay")


HOST_REFUSAL = "hosted on macOS/Linux boxes only"  # the relay's inbox refusal text; the rc it arrives under varies


def _client_log_tail(log_dir, name):
    try:
        with open(os.path.join(log_dir, f"unimatrix-inbox-{name}.log"), "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 4096))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def refused_by_far_owner(log_dir, name):
    """True when the client's last attempt was refused because the far box already holds `name`.

    The far box's pull supervisor owns such a name: one owner per name, so this push side parks it
    instead of redialing every poll and flipping the owner."""
    tail = _client_log_tail(log_dir, name)
    return any(m in tail for m in FAR_OWNER_REFUSALS)


def refused_by_far_host(log_dir, name):
    """True when the client's last attempt was told the far box hosts no mirrors, whatever the exit code."""
    return HOST_REFUSAL in _client_log_tail(log_dir, name)


def sync(clients, live, cfg, say):
    """One reconcile pass: `clients` is name -> (session pid, Popen)."""
    log_dir = os.path.dirname(os.path.abspath(cfg.log))
    fails, retry_at, absent = _STATE["fails"], _STATE["retry_at"], _STATE["absent"]
    up_since = _STATE["up_since"]
    for name in live:
        absent.pop(name, None)
    for name, d in live.items():
        if _STATE["unsupported"]:
            break
        c = clients.get(name)
        if c and c[0] == d["pid"]:
            rc = c[1].poll()
            if rc is None:
                if _now() - up_since.setdefault(name, _now()) >= STABLE_SECONDS:
                    fails.pop(name, None)  # up for STABLE_SECONDS: the next death starts the backoff over
                continue
            if rc == UNSUPPORTED_HOST_RC or refused_by_far_host(log_dir, name):
                _STATE["unsupported"] = True
                _STATE["rc"][name] = rc
                say(f"far box does not host mirror inboxes (rc={rc}); mirroring to it is off, state recorded")
                break
            _STATE["rc"][name] = rc
            if name not in retry_at:
                if refused_by_far_owner(log_dir, name):
                    wait = BACKOFF_CAP_SECONDS
                    say(f"client {c[1].pid} for {name} refused: the far box's own mirror holds the name; retry in {wait}s")
                else:
                    wait = backoff_seconds(fails.get(name, 0))
                    fails[name] = fails.get(name, 0) + 1
                    say(f"client {c[1].pid} for {name} exited rc={rc}; retry in {wait}s")
                retry_at[name] = _now() + wait
            if _now() < retry_at[name]:
                continue
        if c:
            _roster().tree_kill(c[1].pid)
        retry_at.pop(name, None)
        if not (c and c[0] == d["pid"]):
            fails.pop(name, None)
        p = _roster().spawn(inbox_cmd(d, name, log_dir, cfg.remote, cfg.key, cfg.tunnel_remote))
        clients[name] = (d["pid"], p)
        up_since[name] = _now()
        say(f"mirror {name} pid {d['pid']} -> client {p.pid}")
    for name in [n for n in clients if n not in live]:
        missed = absent[name] = absent.get(name, 0) + 1
        if missed < ABSENT_POLLS_BEFORE_UNMIRROR and clients[name][1].poll() is None:
            say(f"session {name} absent ({missed}/{ABSENT_POLLS_BEFORE_UNMIRROR}); client kept")
            continue
        absent.pop(name, None)
        up_since.pop(name, None)
        # The client's ssh child holds the far inbox open; killing python alone orphans it.
        _roster().tree_kill(clients.pop(name)[1].pid)
        say(f"unmirror {name}")
    for name in [n for n in absent if n not in clients]:
        del absent[name]
    write_mirror_state(log_dir, clients, live)
    publish_stale(clients, live, cfg, say)


STALE_RENEW_SECONDS = 600
DEAD_MARK_SECONDS = 2 * BACKOFF_CAP_SECONDS
_PUBLISHED = {}  # name -> (state, until epoch) last sent to the far box


def _box_mux():
    path = HERE / "lib" / "unimatrix_box_mux.py"
    mod = sys.modules.get("unimatrix_box_mux")
    if mod is None:
        spec = importlib.util.spec_from_file_location("unimatrix_box_mux", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["unimatrix_box_mux"] = mod
        spec.loader.exec_module(mod)
    return mod


def _send_stale_mark(cfg, command):
    remote = _roster().pick_remote(cfg.remote, getattr(cfg, "tunnel_remote", None))
    return _box_mux().for_remote(remote, key=cfg.key, extra=["-o", "ConnectTimeout=10"]).run(command, b"", timeout=15)


def publish_stale(clients, live, cfg, say, send=None):
    """Tell the far box about each dead or backed-off mirror so a sender there lists it as stale.

    The mirror-state file describes this box; the far box's senders cannot read it. One relay call
    per name when its state changes or its mark is within STALE_RENEW_SECONDS of expiring, run off
    the poll loop. A refused host is not published: the far box hosts no mirrors to mark."""
    if not getattr(cfg, "remote", None):
        return
    now = time.time()
    due = []
    for name, v in mirror_states(clients, live).items():
        if v["state"] not in ("backoff", "dead"):
            _PUBLISHED.pop(name, None)
            continue
        until = max(v["retry_at_epoch"] if v["state"] == "backoff" else int(now) + DEAD_MARK_SECONDS,
                    int(now) + 60)
        sent = _PUBLISHED.get(name)
        keep = STALE_RENEW_SECONDS if v["state"] == "dead" else 0
        if sent and sent[0] == v["state"] and sent[1] - now > keep:
            continue
        _PUBLISHED[name] = (v["state"], until)
        due.append(f"mirror-stale {name} {v['state']} {until} "
                   f"{re.sub(r'[^a-z0-9-]', '-', socket.gethostname().lower()) or 'unknown'}")
    if due:
        (send or _start_publish)(cfg, due, say)


def _start_publish(cfg, commands, say):
    def run():
        for command in commands:
            try:
                r = _send_stale_mark(cfg, command)
                if r.returncode != 0:
                    say(f"far box refused stale mark ({command.split()[1]}): rc={r.returncode}")
            except Exception as e:
                say(f"far box stale mark failed ({command.split()[1]}): {type(e).__name__}")
    threading.Thread(target=run, daemon=True).start()


MIRROR_STATE_FILE = "unimatrix-mirror-state.json"


def mirror_states(clients, live):
    """name -> {state, rc, retry_at_epoch} for every live session and every client still held.

    State is host-refused (far box cannot host mirrors, never retried), up (client running),
    backoff (client dead, a respawn is scheduled) or dead (client dead, nothing scheduled)."""
    wall_offset = time.time() - _now()
    out = {}
    for name in set(live) | set(clients):
        c = clients.get(name)
        rc = _STATE["rc"].get(name)
        retry = _STATE["retry_at"].get(name)
        if _STATE["unsupported"]:
            state = "host-refused"
        elif c and c[1].poll() is None:
            state = "up"
        elif retry is not None:
            state = "backoff"
        else:
            state = "dead"
        out[name] = {"state": state, "rc": rc,
                     "retry_at_epoch": int(retry + wall_offset) if state == "backoff" else None}
    return out


def write_mirror_state(log_dir, clients, live):
    """Atomically rewrite the mirror-state file when a name's state, rc or retry time changed."""
    states = mirror_states(clients, live)
    sig = {n: (v["state"], v["rc"], _STATE["retry_at"].get(n)) for n, v in states.items()}
    if sig == _STATE["state_sig"]:
        return
    try:
        _roster().write_json_atomic(os.path.join(log_dir, MIRROR_STATE_FILE), states)
    except OSError:
        return
    _STATE["state_sig"] = sig


_ROUTER_MODULE = None


def _router_module():
    """Load the router module once: ensure_router probes every poll and every 0.2s while waiting."""
    global _ROUTER_MODULE
    if _ROUTER_MODULE is None:
        spec = importlib.util.spec_from_file_location("unimatrix_peer_router", HERE / "unimatrix-peer-router.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _ROUTER_MODULE = mod
    return _ROUTER_MODULE


def router_answers(ctl):
    """True when the router control endpoint answers {"op":"list"}."""
    try:
        return bool(_router_module().request_line(ctl, {"op": "list"}, timeout=2).get("ok"))
    except (OSError, ValueError):
        return False


def ensure_router(say, wait=3.0):
    """Spawn the peer router detached when its endpoint does not answer on either OS.

    A nonblocking process lock held across probe, spawn and bind keeps two supervisors (one per far
    alias) from both spawning. Returns True when this call spawned one.
    """
    if os.environ.get("UNIMATRIX_ROUTER_SUPERVISION") == "off":
        return False
    router = _router_module()
    transport = router._transport()
    ctl = router.endpoint_path(router.ROUTER_ENDPOINT_SEED)
    lock_dir = _roster().settings_home() / "unimatrix" / "router"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / "supervisor.lock", "a+b") as lock:
        if not transport.lock_file(lock):
            return False
        try:
            if router_answers(ctl):
                return False
            # A live router may be draining children with its listener already closed.
            # Its lifetime lease prevents a replacement binding before that cleanup ends.
            with open(lock_dir / "router.lock", "a+b") as lease:
                if not transport.lock_file(lease):
                    return False
                transport.unlock_file(lease)
            p = _roster().spawn([sys.executable, str(HERE / "unimatrix-peer-router.py")])
            say(f"router not answering at {ctl}; spawned router pid {p.pid}")
            deadline = time.time() + wait
            while time.time() < deadline and not router_answers(ctl):
                time.sleep(0.2)
            return True
        finally:
            transport.unlock_file(lock)


def main(argv=None):
    cfg = parse_args(sys.argv[1:] if argv is None else argv)
    skip = set(cfg.skip)
    log = open(cfg.log, "a", encoding="utf-8", buffering=1)

    def say(msg):
        log.write(f"{time.strftime('%H:%M:%SZ', time.gmtime())} {msg}\n")

    if sys.platform != "win32":
        # A stop is never silent: a clean 0 would read as "nothing to mirror" while every mirror drops.
        def stopped(signum, _frame):
            say(f"stopped by signal {signum}; mirrors drop until relaunched")
            sys.exit(128 + signum)
        signal.signal(signal.SIGTERM, stopped)
    clients = {}
    try:
        while True:
            ensure_router(say)
            sync(clients, live_sessions(skip), cfg, say)
            time.sleep(POLL_SECONDS)
    finally:
        for _, p in clients.values():
            _roster().tree_kill(p.pid)


if __name__ == "__main__":
    main()
