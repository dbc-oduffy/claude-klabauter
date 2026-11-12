#!/usr/bin/env python3
"""Start or wake a claude-family session on a far box over its pinned control key.

Usage:
  unimatrix-launch.py <box> <repo-key>
  unimatrix-launch.py <box> --wake <session-name>

Runs `ssh -i ~/.ssh/unimatrix_control_<box> -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=5
unimatrix-control-<box> "launch <repo-key>"` (or `"wake <name>"`); the far forced command replies one
JSON line. On ok:true this prints the far session name and exits 0; on ok:false, an unparseable reply,
a timeout or an ssh failure it prints the error to stderr and exits 1.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SSH_TIMEOUT_SECONDS = 60


def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def run_ssh(argv):
    """Run argv; return (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=SSH_TIMEOUT_SECONDS,
                           creationflags=_no_window())
    except subprocess.TimeoutExpired:
        return 255, "", f"ssh timed out after {SSH_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return 255, "", f"ssh could not run: {exc}"
    return p.returncode, p.stdout, p.stderr


def parse_reply(stdout):
    """Return the last JSON-object line of stdout, or None."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def control(box, verb, arg, runner=run_ssh):
    """Send `<verb> <arg>` over the box's control alias; return (ok, session_name_or_error)."""
    key = Path.home() / ".ssh" / f"unimatrix_control_{box}"
    argv = ["ssh", "-i", str(key), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5", f"unimatrix-control-{box}", f"{verb} {arg}"]
    rc, out, err = runner(argv)
    reply = parse_reply(out)
    if reply is None:
        detail = (err or out or "").strip() or f"ssh exited {rc}"
        return False, f"no control reply: {detail}"
    if reply.get("ok") is True and reply.get("session_name"):
        return True, str(reply["session_name"])
    if reply.get("ok") is True:
        return False, "control reply carried no session_name"
    return False, str(reply.get("error") or "control refused")


def main(argv=None, runner=run_ssh):
    ap = argparse.ArgumentParser(prog="unimatrix-launch.py")
    ap.add_argument("box")
    ap.add_argument("repo_key", nargs="?")
    ap.add_argument("--wake", metavar="NAME")
    args = ap.parse_args(argv)
    if bool(args.wake) == bool(args.repo_key):
        ap.error("give exactly one of <repo-key> or --wake <session-name>")
    verb, arg = ("wake", args.wake) if args.wake else ("launch", args.repo_key)
    if not TOKEN_RE.match(args.box) or not TOKEN_RE.match(arg):
        print(f"unimatrix-launch: invalid box or {verb} argument", file=sys.stderr)
        return 2
    ok, value = control(args.box, verb, arg, runner)
    if ok:
        print(value)
        return 0
    print(value, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
