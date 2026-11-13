#!/usr/bin/env python3
"""Launch codex, agy, grok or claude as a headless EM carrying a launcher-written fire grant.

Usage: headless-em-launch.py <codex|agy|grok|claude> [--session-id ID] [--prepare-only] [--native-executable PATH]
       [--name NAME] [--resume ID] [--cwd DIR] [-- cli args...]
       (--help prints this text)

Without --native-executable the CLI is resolved from PATH (on Windows a .exe as-is, or codex's npm shim
mapped to its native exe).

Resolves the child session id (--session-id, else COORDINATOR_SESSION_ID, else a fresh uuid4), writes
the grant for (session id, family), and hands the child COORDINATOR_SESSION_ID plus the grant token
(COORDINATOR_ALLOW_HEADLESS_EM_TOKEN). Exit 2 on a refusal, 127 when the CLI is not on PATH.

This is the only caller of write_headless_em_grant. It refuses when the calling env carries a worker
marker, so a worker cannot mint itself an EM grant; agent-worker-launch.py never writes one.
claude (both OSes) runs `claude --bg -n NAME` (or `claude --bg --resume ID`) captured in --cwd and prints
{"name", "id", "session_id"} JSON, id being the one claude printed; it is never exec'd.
--prepare-only prints {argv, session_id, environment} as JSON (the environment holds the token, and for
codex on Windows a shaped PATH: real pwsh dir first, Store alias dirs dropped) and execs nothing;
Windows non-prepare launches are refused because exec must not sit under python.exe.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
_REPO_ROOT = HERE.parents[1]

REFUSED = 2
SESSION_ENV = "COORDINATOR_SESSION_ID"
FAMILIES = ("codex", "agy", "grok", "claude")
BG_TIMEOUT_S = 60
_BG_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_BG_ID = re.compile(r"\b(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{6,12})\b")


def _worker_marker(environ):
    """Why this env is a worker's, or '' when it is not."""
    if str(environ.get("COORDINATOR_AGENT_WORKER", "")).lower() in ("1", "true", "yes"):
        return "COORDINATOR_AGENT_WORKER is set"
    role = environ.get("COORDINATOR_AGENT_ROLE")
    if role and role != "em":
        return f"COORDINATOR_AGENT_ROLE={role}"
    return ""


def _printed_id(text):
    """The session id `claude --bg` prints: a uuid, else a short hex id; '' when none is present."""
    found = _BG_UUID.findall(text or "") or _BG_ID.findall(text or "")
    return found[-1] if found else ""


def _run_bg(argv, *, cwd, env, runner, name):
    """Run `claude --bg` captured; print {name, id, session_id} JSON and return 0, else a refusal code."""
    kw = {"cwd": cwd, "env": env, "capture_output": True, "text": True, "timeout": BG_TIMEOUT_S,
          "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    try:
        done = runner(argv, **kw)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"headless-em-launch: claude --bg failed: {exc}", file=sys.stderr)
        return REFUSED
    if done.returncode != 0:
        print(f"headless-em-launch: claude --bg exited {done.returncode}: {(done.stderr or '').strip()}",
              file=sys.stderr)
        return REFUSED
    bg_id = _printed_id(done.stdout)
    if not bg_id:
        print(f"headless-em-launch: claude --bg printed no session id: {(done.stdout or '').strip()!r}",
              file=sys.stderr)
        return REFUSED
    print(json.dumps({"name": name, "id": bg_id, "session_id": env.get(SESSION_ENV)}))
    return 0


def launch(family, extra, *, session_id=None, which=shutil.which, execv=os.execv, environ=None,
           prepare_only=False, platform=None, native_executable=None, mint=uuid.uuid4,
           name=None, resume=None, cwd=None, runner=subprocess.run):
    """Write the grant and exec the CLI with the token env; returns an exit code on refusal or prepare.

    claude is not exec'd: `claude --bg [-n NAME | --resume ID]` runs captured in `cwd` on both OSes and
    the printed id is returned with the name.
    """
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
    if (name or resume) and family != "claude":
        print("headless-em-launch: --name and --resume apply to claude only", file=sys.stderr)
        return REFUSED
    if platform == "win32" and not prepare_only and family != "claude":
        print("headless-em-launch: Windows launch must run in the current PowerShell process; "
              "use --prepare-only and start the CLI from it", file=sys.stderr)
        return REFUSED
    cli = native_executable
    if not cli and family == "claude":
        cli = (which("claude.exe") if platform == "win32" else None) or which("claude")
    elif not cli:
        from coordinator_core.codex_runner.resolve import CodexUnavailable, resolve_native_cli
        try:
            cli = resolve_native_cli(family, platform, which=which)
        except CodexUnavailable as exc:
            print(f"headless-em-launch: {exc}; pass --native-executable with its installed path", file=sys.stderr)
            return REFUSED
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
    if family == "claude":
        cmd = [cli, "--bg", *(["--resume", resume] if resume else ["-n", name] if name else []), *extra]
    child = {SESSION_ENV: sid, HEADLESS_EM_TOKEN_ENV: token}
    if family == "codex" and platform == "win32":
        from coordinator_core.codex_runner.codex_spawn_env import place_installed_pwsh
        path_key = next((k for k in environ if k.upper() == "PATH"), None)
        if path_key is not None:
            inherited = environ[path_key]
            shaped = place_installed_pwsh({"PATH": inherited}, environ, windows=True)["PATH"]
            if shaped != inherited:
                child["PATH"] = shaped
    if prepare_only:
        print(json.dumps({"argv": cmd, "session_id": sid, "environment": child}))
        return 0
    if family == "claude":
        return _run_bg(cmd, cwd=cwd, env={**environ, **child}, runner=runner, name=name)
    environ.update(child)
    execv(cmd[0], cmd)
    return 0


def main(argv=None, **kw):
    import lib  # noqa: F401 -- bootstraps coordinator/bin/lib onto sys.path
    from cc_invoke import place_engine_root_on_path

    place_engine_root_on_path(str(_REPO_ROOT))
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
    ap.add_argument("--name")
    ap.add_argument("--resume")
    ap.add_argument("--cwd")
    ns = ap.parse_args(args)
    return launch(ns.family, extra, session_id=ns.session_id, prepare_only=ns.prepare_only,
                  native_executable=ns.native_executable, name=ns.name, resume=ns.resume,
                  cwd=ns.cwd, **kw)


if __name__ == "__main__":
    sys.exit(main())
