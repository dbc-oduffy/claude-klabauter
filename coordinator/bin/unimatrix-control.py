#!/usr/bin/env python3
"""Forced command for the unimatrix CONTROL key on the receiving box: the ONLY thing that key can run.
It starts or wakes a session and nothing else; the message key's forced command (unimatrix-relay.py)
and the file key's (unimatrix-file-relay.py) refuse both modes, and this one refuses every other mode.

authorized_keys line (restrict = no pty, no forwarding; key body elided; absolute interpreter and script
paths). `unimatrix-install.py --install` prints the exact form:
  restrict,command="/usr/bin/python3 /home/me/.coordinator-claude-settings/unimatrix/unimatrix-control.py" ssh-ed25519 <key-body> unimatrix-control

Modes, read from SSH_ORIGINAL_COMMAND (stdin is never read):
  launch <repo-key>   resolve <repo-key> through the machine-local `repos.` registry, mint a session name
                      `<repo-key>-<6 hex>` and run `headless-em-launch.py claude --name <name>` there
                      (`claude --bg -n <name>` on every OS); its one JSON line carries the session id. A path
                      is never accepted; an unknown key is refused.
  wake <session>      resolve the not-live local session of that name to its recorded session id and run
                      `headless-em-launch.py claude --resume <id>` in its recorded cwd; a live, unknown or
                      ambiguous name is refused. Never attaches to or messages a live session.
Reply: one JSON line {"ok": true, "session_name", "session_id", "pid", "family"} (`pid` is null for claude:
`claude --bg` owns the background process) or {"ok": false, "error"}; exit 0 only when ok.
"""
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

SESS = "~/.claude/sessions"
REPO_KEY_RE = re.compile(r"[A-Za-z0-9_]{1,56}")
NAME_RE = re.compile(r"[A-Za-z0-9_-](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9_-])?")
SESSION_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
LAUNCH_FAMILIES = ("claude", "codex", "agy", "grok")
DEFAULT_LAUNCH_FAMILY = "claude"
HERE = Path(__file__).resolve().parent


def settings_home():
    return Path(os.environ.get("COORDINATOR_SETTINGS_HOME") or Path.home() / ".coordinator-claude-settings")


def _load_sibling(filename, modname):
    import importlib.util
    spec = importlib.util.spec_from_file_location(modname, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_mode(original_command):
    """Map an SSH_ORIGINAL_COMMAND string to ("launch"|"wake", arg), or None when it is not allowed."""
    cmd = (original_command or "").split()
    if len(cmd) != 2 or original_command != f"{cmd[0]} {cmd[1]}":
        return None
    if cmd[0] == "launch" and REPO_KEY_RE.fullmatch(cmd[1]):
        return "launch", cmd[1]
    if cmd[0] == "wake" and NAME_RE.fullmatch(cmd[1]):
        return "wake", cmd[1]
    return None


def resolve_repo_key(key):
    """The directory the machine-local `repos.<key>` registry entry names, or None."""
    reader = settings_home() / "bin" / "_machine_local.py"
    if not reader.is_file():
        return None
    try:
        r = subprocess.run([sys.executable, str(reader), "get", f"repos.{key}"], capture_output=True,
                           text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    path = os.path.expanduser(r.stdout.strip()) if not r.returncode else ""
    return path if path and os.path.isdir(path) else None


def named_session_entries(name):
    """Registry entries named `name` that carry a session id, as (entry, live) pairs."""
    root = os.path.expanduser(SESS)
    found = []
    try:
        files = os.listdir(root)
    except OSError:
        return found
    liveness = None
    for f in files:
        m = re.fullmatch(r"(\d+)(?:\.([0-9a-f]{16}))?\.json", f)
        if not m:
            continue
        try:
            with open(os.path.join(root, f), encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        if (isinstance(d, dict) and d.get("name") == name and isinstance(d.get("sessionId"), str)
                and SESSION_ID_RE.fullmatch(d["sessionId"])):
            liveness = liveness or _load_sibling("unimatrix-liveness.py", "unimatrix_control_liveness")
            found.append((d, bool(d.get("pid") == int(m.group(1)) and liveness.entry_live(d))))
    return found


def spawn_launch(family, repo_dir, session_id, session_name, *, spawner=None, platform=None):
    """Start `headless-em-launch <family> --session-id <id> --name <name>` detached in `repo_dir`; the reply dict."""
    launcher = HERE / "headless-em-launch.py"
    if spawner is None:
        if not (HERE / "lib" / "session_detach.py").is_file():
            return {"ok": False, "error": "session_detach not vendored beside the control command"}
        spawner = _load_sibling("lib/session_detach.py", "unimatrix_control_session_detach").spawn_detached
    log = settings_home() / "unimatrix" / "control-launch.log"
    cmd = [sys.executable, str(launcher), family, "--session-id", session_id, "--name", session_name]
    pid = spawner(cmd, log, cwd=repo_dir, strict=True, platform=platform)
    if pid is None:
        return {"ok": False, "error": "spawn failed"}
    return {"ok": True, "session_name": session_name, "session_id": session_id, "pid": pid, "family": family}


def run_claude_launch(args, repo_dir, session_name, *, runner=None, timeout=60):
    """Run `headless-em-launch.py claude <args>` synchronously in `repo_dir`; the reply dict.

    The launcher prints one JSON line `{"name", "id", "session_id"}` on success; `id` is the one `claude --bg`
    printed (what `--resume` takes), `session_id` is only the grant's minted id."""
    cmd = [sys.executable, str(HERE / "headless-em-launch.py"), "claude", *args]
    try:
        r = (runner or subprocess.run)(cmd, cwd=repo_dir, capture_output=True, text=True, timeout=timeout,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": f"launch failed: {type(e).__name__}"}
    if r.returncode:
        return {"ok": False, "error": "launch failed"}
    try:
        sid = json.loads((r.stdout or "").strip().splitlines()[-1])["id"]
    except (IndexError, ValueError, KeyError, TypeError):
        return {"ok": False, "error": "launch reply unparseable"}
    if not isinstance(sid, str) or not SESSION_ID_RE.fullmatch(sid):
        return {"ok": False, "error": "launch reply unparseable"}
    return {"ok": True, "session_name": session_name, "session_id": sid, "pid": None, "family": "claude"}


def launch(key, *, resolve=None, runner=None):
    """`launch <repo-key>`: the repo resolves through the machine-local registry, never a path argument."""
    repo_dir = (resolve or resolve_repo_key)(key)
    if not repo_dir:
        return {"ok": False, "error": f"unknown repo key {key}"}
    name = f"{key}-{uuid.uuid4().hex[:6]}"
    return run_claude_launch(["--name", name], repo_dir, name, runner=runner)


def wake(name, *, sessions=None, spawner=None, platform=None, runner=None):
    """`wake <session>`: relaunch the one not-live local session of that name under its session id."""
    found = (sessions or named_session_entries)(name)
    if not found:
        return {"ok": False, "error": f"unknown session {name}"}
    if any(live for _, live in found):
        return {"ok": False, "error": f"session {name} is already live"}
    if len({d["sessionId"] for d, _ in found}) != 1:
        return {"ok": False, "error": f"session name {name} is ambiguous"}
    d = found[0][0]
    cwd = d.get("cwd")
    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        return {"ok": False, "error": f"session {name} has no usable working directory"}
    family = d.get("vendor") if d.get("vendor") in LAUNCH_FAMILIES else DEFAULT_LAUNCH_FAMILY
    if family == "claude":
        return run_claude_launch(["--resume", d["sessionId"]], cwd, name, runner=runner)
    return spawn_launch(family, cwd, d["sessionId"], name, spawner=spawner, platform=platform)


def main():
    parsed = parse_mode(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    if parsed is None:
        sys.exit("usage: launch <repo-key> | wake <session>")
    mode, arg = parsed
    reply = (launch if mode == "launch" else wake)(arg)
    print(json.dumps(reply))
    sys.exit(0 if reply.get("ok") is True else 1)


if __name__ == "__main__":
    main()
