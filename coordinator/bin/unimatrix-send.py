"""Send a message to another agent through the unimatrix peer router.

For Codex and agy peers: an agent's message in is answered with this command out, addressed to
the sender, never with plain text to the human at the keyboard.

    unimatrix-send.py --to coordinator-content-repo-ea 'Reviewed the plan: two issues, ...'
    unimatrix-send.py --list

The sender is identified from the shell this runs in: CODEX_THREAD_ID (Codex) or
ANTIGRAVITY_CONVERSATION_ID (agy) names the session, and the router maps it to that peer's
name. --as overrides it. Reads no token and puts no credential in argv.
"""
import argparse
import importlib.util
import json
import os
import sys
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


def _print_rows(rows):
    print("NAME\tBOX\tVENDOR\tSTATE")
    for name, box, vendor, state in rows:
        print(f"{name}\t{box}\t{vendor}\t{state}")


def _list_from_sessions():
    c = _sibling("unimatrix-peer-contract.py", "unimatrix_peer_contract_send")
    box = c.local_box()
    rows = [c.agent_row(e, box) for e in _session_entries()]
    _print_rows([(r["name"], r["box"], r["vendor"], "via SendMessage") for r in rows])
    print("Message any of these with SendMessage; ListAgents lists them too.")


def own_target(env=os.environ):
    return env.get("CODEX_THREAD_ID") or env.get("ANTIGRAVITY_CONVERSATION_ID") or ""


def main(argv=None):
    p = argparse.ArgumentParser(prog="unimatrix-send.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--to", help="name of the agent to message (see --list)")
    p.add_argument("--list", action="store_true", help="list the agents you can message")
    p.add_argument("--as", dest="as_name", default=os.environ.get("UNIMATRIX_PEER_NAME", ""))
    p.add_argument("--endpoint-dir", default=None)
    p.add_argument("text", nargs="*")
    a = p.parse_args(argv)
    r = _router()
    ctl = r.endpoint_path(r.ROUTER_ENDPOINT_SEED, a.endpoint_dir)
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
        print(resp.get("error", "failed"), file=sys.stderr)
        if resp.get("peers"):
            print("agents: " + ", ".join(resp["peers"]), file=sys.stderr)
        return 1
    if a.list:
        ready = resp.get("ready", {})
        details = resp.get("details", {})
        peers = set(resp.get("peers", []))
        rows = []
        for name in sorted(peers | set(resp.get("sessions", []))):
            info = details.get(name, {})
            status = ("ready" if ready.get(name, True) else "not-ready (no leg)") if name in peers else info.get("status", "unknown")
            columns = [name, status, "vendor=" + str(info.get("vendor", "unknown")),
                       "box=" + str(info.get("box", "unknown")), "cwd=" + str(info.get("cwd", ""))]
            rows.append("\t".join(str(column).replace("\t", " ").replace("\n", " ").replace("\r", " ")
                                  for column in columns))
        print("\n".join(rows))
    elif resp.get("reply"):
        print(resp["reply"])
    elif resp.get("written") is True:
        print(f"sent to {a.to}")
    else:
        print(f"not confirmed: router did not report {a.to} accepted the message", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
