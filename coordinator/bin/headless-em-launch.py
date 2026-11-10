"""Launch codex, agy or grok as a headless EM carrying a launcher-written fire grant.

Usage: headless-em-launch.py <codex|agy|grok> [--session-id ID] [--prepare-only] [--native-executable PATH] [-- cli args...]
       (--help prints this text)

Resolves the child session id (--session-id, else COORDINATOR_SESSION_ID, else a fresh uuid4), writes
the grant for (session id, family), and hands the child COORDINATOR_SESSION_ID plus the grant token
(COORDINATOR_ALLOW_HEADLESS_EM_TOKEN). Exit 2 on a refusal, 127 when the CLI is not on PATH.

This is the only caller of write_headless_em_grant. It refuses when the calling env carries a worker
marker, so a worker cannot mint itself an EM grant; agent-worker-launch.py never writes one.
--prepare-only prints {argv, session_id, environment} as JSON (the environment holds the token) and
execs nothing; Windows non-prepare launches are refused because exec must not sit under python.exe.
"""
import argparse
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
_REPO_ROOT = HERE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

REFUSED = 2
SESSION_ENV = "COORDINATOR_SESSION_ID"
FAMILIES = ("codex", "agy", "grok")


def _worker_marker(environ):
    """Why this env is a worker's, or '' when it is not."""
    if str(environ.get("COORDINATOR_AGENT_WORKER", "")).lower() in ("1", "true", "yes"):
        return "COORDINATOR_AGENT_WORKER is set"
    role = environ.get("COORDINATOR_AGENT_ROLE")
    if role and role != "em":
        return f"COORDINATOR_AGENT_ROLE={role}"
    return ""


def launch(family, extra, *, session_id=None, which=shutil.which, execv=os.execv, environ=None,
           prepare_only=False, platform=None, native_executable=None, mint=uuid.uuid4):
    """Write the grant and exec the CLI with the token env; returns an exit code on refusal or prepare."""
    from coordinator_core.ops.workflow_fire.headless_em_grant import (
        HEADLESS_EM_TOKEN_ENV, HeadlessEmGrantError, write_headless_em_grant)

    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    if family not in FAMILIES:
        print(f"headless-em-launch: refused: family must be one of {', '.join(FAMILIES)}", file=sys.stderr)
        return REFUSED
    marker = _worker_marker(environ)
    if marker:
        print(f"headless-em-launch: refused: a worker env cannot mint an EM grant ({marker})", file=sys.stderr)
        return REFUSED
    if platform == "win32" and not prepare_only:
        print("headless-em-launch: Windows launch must run in the current PowerShell process; "
              "use --prepare-only and start the CLI from it", file=sys.stderr)
        return REFUSED
    cli = native_executable or which(family + ".exe" if platform == "win32" else family)
    if not cli:
        print(f"headless-em-launch: {family} is not on PATH", file=sys.stderr)
        return 127
    if platform == "win32" and not cli.lower().endswith(".exe"):
        print("headless-em-launch: Windows requires the native CLI .exe, not a .cmd/.bat shim; "
              "pass --native-executable with its installed path", file=sys.stderr)
        return REFUSED
    sid = (session_id or environ.get(SESSION_ENV) or "").strip() or str(mint())
    try:
        token = write_headless_em_grant(sid, family)
    except HeadlessEmGrantError as exc:
        print(f"headless-em-launch: refused: {exc}", file=sys.stderr)
        return REFUSED
    cmd = [cli, *extra]
    child = {SESSION_ENV: sid, HEADLESS_EM_TOKEN_ENV: token}
    if prepare_only:
        print(json.dumps({"argv": cmd, "session_id": sid, "environment": child}))
        return 0
    environ.update(child)
    execv(cmd[0], cmd)
    return 0


def main(argv=None, **kw):
    args = list(sys.argv[1:] if argv is None else argv)
    if args in ([], ["--help"], ["-h"]):
        print(__doc__)
        return 0 if args else REFUSED
    if "--" in args:
        i = args.index("--")
        args, extra = args[:i], args[i + 1:]
    else:
        extra = []
    ap = argparse.ArgumentParser(prog="headless-em-launch", add_help=False)
    ap.add_argument("family", choices=FAMILIES)
    ap.add_argument("--session-id")
    ap.add_argument("--prepare-only", action="store_true")
    ap.add_argument("--native-executable")
    ns = ap.parse_args(args)
    return launch(ns.family, extra, session_id=ns.session_id, prepare_only=ns.prepare_only,
                  native_executable=ns.native_executable, **kw)


if __name__ == "__main__":
    sys.exit(main())
