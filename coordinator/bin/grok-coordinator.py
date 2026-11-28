#!/usr/bin/env python3
"""Parent for a Grok session that receives Unimatrix agent mail.

Bare `grok` is unchanged. `coordinator-grok` and `grok-coordinator` stay the
parent, start `grok --dangerously-skip-permissions`, and drop a leading
`--coordinator` so a wrapper can pass that flag through. When the child
appears in active_sessions.json, this process writes the resumable marker
and holds that session's agent inbox until the child exits. Mail that
already says `From agent <name>, not the PM.` is submitted with `grok -p`
(an argv, not the TUI, and no Enter). A failed submit stays on the spool
for the Stop hook, which feeds it as a block reason. Unannounced text is
never submitted as a turn.

`grok-coordinator -p TEXT` runs one headless turn, marks the session named
in the json stdout, and prints that stdout. That path does not listen.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path


def grok_binary() -> str:
    found = shutil.which("grok")
    if not found:
        raise SystemExit("grok-coordinator: grok is not on PATH")
    return found


def _plan_flags(extra: list[str]) -> list[str]:
    """Plan-role model and effort, unless the caller already set them.

    The catalogue lives in coordinator_core. A copy of this file launched outside the
    repo keeps the CLI default rather than inventing an id.
    """
    root = Path(__file__).resolve().parents[2]
    catalogue = root / "coordinator_core" / "agent_pipe" / "model_tiers.py"
    if catalogue.is_file() and str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from coordinator_core.agent_pipe.model_tiers import GROK_EM_EFFORT, model_for_role
    except Exception:
        return []
    flags: list[str] = []
    if not any(a in ("--model", "-m") or a.startswith("--model=") for a in extra):
        flags += ["--model", model_for_role("grok", "em")]
    if not any(a in ("--reasoning-effort", "--effort") or a.startswith("--reasoning-effort=")
               or a.startswith("--effort=") for a in extra):
        flags += ["--reasoning-effort", GROK_EM_EFFORT]
    return flags


DEFAULT_CONTEXT_WINDOW = 500_000
CONTEXT_WINDOW_ENV = "GROK_COORDINATOR_CONTEXT_WINDOW"


def _argv_model(argv: list[str]) -> str | None:
    for i, a in enumerate(argv):
        if a in ("--model", "-m") and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--model="):
            return a.split("=", 1)[1]
    return None


def ensure_context_window(home, argv: list[str], environ=None) -> bool:
    """Make grok's config pick the 500k window for the launch model; True when it appended the key.

    grok has no window flag, and its GROK_CONFIG overlay drops `[model.<id>]` tables, so the only
    launch-time selector is `[model."<id>"] context_window` in `<grok home>/config.toml`. Any
    `[model.<id>]` table already there is the caller's choice and stays. A model whose cached
    `context_windows` lacks the size is left on grok's default. `GROK_COORDINATOR_CONTEXT_WINDOW`
    names another listed size, or `default` to skip.
    """
    import tomllib

    env = os.environ if environ is None else environ
    want = env.get(CONTEXT_WINDOW_ENV, "").strip().lower()
    if want == "default":
        return False
    try:
        size = int(want) if want else DEFAULT_CONTEXT_WINDOW
    except ValueError:
        return False
    home = Path(home)
    config = home / "config.toml"
    try:
        text = config.read_text(encoding="utf-8") if config.is_file() else ""
        parsed = tomllib.loads(text)
        model = _argv_model(argv) or (parsed.get("models") or {}).get("default")
        if not isinstance(model, str) or not model:
            return False
        if model in (parsed.get("model") or {}):
            return False
        cache = json.loads((home / "models_cache.json").read_text(encoding="utf-8"))
        windows = cache["models"][model]["info"]["context_windows"]
        if size not in windows:
            return False
        block = f'\n[model."{model}"]\ncontext_window = {size}\n'
        tmp = config.with_name(config.name + ".tmp")
        tmp.write_text(text + block, encoding="utf-8")
        tmp.replace(config)
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def interactive_argv(binary: str, extra: list[str]) -> list[str]:
    """TUI launch. No -p and no -r. Extra args are the caller's."""
    return [binary, "--dangerously-skip-permissions", *_plan_flags(extra), *extra]


def headless_argv(binary: str, cwd: str, text: str) -> list[str]:
    """One headless turn. The json stdout names the session to mark."""
    return [binary, "--dangerously-skip-permissions", *_plan_flags([]), "-p", text, "--cwd", cwd,
            "--output-format", "json"]


def session_for_pid(rows, pid: int) -> dict | None:
    """The live registry row for this pid, else None.

    A resumed Grok keeps the process pid and appends a row. The latest
    ``opened_at`` wins, and a missing stamp loses to a later row.
    """
    if not isinstance(rows, list):
        return None
    found = None
    found_key = None
    for index, row in enumerate(rows):
        if not (isinstance(row, dict) and row.get("pid") == pid and isinstance(row.get("session_id"), str)):
            continue
        opened = row.get("opened_at")
        key = (opened if isinstance(opened, str) else "", index)
        if found is None or key >= found_key:
            found, found_key = row, key
    return found


def load_grok_adapter():
    """The adapter beside this script, else the installed peer facade."""
    import importlib
    bin_dir = Path(__file__).resolve().parent
    if (bin_dir / "_unimatrix" / "adapters" / "grok.py").is_file():
        if str(bin_dir) not in sys.path:
            sys.path.insert(0, str(bin_dir))
        return importlib.import_module("_unimatrix.adapters.grok")
    path = Path(os.environ.get("COORDINATOR_SETTINGS_HOME", Path.home() / ".coordinator-claude-settings"))
    path = path / "unimatrix" / "unimatrix-peer-grok.py"
    import importlib.util
    spec = importlib.util.spec_from_file_location("unimatrix_peer_grok_coordinator", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"grok-coordinator: cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def strip_coordinator(argv: list[str]) -> list[str]:
    """Drop `--coordinator`. The real grok binary does not know that flag."""
    return [arg for arg in argv if arg != "--coordinator"]


STOP_HOOK_CMD = "unimatrix-grok-stop.cmd"


def _write_if_changed(path: Path, text: str) -> None:
    if not path.is_file() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")


def _console_python() -> str:
    """The console interpreter: a `pythonw` parent would give the hook no stdin."""
    exe = Path(sys.executable)
    console = exe.with_name(exe.name.replace("pythonw", "python"))
    return str(console if console.is_file() else exe)


def ensure_stop_hook(home, command: str | None = None) -> Path:
    """Write the Stop hook grok loads from `<grok home>/hooks`. Same bytes stay put.

    Trap: Grok on Windows runs a hook command through PowerShell, which opens a bare `.py` with its
    file-type app and refuses it. A path-only `.cmd` beside the hook file runs the console python
    on the script with stdin inherited; quoting stays inside the `.cmd`, never in `command`."""
    hooks = Path(home) / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    if command is None:
        script = Path(__file__).resolve().parent / "unimatrix-grok-stop.py"
        if os.name == "nt":
            wrapper = hooks / STOP_HOOK_CMD
            q = lambda v: '"' + str(v).replace("%", "%%") + '"'
            _write_if_changed(wrapper, f"@echo off\r\n{q(_console_python())} {q(script)}\r\n")
            command = str(wrapper)
        else:
            command = str(script)
    body = json.dumps({
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": command, "timeout": 5}]}]},
    }, indent=2) + "\n"
    path = hooks / "unimatrix-agent-mail.json"
    _write_if_changed(path, body)
    return path


def register_host_binding(session_id: str) -> bool:
    """Write the engine's host-sessions/grok row for the interactive child; False when the engine or evidence is absent.

    A copy launched outside the repo has no engine, so the fire gate's own adopt leg stays the fallback."""
    root = Path(__file__).resolve().parents[2]
    if (root / "coordinator_core").is_dir() and str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from coordinator_core.session import core, external_registry
        sid = core.canonical_session_env_value("GROK_SESSION_ID", session_id)
        return external_registry.adopt_grok_session(sid, {"GROK_SESSION_ID": session_id}) is not None
    except Exception:
        return False


def _hold_inbox(mod, child_pid: int, stop: threading.Event) -> None:
    """Mark the child, then listen until it exits. Mail waits on the shown spool."""
    home = mod.grok_home()
    registry = home / "active_sessions.json"
    while not stop.is_set():
        try:
            rows = json.loads(registry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows = []
        row = session_for_pid(rows, child_pid)
        if row is not None:
            session_id = row["session_id"]
            cwd = row.get("cwd") or os.getcwd()
            mod.mark_resumable(home, session_id, cwd)
            register_host_binding(session_id)

            def _on_text(text, sid=session_id, work=cwd):
                if mod.submit_peer_turn(sid, work, text) is None:
                    return
                mod.append_shown(home, sid, "UNIMATRIX-AGENT-MAIL " + text.replace("\r", " ").replace("\n", "\\n"))

            inbox = mod.AgentInbox(home, session_id, on_line=None)
            inbox.on_text = _on_text
            inbox.start()
            stop.wait()
            inbox.stop()
            return
        stop.wait(0.4)


def bind_kill_on_close(pid: int) -> int | None:
    """Windows: put `pid` in a job that kills it when this process's last handle to the job closes.

    Returns the job handle (keep it referenced for the process lifetime) or None off Windows or
    when the job cannot be made. The OS closes the handle when this process dies, however it dies,
    so grok never outlives the python that holds its inbox.
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class _Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    proc = k32.OpenProcess(0x0001 | 0x0100, 0, pid)  # PROCESS_TERMINATE | PROCESS_SET_QUOTA
    ok = bool(proc) and k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) \
        and k32.AssignProcessToJobObject(job, proc)
    if proc:
        k32.CloseHandle(proc)
    if not ok:
        k32.CloseHandle(job)
        return None
    return job


def _live_session_ids(home) -> set[str]:
    try:
        rows = json.loads((Path(home) / "active_sessions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(r.get("session_id")) for r in rows if isinstance(r, dict) and r.get("session_id")}


def sweep_tmp_sessions(cwd, live_ids: set[str], drive_root=None) -> int:
    """Remove grok's empty per-session dirs at `<cwd drive>/tmp/sessions/<id>`; returns how many went.

    Trap: grok writes a POSIX "/tmp/sessions/<sid>" for each session and subagent, and Windows resolves
    it to the ROOT of the cwd's drive (D:\\tmp\\sessions\\...), never cleaning it. grok documents no
    setting for that dir, so the launcher sweeps it. Only empty trees of dead ids are removed: rmdir
    refuses a non-empty dir, so content grok wrote is never deleted."""
    tmp = Path(drive_root or Path(cwd).anchor or "/") / "tmp"
    root = tmp / "sessions"
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    gone = 0
    for entry in entries:
        if entry.name in live_ids or not entry.is_dir(follow_symlinks=False):
            continue
        for dirpath, _dirs, _files in sorted(os.walk(entry.path, topdown=False), key=lambda w: -len(w[0])):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
        gone += not os.path.exists(entry.path)
    for d in (root, tmp):
        try:
            os.rmdir(d)  # only when nothing else lives there
        except OSError:
            break
    return gone


def run_interactive(extra: list[str]) -> int:
    mod = load_grok_adapter()
    ensure_stop_hook(mod.grok_home())
    ensure_context_window(mod.grok_home(), [*_plan_flags(extra), *extra])
    try:
        return _run_interactive(mod, extra)
    finally:
        sweep_tmp_sessions(os.getcwd(), _live_session_ids(mod.grok_home()))


def _run_interactive(mod, extra: list[str]) -> int:
    child = subprocess.Popen(interactive_argv(grok_binary(), extra))
    job = bind_kill_on_close(child.pid)  # the handle must outlive this call: closing it kills grok
    if job is None and os.name == "nt":
        print("grok-coordinator: kill-on-exit job not bound; grok may outlive its inbox holder", file=sys.stderr)
    stop = threading.Event()
    watcher = threading.Thread(target=_hold_inbox, args=(mod, child.pid, stop), daemon=True)
    watcher.start()
    try:
        return child.wait()
    finally:
        stop.set()
        watcher.join(timeout=2)


def run_headless(text: str) -> int:
    mod = load_grok_adapter()
    cwd = os.getcwd()
    ensure_context_window(mod.grok_home(), _plan_flags([]))
    proc = subprocess.run(headless_argv(grok_binary(), cwd, text), cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    sys.stdout.write(proc.stdout or "")
    sys.stderr.write(proc.stderr or "")
    sweep_tmp_sessions(cwd, _live_session_ids(mod.grok_home()))
    if proc.returncode == 0:
        mod.record_launch(mod.grok_home(), proc.stdout, cwd)
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    argv = strip_coordinator(list(sys.argv[1:] if argv is None else argv))
    if "-p" in argv:
        i = argv.index("-p")
        if i + 1 >= len(argv):
            raise SystemExit("grok-coordinator: -p needs the prompt text")
        return run_headless(argv[i + 1])
    return run_interactive(argv)


if __name__ == "__main__":
    raise SystemExit(main())
