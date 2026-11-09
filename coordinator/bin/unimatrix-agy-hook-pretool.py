"""agy PreToolUse hook: the Klabauter guard first, then keep a live agy leg for this conversation.

Order: unimatrix-host-guard.py (loaded as a sibling; `--engine-root <path>` selects the engine) answers
first. A guard answer other than "ask" (a deny) is returned unchanged with no rewrite. An "ask" answer,
including the guard's own degraded neutral "ask", falls through to the leg bootstrap below; a guard
reason and the bootstrap rewrite then share one "ask". The one "allow" is a peer reply: a run_command that
is exactly this interpreter and the installed unimatrix-send.py beside this hook, plain arguments, no shell
operator or expansion (mirrors the Codex unimatrix.rules send rule); it is never a fallback.

Hooks do not receive agy's language-server address or token, but run_command children do. When no
current leg exists, the hook prefixes the command the agent is about to run with a detached start of
unimatrix-agy-hook-launch.py, which inherits the token. That rewrite answers "ask": the user sees and
approves the exact command. Every other path also answers "ask": agy fails closed on a non-zero exit,
invalid JSON, or a reply without `decision`, and "ask" keeps agy's own approval behaviour where
"allow" would silently widen it.

A leg is current only if it is alive, was started under this same agy process (the hook's parent),
and its language server still accepts connections. An agy restart changes the token and port, so a
leg that survives one is stale even though its pid is alive. A `pending` leg (started by SessionStart
without a token) is never current: the rewrite relaunches it live.

Leg record (STATE/<conversationId>.json, written by the launcher, read here and by the router):
  {"pid": <leg pid>, "conversation": <id>, "endpoint": <leg socket>, "ls_address": "host:port",
   "agy_pid": <agy pid>, "mode": "pending" | "live"}   (mode absent reads as live)
"""
import importlib.util
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

SPAWN_DEBOUNCE_S = 30.0
HERE = Path(__file__).resolve().parent
LAUNCH = HERE / "unimatrix-agy-hook-launch.py"
PASS = {"decision": "ask"}
CONV_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def settings_home():
    env = os.environ.get("COORDINATOR_SETTINGS_HOME")
    return Path(env) if env else Path.home() / ".coordinator-claude-settings"


def state_dir():
    return settings_home() / "unimatrix" / "agy-legs"


def pid_alive(pid):
    """True while `pid` runs. os.kill(pid, 0) means CTRL_C_EVENT on Windows, so ask the roster helper there."""
    if os.name == "nt":
        spec = importlib.util.spec_from_file_location("unimatrix_roster", HERE / "unimatrix-roster.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return bool(mod.pid_alive(pid))
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def ls_reachable(addr):
    host, _, port = (addr or "").rpartition(":")
    try:
        with socket.create_connection((host or "localhost", int(port)), timeout=0.5):
            return True
    except (OSError, ValueError):
        return False


def leg_current(conv, agy_pid, state=None):
    try:
        with open((state or state_dir()) / f"{conv}.json", encoding="utf-8") as f:
            rec = json.load(f)
        if not pid_alive(int(rec["pid"])):
            return False
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if rec.get("mode", "live") != "live":
        return False  # a pending leg only queues; the first run_command relaunches it live
    return rec.get("agy_pid") == agy_pid and ls_reachable(rec.get("ls_address"))


def launch_prefix(conv, agy_pid):
    """Shell text that starts the launcher detached and silent, for sh. None on Windows: agy runs
    PowerShell there, which rejects cmd syntax, so bootstrap_rewrite spawns the launcher itself."""
    if os.name == "nt":
        return None
    argv = [sys.executable, str(LAUNCH), "--conversation", conv, "--agy-pid", str(agy_pid)]
    return f"(nohup {' '.join(shlex.quote(a) for a in argv)} >/dev/null 2>&1 &)", "; "


def spawn_launcher(conv, agy_pid):
    """Windows: start the launcher detached from this hook, since the command line cannot carry the
    start (agy's PowerShell rejects cmd syntax). The hook's own environment lacks the language-server
    token; the launcher is idempotent, so a failed or tokenless start only leaves the leg pending.
    A start stamp (STATE/<conv>.spawned) debounces it: each hook is a fresh process, so a start within
    SPAWN_DEBOUNCE_S of the stamp is skipped. An unwritable stamp never blocks the start."""
    stamp = state_dir() / f"{conv}.spawned"
    try:
        if time.time() - stamp.stat().st_mtime < SPAWN_DEBOUNCE_S:
            return
    except OSError:
        pass
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except OSError:
        pass
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
             | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        subprocess.Popen([sys.executable, str(LAUNCH), "--conversation", conv, "--agy-pid", str(agy_pid)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, creationflags=flags)
    except OSError:
        pass


def load_sibling(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_guard(raw, engine_root):
    """The host guard's agy answer for the raw payload text; the neutral "ask" when it cannot run."""
    try:
        answer, _state = load_sibling("unimatrix-host-guard").guard("agy", raw, engine_root)
    except Exception:
        return dict(PASS)
    return answer if isinstance(answer, dict) and isinstance(answer.get("decision"), str) else dict(PASS)


def bootstrap_rewrite(payload, agy_pid):
    """{"reason", "overwrite"} when a run_command needs the leg started, else None."""
    conv = payload.get("conversationId")
    call = payload.get("toolCall")
    call = call if isinstance(call, dict) else {}
    args = call.get("args")
    cmd = args.get("CommandLine") if isinstance(args, dict) else None
    if (not isinstance(conv, str) or not CONV_RE.match(conv) or call.get("name") != "run_command"
            or not isinstance(cmd, str) or not cmd or leg_current(conv, agy_pid)):
        return None
    prefix = launch_prefix(conv, agy_pid)
    if prefix is None:
        spawn_launcher(conv, agy_pid)
        return None
    start, sep = prefix
    return {"reason": "PM-installed unimatrix hook: also (re)starts the agy leg for Claude/Codex peer messaging in the background.",
            "overwrite": {"CommandLine": f"{start}{sep}{cmd}"}}


SEND_SCRIPT = HERE / "unimatrix-send.py"
UNSAFE_SHELL_TEXT = ("$", "`", "\n", "\r", "%", "^")


SHELL_SYNTAX_OUTSIDE_QUOTES = set("();<>|&{}@#,")


def quote_aware_tokens(cmd):
    """Split a Windows command line on unquoted whitespace, keeping backslashes literal and dropping the
    quote marks. None on an unterminated quote or on shell syntax outside quotes."""
    tokens, cur, quote, has = [], [], None, False
    for c in cmd:
        if quote:
            if c == quote:
                quote = None
            else:
                cur.append(c)
        elif c in "'\"":
            quote, has = c, True
        elif c.isspace():
            if has or cur:
                tokens.append("".join(cur))
            cur, has = [], False
        elif c in SHELL_SYNTAX_OUTSIDE_QUOTES:
            return None
        else:
            cur.append(c)
    if quote:
        return None
    if has or cur:
        tokens.append("".join(cur))
    return tokens


def is_peer_reply(payload):
    """True only for a run_command that is the interpreter, then the installed send script, then plain
    arguments: the shape of the Codex exec rule's argv. Any shell operator, expansion, other interpreter
    or other script path is False."""
    call = payload.get("toolCall")
    call = call if isinstance(call, dict) else {}
    args = call.get("args")
    cmd = args.get("CommandLine") if isinstance(args, dict) else None
    if call.get("name") != "run_command" or not isinstance(cmd, str) or any(c in cmd for c in UNSAFE_SHELL_TEXT):
        return False
    if os.name == "nt":
        tokens = quote_aware_tokens(cmd)
        if tokens is None:
            return False
    else:
        try:
            lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            return False
    if len(tokens) < 2 or tokens[0] != sys.executable or tokens[1] != str(SEND_SCRIPT):
        return False
    return os.name == "nt" or not any(t and set(t) <= set("();<>|&") for t in tokens)


def decide(payload, agy_pid, engine_root=None, raw=None):
    if not isinstance(payload, dict):
        return dict(PASS)
    guard = run_guard(json.dumps(payload) if raw is None else raw, engine_root)
    if guard.get("decision") != "ask":
        return guard
    if is_peer_reply(payload):
        return {**guard, "decision": "allow"}
    rewrite = bootstrap_rewrite(payload, agy_pid)
    if rewrite is None:
        return guard
    reasons = [r for r in (guard.get("reason"), rewrite["reason"]) if isinstance(r, str) and r]
    return {**guard, "decision": "ask", "reason": " | ".join(reasons), "overwrite": rewrite["overwrite"]}


def engine_root_arg(argv):
    if "--engine-root" in argv:
        i = argv.index("--engine-root")
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def main(argv=None):
    """One JSON object with `decision` on stdout and exit 0, whatever stdin or the checks do."""
    argv = list(sys.argv[1:] if argv is None else argv)
    out = dict(PASS)
    try:
        raw = sys.stdin.read()
        out = decide(json.loads(raw), os.getppid(), engine_root_arg(argv), raw)
    except Exception:
        out = dict(PASS)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
