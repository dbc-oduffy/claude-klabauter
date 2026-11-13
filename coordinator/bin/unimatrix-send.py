#!/usr/bin/env python3
"""Send a message to another agent through the unimatrix peer router.

For Codex and agy peers: an agent's message in is answered with this command out, addressed to
the sender, never with plain text to the human at the keyboard.

    unimatrix-send.py --to <peer-name> 'Reviewed the plan: two issues, ...'
    unimatrix-send.py --list

The sender is identified from the shell this runs in: CODEX_THREAD_ID (Codex) or
ANTIGRAVITY_CONVERSATION_ID (agy) names the session, or a Grok session whose registry pid is an
ancestor of this shell; the router maps it to that peer's name. --as overrides it. Reads no token
and puts no credential in argv.
"""
import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _router():
    spec = importlib.util.spec_from_file_location("unimatrix_peer_router", HERE / "unimatrix-peer-router.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sibling(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, HERE / filename)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _windows():
    return os.name == "nt"


def _session_entries():
    """Live session entries (dicts) from the sessions dir; no router, so it works on every OS."""
    roster = _sibling("unimatrix-roster.py", "unimatrix_roster_send")
    sess, out = roster.sessions_dir(), []
    try:
        files = sorted(os.listdir(sess))
    except OSError:
        return out
    for f in files:
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(sess, f), encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        if (isinstance(d, dict) and isinstance(d.get("name"), str) and isinstance(d.get("pid"), int)
                and roster.pid_alive(d["pid"])):
            out.append(d)
    return out


RELAY_SETTLE_SECONDS = 15
# A mirror row may lack its host (`unimatrixRemote`) yet still carry the far-socket field or a box label.
_MIRROR_FIELDS = ("unimatrixRemote", "macSocketPath", "unimatrixBox")


def _mirror_name(name):
    """The live mirror entry's own name when `name` is that name or its far name, else ''.

    The bridge keys its deliveries.jsonl row on the mirror's own name, never the bare far name."""
    return next((e["name"] for e in _session_entries()
                 if any(e.get(f) for f in _MIRROR_FIELDS) and name in (e.get("name"), e.get("unimatrixFarName"))), "")


def _is_mirror(name):
    """True when `name` is a live mirror here: its router receipt only means the bridge took the frame."""
    return bool(_mirror_name(name))


def relay_outcome(name, since, wait=RELAY_SETTLE_SECONDS, sleep=time.sleep, now=time.time):
    """The bridge's recorded far-relay outcome for `name` after `since`, polled up to `wait` seconds; None if none.

    The bridge answers the sender before its ssh leg, so a `written` receipt cannot say the far
    relay delivered; the bridge's deliveries.jsonl row can. Failed row wins over an ok one."""
    roster = _sibling("unimatrix-roster.py", "unimatrix_roster_send")
    path = roster.settings_home() / "unimatrix" / "deliveries.jsonl"
    deadline = now() + wait
    while True:
        try:
            rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except (OSError, ValueError):
            rows = []
        mine = [r for r in rows if isinstance(r, dict) and isinstance(r.get("target"), str)
                and r["target"].startswith(f"{name} (pid ") and (r.get("at") or 0) >= since]
        if mine:
            return next((r for r in mine if not r.get("ok")), mine[-1])
        if now() >= deadline:
            return None
        sleep(0.25)


def _print_rows(rows):
    print("NAME\tBOX\tVENDOR\tSTATE")
    for name, box, vendor, state in rows:
        print(f"{name}\t{box}\t{vendor}\t{state}")


def _member_ip_hosts():
    """ip literal -> member host name, from the verified membership; {} when membership is unusable.

    Only called when a listed row's box stamp is an IP, so the common listing resolves nothing."""
    import socket
    try:
        roster = _sibling("unimatrix-roster.py", "unimatrix_roster_send")
        mem = _sibling("unimatrix-membership.py", "unimatrix_membership_send")
        members = mem.verified_members(roster.settings_home())
    except Exception:
        return {}
    out = {}
    for m in members:
        try:
            for info in socket.getaddrinfo(m["host"], None, type=socket.SOCK_STREAM):
                out.setdefault(info[4][0].split("%", 1)[0].lower(), m["host"])
        except (OSError, UnicodeError):
            continue
    return out


def _list_from_sessions():
    c = _sibling("unimatrix-peer-contract.py", "unimatrix_peer_contract_send")
    box = c.local_box()
    rows = [c.agent_row(e, box) for e in _session_entries()]
    if any(c.is_ip_literal(r["box"]) for r in rows):
        ip_hosts = _member_ip_hosts()
        for r in rows:
            if c.is_ip_literal(r["box"]):
                r["box"] = c.box_id(r["box"], ip_hosts)
    _print_rows([(r["name"], r["box"], r["vendor"], "via SendMessage") for r in rows])
    print("Message any of these with SendMessage; ListAgents lists them too.")


def _grok_target():
    """The Grok session id whose registry pid is an ancestor of this shell, else ''."""
    try:
        peer = _sibling("unimatrix-peer-grok.py", "unimatrix_peer_grok_send").own_session()
    except Exception:
        return ""
    return peer["target"] if peer else ""


def own_target(env=os.environ, grok=None):
    """Session id of the sending agent: Codex or agy env, else the Grok session this shell descends from."""
    return (env.get("CODEX_THREAD_ID") or env.get("ANTIGRAVITY_CONVERSATION_ID")
            or (grok or _grok_target)())


def main(argv=None):
    p = argparse.ArgumentParser(prog="unimatrix-send.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--to", help="name of the agent to message (see --list)")
    p.add_argument("--list", action="store_true", help="list the agents you can message")
    p.add_argument("--as", dest="as_name", default=os.environ.get("UNIMATRIX_PEER_NAME", ""))
    p.add_argument("--endpoint-dir", default=None)
    p.add_argument("text", nargs="*")
    a = p.parse_args(argv)
    r = _router()
    started = time.time() - 1  # one second of slack: the bridge stamps `at` on its own clock read, after ours
    ctl = r.endpoint_path(r.ROUTER_ENDPOINT_SEED, a.endpoint_dir)
    # Resolved before the send: a misrouted mirror is unmirrored by the roster sync while the bridge
    # records its FAILED row, so a lookup after the router's reply finds no mirror and reads success.
    to_mirror, to_mirror_name = (_is_mirror(a.to), _mirror_name(a.to)) if a.to and not a.list else (False, "")
    if a.list:
        req = {"op": "list", "as_target": own_target(), "as": a.as_name or "cli"}
    elif a.to and a.text:
        req = {"op": "send", "to": a.to, "text": " ".join(a.text),
               "as_target": "" if a.as_name else own_target(), "as": a.as_name or "cli"}
    else:
        p.error("give --to NAME and the message text, or --list")
    try:
        resp = r.request_line(ctl, req)
    except OSError as e:
        if a.list:
            _list_from_sessions()
            return 0
        print(f"unimatrix router not reachable: {e}", file=sys.stderr)
        print("Claude sessions and bridged mirrors are reached with SendMessage; ListAgents lists them.",
              file=sys.stderr)
        return 1
    if not resp.get("ok"):
        prefix = "" if a.list else f"undelivered: {a.to}: "
        print(prefix + resp.get("error", "failed"), file=sys.stderr)
        if resp.get("peers"):
            print("agents: " + ", ".join(resp["peers"]), file=sys.stderr)
        return 1
    if a.list:
        reachable = resp.get("reachable")
        if not isinstance(reachable, list):
            print("router list reply lacks 'reachable'; restart the router", file=sys.stderr)
            return 1
        agents = {row.get("name"): row for row in resp.get("agents", []) if isinstance(row, dict)}
        peers = set(resp.get("peers", []))
        _print_rows([(name, agents.get(name, {}).get("box", "-"), agents.get(name, {}).get("vendor", "-"),
                      "stale" if agents.get(name, {}).get("stale") is True
                      else "ready" if name in peers else "session") for name in reachable])
        print("Message any of these with SendMessage; ListAgents lists them too.")
    elif resp.get("reply"):
        print(resp["reply"])
    elif resp.get("written") is True or resp.get("msg_id"):
        outcome = relay_outcome(to_mirror_name or a.to, started) if to_mirror else {"ok": True}
        if outcome is None:
            print(f"unconfirmed: the relay recorded no outcome for {a.to} within {RELAY_SETTLE_SECONDS}s; "
                  "check before resending", file=sys.stderr)
            return 1
        elif not outcome.get("ok"):
            print(f"undelivered: {a.to}: {outcome.get('error', 'far relay refused')}", file=sys.stderr)
            return 1
        else:
            print(f"sent to {a.to}")
    else:
        print(f"unconfirmed: router gave no delivery receipt for {a.to}; the message may have been delivered, "
              "check before resending", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
